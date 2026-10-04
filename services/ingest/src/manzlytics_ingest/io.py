"""PositionReport'ları gzip'li JSON Lines olarak okur/yazar (prototip depolaması)."""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterable, Iterator
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from manzlytics_ingest.models import PositionReport


def write_jsonl(path: Path, reports: Iterable[PositionReport]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with gzip.open(path, "at", encoding="utf-8") as f:
        for r in reports:
            row = asdict(r)
            row["ts"] = r.ts.isoformat()
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
            n += 1
    return n


def read_jsonl(paths: Iterable[Path]) -> Iterator[PositionReport]:
    for path in paths:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                row = json.loads(line)
                row["ts"] = datetime.fromisoformat(row["ts"])
                yield PositionReport(**row)
