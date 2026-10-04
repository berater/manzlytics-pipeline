"""Konum sıçraması tespiti (N5, adım 2): aynı uçağın ardışık iki raporu arasında fiziksel
olarak imkânsız hız.

Kural ve eşikler `docs/research/spoofing-olcum.md` ölçümünden gelir (§8, madde 1–2):

- havada, iki rapor da MLAT dışı;
- 1 ≤ aralık ≤ 120 sn (1 sn altı zaman damgası artefaktıdır, ham sayımın ~%68'i);
- mesafe ≥ 10 nm ve ima edilen hız > 1.500 kt;
- paylaşılan/placeholder adres: aynı uçağın sıçramaları iki sabit konum kümesi arasında
  ileri-geri salınıyorsa bu iki ayrı uçaktır, spoofing değil (örn. `300000`).

Bu, **sıçrama çifti** düzeyidir; olay birleştirme ve güven derecesi sonraki adımdır. Etiket
yoktur: bir sıçrama "gerçek spoofing" değil, "fiziksel olarak tutarsız konum" demektir.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC
from math import asin, cos, radians, sin, sqrt

from manzlytics_ingest.models import PositionReport

MIN_GAP_S = 1.0
MAX_GAP_S = 120.0
MIN_DIST_NM = 10.0
MIN_SPEED_KT = 1500.0
EARTH_RADIUS_NM = 3440.065
# Salınım testi: iki sıçrama ucu bu yarıçapın içindeyse "aynı konum kümesi" sayılır.
CLUSTER_NM = 30.0
MIN_JUMPS_FOR_SHARED = 4


@dataclass(frozen=True, slots=True)
class Jump:
    icao24: str
    before: PositionReport
    after: PositionReport
    dt_s: float
    dist_nm: float

    @property
    def speed_kt(self) -> float:
        return self.dist_nm / (self.dt_s / 3600.0)


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    a = sin(radians(lat2 - lat1) / 2) ** 2
    a += cos(radians(lat1)) * cos(radians(lat2)) * sin(radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_NM * asin(sqrt(a))


def _is_jump(prev: PositionReport, cur: PositionReport) -> Jump | None:
    if prev.on_ground or cur.on_ground:
        return None
    if prev.source.endswith(":mlat") or cur.source.endswith(":mlat"):
        return None
    dt = (cur.ts - prev.ts).total_seconds()
    if not MIN_GAP_S <= dt <= MAX_GAP_S:
        return None
    dist = haversine_nm(prev.lat, prev.lon, cur.lat, cur.lon)
    if dist < MIN_DIST_NM or dist / (dt / 3600.0) <= MIN_SPEED_KT:
        return None
    return Jump(cur.icao24, prev, cur, dt, dist)


def _oscillates(jumps: list[Jump]) -> bool:
    """Sıçrama uçları yalnız iki konum kümesi arasında mı gidip geliyor?"""
    if len(jumps) < MIN_JUMPS_FOR_SHARED:
        return False
    centers: list[tuple[float, float]] = []
    for j in jumps:
        for p in (j.before, j.after):
            if not any(haversine_nm(p.lat, p.lon, *c) <= CLUSTER_NM for c in centers):
                centers.append((p.lat, p.lon))
                if len(centers) > 2:
                    return False
    return len(centers) == 2


def detect_jumps(reports: Iterable[PositionReport]) -> list[Jump]:
    """Konum sıçramalarını döndürür (uçak, zaman sırasıyla). Girdi sırası önemsizdir."""
    by_aircraft: dict[str, list[PositionReport]] = defaultdict(list)
    for r in reports:
        by_aircraft[r.icao24].append(r)
    out: list[Jump] = []
    for rs in by_aircraft.values():
        rs.sort(key=lambda r: r.ts)
        jumps = [j for a, b in zip(rs, rs[1:], strict=False) if (j := _is_jump(a, b))]
        if not _oscillates(jumps):
            out += jumps
    return out


# --- Olay birleştirme ve güven derecesi (ölçüm belgesi §8, madde 3) ---

EVENT_GAP_S = 600.0  # aynı uçağın bu süre içindeki sıçramaları tek olay
GOOD_NIC = 7
CONVERGENCE_DEG = 2.0
CONVERGENCE_WINDOW_S = 600.0

HIGH = "high"
LOW = "low"
INTEGRITY_OVERLAP = "integrity_overlap"


@dataclass(frozen=True, slots=True)
class SpoofEvent:
    icao24: str
    jumps: tuple[Jump, ...]
    confidence: str

    @property
    def start(self):
        return self.jumps[0].before.ts

    @property
    def end(self):
        return self.jumps[-1].after.ts


def _nic0(p: PositionReport) -> bool:
    return not p.nic


def _good_nic(j: Jump) -> bool:
    return (j.before.nic or 0) >= GOOD_NIC and (j.after.nic or 0) >= GOOD_NIC


def group_events(jumps: Iterable[Jump]) -> list[list[Jump]]:
    by_aircraft: dict[str, list[Jump]] = defaultdict(list)
    for j in jumps:
        by_aircraft[j.icao24].append(j)
    events: list[list[Jump]] = []
    for js in by_aircraft.values():
        js.sort(key=lambda j: j.after.ts)
        current = [js[0]]
        for j in js[1:]:
            if (j.before.ts - current[-1].after.ts).total_seconds() <= EVENT_GAP_S:
                current.append(j)
            else:
                events.append(current)
                current = [j]
        events.append(current)
    return events


def _cell(j: Jump) -> tuple[int, int, int]:
    p = j.after
    return (
        int(p.lat // CONVERGENCE_DEG),
        int(p.lon // CONVERGENCE_DEG),
        int(p.ts.timestamp() // CONVERGENCE_WINDOW_S),
    )


def detect_events(reports: Iterable[PositionReport]) -> list[SpoofEvent]:
    """Sıçramaları olaylara birleştirir ve güven derecesi verir.

    - `high`: NIC ≥ 7 iken sıçrama, ya da aynı 2°×2° hücrede aynı 10 dk penceresinde ≥ 2 farklı
      uçağın sıçraması (yakınsama).
    - `integrity_overlap`: tüm sıçramalarda NIC 0/yok ve tek uçak (jamming'in yan etkisi olması
      muhtemel).
    - `low`: geri kalanı.
    """
    return events_from_jumps(detect_jumps(reports))


def events_from_jumps(jumps: list[Jump]) -> list[SpoofEvent]:
    aircraft_per_cell: dict[tuple[int, int, int], set[str]] = defaultdict(set)
    for j in jumps:
        aircraft_per_cell[_cell(j)].add(j.icao24)
    events = []
    for js in group_events(jumps):
        if any(_good_nic(j) or len(aircraft_per_cell[_cell(j)]) >= 2 for j in js):
            conf = HIGH
        elif all(_nic0(j.before) or _nic0(j.after) for j in js):
            conf = INTEGRITY_OVERLAP
        else:
            conf = LOW
        events.append(SpoofEvent(js[0].icao24, tuple(js), conf))
    events.sort(key=lambda e: e.start)
    return events


def _iso(t) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _point(p: PositionReport) -> dict:
    return {"ts": _iso(p.ts), "lat": round(p.lat, 4), "lon": round(p.lon, 4), "nic": p.nic}


def events_payload(day: str, events: Iterable[SpoofEvent], region=None) -> dict:
    """Olayları harita için JSON'a çevirir (`spoofing.json`).

    Gizlilik programındaki (anonymous) uçakların kimliği yazılmaz. `region`: verilirse yalnız
    ilk sıçramanın "önce" noktası bu bölgede olan olaylar (BBox: south/west/north/east).
    """
    out = []
    for e in events:
        first = e.jumps[0]
        if region is not None and not (
            region.south <= first.before.lat <= region.north
            and region.west <= first.before.lon <= region.east
        ):
            continue
        anon = any(j.before.anonymous or j.after.anonymous for j in e.jumps)
        out.append(
            {
                "icao24": None if anon else e.icao24,
                "confidence": e.confidence,
                "start": _iso(e.start),
                "end": _iso(e.end),
                "jumps": [
                    {
                        "before": _point(j.before),
                        "after": _point(j.after),
                        "dist_nm": round(j.dist_nm, 1),
                        "speed_kt": round(j.speed_kt),
                    }
                    for j in e.jumps
                ],
            }
        )
    by_conf = Counter(x["confidence"] for x in out)
    return {"date": day, "count": len(out), "by_confidence": dict(by_conf), "events": out}


def events_from_parquet(path) -> list[SpoofEvent]:
    """Arşivlenmiş günün `positions.parquet`inden olayları çıkarır (uçak uçak, bellek dostu)."""
    from manzlytics_detector.verify import _reports_by_aircraft

    jumps: list[Jump] = []
    for reports in _reports_by_aircraft(path):
        jumps += detect_jumps(reports)
    return events_from_jumps(jumps)
