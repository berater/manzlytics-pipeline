"""Olay eşiklerinin çok günlü kalibrasyon raporu (N16, plan §3.6–3.7: A7–A9).

Arşivdeki her günün saatlik özetinden olayları çıkarır ve şunları basar:

- olay boyutuna göre sınıflar ve "başka günlerde aynı yerde tekrarlama" payı (komşu hücre dahil);
- asgari etkilenen uçak sayısı adayları (kalan olay/gün, tekrar payı);
- olay düzeyi güven/seviye adayları (olay boyunca **tekil** gözlenen uçakla);
- arka plan oranıyla beklenen sahte sıcak hücre-saat (çoklu test: günde yüz binlerce test).

Kullanım: `uv run python services/detector/scripts/calibrate_events.py [--archive data/archive]`
Her gün için `jamming_ac_hourly_r5.parquet` + `avionics.json` gerekir (`archive-restore` ya da
GitHub REST ile indirilir).
Ürün koduna dokunmaz; sonuçları belgeye elle işleyin.
"""

from __future__ import annotations

import argparse
import math
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path

import h3
import manzlytics_detector.events as events_module
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from manzlytics_detector.daily import AVIONICS_FILE, HOURLY_FILE, faulty_from_file
from manzlytics_detector.events import (
    _COLUMNS,
    HOUR_S,
    _epoch_seconds,
    build_events,
    cell_hours_from_table,
)
from manzlytics_detector.levels import BACKGROUND_RATE, jamming_level
from manzlytics_detector.quality import data_confidence

REPEAT_DAYS = 5  # "tekrarlıyor": başka en az bu kadar günde aynı yerde olay
MIN_AIRCRAFT_CANDIDATES = (1, 2, 3, 4, 5, 8)


def day_events(day_dir: Path) -> tuple[list[dict], dict, int]:
    """(olaylar; `_obs` = tekil gözlenen uçak, n ≥ 5 düğümlerin n dağılımı, sıcak düğüm sayısı)."""
    table = pq.read_table(day_dir / HOURLY_FILE, columns=list(_COLUMNS))
    faulty = faulty_from_file(day_dir / AVIONICS_FILE)
    nodes = cell_hours_from_table(table, faulty)
    # Analiz süzülmemiş olaylar üzerinde yapılır (asgari uçak kuralı aday olarak değerlendirilir).
    events_module.MIN_EVENT_AIRCRAFT = 1
    events = build_events(nodes.values())
    hot: dict[tuple[int, str], int] = {}
    for i, e in enumerate(events):
        first = int(datetime.fromisoformat(e["first_seen"]).timestamp())
        for hour in range(first, first + HOUR_S * e["duration_hours"], HOUR_S):
            for cell in e["cells"]:
                if (hour, cell) in nodes and nodes[(hour, cell)].level in ("medium", "high"):
                    hot[(hour, cell)] = i
    keys = pa.table(
        {
            "hour": [k[0] for k in hot],
            "h3": pa.array([h3.str_to_int(k[1]) for k in hot], pa.uint64()),
            "ev": list(hot.values()),
        }
    )
    rows = pa.table(
        {"hour": _epoch_seconds(table["hour"]), "h3": table["h3"], "icao24": table["icao24"]}
    )
    rows = rows.filter(
        pc.invert(pc.is_in(rows["icao24"], value_set=pa.array(sorted(faulty), pa.string())))
    )
    grouped = (
        rows.join(keys, keys=["hour", "h3"], join_type="inner")
        .group_by("ev")
        .aggregate([("icao24", "count_distinct")])
    )
    observed = dict(
        zip(grouped["ev"].to_pylist(), grouped["icao24_count_distinct"].to_pylist(), strict=True)
    )
    for i, e in enumerate(events):
        e["_obs"] = observed.get(i, 0)
    n_hist = Counter(n.n_total for n in nodes.values() if n.n_total >= 5)
    return events, n_hist, sum(1 for n in nodes.values() if n.level in ("medium", "high"))


def expected_false_hot(n_hist: Counter) -> float:
    """Yalnız arka plan (`BACKGROUND_RATE`) olsaydı beklenen sıcak hücre-saat sayısı."""
    p = BACKGROUND_RATE
    total = 0.0
    for n, count in n_hist.items():
        total += count * sum(
            math.comb(n, k) * p**k * (1 - p) ** (n - k)
            for k in range(n + 1)
            if jamming_level(n, k) in ("medium", "high")
        )
    return total


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", type=Path, default=Path("data/archive"))
    args = ap.parse_args()
    root = args.archive / "adsblol"
    days = sorted(p.parent for p in root.rglob(HOURLY_FILE))
    by_day: dict[str, list[dict]] = {}
    for d in days:
        name = f"{d.parent.parent.name}-{d.parent.name}-{d.name}"
        events, n_hist, hot = day_events(d)
        by_day[name] = events
        print(
            f"{name}: {len(events)} olay, {hot} sıcak hücre-saat, "
            f"yalnız arka planla beklenen ≈ {expected_false_hot(n_hist):.0f}"
        )
    cells = {d: {c for e in es for c in e["cells"]} for d, es in by_day.items()}

    def repeats(e: dict, day: str) -> int:
        return sum(
            1
            for other, seen in cells.items()
            if other != day and any(set(h3.grid_disk(c, 1)) & seen for c in e["cells"][:30])
        )

    rows = [(e, repeats(e, d)) for d, es in by_day.items() for e in es]
    n_days = len(by_day)

    def share(sel: list) -> str:
        return f"%{100 * sum(1 for _, r in sel if r >= REPEAT_DAYS) / max(len(sel), 1):.1f}"

    print(
        f"\n{len(rows)} olay, {n_days} gün; 'tekrar' = başka ≥{REPEAT_DAYS} günde aynı/komşu hücre"
    )
    for k in MIN_AIRCRAFT_CANDIDATES:
        sel = [(e, r) for e, r in rows if e["affected_aircraft"] >= k]
        print(f"  etkilenen uçak ≥ {k}: {len(sel) / n_days:6.1f} olay/gün, tekrar {share(sel)}")
    obs = sorted(e["_obs"] for e, _ in rows)
    print(
        f"\nolay boyunca tekil gözlenen uçak: medyan {statistics.median(obs)}, "
        f"p90 {obs[int(0.9 * len(obs))]}"
    )
    print("  güven (olayda yazılan alan):", dict(Counter(e["confidence"] for e, _ in rows)))
    new = Counter(data_confidence(e["_obs"], e["affected_aircraft"]) for e, _ in rows)
    print("  güven (olay düzeyi, tekil uçak):", dict(new))
    for level in ("low", "medium", "high"):
        sel = [
            (e, r) for e, r in rows if data_confidence(e["_obs"], e["affected_aircraft"]) == level
        ]
        if sel:
            print(f"    {level:6}: {len(sel):5} olay, tekrar {share(sel)}")
    print("  ölçek (şimdi):", dict(Counter(e["scale"] for e, _ in rows)))
    print(
        "  seviye (olay düzeyi):",
        dict(Counter(jamming_level(e["_obs"], e["affected_aircraft"]) for e, _ in rows)),
    )
    print(
        f"\n≥ 40 hücreli olay: {sum(1 for e, _ in rows if len(e['cells']) >= 40)}, "
        f"en büyük {max(len(e['cells']) for e, _ in rows)} hücre"
    )


if __name__ == "__main__":
    main()
