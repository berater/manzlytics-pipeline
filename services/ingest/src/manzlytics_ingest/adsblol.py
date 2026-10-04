"""adsb.lol canlı API istemcisi (ODbL).

`/v2/point/{lat}/{lon}/{radius}` en fazla 250 nm yarıçaplı bir dairedeki uçakları döner.
Bir bölgeyi kaplamak için bbox üzerine daire ızgarası atılır; aynı anlık görüntüde birden
çok daireye düşen uçaklar `hex` ile tekilleştirilir.

Not: Belgelere göre üretim kullanımı için adsb.lol ile iletişime geçilmeli; ileride API
anahtarı yalnızca feeder'lara verilecek. Günlük arşiv (globe_history) MVP'nin ana kaynağıdır;
bu istemci prototip ve yakın-gerçek-zamanlı katman içindir.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

from manzlytics_ingest.models import PositionReport

API = "https://api.adsb.lol/v2/point/{lat:.3f}/{lon:.3f}/{radius}"
MAX_RADIUS_NM = 250
NM_PER_DEG_LAT = 60.0
USER_AGENT = "manzlytics-prototype/0.0 (+https://manzlytics.pages.dev)"

# adsb.lol `dbFlags` bitleri: 1 askerî, 2 ilginç, 4 PIA, 8 LADD (gizlilik programları)
PRIVACY_FLAGS = 4 | 8


@dataclass(frozen=True, slots=True)
class BBox:
    south: float
    west: float
    north: float
    east: float

    @classmethod
    def parse(cls, s: str) -> BBox:
        south, west, north, east = (float(x) for x in s.split(","))
        return cls(south, west, north, east)

    def contains(self, lat: float, lon: float) -> bool:
        return self.south <= lat <= self.north and self.west <= lon <= self.east


def cover_bbox(bbox: BBox, radius_nm: int = MAX_RADIUS_NM) -> list[tuple[float, float]]:
    """bbox'ı tamamen kaplayan daire merkezleri (kare ızgara, aralık = r·√2)."""
    step_lat = radius_nm * math.sqrt(2) / NM_PER_DEG_LAT
    centers: list[tuple[float, float]] = []
    lat = bbox.south + step_lat / 2
    while lat - step_lat / 2 < bbox.north:
        step_lon = step_lat / max(math.cos(math.radians(lat)), 0.1)
        lon = bbox.west + step_lon / 2
        while lon - step_lon / 2 < bbox.east:
            centers.append((round(lat, 3), round(lon, 3)))
            lon += step_lon
        lat += step_lat
    return centers


def fetch_point(
    lat: float, lon: float, radius_nm: int = MAX_RADIUS_NM, timeout: float = 30, retries: int = 3
) -> dict:
    req = urllib.request.Request(
        API.format(lat=lat, lon=lon, radius=radius_nm), headers={"User-Agent": USER_AGENT}
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == retries:
                raise
            time.sleep(float(e.headers.get("Retry-After") or 2 ** (attempt + 1)))
    raise AssertionError("unreachable")


def parse_aircraft(ac: dict, now: datetime) -> PositionReport | None:
    """readsb/adsb.lol `ac` nesnesini PositionReport'a çevirir. Konumsuz veya bayat kayıt → None."""
    lat, lon = ac.get("lat"), ac.get("lon")
    if lat is None or lon is None:
        return None
    seen_pos = ac.get("seen_pos", 0.0)
    if seen_pos > 60:
        return None
    alt_baro = ac.get("alt_baro")
    on_ground = alt_baro == "ground"
    callsign = (ac.get("flight") or "").strip() or None
    return PositionReport(
        ts=datetime.fromtimestamp(now.timestamp() - seen_pos, tz=UTC),
        icao24=ac["hex"].lower(),
        lat=float(lat),
        lon=float(lon),
        alt_baro_ft=None if on_ground or alt_baro is None else float(alt_baro),
        alt_geom_ft=ac.get("alt_geom"),
        callsign=callsign,
        nic=ac.get("nic"),
        nac_p=ac.get("nac_p"),
        sil=ac.get("sil"),
        on_ground=on_ground,
        # Yalnızca doğrudan ADS-B mesajları NIC için anlamlı; MLAT/TIS-B vb. işaretlenir.
        source=f"adsblol:{ac.get('type', 'unknown')}",
        anonymous=bool(int(ac.get("dbFlags") or 0) & PRIVACY_FLAGS),
    )


def snapshot(
    bbox: BBox, pause_s: float = 1.0, radius_nm: int = MAX_RADIUS_NM
) -> Iterator[PositionReport]:
    """bbox için tek anlık görüntü; uçaklar hex ile tekilleştirilir."""
    seen: set[str] = set()
    for lat, lon in cover_bbox(bbox, radius_nm):
        now = datetime.now(UTC)
        try:
            data = fetch_point(lat, lon, radius_nm)
        except OSError as e:  # ağ hatası tek daireyi düşürür, tüm görüntüyü değil
            print(f"uyarı: {lat},{lon} alınamadı: {e}")
            continue
        for ac in data.get("ac", []):
            hex_ = ac.get("hex")
            if not hex_ or hex_ in seen:
                continue
            report = parse_aircraft(ac, now)
            if report is None:
                continue
            seen.add(hex_)
            if report.lat < bbox.south or report.lat > bbox.north:
                continue
            if report.lon < bbox.west or report.lon > bbox.east:
                continue
            yield report
        time.sleep(pause_s)
