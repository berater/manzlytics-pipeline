"""Olay nesnesi (N16): hücre×saat sinyalinden ve konum sıçramalarından tek üst şemalı olaylar.

Jamming olayı = zaman içinde bitişik, orta/yüksek seviyeli hücre-saatlerin birleşimi:

- düğüm: (UTC saat, res-5 hücre), seviyesi `levels.jamming_level` ile (hücre-saat içinde etkilenen
  uçak payı; arızalı aviyonik uçaklar `exclude` ile düşülür, `hourly.cells_from_hourly` ile aynı
  tanım);
- bağ: aynı saat ya da ardışık saatte, hücreler aynı ya da komşu (H3 halka 1);
- olay: bağlı bileşen. Saat boşluğu (arada hiç sinyalsiz bir saat) olayı böler.

Spoofing olayı mevcut `spoofing.SpoofEvent`ten aynı şemaya çevrilir (`spoofing_event`).

Sınırlar (bilerek kaba, `algorithm_version` ile işaretli):

- `MIN_EVENT_AIRCRAFT` (3) altındaki olay yayınlanmaz (sürüm 1.1.0).

- Olaylar UTC günü içinde çıkarılır; gece yarısını aşan olay iki ayrı olay olur (kimlik `first_seen`
  içerdiği için çakışmaz).
- `confidence`, olay boyunca gözlenen/etkilenen **tekil** uçaklardan (`quality.data_confidence`)
  gelir (1.1.0; 1.0.0'da en güçlü hücre-saatti); kapsama (saat doluluğu) henüz hesaba girmez.
  Eşikler #24 verisiyle kalibre edilecek.
- Etiket yoktur: olay "birleşik NIC düşüşü bölgesi" demektir, kaynağı (karıştırma/spoofing/arıza)
  söylemez.

Şema: `docs/research/lisans-ve-olay-semasi-plani.md` §2.1.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import h3
import pyarrow as pa
import pyarrow.compute as pc
from manzlytics_ingest.adsblol import BBox

from manzlytics_detector.jamming import BAD_SHARE, FINEST_RESOLUTION
from manzlytics_detector.levels import jamming_level
from manzlytics_detector.places import fir_for, fir_for_cell
from manzlytics_detector.quality import data_confidence
from manzlytics_detector.spoofing import SpoofEvent, _iso
from manzlytics_detector.version import ALGORITHM_VERSION

JAMMING = "jamming"
SPOOFING = "spoofing"
SOURCE = "adsblol"

HOT_LEVELS = ("medium", "high")  # olayı başlatan/sürdüren seviyeler
NEIGHBOR_RINGS = 1  # komşu hücre: H3 halka sayısı
MAX_HOUR_STEP = 1  # bu kadar saat arayla (0 = aynı saat, 1 = ardışık saat) bağlanır
HOUR_S = 3600
# Etkilenen tekil uçak sayısı bunun altındaki olay üretilmez: günde ≈ 570 küçük (≤ 2 uçak) sinyalin
# çoğu çoklu test gürültüsü ve boyut büyüdükçe tekrarlama payı artıyor (plan §3.7).
MIN_EVENT_AIRCRAFT = 3
# Olay ölçeği (1.2.0, `peak_level`in yerine): etkilenen tekil uçak sayısına göre kademe. Seviye
# (`high`) olayların neredeyse tamamında aynıydı ve bilgi taşımıyordu (plan §3.4 A8).
SCALE_MEDIUM_AIRCRAFT = 10
SCALE_LARGE_AIRCRAFT = 30


def event_scale(affected_aircraft: int) -> str:
    """`small` / `medium` / `large`: etkilenen tekil uçak sayısı kademesi."""
    if affected_aircraft >= SCALE_LARGE_AIRCRAFT:
        return "large"
    if affected_aircraft >= SCALE_MEDIUM_AIRCRAFT:
        return "medium"
    return "small"


# Spoofing güven kademesi → ortak kademe. `integrity_overlap`: NIC 0/yok iken sıçrama; jamming'in
# yan etkisi olması muhtemel, bu yüzden "low".
SPOOF_CONFIDENCE = {"high": "high", "low": "low", "integrity_overlap": "low"}

_COLUMNS = ("hour", "h3", "icao24", "n_reports", "n_bad_reports", "anonymous")


@dataclass(slots=True)
class CellHour:
    """Bir (saat, hücre) için gözlem özeti; `bad` etkilenen uçak kimlikleri (yayınlanmaz)."""

    hour: int  # UTC saat başı, epoch saniye
    cell: str  # H3 res 5
    n_total: int = 0
    n_bad: int = 0
    bad: set[str] = field(default_factory=set)
    anonymous: bool = False

    @property
    def level(self) -> str:
        return jamming_level(self.n_total, self.n_bad)


def _epoch_seconds(col: pa.ChunkedArray) -> pa.ChunkedArray:
    """Saat sütunu → epoch saniye. Parquet saniye çözünürlüğünü milisaniyeye çevirdiği için okunan
    tablo `timestamp[ms]` gelir; birim ne olursa olsun önce saniyeye indirilir."""
    if pa.types.is_timestamp(col.type):
        col = pc.cast(col, pa.timestamp("s", tz=col.type.tz), safe=False)
    return pc.cast(col, pa.int64())


def cell_hours_from_table(
    table: pa.Table, exclude: Collection[str] = (), batch_rows: int = 1 << 20
) -> dict[tuple[int, str], CellHour]:
    """Saatlik özet tablosundan (`HOURLY_SCHEMA`) hücre-saat özetleri.

    Dünya tablosu ≈ 8 M satırdır: parça parça ve Arrow ile gruplanır, satırlar Python'a yalnız
    etkilenen uçaklar için inmez (`to_pylist()` tüm tabloya uygulanmaz).
    """
    exclude_set = pa.array(sorted(exclude), pa.string())
    out: dict[tuple[int, str], CellHour] = {}

    def node(hour: int, h3_int: int) -> CellHour:
        key = (hour, h3.int_to_str(h3_int))
        if key not in out:
            out[key] = CellHour(hour, key[1])
        return out[key]

    for batch in table.select(list(_COLUMNS)).to_batches(max_chunksize=batch_rows):
        t = pa.Table.from_batches([batch])
        if exclude:
            t = t.filter(pc.invert(pc.is_in(t["icao24"], value_set=exclude_set)))
        if t.num_rows == 0:
            continue
        share = pc.divide(
            pc.cast(t["n_bad_reports"], pa.float64()), pc.cast(t["n_reports"], pa.float64())
        )
        bad = pc.greater_equal(share, BAD_SHARE)
        t = pa.table(
            {
                "hour": _epoch_seconds(t["hour"]),
                "h3": t["h3"],
                "icao24": t["icao24"],
                "anonymous": t["anonymous"],
                "bad": pc.cast(bad, pa.int64()),
            }
        )
        grouped = t.group_by(["hour", "h3"]).aggregate([("icao24", "count"), ("bad", "sum")])
        for hour, cell, total, n_bad in zip(
            *(grouped[c].to_pylist() for c in ("hour", "h3", "icao24_count", "bad_sum")),
            strict=True,
        ):
            n = node(hour, cell)
            n.n_total += total
            n.n_bad += n_bad
        bad_rows = t.filter(pc.equal(t["bad"], 1))
        for hour, cell, icao, anon in zip(
            *(bad_rows[c].to_pylist() for c in ("hour", "h3", "icao24", "anonymous")), strict=True
        ):
            n = node(hour, cell)
            n.bad.add(icao)
            n.anonymous = n.anonymous or bool(anon)
    return out


def event_id(
    type_: str,
    first_seen: str,
    anchor: str,
    algorithm_version: str = ALGORITHM_VERSION,
    extra: str = "",
) -> str:
    """Deterministik kimlik: aynı veri aynı sürümle yeniden işlenince aynı, sürüm değişince yeni.

    `anchor`: olayın en küçük hücresi. `extra`: aynı saniye ve hücrede ayrı olayları ayıran ek
    anahtar (spoofing'de uçak kimliği; kimlik özetin içinde kalır, yayınlanmaz).
    """
    seed = "|".join([type_, algorithm_version, first_seen, anchor, extra])
    return "ev_" + hashlib.sha1(seed.encode()).hexdigest()[:16]


class _Components:
    def __init__(self) -> None:
        self.parent: dict[tuple[int, str], tuple[int, str]] = {}

    def find(self, x: tuple[int, str]) -> tuple[int, str]:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: tuple[int, str], b: tuple[int, str]) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def group_nodes(nodes: Iterable[CellHour]) -> list[list[CellHour]]:
    """Sıcak hücre-saatleri bağlı bileşenlere ayırır (her grup (saat, hücre) sıralı; gruplar da)."""
    hot = {(n.hour, n.cell): n for n in nodes if n.level in HOT_LEVELS}
    comps = _Components()
    for hour, cell in hot:
        comps.find((hour, cell))
        near = h3.grid_disk(cell, NEIGHBOR_RINGS)
        for step in range(MAX_HOUR_STEP + 1):
            for other in near:
                if (hour + step * HOUR_S, other) in hot and (step, other) != (0, cell):
                    comps.union((hour, cell), (hour + step * HOUR_S, other))
    groups: dict[tuple[int, str], list[CellHour]] = defaultdict(list)
    for key, n in hot.items():
        groups[comps.find(key)].append(n)
    return sorted(
        (sorted(g, key=lambda n: (n.hour, n.cell)) for g in groups.values()),
        key=lambda g: (g[0].hour, g[0].cell),
    )


def events_from_groups(
    groups: Iterable[list[CellHour]],
    observed: Iterable[int] | None = None,
    algorithm_version: str = ALGORITHM_VERSION,
) -> list[dict]:
    """Gruplardan jamming olayları.

    `observed`: her grup için olay boyunca gözlenen tekil uçak sayısı (yoksa en güçlü hücre-saatin
    uçak sayısı, yani alt sınır). Etkilenen tekil uçak sayısı `MIN_EVENT_AIRCRAFT`ın altındaysa
    olay üretilmez (çoklu test gürültüsü, plan §3.7).
    """
    groups = list(groups)
    counts = list(observed) if observed is not None else [None] * len(groups)
    events = [
        _jamming_event(g, algorithm_version, n_obs) for g, n_obs in zip(groups, counts, strict=True)
    ]
    kept = [e for e in events if e["affected_aircraft"] >= MIN_EVENT_AIRCRAFT]
    return sorted(kept, key=lambda e: (e["first_seen"], e["event_id"]))


def build_events(
    nodes: Iterable[CellHour], algorithm_version: str = ALGORITHM_VERSION
) -> list[dict]:
    """Hücre-saat özetlerinden jamming olayları (ortak şema, tarih sırasına göre). Saf fonksiyon.

    Olay düzeyi tekil gözlenen uçak sayısı düğümlerden bilinmez; tablodan hesaplayan
    `jamming_events_from_table` kullanın.
    """
    return events_from_groups(group_nodes(nodes), None, algorithm_version)


def _hour_iso(hour: int) -> str:
    return _iso(datetime.fromtimestamp(hour, UTC))


def _jamming_event(group: list[CellHour], algorithm_version: str, n_observed: int | None) -> dict:
    first, last = group[0].hour, max(n.hour for n in group)
    cells = sorted({n.cell for n in group})
    hot_hours = Counter(n.cell for n in group)
    region = min(cells, key=lambda c: (-hot_hours[c], c))
    peak = max(group, key=lambda n: (n.n_bad, n.n_bad / n.n_total, -n.hour, n.cell))
    firs = sorted({f.icao for c in cells if (f := fir_for_cell(c))})
    region_fir = fir_for_cell(region)
    affected = set().union(*(n.bad for n in group))
    observed = max(n_observed or 0, peak.n_total, len(affected))
    first_seen = _hour_iso(first)
    return {
        "event_id": event_id(JAMMING, first_seen, cells[0], algorithm_version),
        "type": JAMMING,
        "first_seen": first_seen,
        "last_seen": _hour_iso(last),
        "duration_hours": (last - first) // HOUR_S + 1,
        "cells": cells,
        "region_h3": region,
        "fir": region_fir.icao if region_fir else None,
        "firs": firs,
        "affected_aircraft": len(affected),
        "scale": event_scale(len(affected)),
        "confidence": data_confidence(observed, len(affected)),
        "data_coverage": {
            "aircraft_observed": observed,
            "aircraft_affected": len(affected),
            "peak_hour": _hour_iso(peak.hour),
            "peak_cell": peak.cell,
            "peak_aircraft_observed": peak.n_total,
            "peak_aircraft_affected": peak.n_bad,
            "cell_hours": len(group),
        },
        "evidence": None,
        "source": SOURCE,
        "algorithm_version": algorithm_version,
        "anonymous": any(n.anonymous for n in group),
    }


def _observed_aircraft(
    table: pa.Table, exclude: Collection[str], groups: list[list[CellHour]]
) -> list[int]:
    """Her olay için, olayın sıcak hücre-saatlerinde gözlenen tekil uçak sayısı (ikinci geçiş)."""
    keys = {(n.hour, n.cell): i for i, g in enumerate(groups) for n in g}
    if not keys:
        return []
    wanted = pa.table(
        {
            "hour": [k[0] for k in keys],
            "h3": pa.array([h3.str_to_int(k[1]) for k in keys], pa.uint64()),
            "event": list(keys.values()),
        }
    )
    rows = pa.table(
        {"hour": _epoch_seconds(table["hour"]), "h3": table["h3"], "icao24": table["icao24"]}
    )
    if exclude:
        drop = pa.array(sorted(exclude), pa.string())
        rows = rows.filter(pc.invert(pc.is_in(rows["icao24"], value_set=drop)))
    joined = rows.join(wanted, keys=["hour", "h3"], join_type="inner")
    grouped = joined.group_by("event").aggregate([("icao24", "count_distinct")])
    counts = dict(
        zip(grouped["event"].to_pylist(), grouped["icao24_count_distinct"].to_pylist(), strict=True)
    )
    return [counts.get(i, 0) for i in range(len(groups))]


def jamming_events_from_table(
    table: pa.Table, exclude: Collection[str] = (), algorithm_version: str = ALGORITHM_VERSION
) -> list[dict]:
    groups = group_nodes(cell_hours_from_table(table, exclude).values())
    return events_from_groups(groups, _observed_aircraft(table, exclude, groups), algorithm_version)


def spoofing_event(e: SpoofEvent, algorithm_version: str = ALGORITHM_VERSION) -> dict:
    """`SpoofEvent` → ortak şema. Gizlilik programındaki uçağın kimliği yazılmaz."""
    points = [p for j in e.jumps for p in (j.before, j.after)]
    cells = sorted({h3.latlng_to_cell(p.lat, p.lon, FINEST_RESOLUTION) for p in points})
    origin = e.jumps[0].before
    region = h3.latlng_to_cell(origin.lat, origin.lon, FINEST_RESOLUTION)
    fir = fir_for(round(origin.lat, 4), round(origin.lon, 4))
    anon = any(p.anonymous for p in points)
    first_seen = _iso(e.start)
    return {
        "event_id": event_id(SPOOFING, first_seen, cells[0], algorithm_version, e.icao24),
        "type": SPOOFING,
        "first_seen": first_seen,
        "last_seen": _iso(e.end),
        "duration_hours": (int(e.end.timestamp()) // HOUR_S)
        - (int(e.start.timestamp()) // HOUR_S)
        + 1,
        "cells": cells,
        "region_h3": region,
        "fir": fir.icao if fir else None,
        "firs": sorted({f.icao for c in cells if (f := fir_for_cell(c))}),
        "affected_aircraft": 1,
        "scale": None,
        "confidence": SPOOF_CONFIDENCE[e.confidence],
        "data_coverage": None,
        "evidence": {
            "spoofing_confidence": e.confidence,
            "icao24": None if anon else e.icao24,
            "jumps": len(e.jumps),
            "max_dist_nm": round(max(j.dist_nm for j in e.jumps), 1),
            "max_speed_kt": round(max(j.speed_kt for j in e.jumps)),
        },
        "source": SOURCE,
        "algorithm_version": algorithm_version,
        "anonymous": anon,
    }


def in_region(event: dict, region: BBox | None) -> bool:
    """Olayın ana hücresi (`region_h3`) bölge içinde mi (`region=None`: dünya)."""
    if region is None:
        return True
    return region.contains(*h3.cell_to_latlng(event["region_h3"]))


def events_payload(
    day: str,
    events: Iterable[dict],
    region: BBox | None = None,
    algorithm_version: str = ALGORITHM_VERSION,
) -> dict:
    """Günün `events.json` içeriği: bölge filtresi + özet sayılar + olaylar (zamana göre)."""
    chosen = sorted(
        (e for e in events if in_region(e, region)), key=lambda e: (e["first_seen"], e["event_id"])
    )
    return {
        "date": day,
        "algorithm_version": algorithm_version,
        "count": len(chosen),
        "by_type": dict(Counter(e["type"] for e in chosen)),
        "by_confidence": dict(Counter(e["confidence"] for e in chosen)),
        "events": chosen,
    }


def events_for_day(day_dir: Path, spoof: list[SpoofEvent] | None = None) -> list[dict]:
    """Arşivlenmiş günün olayları: dünya saatlik özetinden jamming + (varsa) konum sıçraması.

    Arızalı aviyonik uçaklar (`avionics.json`) jamming olaylarından düşülür; spoofing yalnız odak
    bölge konumlarından (`positions.parquet`) çıkar; `spoof` verilirse yeniden hesaplanmaz.
    """
    import pyarrow.parquet as pq

    from manzlytics_detector.daily import AVIONICS_FILE, HOURLY_FILE, faulty_from_file
    from manzlytics_detector.spoofing import events_from_parquet

    avionics = day_dir / AVIONICS_FILE
    exclude = faulty_from_file(avionics) if avionics.exists() else frozenset()
    events = jamming_events_from_table(
        pq.read_table(day_dir / HOURLY_FILE, columns=list(_COLUMNS)), exclude
    )
    positions = day_dir / "positions.parquet"
    if spoof is None and positions.exists():
        spoof = events_from_parquet(positions)
    events += [spoofing_event(e) for e in spoof or ()]
    return events
