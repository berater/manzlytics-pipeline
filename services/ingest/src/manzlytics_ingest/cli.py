"""mz-ingest komut satırı."""

from __future__ import annotations

import argparse
import shutil
import time
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path

from manzlytics_common.clickhouse import ClickHouse

from manzlytics_ingest import globe_history
from manzlytics_ingest.adsblol import BBox, cover_bbox, snapshot
from manzlytics_ingest.archive import build_table, to_columns, write_day
from manzlytics_ingest.io import read_jsonl, write_jsonl
from manzlytics_ingest.store import insert_positions

# Odak bölge: Doğu Akdeniz, Karadeniz, Kafkasya, Orta Doğu (güney, batı, kuzey, doğu)
FOCUS_BBOX = "28,20,48,52"


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def run_live(
    bbox: BBox, interval: float, snapshots: int | None, ch: ClickHouse | None, out: Path | None
) -> None:
    """Anlık görüntüleri sürekli topla; ClickHouse'a ve/veya JSONL'e yaz."""
    print(f"{len(cover_bbox(bbox))} daire / görüntü, aralık {interval:.0f} sn", flush=True)
    i = 0
    while snapshots is None or i < snapshots:
        started = time.monotonic()
        stamp = _stamp()
        reports = list(snapshot(bbox))
        written = []
        if ch is not None:
            try:
                insert_positions(ch, reports)
                written.append("clickhouse")
            except Exception as e:  # geçici DB hatası süreci öldürmesin
                print(f"hata: ClickHouse yazılamadı: {e}", flush=True)
        if out is not None:
            write_jsonl(out / f"adsblol-{stamp}.jsonl.gz", reports)
            written.append("jsonl")
        i += 1
        total = "∞" if snapshots is None else snapshots
        print(f"[{i}/{total}] {stamp}: {len(reports)} uçak → {','.join(written)}", flush=True)
        if snapshots is None or i < snapshots:
            time.sleep(max(0.0, interval - (time.monotonic() - started)))


def _trace_columns(raw: bytes, bbox: BBox | None) -> dict[str, list]:
    return to_columns(globe_history.parse_trace(globe_history.load_trace(raw), bbox))


def run_daily(
    day: date, out: Path, cache: Path, bbox: BBox | None, workers: int | None, keep_parts: bool
) -> Path:
    """Bir UTC gününün adsb.lol arşivini indir, ayrıştır, temiz Parquet olarak yaz."""
    started = time.monotonic()
    urls, parts = globe_history.fetch_day(day, cache)
    downloaded = time.monotonic()
    files = 0

    def counted():
        nonlocal files
        for raw in globe_history.iter_trace_files(parts):
            files += 1
            yield raw

    table = build_table(counted(), partial(_trace_columns, bbox=bbox), workers)
    path = write_day(
        table,
        out,
        "adsblol",
        day,
        bbox=bbox,
        inputs=urls,
        stats={
            "trace_files": files,
            "download_s": round(downloaded - started),
            "process_s": round(time.monotonic() - downloaded),
        },
    )
    if not keep_parts:
        shutil.rmtree(cache / day.isoformat(), ignore_errors=True)
    mb = path.stat().st_size / 1e6
    print(f"{day.isoformat()}: {files} uçak dosyası, {table.num_rows} nokta → {path} ({mb:.0f} MB)")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mz-ingest")
    sub = parser.add_subparsers(dest="command", required=True)

    daily = sub.add_parser("daily", help="Bir UTC gününün arşivini işle")
    daily.add_argument("--date", type=date.fromisoformat, required=True)
    daily.add_argument("--source", default="adsblol", choices=["adsblol"])
    daily.add_argument("--out", type=Path, default=Path("data/archive"))
    daily.add_argument("--cache", type=Path, default=Path("data/cache"), help="indirilen parçalar")
    region = daily.add_mutually_exclusive_group()
    region.add_argument("--bbox", help="güney,batı,kuzey,doğu (varsayılan: odak bölge)")
    region.add_argument("--world", action="store_true", help="bölge filtresi olmadan")
    daily.add_argument("--workers", type=int, help="süreç sayısı (varsayılan: çekirdek sayısı)")
    daily.add_argument("--keep-parts", action="store_true", help="indirilen parçaları silme")

    def live_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--bbox", default=FOCUS_BBOX, help="güney,batı,kuzey,doğu")
        p.add_argument("--interval", type=float, default=90, help="görüntüler arası saniye")

    sample = sub.add_parser("live-sample", help="Belirli sayıda görüntüyü JSONL'e yaz")
    live_args(sample)
    sample.add_argument("--snapshots", type=int, default=10)
    sample.add_argument("--out", type=Path, default=Path("data/raw"))

    live = sub.add_parser("live", help="Sürekli topla ve ClickHouse'a yaz (servis modu)")
    live_args(live)
    live.add_argument("--also-jsonl", type=Path, help="ham görüntüleri ayrıca buraya yaz")

    load = sub.add_parser("load", help="JSONL dosyalarını ClickHouse'a yükle")
    load.add_argument("inputs", nargs="+", type=Path)

    def archive_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--date", type=date.fromisoformat, required=True)
        p.add_argument("--source", default="adsblol", choices=["adsblol"])
        p.add_argument("--archive", type=Path, default=Path("data/archive"))
        p.add_argument("--repo", help="sahip/depo (varsayılan: gh'nin bulduğu depo)")

    archive_args(sub.add_parser("archive-publish", help="Günü kalıcı arşive (Releases) yükle"))
    archive_args(sub.add_parser("archive-restore", help="Günü kalıcı arşivden indir, doğrula"))

    args = parser.parse_args(argv)
    if args.command == "daily":
        if args.world:
            bbox = None
        else:
            bbox = BBox.parse(args.bbox) if args.bbox else globe_history.ARCHIVE_BBOX
        run_daily(args.date, args.out, args.cache, bbox, args.workers, args.keep_parts)
    elif args.command == "live-sample":
        run_live(BBox.parse(args.bbox), args.interval, args.snapshots, None, args.out)
    elif args.command == "live":
        run_live(BBox.parse(args.bbox), args.interval, None, ClickHouse.from_env(), args.also_jsonl)
    elif args.command == "archive-publish":
        from manzlytics_ingest.release import publish_day, release_tag

        names = publish_day(args.archive, args.source, args.date, args.repo)
        print(f"{release_tag(args.date)}: {len(names)} dosya yüklendi ({', '.join(names)})")
    elif args.command == "archive-restore":
        from manzlytics_ingest.release import restore_day

        d = restore_day(args.archive, args.source, args.date, args.repo)
        print(f"{args.date.isoformat()}: arşivden indirildi ve doğrulandı → {d}")
    elif args.command == "load":
        files = [
            f for p in args.inputs for f in (sorted(p.rglob("*.jsonl.gz")) if p.is_dir() else [p])
        ]
        n = insert_positions(ClickHouse.from_env(), read_jsonl(files))
        print(f"{len(files)} dosya, {n} rapor yüklendi")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
