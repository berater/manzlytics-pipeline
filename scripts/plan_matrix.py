"""backfill.yml'in iş planı: hedef arşivde olmayan günleri parçalara böler (iş matrisi).

Girdi (ortam): FROM, TO, DAYS, CHUNK, PARALLEL, TARGET_REPO, GITHUB_OUTPUT.
Çıktı: `matrix` (her eleman bir işin günleri, en yeniden eskiye), `parallel`, `months`
(işlenecek günlerin ayları; release'ler önceden oluşturulur).
"""

from __future__ import annotations

import json
import math
import os
from datetime import UTC, date, datetime, timedelta

from manzlytics_detector.plan import archived_days
from manzlytics_ingest.globe_history import FIRST_DAY

MAX_JOBS = 256  # GitHub Actions: bir matristeki en çok iş


def wanted_days() -> list[date]:
    if listed := os.environ.get("DAYS", "").split():
        return sorted({date.fromisoformat(d) for d in listed}, reverse=True)
    start = max(date.fromisoformat(os.environ.get("FROM") or FIRST_DAY.isoformat()), FIRST_DAY)
    end = date.fromisoformat(os.environ["TO"]) if os.environ.get("TO") else None
    end = end or datetime.now(UTC).date() - timedelta(days=1)
    return [end - timedelta(days=i) for i in range((end - start).days + 1)]


def main() -> None:
    days = [d for d in wanted_days() if d >= FIRST_DAY]
    done = archived_days(min(days), max(days), os.environ["TARGET_REPO"]) if days else set()
    todo = [d for d in days if d not in done]

    chunk = max(int(os.environ.get("CHUNK") or 6), math.ceil(len(todo) / MAX_JOBS), 1)
    matrix = []
    for i in range(0, len(todo), chunk):
        part = todo[i : i + chunk]
        label = f"{part[0]} … {part[-1]}" if len(part) > 1 else str(part[0])
        matrix.append({"days": " ".join(d.isoformat() for d in part), "label": label})
    months = sorted({f"{d:%Y-%m}" for d in todo})
    parallel = max(1, min(int(os.environ.get("PARALLEL") or 8), 20))

    print(f"istenen {len(days)} gün, arşivde {len(done)}, işlenecek {len(todo)}")
    print(f"{len(matrix)} iş × en çok {chunk} gün, aynı anda {parallel}")
    with open(os.environ["GITHUB_OUTPUT"], "a") as out:
        out.write(f"matrix={json.dumps(matrix)}\n")
        out.write(f"parallel={parallel}\n")
        out.write(f"months={' '.join(months)}\n")


if __name__ == "__main__":
    main()
