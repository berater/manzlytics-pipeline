"""mz-detect komut satırı: ham raporlardan jamming özeti ve GeoJSON üretir."""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import h3
from manzlytics_common.clickhouse import ClickHouse
from manzlytics_ingest import globe_history
from manzlytics_ingest.adsblol import BBox
from manzlytics_ingest.archive import day_dir
from manzlytics_ingest.globe_history import ARCHIVE_BBOX
from manzlytics_ingest.io import read_jsonl

from manzlytics_detector.avionics import faulty_aircraft, observations_from_reports
from manzlytics_detector.jamming import BAD_NIC_BELOW, BAD_SHARE, aggregate_jamming, is_adsb
from manzlytics_detector.levels import jamming_level
from manzlytics_detector.store import aggregate

# `mz-detect daily`: günün arşivi kaynakta henüz yok (hata değil; archive.yml bunu atlar).
EXIT_NOT_PUBLISHED = 3
# `mz-detect mark --failed`: gün deneme sınırına ulaştı, karantinada (archive.yml kırmızı olur).
EXIT_QUARANTINED = 10
# `mz-detect check-contract`: gün yayınlanamaz (saatlik özet boş/okunamıyor); publish çalışmaz.
EXIT_CONTRACT_ERROR = 12


def _files(inputs: list[Path]) -> list[Path]:
    out: list[Path] = []
    for p in inputs:
        out.extend(sorted(p.rglob("*.jsonl.gz")) if p.is_dir() else [p])
    return out


def to_geojson(cells, meta: dict | None = None) -> dict:
    features = []
    for c in cells.values():
        ring = [[lon, lat] for lat, lon in h3.cell_to_boundary(c.h3)]
        ring.append(ring[0])
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": {
                    "h3": c.h3,
                    "n_total": c.n_total,
                    "n_bad": c.n_bad,
                    "ratio_bad": round(c.ratio_bad, 4),
                    "level": jamming_level(c.n_total, c.n_bad),
                    "bad_aircraft": [
                        {
                            "icao24": None if a.anonymous else a.icao24,
                            "callsign": None if a.anonymous else a.callsign,
                            "min_nic": a.min_nic,
                            "anonymous": a.anonymous,
                        }
                        for a in c.bad_aircraft
                    ],
                },
            }
        )
    return {"type": "FeatureCollection", "meta": meta or {}, "features": features}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mz-detect")
    sub = parser.add_subparsers(dest="command", required=True)
    jam = sub.add_parser("jamming", help="H3 jamming özeti")
    jam.add_argument("inputs", nargs="+", type=Path)
    jam.add_argument("--res", type=int, default=4)
    jam.add_argument("--geojson", type=Path)
    jam.add_argument("--top", type=int, default=15)
    jam.add_argument(
        "--keep-faulty", action="store_true", help="arızalı aviyonik filtresini uygulama"
    )
    agg = sub.add_parser("aggregate", help="ClickHouse'ta saatlik agregasyonu (yeniden) hesapla")
    agg.add_argument("--start", type=datetime.fromisoformat)
    agg.add_argument("--end", type=datetime.fromisoformat)
    agg.add_argument("--hours-back", type=int, default=3, help="--start yoksa: son N saat")
    agg.add_argument("--every", type=float, help="servis modu: her N saniyede bir tekrarla")

    daily = sub.add_parser(
        "daily", help="adsb.lol günlük arşivi → odak bölge konumları + dünya saatlik özeti"
    )
    daily.add_argument("--date", type=date.fromisoformat, required=True)
    daily.add_argument("--out", type=Path, default=Path("data/archive"))
    daily.add_argument("--cache", type=Path, default=Path("data/cache"), help="indirilen parçalar")
    daily.add_argument("--bbox", help="konumların saklanacağı bölge (varsayılan: odak bölge)")
    daily.add_argument("--workers", type=int, help="süreç sayısı (varsayılan: çekirdek sayısı)")
    daily.add_argument("--keep-parts", action="store_true", help="indirilen parçaları silme")

    def day_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--date", type=date.fromisoformat, required=True)
        p.add_argument("--archive", type=Path, default=Path("data/archive"))

    day_args(
        sub.add_parser(
            "check-contract",
            help="Yayın öncesi kapı: saatlik özet boşsa (0 satır) çıkış 12; yalnız bu kontrol",
        )
    )
    avi = sub.add_parser(
        "avionics", help="arşivlenmiş bir günde arızalı aviyonik filtresinin etkisi (eşik kontrolü)"
    )
    day_args(avi)
    avi.add_argument("--res", type=int, nargs="+", default=[3, 4])
    avi.add_argument("--top", type=int, default=10)
    spoof = sub.add_parser(
        "spoofing", help="arşivlenmiş bir günde konum sıçraması olayları (N5, deneysel)"
    )
    day_args(spoof)
    spoof.add_argument("--top", type=int, default=10)
    spoof.add_argument(
        "--json", type=Path, help="olayları bu dosyaya JSON olarak yaz (harita için)"
    )
    spoof.add_argument("--bbox", help="güney,batı,kuzey,doğu (yalnız --json için bölge filtresi)")
    ev = sub.add_parser(
        "events", help="arşivlenmiş bir günde olay nesneleri (N16: jamming + spoofing ortak şema)"
    )
    day_args(ev)
    ev.add_argument("--json", type=Path, help="olayları bu dosyaya JSON olarak yaz (events.json)")
    ev.add_argument("--bbox", help="güney,batı,kuzey,doğu (yalnız --json için bölge filtresi)")
    ev.add_argument("--top", type=int, default=10)
    day_args(
        sub.add_parser("verify", help="arşivden günü baştan hesapla, arşivdeki özetle karşılaştır")
    )

    export = sub.add_parser(
        "export-static", help="Haritanın okuyacağı statik JSON dosyalarını üret (#48)"
    )
    export.add_argument("--date", type=date.fromisoformat, required=True)
    export.add_argument("--out", type=Path, default=Path("apps/web/public/data"))
    export.add_argument("--archive", type=Path, default=Path("data/archive"))
    scope = export.add_mutually_exclusive_group()
    scope.add_argument("--bbox", help="güney,batı,kuzey,doğu (varsayılan: harita bölgesi)")
    scope.add_argument("--world", action="store_true", help="bölge filtresi olmadan (çok büyük)")

    backfill = sub.add_parser(
        "load-hourly",
        help="Arşivlenmiş bir günün dünya saatlik özetini ClickHouse'a yükle (#24, idempotent)",
    )
    day_args(backfill)

    plan = sub.add_parser(
        "plan",
        help="Eksik günleri (arşivde ya da statik veride yok) en yeniden eskiye listele",
    )
    plan.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    plan.add_argument("--to", dest="end", type=date.fromisoformat, help="varsayılan: dün (UTC)")
    plan.add_argument("--static-dir", type=Path, default=Path("apps/web/public/data"))
    plan.add_argument("--repo", help="sahip/repo (varsayılan: gh'nin geçerli reposu)")
    plan.add_argument("--max-attempts", type=int, default=3)
    plan.add_argument("--cooldown-days", type=int, default=3)

    mark = sub.add_parser("mark", help="Günün sonucunu kaydet (başarısızlık sayacı; archive.yml)")
    mark.add_argument("--date", type=date.fromisoformat, required=True)
    mark.add_argument("--repo")
    outcome = mark.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--failed", action="store_true")
    outcome.add_argument("--ok", action="store_true")
    mark.add_argument("--error", default="", help="--failed için kısa açıklama")
    mark.add_argument("--max-attempts", type=int, default=3)

    args = parser.parse_args(argv)
    if args.command == "plan":
        return run_plan(args)
    if args.command == "mark":
        return run_mark(args)
    if args.command == "check-contract":
        return run_check_contract(args)
    if args.command == "aggregate":
        return run_aggregate(args)
    if args.command == "daily":
        from manzlytics_detector.daily import run_daily

        focus = BBox.parse(args.bbox) if args.bbox else ARCHIVE_BBOX
        try:
            run_daily(args.date, args.out, args.cache, focus, args.workers, args.keep_parts)
        except globe_history.ArchiveNotPublished as e:
            # Hata değil: gece işi (archive.yml) bu kodla "henüz yayınlanmadı" der ve sonraki
            # zamanlanmış çalıştırmada yeniden dener.
            print(f"{e}; kaynakta henüz yok")
            return EXIT_NOT_PUBLISHED
        return 0
    if args.command == "avionics":
        return run_avionics(args)
    if args.command == "spoofing":
        return run_spoofing(args)
    if args.command == "events":
        return run_events(args)
    if args.command == "verify":
        return run_verify(args)
    if args.command == "export-static":
        return run_export_static(args)
    if args.command == "load-hourly":
        return run_load_hourly(args)
    return run_jamming(args)


def run_plan(args: argparse.Namespace) -> int:
    """Her satır `GÜN process|export`; boş çıktı = pencere tamam ya da kalanlar karantinada."""
    from manzlytics_detector import plan

    end = args.end or datetime.now(UTC).date() - timedelta(days=1)
    items = plan.select_days(
        args.start,
        end,
        plan.archived_days(args.start, end, args.repo),
        plan.exported_days(args.static_dir),
        plan.load_failures(args.repo),
        max_attempts=args.max_attempts,
        cooldown_days=args.cooldown_days,
    )
    if items:
        print(plan.format_plan(items))
    return 0


def run_check_contract(args: argparse.Namespace) -> int:
    """Saatlik özet boş ya da okunamıyorsa `::error::` + 12 (gün yayınlanmaz); aksi halde 0."""
    from manzlytics_detector.contract import ensure_nonempty
    from manzlytics_detector.daily import HOURLY_FILE

    day = args.date.isoformat()
    try:
        ensure_nonempty(day_dir(args.archive, "adsblol", args.date) / HOURLY_FILE)
    except Exception as e:  # noqa: BLE001 - her kapı hatası görünür ve sıfırdan farklı çıkmalı
        print(f"::error::{day}: sözleşme kontrolü başarısız: {type(e).__name__}: {e}")
        return EXIT_CONTRACT_ERROR
    print(f"{day}: saatlik özet dolu")
    return 0


def run_mark(args: argparse.Namespace) -> int:
    from manzlytics_detector import plan

    failures = plan.load_failures(args.repo)
    if args.failed:
        updated = plan.record_failure(failures, args.date, args.error)
    else:
        updated = plan.record_success(failures, args.date)
    if updated != failures:
        plan.save_failures(updated, args.repo)
    if args.failed and updated[args.date.isoformat()]["count"] >= args.max_attempts:
        return EXIT_QUARANTINED
    return 0


def run_spoofing(args: argparse.Namespace) -> int:
    """Günün positions.parquet'inde sıçrama olaylarını say; ölçüm/eşik kontrolü için."""
    from manzlytics_detector.spoofing import detect_jumps, events_from_jumps
    from manzlytics_detector.verify import _reports_by_aircraft

    d = day_dir(args.archive, "adsblol", args.date)
    jumps = []
    aircraft = 0
    for reports in _reports_by_aircraft(d / "positions.parquet"):
        aircraft += 1
        jumps += detect_jumps(reports)
    events = events_from_jumps(jumps)
    by_conf = Counter(e.confidence for e in events)
    print(
        f"{args.date.isoformat()}: {aircraft} uçak, {len(jumps)} sıçrama, {len(events)} olay "
        f"({dict(by_conf)})"
    )
    if args.json:
        from manzlytics_detector.spoofing import events_payload

        region = BBox.parse(args.bbox) if args.bbox else None
        payload = events_payload(args.date.isoformat(), events, region)
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        print(f"  → {args.json} ({payload['count']} olay)")
    for e in sorted(events, key=lambda e: -len(e.jumps))[: args.top]:
        j = e.jumps[0]
        print(
            f"  {e.icao24} {e.confidence:<17} {len(e.jumps)} sıçrama, {e.start:%H:%M}Z, "
            f"{j.dist_nm:.0f} nm, ~{j.speed_kt:.0f} kt"
        )
    return 0


def run_events(args: argparse.Namespace) -> int:
    """Günün olay nesnelerini çıkar (N16); özet yazdırır, istenirse `events.json` yazar."""
    from manzlytics_detector.events import events_for_day, events_payload

    events = events_for_day(day_dir(args.archive, "adsblol", args.date))
    payload = events_payload(args.date.isoformat(), events)
    print(
        f"{args.date.isoformat()}: {payload['count']} olay {payload['by_type']}, "
        f"güven {payload['by_confidence']} (algoritma {payload['algorithm_version']})"
    )
    if args.json:
        region = BBox.parse(args.bbox) if args.bbox else None
        out = events_payload(args.date.isoformat(), events, region)
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
        print(f"  → {args.json} ({out['count']} olay)")
    for e in sorted(payload["events"], key=lambda e: -e["affected_aircraft"])[: args.top]:
        print(
            f"  {e['event_id']} {e['type']:<8} {e['first_seen']} {e['duration_hours']} sa, "
            f"{len(e['cells'])} hücre, {e['affected_aircraft']} uçak, "
            f"{e['scale'] or '-'}, güven {e['confidence']}, FIR {e['fir'] or '-'}"
        )
    return 0


def run_export_static(args: argparse.Namespace) -> int:
    from manzlytics_detector.static_export import (
        MAP_REGION,
        archive_extras,
        check_world_target,
        export_data_range,
        export_day,
    )

    region = None if args.world else BBox.parse(args.bbox) if args.bbox else MAP_REGION
    check_world_target(region, args.out)
    ch = ClickHouse.from_env()
    extra = archive_extras(day_dir(args.archive, "adsblol", args.date), args.date, region, ch)
    d = export_day(ch, args.date, args.out, region=region, extra=extra)
    export_data_range(ch, args.out)
    n_cells = sum(1 for _ in (d / "cells").glob("*.json"))
    print(f"{args.date.isoformat()}: statik veri → {d} ({n_cells} hücre)")
    return 0


def run_load_hourly(args: argparse.Namespace) -> int:
    """Arşivdeki `jamming_ac_hourly_r5.parquet`ı ClickHouse'a yükler (geriye dönük, #24)."""
    import pyarrow.parquet as pq

    from manzlytics_detector.daily import HOURLY_FILE
    from manzlytics_detector.hourly import load_into_clickhouse

    ch = ClickHouse.from_env()
    d = day_dir(args.archive, "adsblol", args.date)
    hourly_table = pq.read_table(d / HOURLY_FILE)
    n = load_into_clickhouse(ch, hourly_table)
    print(
        f"{args.date.isoformat()}: {hourly_table.num_rows} özet satırı → ClickHouse "
        f"({n} satır eklendi)"
    )
    return 0


def run_verify(args: argparse.Namespace) -> int:
    from manzlytics_detector.verify import verify_day

    r = verify_day(day_dir(args.archive, "adsblol", args.date))
    print(
        f"{args.date.isoformat()}: {r.cells_compared} hücre, {r.rows_compared} özet satırı "
        f"karşılaştırıldı; yalnız arşivde {r.rows_only_archive}, yalnız yeniden hesapta "
        f"{r.rows_only_recomputed}, farklı {r.rows_different}; arızalı aviyonik kararı "
        f"{'aynı' if r.avionics_same else 'FARKLI'}"
    )
    print("sonuç: " + ("aynı" if r.ok else "FARKLI"))
    return 0 if r.ok else 1


def run_avionics(args: argparse.Namespace) -> int:
    """Filtre hücre seviyelerini nasıl değiştiriyor: eşiklerin gerçek bir günle kontrolü."""
    import pyarrow.parquet as pq

    from manzlytics_detector.daily import AVIONICS_FILE, HOURLY_FILE
    from manzlytics_detector.hourly import cells_from_hourly

    d = day_dir(args.archive, "adsblol", args.date)
    report = json.loads((d / AVIONICS_FILE).read_text())
    faulty = frozenset(a["icao24"] for a in report["faulty"])
    rows = pq.read_table(d / HOURLY_FILE).to_pylist()
    affected = {r["icao24"] for r in rows if r["n_bad_reports"] >= BAD_SHARE * r["n_reports"]}
    print(
        f"{args.date.isoformat()}: {report['aircraft_assessed']} uçak, karar verilebilen "
        f"{report['aircraft_decidable']}, arızalı {len(faulty)} "
        f"(en az bir saat-hücrede etkilenen {len(affected)} uçağın "
        f"%{100 * len(faulty & affected) / max(len(affected), 1):.1f}'i)"
    )
    order = ["noData", "low", "medium", "high"]
    for res in args.res:
        before = cells_from_hourly(rows, res)
        after = cells_from_hourly(rows, res, exclude=faulty)
        lv = {c: jamming_level(s.n_total, s.n_bad) for c, s in before.items()}
        lv2 = {c: jamming_level(s.n_total, s.n_bad) for c, s in after.items()}
        moves = Counter((lv[c], lv2.get(c, "noData")) for c in lv if lv[c] != lv2.get(c))
        print(f"res {res}: {len(lv)} hücre; önce {dict(Counter(lv.values()))}")
        print(f"        sonra {dict(Counter(lv2.values()))}")
        for (a, b), n in sorted(moves.items(), key=lambda kv: (order.index(kv[0][0]), kv[0][1])):
            print(f"        {a:>12} → {b:<12} {n}")
        changed = sorted(
            (c for c in lv if lv[c] != lv2.get(c) and lv[c] in ("medium", "high")),
            key=lambda c: before[c].n_bad,
            reverse=True,
        )
        for c in changed[: args.top]:
            lat, lon = h3.cell_to_latlng(c)
            b, a = before[c], after.get(c)
            print(
                f"        {c} ({lat:6.2f},{lon:7.2f}) {b.n_bad}/{b.n_total} {lv[c]} → "
                f"{a.n_bad if a else 0}/{a.n_total if a else 0} {lv2.get(c, 'insufficient')}"
            )
    return 0


def run_aggregate(args: argparse.Namespace) -> int:
    ch = ClickHouse.from_env()
    while True:
        end = args.end or datetime.now(UTC)
        start = args.start or end - timedelta(hours=args.hours_back)
        t0 = time.monotonic()
        try:
            aggregate(ch, start, end)
            print(
                f"agregasyon {start:%Y-%m-%d %H:%M} – {end:%H:%M}Z "
                f"({time.monotonic() - t0:.1f} sn)",
                flush=True,
            )
        except Exception as e:
            if not args.every:
                raise
            print(f"hata: {e}", flush=True)
        if not args.every:
            return 0
        time.sleep(args.every)


def run_jamming(args: argparse.Namespace) -> int:
    reports = [r for r in read_jsonl(_files(args.inputs)) if not r.on_ground]
    adsb = [r for r in reports if is_adsb(r)]
    aircraft = {r.icao24 for r in adsb}
    nic = Counter(r.nic for r in adsb)
    print(f"rapor: {len(reports)} (ADS-B: {len(adsb)}), tekil uçak: {len(aircraft)}")
    print(f"kaynak: {dict(Counter(r.source for r in reports).most_common())}")
    print("NIC dağılımı (rapor):")
    for k in sorted(nic, key=lambda x: (x is None, x if x is not None else 0)):
        share = nic[k] / len(adsb)
        print(f"  NIC {str(k):>4}: {nic[k]:>7}  {share:6.1%}")
    bad_ac = {r.icao24 for r in adsb if r.nic is not None and r.nic < BAD_NIC_BELOW}
    print(f"NIC<{BAD_NIC_BELOW} en az bir kez bildiren uçak: {len(bad_ac)} / {len(aircraft)}")

    faulty = frozenset() if args.keep_faulty else faulty_aircraft(observations_from_reports(adsb))
    if faulty:
        print(f"arızalı aviyonik (hesaptan çıkarıldı): {len(faulty)} uçak")
    cells = aggregate_jamming(adsb, resolution=args.res, exclude=faulty)
    levels = Counter(jamming_level(c.n_total, c.n_bad) for c in cells.values())
    print(f"H3 res {args.res}: {len(cells)} hücre, seviyeler: {dict(levels)}")
    ranked = sorted(cells.values(), key=lambda c: (c.n_bad, c.ratio_bad), reverse=True)
    print(f"En çok etkilenen {args.top} hücre:")
    for c in ranked[: args.top]:
        if not c.n_bad:
            break
        lat, lon = h3.cell_to_latlng(c.h3)
        who = ", ".join(
            "(gizli)" if a.anonymous else (a.callsign or a.icao24) for a in c.bad_aircraft[:5]
        )
        print(
            f"  {c.h3} ({lat:6.2f},{lon:6.2f}) {c.n_bad:>3}/{c.n_total:<4} "
            f"{c.ratio_bad:6.1%} {jamming_level(c.n_total, c.n_bad):<7} {who}"
        )

    if args.geojson:
        args.geojson.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "period_start": min(r.ts for r in adsb).isoformat() if adsb else None,
            "period_end": max(r.ts for r in adsb).isoformat() if adsb else None,
            "resolution": args.res,
            "n_aircraft": len(aircraft),
            "n_faulty_excluded": len(faulty),
            "source": "adsb.lol (ODbL)",
        }
        args.geojson.write_text(json.dumps(to_geojson(cells, meta), separators=(",", ":")))
        print(f"GeoJSON: {args.geojson}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
