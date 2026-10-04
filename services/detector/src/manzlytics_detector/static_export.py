"""Statik veri: harita, API'yi değil bu modülün ürettiği JSON dosyalarını okur (#48).

Yayında API yok (apps/web statik dışa aktarım); bu modül `apps/api`nin verdiği yanıtlarla
aynı şemada JSON dosyaları üretir, `apps/web/public/data/` altına yazılır ve CDN önbelleğe
alınır. Yalnız tam gün (00:00–24:00Z) sorgusunu kapsar — saat aralığı ve gerçek zamanlı
özellikler API çalışırken (geliştirme) kullanılabilir.

Dosya düzeni:
    data/data-range.json                  — DataRange (tüm verinin kapsadığı aralık)
    data/<YYYY-MM-DD>/jamming-r{3,4,5}.json — JammingResponse (o günün tamamı)
    data/<YYYY-MM-DD>/timeline.json         — TimelineResponse
    data/<YYYY-MM-DD>/analytics.json        — günlük FIR/ülke toplamı (r4 hücre-uçak)
    data/<YYYY-MM-DD>/route-hours-r4.json   — hücre × saat serisi (rota kontrolü, saat aralığı; N12)
    data/<YYYY-MM-DD>/events.json           — olay nesneleri (N16; jamming + spoofing ortak şema)
    data/<YYYY-MM-DD>/cells/<h3>.json       — CellDetailResponse (o gün görünen her hücre)
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import h3 as h3lib
from manzlytics_common.clickhouse import ClickHouse
from manzlytics_ingest.adsblol import BBox
from manzlytics_ingest.sources import SOURCES

from manzlytics_detector.jamming import BAD_NACP_BELOW, LOW_ALT_FT
from manzlytics_detector.levels import jamming_level
from manzlytics_detector.places import fir_for_cell, place_for_cell
from manzlytics_detector.store import (
    RESOLUTIONS,
    query_cell,
    query_cell_hours,
    query_jamming,
    query_timeline,
)
from manzlytics_detector.version import ALGORITHM_VERSION

SOURCE = "adsblol"  # statik veri bu kaynağın günlük arşivinden türer
MAX_BAD_AIRCRAFT = 50
MAX_CELL_AIRCRAFT = 100

# Harita bölgesi (güney, batı, kuzey, doğu): Türkiye ve komşu hava sahaları (Balkanlar, Karadeniz,
# Kafkasya, Doğu Akdeniz, Orta Doğu). Dünya özeti ClickHouse'ta tümüyle durur; ama statik dosyalar
# git'e işlenir ve Cloudflare Pages ücretsiz planı 20.000 dosya / 25 MB sınırlıdır, dünya günü
# on binlerce hücre demektir. `--world` ya da `--bbox` ile değiştirilebilir.
MAP_REGION = BBox(south=28.0, west=16.0, north=48.0, east=52.0)

# Ayrıntı dosyası (cells/<h3>.json) yalnız sinyal veren hücrelere yazılır; diğer hücrelerde panel
# jamming-r*.json'daki özeti gösterir. Hücre başına 5 sorgu gerektiği için de sınırlıdır.
DETAIL_LEVELS = ("medium", "high")
MAX_DETAIL_CELLS = 2000


# Cloudflare Pages ücretsiz plan sınırları (git'e işlenen `apps/web/public` için geçerli).
PAGES_MAX_FILES = 20_000
PAGES_MAX_BYTES = 25 * 1024 * 1024


def is_git_served(out: Path) -> bool:
    """`out` sitenin git'e işlenen `public/` klasörü altında mı (Pages sınırları geçerli)."""
    parts = out.resolve().parts
    return any(a == "public" and b == "data" for a, b in zip(parts, parts[1:], strict=False))


def check_world_target(region: BBox | None, out: Path) -> None:
    """Dünya dışa aktarımı git'e işlenen klasöre yazılamaz (dosya sayısı/boyut sınırı)."""
    if region is None and is_git_served(out):
        raise SystemExit(
            "--world, git'e işlenen public/data altına yazılamaz (Pages: 20.000 dosya / 25 MB). "
            "Nesne depolama için --out ile ayrı bir klasör verin (docs/operasyon-notlari.md)."
        )


def write_manifest(day_dir: Path) -> dict:
    """Günün dosya sayısı ve toplam boyutu (`manifest.json`); yükleme/doğrulama için."""
    files = [f for f in day_dir.rglob("*.json") if f.name != "manifest.json"]
    src = SOURCES[SOURCE]
    manifest = {
        "files": len(files),
        "bytes": sum(f.stat().st_size for f in files),
        "source": SOURCE,
        "license": src.license,
        "attribution": src.attribution,
        "algorithm_version": ALGORITHM_VERSION,
    }
    _write(day_dir / "manifest.json", manifest)
    return manifest


def in_region(cell: str, region: BBox | None) -> bool:
    """Hücre merkezi bölge içinde mi (`region=None`: dünya)."""
    if region is None:
        return True
    lat, lon = h3lib.cell_to_latlng(cell)
    return region.contains(lat, lon)


def select_detail_cells(cells, limit: int = MAX_DETAIL_CELLS) -> list[str]:
    """Ayrıntı dosyası yazılacak hücreler: orta/yüksek seviye, en çok etkilenenden başlayarak."""
    ranked = sorted(
        (c for c in cells if jamming_level(c.n_total, c.n_bad) in DETAIL_LEVELS),
        key=lambda c: (-c.n_bad, -c.ratio_bad, c.h3),
    )
    return [c.h3 for c in ranked[:limit]]


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _jamming_payload(start: datetime, end: datetime, resolution: int, cells) -> dict:
    return {
        "start": _iso(start),
        "end": _iso(end),
        "resolution": resolution,
        "cells": [
            {
                "h3": c.h3,
                "n_total": c.n_total,
                "n_bad": c.n_bad,
                "ratio_bad": round(c.ratio_bad, 4),
                "level": jamming_level(c.n_total, c.n_bad),
                "n_total_cruise": c.n_total_cruise,
                "n_bad_cruise": c.n_bad_cruise,
                "level_cruise": jamming_level(c.n_total_cruise, c.n_bad_cruise),
                "bad_aircraft": [
                    {
                        "icao24": a.icao24,
                        "callsign": a.callsign,
                        "min_nic": a.min_nic,
                        "anonymous": a.anonymous,
                    }
                    for a in c.bad_aircraft
                ],
            }
            for c in cells
        ],
    }


def analytics_payload(day: date, cells) -> dict:
    """Günün FIR ve ülke toplamları (/analytics panoları).

    `cells` r4 hücreleridir; sayılar hücre-uçak toplamıdır (iki hücreden geçen uçak iki kez
    sayılır), oran yine de bölgeler arası karşılaştırma için tutarlıdır. Yalnız dışa aktarım
    bölgesindeki hücreler girer, yani toplamlar o bölgeye aittir.
    """
    firs: dict[str, dict] = {}
    countries: dict[str, dict] = {}
    for c in cells:
        targets = []
        if (fir := fir_for_cell(c.h3)) is not None:
            targets.append((firs, fir.icao, {"name": fir.name}))
        place = place_for_cell(c.h3)
        if place is not None and place.kind == "country":
            targets.append((countries, place.name_en, {"name_tr": place.name_tr}))
        for bucket, key, meta in targets:
            row = bucket.setdefault(key, {"id": key, **meta, "aircraft": 0, "affected": 0})
            row["aircraft"] += c.n_total
            row["affected"] += c.n_bad
    return {
        "date": day.isoformat(),
        "firs": sorted(firs.values(), key=lambda r: r["id"]),
        "countries": sorted(countries.values(), key=lambda r: r["id"]),
    }


def route_hours_payload(day: date, series: dict[str, list[tuple[int, int]]]) -> dict:
    """Hücre başına 24 saatlik uçak (`n`) ve etkilenen (`b`) dizisi; rota sayfası seçilen saat
    aralığını istemcide toplar. Sıkıştırma için dizi biçimi; hiç uçak görmeyen hücre yazılmaz."""
    return {
        "date": day.isoformat(),
        "resolution": 4,
        "cells": [
            {"h3": h, "n": [n for n, _ in s], "b": [b for _, b in s]}
            for h, s in sorted(series.items())
            if any(n for n, _ in s)
        ],
    }


def _timeline_payload(start: datetime, end: datetime, hours) -> dict:
    return {
        "start": _iso(start),
        "end": _iso(end),
        "hours": [
            {"hour": _iso(h.hour), "n_aircraft": h.n_aircraft, "n_affected": h.n_affected}
            for h in hours
        ],
    }


def _altitude_payload(detail) -> dict:
    """N3: seyir irtifasında (≥ LOW_ALT_FT) yeniden sayım + düşük NIC'lerin alçak irtifa payı."""
    return {
        "low_alt_ft": LOW_ALT_FT,
        "n_total_cruise": detail.n_total_cruise,
        "n_bad_cruise": detail.n_bad_cruise,
        "level_cruise": jamming_level(detail.n_total_cruise, detail.n_bad_cruise),
        "bad_reports": detail.bad_reports,
        "low_bad_reports": detail.low_bad_reports,
    }


def _nacp_payload(detail) -> dict:
    """NACp ikinci sinyali: NACp < BAD_NACP_BELOW bildiren uçaklar ve NIC ile örtüşme."""
    return {
        "bad_below": BAD_NACP_BELOW,
        "n_total": detail.n_total_nacp,
        "n_bad": detail.n_bad_nacp,
        "n_bad_both": detail.n_bad_both,
    }


def _cell_payload(start: datetime, end: datetime, detail) -> dict:
    place = place_for_cell(detail.h3)
    peak = max(detail.hours, key=lambda h: h.n_affected, default=None)
    return {
        "h3": detail.h3,
        "resolution": detail.resolution,
        "place": asdict(place) if place else None,
        "start": _iso(start),
        "end": _iso(end),
        "n_total": detail.n_total,
        "n_bad": detail.n_bad,
        "ratio_bad": round(detail.n_bad / detail.n_total, 4) if detail.n_total else 0.0,
        "level": jamming_level(detail.n_total, detail.n_bad),
        "peak_hour": _iso(peak.hour) if peak and peak.n_affected else None,
        "hours": [
            {"hour": _iso(h.hour), "n_aircraft": h.n_aircraft, "n_affected": h.n_affected}
            for h in detail.hours
        ],
        "nic_histogram": list(detail.nic_histogram),
        "altitude": _altitude_payload(detail),
        "nacp": _nacp_payload(detail),
        "aircraft": [
            {
                "icao24": a.icao24,
                "callsign": a.callsign,
                "n_reports": a.n_reports,
                "n_bad_reports": a.n_bad_reports,
                "min_nic": a.min_nic,
                "affected": a.affected,
                "anonymous": a.anonymous,
            }
            for a in detail.aircraft
        ],
    }


def export_day(
    ch: ClickHouse,
    day: date,
    out: Path,
    region: BBox | None = MAP_REGION,
    max_detail: int = MAX_DETAIL_CELLS,
    extra: dict[str, dict] | None = None,
) -> Path:
    """Bir günün tam gün jamming/timeline/hücre dosyalarını `out/<gün>/` altına yazar.

    `region`: yalnız merkezi bu bölgede olan hücreler yazılır (None: dünya). Ayrıntı dosyaları
    yalnız orta/yüksek seviyeli hücreler içindir (en çok `max_detail`).
    """
    start = datetime.combine(day, time(), UTC)
    end = start + timedelta(days=1)
    d = out / day.isoformat()

    detail_candidates = []
    for res in RESOLUTIONS:
        cells = [
            c
            for c in query_jamming(ch, start, end, res, max_aircraft=MAX_BAD_AIRCRAFT)
            if in_region(c.h3, region)
        ]
        _write(d / f"jamming-r{res}.json", _jamming_payload(start, end, res, cells))
        detail_candidates.extend(cells)
        if res == 4:
            _write(d / "analytics.json", analytics_payload(day, cells))
            series = {
                h: s for h, s in query_cell_hours(ch, start, end, 4).items() if in_region(h, region)
            }
            _write(d / "route-hours-r4.json", route_hours_payload(day, series))
        print(f"  r{res}: {len(cells)} hücre")

    timeline = query_timeline(ch, start, end)
    _write(d / "timeline.json", _timeline_payload(start, end, timeline))

    detail_h3 = select_detail_cells(detail_candidates, max_detail)
    print(f"  ayrıntı: {len(detail_h3)} hücre (orta/yüksek seviye, üst sınır {max_detail})")
    for cell in detail_h3:
        detail = query_cell(
            ch, cell, h3lib.get_resolution(cell), start, end, max_aircraft=MAX_CELL_AIRCRAFT
        )
        _write(d / "cells" / f"{cell}.json", _cell_payload(start, end, detail))

    for name, payload in (extra or {}).items():
        _write(d / name, payload)

    manifest = write_manifest(d)
    print(f"  manifest: {manifest['files']} dosya, {manifest['bytes'] / 1024:.0f} KB")

    return d


def export_data_range(ch: ClickHouse, out: Path) -> Path:
    """`GET /v1/data-range` ile aynı sorgu; tüm ClickHouse verisinin kapsadığı aralık."""
    rows = ch.query(
        "SELECT min(hour) AS first, max(hour) AS last, count() AS n "
        "FROM jamming_ac_hourly WHERE resolution = 4"
    )
    row = rows[0] if rows else {}
    if not row or int(row.get("n", 0)) == 0:
        payload = {"first_hour": None, "last_hour": None}
    else:
        parse = lambda v: _iso(datetime.fromisoformat(v).replace(tzinfo=UTC))  # noqa: E731
        payload = {"first_hour": parse(row["first"]), "last_hour": parse(row["last"])}
    path = out / "data-range.json"
    _write(path, payload)
    return path


def archive_extras(
    day_path: Path, day: date, region: BBox | None, ch: ClickHouse | None = None
) -> dict[str, dict]:
    """Arşivlenmiş günün parquet'lerinden üretilen ek dosyalar (`spoofing.json`, `events.json`).

    Arşivde ilgili parquet yoksa o dosya üretilmez. `ch` verilirse dünya geneli olaylar (bölge
    süzgeci uygulanmadan) ClickHouse `events` tablosuna da yazılır (N16/E5).
    """
    from manzlytics_detector.daily import HOURLY_FILE
    from manzlytics_detector.events import events_for_day
    from manzlytics_detector.events import events_payload as common_payload
    from manzlytics_detector.spoofing import events_from_parquet, events_payload

    extra: dict[str, dict] = {}
    spoof = None
    if (day_path / "positions.parquet").exists():
        spoof = events_from_parquet(day_path / "positions.parquet")
        extra["spoofing.json"] = events_payload(day.isoformat(), spoof, region)
        print(f"  spoofing: {extra['spoofing.json']['count']} olay")
    if (day_path / HOURLY_FILE).exists():
        events = events_for_day(day_path, spoof)
        extra["events.json"] = common_payload(day.isoformat(), events, region)
        print(f"  olaylar: {extra['events.json']['count']} olay")
        if ch is not None:
            from manzlytics_detector.store import load_events

            print(f"  events tablosu: {load_events(ch, events)} satır")
    return extra
