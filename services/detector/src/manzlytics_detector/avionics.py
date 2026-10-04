"""Arızalı/eski aviyonik ayıklama (komşu karşılaştırması).

Bazı uçaklar ortada karıştırma yokken de sürekli düşük NIC bildirir: GNSS kaynağı transpondere
bağlı değil, eski transponder ya da yanlış yapılandırma. Bu jamming değildir ama seyrek
hücrelerde yanlış kırmızıya, her yerde de arka plan oranının şişmesine yol açar.

Jamming bölgeseldir: aynı yerde aynı saatte diğer uçakları da etkiler. Arızalı cihaz ise uçakla
birlikte gezer. Bu yüzden her uçak, her (saat, res-3 hücre) "kovasında" aynı kovadaki DİĞER
uçaklarla karşılaştırılır:

- Temiz kova: uçak dışında en az MIN_PEERS uçak var ve onların en fazla CLEAN_PEER_SHARE'i
  etkilenmiş (etkilenmiş = kovadaki raporlarının en az yarısı NIC < 5, jamming.py ile aynı).
- Arızalı uçak: en az MIN_CLEAN_BUCKETS temiz kovası var ve bunların en az FAULTY_SHARE'inde
  kendisi etkilenmiş. Yani komşuları temizken o, tekrar tekrar kötü.

Gerçekten karıştırılan bölgede uçan uçağın kovaları temiz değildir (komşuları da etkilenir),
bu yüzden o uçak işaretlenmez. Temiz kovası az olan uçak (seyrek bölge, kısa uçuş) hakkında
karar verilmez; o uçak hesapta kalır.

Girdi, `jamming_ac_hourly` satırlarıyla aynı biçimde gözlemlerdir: (saat, hücre, uçak,
rapor sayısı, kötü rapor sayısı). Hücre res 3 veya daha ince olabilir; res-3 ebeveynine çevrilir.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import datetime
from functools import lru_cache

import h3
import pyarrow as pa
from manzlytics_ingest.models import PositionReport

from manzlytics_detector.jamming import BAD_NIC_BELOW, BAD_SHARE, FINEST_RESOLUTION, is_adsb

BUCKET_RESOLUTION = 3  # kenar ≈ 60 km; bir karıştırıcının irtifadaki etki alanından küçük
MIN_PEERS = 3
CLEAN_PEER_SHARE = 0.2
MIN_CLEAN_BUCKETS = 3
FAULTY_SHARE = 0.8

# (saat, hücre, icao24, rapor, kötü rapor); hücre H3 dizgesi ya da tamsayısı
Observation = tuple[datetime | int, str | int, str, int, int]


@dataclass(frozen=True, slots=True)
class AvionicsVerdict:
    icao24: str
    buckets: int  # uçağın görüldüğü kova sayısı
    clean_buckets: int  # komşuları temiz olan kovalar
    bad_in_clean: int  # temiz kovalardan kendisinin etkilendiği

    @property
    def faulty(self) -> bool:
        return (
            self.clean_buckets >= MIN_CLEAN_BUCKETS
            and self.bad_in_clean >= FAULTY_SHARE * self.clean_buckets
        )


THRESHOLDS = {
    "bucket_resolution": BUCKET_RESOLUTION,
    "min_peers": MIN_PEERS,
    "clean_peer_share": CLEAN_PEER_SHARE,
    "min_clean_buckets": MIN_CLEAN_BUCKETS,
    "faulty_share": FAULTY_SHARE,
}


@lru_cache(maxsize=1 << 20)
def _bucket_cell(cell: str | int) -> str:
    s = h3.int_to_str(cell) if isinstance(cell, int) else cell
    res = h3.get_resolution(s)
    if res < BUCKET_RESOLUTION:
        raise ValueError(f"hücre çözünürlüğü {res} < {BUCKET_RESOLUTION}: {s}")
    return s if res == BUCKET_RESOLUTION else h3.cell_to_parent(s, BUCKET_RESOLUTION)


def observations_from_reports(reports: Iterable[PositionReport]) -> list[Observation]:
    """Ham raporlardan (saat, res-5 hücre, uçak) gözlemleri; jamming.py ile aynı filtre."""
    acc: dict[tuple[datetime, str, str], list[int]] = {}
    seen: set[tuple[str, datetime]] = set()
    for r in reports:
        if r.on_ground or r.nic is None or not is_adsb(r) or (r.icao24, r.ts) in seen:
            continue
        seen.add((r.icao24, r.ts))
        hour = r.ts.replace(minute=0, second=0, microsecond=0)
        cell = h3.latlng_to_cell(r.lat, r.lon, FINEST_RESOLUTION)
        a = acc.setdefault((hour, cell, r.icao24), [0, 0])
        a[0] += 1
        a[1] += r.nic < BAD_NIC_BELOW
    return [(h, c, icao, n, b) for (h, c, icao), (n, b) in acc.items()]


def observations_from_hourly(table: pa.Table, batch_rows: int = 1 << 20) -> Iterator[Observation]:
    """`jamming_ac_hourly_r5.parquet` tablosundan gözlemler (bellek için parça parça)."""
    cols = ["hour", "h3", "icao24", "n_reports", "n_bad_reports"]
    for batch in table.select(cols).to_batches(max_chunksize=batch_rows):
        yield from zip(*(batch.column(c).to_pylist() for c in cols), strict=True)


def assess_avionics(observations: Iterable[Observation]) -> dict[str, AvionicsVerdict]:
    """Her uçak için komşu karşılaştırması; `verdict.faulty` arızalı cihaz kararıdır."""
    # kova -> uçak -> [rapor, kötü rapor]
    buckets: dict[tuple, dict[str, list[int]]] = defaultdict(dict)
    for hour, cell, icao, n, b in observations:
        a = buckets[(hour, _bucket_cell(cell))].setdefault(icao, [0, 0])
        a[0] += n
        a[1] += b

    seen: dict[str, int] = defaultdict(int)
    clean: dict[str, int] = defaultdict(int)
    bad_clean: dict[str, int] = defaultdict(int)
    for per_ac in buckets.values():
        affected = {icao for icao, (n, b) in per_ac.items() if b >= BAD_SHARE * n}
        n_ac, n_aff = len(per_ac), len(affected)
        for icao in per_ac:
            seen[icao] += 1
            peers = n_ac - 1
            peers_aff = n_aff - (icao in affected)
            if peers >= MIN_PEERS and peers_aff <= CLEAN_PEER_SHARE * peers:
                clean[icao] += 1
                bad_clean[icao] += icao in affected
    return {icao: AvionicsVerdict(icao, seen[icao], clean[icao], bad_clean[icao]) for icao in seen}


def faulty_aircraft(observations: Iterable[Observation]) -> frozenset[str]:
    return frozenset(v.icao24 for v in assess_avionics(observations).values() if v.faulty)


def avionics_report(verdicts: dict[str, AvionicsVerdict]) -> dict:
    """Günlük arşive yazılan karar dosyası (`avionics.json`): eşikler + arızalı uçaklar.

    Hariç tutma uçak kimliğiyle yapıldığı için ICAO24'ler yazılır; dosya saatlik özet gibi özel
    arşivde kalır, statik veriye/yayına girmez (gizli uçaklar LADD/PIA dahil).
    """
    faulty = sorted((v for v in verdicts.values() if v.faulty), key=lambda v: v.icao24)
    return {
        "method": "avionics.py: komşuları temizken tekrar tekrar kötü olan uçak",
        "thresholds": THRESHOLDS,
        "aircraft_assessed": len(verdicts),
        "aircraft_decidable": sum(v.clean_buckets >= MIN_CLEAN_BUCKETS for v in verdicts.values()),
        "faulty": [asdict(v) for v in faulty],
    }
