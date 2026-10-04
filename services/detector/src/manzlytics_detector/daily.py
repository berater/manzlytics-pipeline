"""Günlük arşiv işi: bir UTC gününün adsb.lol arşivinden iki çıktı üretir.

1. Odak bölgenin temiz konum verisi (`positions.parquet`, ingest/archive.py)
2. Dünya geneli saatlik hücre × uçak özeti (`jamming_ac_hourly_r5.parquet`, hourly.py)
3. Arızalı aviyonik kararı (`avionics.json`, avionics.py): dünya özetinden, komşu karşılaştırması.
   Özet satırları silinmez; hücre hesaplayan her adım bu listeyi `exclude` olarak kullanır.

Karar A (docs/veri-stratejisi.md §8): odak bölge her nokta, dünyanın geri kalanı yalnız özet.
Arşiv tek geçişte okunur; her iz dosyası iki çıktıya birden ayrıştırılır.
"""

from __future__ import annotations

import json
import shutil
import time
from datetime import date
from functools import partial
from pathlib import Path

from manzlytics_ingest import globe_history
from manzlytics_ingest.adsblol import BBox
from manzlytics_ingest.archive import (
    POSITIONS_SORT,
    Collector,
    day_dir,
    file_entry,
    parallel_map,
    to_columns,
    write_day,
    write_parquet,
)

from manzlytics_detector.avionics import assess_avionics, avionics_report, observations_from_hourly
from manzlytics_detector.hourly import HOURLY_SCHEMA, HOURLY_SORT, hourly_rows
from manzlytics_detector.version import ALGORITHM_VERSION

HOURLY_FILE = "jamming_ac_hourly_r5.parquet"
AVIONICS_FILE = "avionics.json"


def write_avionics(hourly_table, path: Path) -> dict:
    report = avionics_report(assess_avionics(observations_from_hourly(hourly_table)))
    path.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n")
    return report


def faulty_from_file(path: Path) -> frozenset[str]:
    return frozenset(a["icao24"] for a in json.loads(path.read_text())["faulty"])


# Kaynak arşivde ara sıra yarım kalmış (kesik gzip / bozuk JSON) tek uçak izi çıkar; bunlar günü
# düşürmesin, sayılıp atlansın. Oran bu sınırı aşarsa indirme/arşiv bozuktur: gün hata verir.
MAX_CORRUPT_SHARE = 0.01
CORRUPT_TRACE_ERRORS = (EOFError, OSError, ValueError, KeyError, TypeError)


def trace_outputs(raw: bytes, focus: BBox | None) -> tuple[dict[str, list], dict[str, list], bool]:
    """(konum sütunları, saatlik sütunlar, iz okunabildi mi)."""
    try:
        reports = list(globe_history.parse_trace(globe_history.load_trace(raw)))
    except CORRUPT_TRACE_ERRORS:
        return to_columns([]), hourly_rows([]), False
    in_focus = reports if focus is None else [r for r in reports if focus.contains(r.lat, r.lon)]
    return to_columns(in_focus), hourly_rows(reports), True


def run_daily(
    day: date,
    out: Path,
    cache: Path,
    focus: BBox | None = globe_history.ARCHIVE_BBOX,
    workers: int | None = None,
    keep_parts: bool = False,
) -> Path:
    started = time.monotonic()
    urls, parts = globe_history.fetch_day(day, cache)
    downloaded = time.monotonic()

    positions, hourly = Collector(), Collector(HOURLY_SCHEMA)
    files = corrupt = 0
    for pos_cols, hourly_cols, ok in parallel_map(
        partial(trace_outputs, focus=focus), globe_history.iter_trace_files(parts), workers
    ):
        files += 1
        corrupt += not ok
        positions.add(pos_cols)
        hourly.add(hourly_cols)
    if corrupt:
        print(f"uyarı: {corrupt} / {files} iz dosyası okunamadı, atlandı")
    if corrupt > files * MAX_CORRUPT_SHARE:
        raise SystemExit(
            f"{day.isoformat()}: iz dosyalarının {corrupt} / {files}'i bozuk "
            f"(sınır %{MAX_CORRUPT_SHARE * 100:.0f}); indirme/arşiv bozuk olabilir"
        )

    d = day_dir(out, "adsblol", day)
    hourly_table = hourly.table(HOURLY_SORT).replace_schema_metadata(
        {"algorithm_version": ALGORITHM_VERSION}
    )
    hourly_path = write_parquet(hourly_table, d / HOURLY_FILE)
    avionics = write_avionics(hourly_table, d / AVIONICS_FILE)
    path = write_day(
        positions.table(POSITIONS_SORT),
        out,
        "adsblol",
        day,
        bbox=focus,
        inputs=urls,
        stats={
            "trace_files": files,
            "trace_files_corrupt": corrupt,
            "download_s": round(downloaded - started),
            "process_s": round(time.monotonic() - downloaded),
        },
        extra={
            "algorithm_version": ALGORITHM_VERSION,
            "hourly": file_entry(
                hourly_path, scope="world", resolution=5, rows=hourly_table.num_rows
            ),
            "avionics": file_entry(d / AVIONICS_FILE, faulty=len(avionics["faulty"])),
        },
    )
    if not keep_parts:
        shutil.rmtree(cache / day.isoformat(), ignore_errors=True)
    print(
        f"{day.isoformat()}: {files} uçak dosyası → {path.parent} "
        f"(konum {path.stat().st_size / 1e6:.0f} MB, "
        f"özet {hourly_table.num_rows} satır / {hourly_path.stat().st_size / 1e6:.0f} MB, "
        f"arızalı aviyonik {len(avionics['faulty'])} uçak)"
    )
    return path
