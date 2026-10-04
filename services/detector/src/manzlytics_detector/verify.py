"""Arşivden bir günü baştan hesaplayıp arşivdeki özetlerle karşılaştırma (#16 kabul kriteri).

1. Konumlar → saatlik özet: `positions.parquet` (odak bölge, her nokta) `hourly_rows` ile yeniden
   özetlenir ve arşivdeki dünya özetinin aynı hücrelerdeki satırlarıyla birebir karşılaştırılır.
   Yalnız tamamen odak bölgenin içinde kalan res-5 hücreler karşılaştırılır: sınırdaki bir
   hücrenin noktalarının bir kısmı bölge dışında olduğu için konum dosyasında yoktur.
2. Saatlik özet → arızalı aviyonik: `avionics.json` özetten yeniden üretilir, aynı olmalı.

Konumlar uçak + zamana göre sıralı olduğundan uçak uçak işlenir (bellek sınırlı kalır).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import h3
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from manzlytics_ingest.adsblol import BBox
from manzlytics_ingest.models import PositionReport

from manzlytics_detector.avionics import assess_avionics, avionics_report, observations_from_hourly
from manzlytics_detector.daily import AVIONICS_FILE, HOURLY_FILE
from manzlytics_detector.hourly import HOURLY_SCHEMA, hourly_rows

# Hücre köşeleri bölgenin bu kadar içinde olmalı (kenarlar jeodezik yay, dışa hafif taşabilir)
EDGE_MARGIN_DEG = 0.05
VALUE_FIELDS = HOURLY_SCHEMA.names[3:]  # hour, h3, icao24 anahtardır
REPORT_FIELDS = [
    "ts",
    "icao24",
    "lat",
    "lon",
    "callsign",
    "nic",
    "source",
    "on_ground",
    "anonymous",
]


@dataclass(frozen=True, slots=True)
class VerifyResult:
    cells_compared: int
    rows_compared: int
    rows_only_archive: int
    rows_only_recomputed: int
    rows_different: int
    avionics_same: bool

    @property
    def ok(self) -> bool:
        return self.avionics_same and not (
            self.rows_only_archive or self.rows_only_recomputed or self.rows_different
        )


def inside(cell: int, bbox: BBox, margin: float = EDGE_MARGIN_DEG) -> bool:
    inner = BBox(bbox.south + margin, bbox.west + margin, bbox.north - margin, bbox.east - margin)
    return all(inner.contains(lat, lon) for lat, lon in h3.cell_to_boundary(h3.int_to_str(cell)))


def _reports_by_aircraft(path: Path, batch_rows: int = 1 << 20) -> Iterator[list[PositionReport]]:
    current: list[PositionReport] = []
    for batch in pq.ParquetFile(path).iter_batches(batch_size=batch_rows, columns=REPORT_FIELDS):
        cols = {c: batch.column(c).to_pylist() for c in REPORT_FIELDS}
        for row in zip(*cols.values(), strict=True):
            r = PositionReport(**dict(zip(REPORT_FIELDS, row, strict=True)))
            if current and current[0].icao24 != r.icao24:
                yield current
                current = []
            current.append(r)
    if current:
        yield current


def _key_rows(table: pa.Table) -> dict[tuple, tuple]:
    """(saat [unix sn], hücre, uçak) → kalan sütunlar; `hourly_rows` çıktısıyla aynı biçim."""
    return {
        (int(r["hour"].timestamp()), r["h3"], r["icao24"]): tuple(r[c] for c in VALUE_FIELDS)
        for r in table.to_pylist()
    }


def verify_day(d: Path) -> VerifyResult:
    manifest = json.loads((d / "manifest.json").read_text())
    if manifest.get("bbox") is None:
        raise ValueError("konumlar dünya geneli; karşılaştırma bölgesi yok (bbox=None)")
    bbox = BBox(**manifest["bbox"])
    hourly = pq.read_table(d / HOURLY_FILE)

    cells = pc.unique(hourly["h3"]).to_pylist()
    keep = pa.array([c for c in cells if inside(c, bbox)], type=pa.uint64())
    archived = _key_rows(hourly.filter(pc.is_in(hourly["h3"], value_set=keep)))

    keep_set = set(keep.to_pylist())
    recomputed: dict[tuple, tuple] = {}
    for reports in _reports_by_aircraft(d / "positions.parquet"):
        cols = hourly_rows(reports)
        for i, cell in enumerate(cols["h3"]):
            if cell in keep_set:
                key = (cols["hour"][i], cell, cols["icao24"][i])
                recomputed[key] = tuple(cols[c][i] for c in VALUE_FIELDS)

    only_a = archived.keys() - recomputed.keys()
    only_r = recomputed.keys() - archived.keys()
    diff = sum(archived[k] != recomputed[k] for k in archived.keys() & recomputed.keys())

    stored = json.loads((d / AVIONICS_FILE).read_text())
    again = avionics_report(assess_avionics(observations_from_hourly(hourly)))
    return VerifyResult(
        cells_compared=len(keep_set),
        rows_compared=len(archived),
        rows_only_archive=len(only_a),
        rows_only_recomputed=len(only_r),
        rows_different=diff,
        avionics_same=stored == json.loads(json.dumps(again)),
    )
