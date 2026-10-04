"""Günlük temiz konum arşivi: PositionReport → Parquet (+ manifest).

Düzen: `<out>/<kaynak>/YYYY/MM/DD/positions.parquet` ve yanında `manifest.json`.
Satırlar uçak + zamana göre sıralıdır (sıkıştırma ve uçak bazlı okuma için).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict
from datetime import UTC, date, datetime
from multiprocessing import Pool
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from manzlytics_ingest.adsblol import BBox
from manzlytics_ingest.models import PositionReport
from manzlytics_ingest.sources import SOURCES, source_key

SCHEMA = pa.schema(
    [
        ("ts", pa.timestamp("ms", tz="UTC")),
        ("icao24", pa.string()),
        ("lat", pa.float64()),
        ("lon", pa.float64()),
        ("alt_baro_ft", pa.float32()),
        ("alt_geom_ft", pa.float32()),
        ("gs_kt", pa.float32()),
        ("track", pa.float32()),
        ("callsign", pa.string()),
        ("nic", pa.int8()),
        ("nac_p", pa.int8()),
        ("sil", pa.int8()),
        ("nic_age_s", pa.float32()),
        ("source", pa.dictionary(pa.int16(), pa.string())),
        ("on_ground", pa.bool_()),
        ("anonymous", pa.bool_()),
    ]
)
COLUMNS = SCHEMA.names
BATCH_ROWS = 1_000_000


def day_dir(out: Path, source: str, day: date) -> Path:
    return out / source / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"


def to_columns(reports: Iterable[PositionReport]) -> dict[str, list]:
    cols: dict[str, list] = {c: [] for c in COLUMNS}
    for r in reports:
        row = asdict(r)
        row["ts"] = int(r.ts.timestamp() * 1000)
        for c in COLUMNS:
            cols[c].append(row[c])
    return cols


class Collector:
    """Sütun sözlüklerini biriktirip belirli aralıklarla Arrow tablosuna çevirir (bellek için)."""

    def __init__(self, schema: pa.Schema = SCHEMA, batch_rows: int = BATCH_ROWS):
        self.schema = schema
        self.batch_rows = batch_rows
        self._tables: list[pa.Table] = []
        self._buf: dict[str, list] = {c: [] for c in schema.names}

    def add(self, cols: dict[str, list]) -> None:
        for c in self.schema.names:
            self._buf[c].extend(cols[c])
        if len(self._buf[self.schema.names[0]]) >= self.batch_rows:
            self._flush()

    def _flush(self) -> None:
        self._tables.append(
            pa.table(
                {c: pa.array(v, type=self.schema.field(c).type) for c, v in self._buf.items()},
                schema=self.schema,
            )
        )
        self._buf = {c: [] for c in self.schema.names}

    def table(self, sort_by: list[tuple[str, str]] | None = None) -> pa.Table:
        self._flush()
        table = pa.concat_tables(self._tables)
        return table.sort_by(sort_by) if sort_by else table


POSITIONS_SORT = [("icao24", "ascending"), ("ts", "ascending")]


def build_table(
    items: Iterable, parse: Callable[..., dict[str, list]], workers: int | None = None
) -> pa.Table:
    """Her öğeyi (ör. bir iz dosyası) `parse` ile sütunlara çevirip tek tabloda toplar.

    workers > 1 ise ayrıştırma süreç havuzunda yapılır; `parse` modül düzeyinde olmalı.
    """
    collector = Collector()
    for cols in parallel_map(parse, items, workers):
        collector.add(cols)
    return collector.table(POSITIONS_SORT)


def parallel_map(func: Callable, items: Iterable, workers: int | None = None) -> Iterator:
    workers = workers or os.cpu_count() or 1
    if workers > 1:
        with Pool(workers) as pool:
            yield from pool.imap_unordered(func, items, chunksize=32)
    else:
        yield from map(func, items)


def write_parquet(table: pa.Table, path: Path) -> Path:
    """Atomik yazım: önce `.part`, sonra yeniden adlandırma."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    pq.write_table(table, tmp, compression="zstd", compression_level=9, row_group_size=1 << 20)
    tmp.replace(path)
    return path


def file_sha256(path: Path, chunk: int = 8 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def file_entry(path: Path, **info) -> dict:
    """Manifest'te bir yan dosyanın kaydı (geri yüklemede boyut ve özet doğrulanır)."""
    return {"file": path.name, **info, "bytes": path.stat().st_size, "sha256": file_sha256(path)}


def write_day(
    table: pa.Table,
    out: Path,
    source: str,
    day: date,
    *,
    bbox: BBox | None,
    inputs: list[str],
    stats: dict | None = None,
    extra: dict | None = None,
) -> Path:
    """Parquet'i ve manifest'i yazar; aynı gün tekrar yazılırsa üzerine yazar (idempotent).

    `extra` manifest'e eklenir (ör. aynı günün özet dosyaları, `file_entry` ile).
    """
    d = day_dir(out, source, day)
    path = write_parquet(table, d / "positions.parquet")
    src = SOURCES[source_key(source)]
    manifest = {
        "date": day.isoformat(),
        "source": source,
        "license": src.license,
        "attribution": src.attribution,
        "bbox": None if bbox is None else asdict(bbox),
        "inputs": inputs,
        "rows": table.num_rows,
        "aircraft": len(table["icao24"].unique()),
        "bytes": path.stat().st_size,
        "sha256": file_sha256(path),
        "stats": dict(Counter(stats or {})),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        **(extra or {}),
    }
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return path
