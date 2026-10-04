"""H3 hücre bazlı jamming agregasyonu (taslak metodoloji).

- Yerdeki uçaklar ve NIC bildirmeyen raporlar dışlanır.
- Yalnızca doğrudan ADS-B kaynakları (`adsb_icao`, `adsb_icao_nt`) kullanılır; MLAT/TIS-B
  konumu uçağın kendi GNSS'inden gelmez.
- Aynı (uçak, zaman) raporu bir kez sayılır.
- Bir uçak, bir hücrede raporlarının en az yarısı NIC < 5 ise o hücre için "bad" sayılır.
- Hücre çıktısı: tekil uçak sayısı, bad sayısı ve açıklanabilirlik için bad uçakların listesi.
- `exclude` verilirse o uçaklar (ör. avionics.py'nin arızalı cihaz kararı) hiç sayılmaz.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from datetime import datetime

import h3
from manzlytics_ingest.models import PositionReport

BAD_NIC_BELOW = 5
# NIC dağılımı 0..MAX_NIC kutularında tutulur (11 ve üstü son kutuya).
MAX_NIC = 11
NIC_BINS = MAX_NIC + 1
BAD_SHARE = 0.5
# NACp (konum doğruluğu kategorisi) bunun altı "kötü": DO-260B'de 8 = 0,05 deniz mili (~93 m)
# hata payı. İkinci, bağımsız sinyal; seviyeyi belirlemez (bkz. store.CELL_NACP_SQL). Eşik
# gerçek veriyle kalibre edilmedi.
BAD_NACP_BELOW = 8
# Bu irtifanın (barometrik, ft) altı "alçak irtifa": iniş/kalkış ve havalimanı çevresi. Düşük NIC
# orada jamming dışı nedenlerle (engel, çok yollu yayılım, alıcı yerleşimi) de görülür.
LOW_ALT_FT = 5000
ADSB_SOURCES = ("adsb_icao", "adsb_icao_nt")


@dataclass(frozen=True, slots=True)
class AircraftInCell:
    icao24: str
    callsign: str | None
    n_reports: int
    n_bad_reports: int
    min_nic: int
    anonymous: bool = False


@dataclass(frozen=True, slots=True)
class JammingCellStats:
    h3: str
    n_total: int
    n_bad: int
    bad_aircraft: tuple[AircraftInCell, ...] = field(default=())

    @property
    def n_good(self) -> int:
        return self.n_total - self.n_bad

    @property
    def ratio_bad(self) -> float:
        return self.n_bad / self.n_total if self.n_total else 0.0


# Saklanan en ince çözünürlük (ingest ile aynı). Kaba hücreler bunun ebeveyni olarak alınır:
# H3'te noktanın res-3 hücresi, res-5 hücresinin ebeveyniyle sınırda farklı olabilir; ebeveyn
# kullanmak zum yapınca hücrenin tam olarak alt hücrelerine bölünmesini sağlar.
FINEST_RESOLUTION = 5


def cell_for(lat: float, lon: float, resolution: int) -> str:
    fine = h3.latlng_to_cell(lat, lon, max(resolution, FINEST_RESOLUTION))
    return h3.cell_to_parent(fine, resolution) if resolution < FINEST_RESOLUTION else fine


def is_low_altitude(report: PositionReport) -> bool:
    """İrtifası bilinen ve LOW_ALT_FT altındaki rapor; irtifa yoksa alçak sayılmaz."""
    return report.alt_baro_ft is not None and report.alt_baro_ft < LOW_ALT_FT


def is_adsb(report: PositionReport) -> bool:
    return report.source.rsplit(":", 1)[-1] in ADSB_SOURCES or report.source == "unknown"


def aggregate_jamming(
    reports: Iterable[PositionReport], resolution: int = 4, exclude: Collection[str] = ()
) -> dict[str, JammingCellStats]:
    # hücre -> uçak -> [bad rapor, toplam rapor, min NIC, son callsign, gizli mi]
    acc: dict[str, dict[str, list]] = defaultdict(dict)
    seen: set[tuple[str, datetime]] = set()
    for r in reports:
        if r.on_ground or r.nic is None or not is_adsb(r) or r.icao24 in exclude:
            continue
        # Uçak iki görüntü arasında yeni konum yollamadıysa aynı rapor tekrar gelir.
        if (r.icao24, r.ts) in seen:
            continue
        seen.add((r.icao24, r.ts))
        cell = cell_for(r.lat, r.lon, resolution)
        a = acc[cell].setdefault(r.icao24, [0, 0, r.nic, r.callsign, False])
        a[0] += r.nic < BAD_NIC_BELOW
        a[1] += 1
        a[2] = min(a[2], r.nic)
        a[3] = r.callsign or a[3]
        a[4] = a[4] or r.anonymous

    result: dict[str, JammingCellStats] = {}
    for cell, per_ac in acc.items():
        bad = tuple(
            AircraftInCell(icao, cs, total, n_bad, min_nic, anon)
            for icao, (n_bad, total, min_nic, cs, anon) in sorted(per_ac.items())
            if n_bad / total >= BAD_SHARE
        )
        result[cell] = JammingCellStats(
            h3=cell, n_total=len(per_ac), n_bad=len(bad), bad_aircraft=bad
        )
    return result


@dataclass(frozen=True, slots=True)
class HourStats:
    hour: datetime
    n_aircraft: int
    n_affected: int


def timeline(reports: Iterable[PositionReport], exclude: Collection[str] = ()) -> list[HourStats]:
    """Saat saat: o saatte görülen tekil uçak ve raporlarının en az yarısı NIC < 5 olan uçak."""
    acc: dict[datetime, dict[str, list[int]]] = defaultdict(dict)
    seen: set[tuple[str, datetime]] = set()
    for r in reports:
        if r.on_ground or r.nic is None or not is_adsb(r) or r.icao24 in exclude:
            continue
        if (r.icao24, r.ts) in seen:
            continue
        seen.add((r.icao24, r.ts))
        hour = r.ts.replace(minute=0, second=0, microsecond=0)
        a = acc[hour].setdefault(r.icao24, [0, 0])
        a[0] += r.nic < BAD_NIC_BELOW
        a[1] += 1
    return [
        HourStats(
            hour=h,
            n_aircraft=len(per_ac),
            n_affected=sum(1 for bad, total in per_ac.values() if bad / total >= BAD_SHARE),
        )
        for h, per_ac in sorted(acc.items())
    ]
