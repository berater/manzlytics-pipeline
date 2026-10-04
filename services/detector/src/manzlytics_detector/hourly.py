"""Saatlik hücre × uçak özeti (ClickHouse `jamming_ac_hourly` tablosunun dosya karşılığı).

Yalnız en ince çözünürlükte (res 5) saklanır; kaba hücreler ebeveyn hücre üzerinden toplanarak
türetilir (store.py'deki `h3ToParent` ile aynı). Filtre ve "bad" tanımı jamming.py ile aynıdır;
`cells_from_hourly(hourly_rows(r), res)` == `aggregate_jamming(r, res)` testle doğrulanır.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Iterable
from datetime import UTC, datetime

import h3
import pyarrow as pa
import pyarrow.compute as pc
from manzlytics_common.clickhouse import ClickHouse
from manzlytics_ingest.models import PositionReport

from manzlytics_detector.jamming import (
    BAD_NACP_BELOW,
    BAD_NIC_BELOW,
    BAD_SHARE,
    FINEST_RESOLUTION,
    MAX_NIC,
    NIC_BINS,
    AircraftInCell,
    JammingCellStats,
    is_adsb,
    is_low_altitude,
)

HOURLY_SCHEMA = pa.schema(
    [
        ("hour", pa.timestamp("s", tz="UTC")),
        ("h3", pa.uint64()),  # res 5
        ("icao24", pa.string()),
        ("callsign", pa.string()),
        ("n_reports", pa.uint32()),
        ("n_bad_reports", pa.uint32()),
        ("min_nic", pa.uint8()),
        ("anonymous", pa.bool_()),
        ("n_low_reports", pa.uint32()),  # LOW_ALT_FT altındaki raporlar (n_reports'un alt kümesi)
        ("n_low_bad_reports", pa.uint32()),  # ...bunlardan NIC < 5 olanlar
        ("n_nacp_reports", pa.uint32()),  # NACp bildiren raporlar (n_reports'un alt kümesi)
        ("n_low_nacp_reports", pa.uint32()),  # ...bunlardan NACp < 8 olanlar
        # Rapor düzeyinde NIC dağılımı: indeks = NIC (0..11), değer = rapor sayısı. Hücre panelinin
        # NIC grafiği bundan gelir (ham `positions` tablosuna bağımlılık yok).
        ("nic_hist", pa.list_(pa.uint32())),
    ]
)
HOURLY_SORT = [("hour", "ascending"), ("h3", "ascending"), ("icao24", "ascending")]


def hourly_rows(reports: Iterable[PositionReport]) -> dict[str, list]:
    """Raporları (saat, res-5 hücre, uçak) bazında özetler; sütun sözlüğü döner."""
    # (saat, hücre, uçak) -> [bad, toplam, min NIC, son callsign, gizli mi]
    acc: dict[tuple[int, int, str], list] = {}
    seen: set[tuple[str, datetime]] = set()
    for r in reports:
        if r.on_ground or r.nic is None or not is_adsb(r) or (r.icao24, r.ts) in seen:
            continue
        seen.add((r.icao24, r.ts))
        hour = int(r.ts.timestamp()) // 3600 * 3600
        cell = h3.str_to_int(h3.latlng_to_cell(r.lat, r.lon, FINEST_RESOLUTION))
        a = acc.setdefault(
            (hour, cell, r.icao24), [0, 0, r.nic, r.callsign, False, 0, 0, 0, 0, [0] * NIC_BINS]
        )
        a[0] += r.nic < BAD_NIC_BELOW
        a[1] += 1
        a[2] = min(a[2], r.nic)
        a[3] = r.callsign or a[3]
        a[4] = a[4] or r.anonymous
        if is_low_altitude(r):
            a[5] += 1
            a[6] += r.nic < BAD_NIC_BELOW
        a[9][min(r.nic, MAX_NIC)] += 1
        if r.nac_p is not None:
            a[7] += 1
            a[8] += r.nac_p < BAD_NACP_BELOW
    cols: dict[str, list] = {c: [] for c in HOURLY_SCHEMA.names}
    for (hour, cell, icao), (
        bad,
        total,
        min_nic,
        cs,
        anon,
        low,
        low_bad,
        nacp,
        low_nacp,
        nic_hist,
    ) in acc.items():
        cols["hour"].append(hour)
        cols["h3"].append(cell)
        cols["icao24"].append(icao)
        cols["callsign"].append(cs)
        cols["n_reports"].append(total)
        cols["n_bad_reports"].append(bad)
        cols["min_nic"].append(min_nic)
        cols["anonymous"].append(anon)
        cols["n_low_reports"].append(low)
        cols["n_low_bad_reports"].append(low_bad)
        cols["n_nacp_reports"].append(nacp)
        cols["n_low_nacp_reports"].append(low_nacp)
        cols["nic_hist"].append(nic_hist)
    return cols


def cells_from_hourly(
    rows: Iterable[dict], resolution: int, exclude: Collection[str] = ()
) -> dict[str, JammingCellStats]:
    """Saatlik satırlardan dönem boyunca hücre istatistikleri (store.QUERY_SQL ile aynı mantık).

    `exclude`: hesaba katılmayacak uçaklar (avionics.faulty_aircraft).
    """
    # hücre -> uçak -> [bad, toplam, min NIC, callsign, gizli mi, son saat]
    acc: dict[str, dict[str, list]] = defaultdict(dict)
    for row in sorted(rows, key=lambda r: r["hour"]):
        if row["icao24"] in exclude:
            continue
        fine = h3.int_to_str(row["h3"])
        cell = fine if resolution >= FINEST_RESOLUTION else h3.cell_to_parent(fine, resolution)
        a = acc[cell].setdefault(row["icao24"], [0, 0, row["min_nic"], None, False])
        a[0] += row["n_bad_reports"]
        a[1] += row["n_reports"]
        a[2] = min(a[2], row["min_nic"])
        a[3] = row["callsign"] or a[3]
        a[4] = a[4] or row["anonymous"]
    result: dict[str, JammingCellStats] = {}
    for cell, per_ac in acc.items():
        bad = tuple(
            AircraftInCell(icao, cs, total, n_bad, min_nic, anon)
            for icao, (n_bad, total, min_nic, cs, anon) in sorted(per_ac.items())
            if n_bad / total >= BAD_SHARE
        )
        result[cell] = JammingCellStats(
            h3=cell, n_total=len(per_ac), n_bad=len(bad), bad_aircraft=bad
        )
    return result


def load_into_clickhouse(
    ch: ClickHouse,
    hourly_table: pa.Table,
    resolutions: Collection[int] = (3, 4, 5),
    batch_size: int = 20_000,
) -> int:
    """`jamming_ac_hourly_r5.parquet` (arşivlenmiş bir günün dünya özeti, res 5) satırlarını
    ClickHouse `jamming_ac_hourly` tablosuna yükler (#24: geriye dönük doldurma).

    Canlı yoldaki `store.AGGREGATE_SQL` ile aynı mantık: her hedef çözünürlük için `h3ToParent`
    ile kabalaştırılır ve (saat, hücre, uçak) bazında toplanır — `n_reports`/`n_bad_reports`
    toplanır, `min_nic` en küçüğü, `anonymous` herhangi biri gizliyse gizlidir, `callsign` boş
    olmayan sonuncusudur.

    İdempotenttir: tablo `ReplacingMergeTree(computed_at)`, sorgular `FINAL` okur, bu yüzden aynı
    günü tekrar yüklemek sorgu sonucunu değiştirmez (yeni satırlar öncekilerin eşdeğer verili
    yerine geçer; store.aggregate() ile aynı yaklaşım).
    """
    # Dünya günü ≈ 8 milyon satır: hepsini Python nesnesine çevirmek runner belleğini (7 GB) aşar.
    # Anahtar saati içerdiği için saatler birbirinden bağımsızdır; saat saat işlenir (≈ 1/24).
    hours = sorted(pc.unique(hourly_table["hour"]).to_pylist())
    n_inserted = 0
    for hour in hours:
        rows = hourly_table.filter(pc.equal(hourly_table["hour"], pa.scalar(hour))).to_pylist()
        n_inserted += _load_hour(ch, rows, resolutions, batch_size)
    return n_inserted


def _load_hour(
    ch: ClickHouse, rows: list[dict], resolutions: Collection[int], batch_size: int
) -> int:
    """Tek bir saatin res-5 satırlarını her hedef çözünürlük için kabalaştırıp yükler."""
    n_inserted = 0
    for res in resolutions:
        # (saat [unix sn], hücre, uçak) -> [n_reports, n_bad_reports, min_nic, callsign, anon,
        #                               n_low_reports, n_low_bad_reports,
        #                               n_nacp_reports, n_low_nacp_reports, nic_hist]
        acc: dict[tuple[int, int, str], list] = {}
        for r in rows:
            hour_ts = int(r["hour"].timestamp())
            fine = r["h3"]
            cell = (
                fine
                if res >= FINEST_RESOLUTION
                else h3.str_to_int(h3.cell_to_parent(h3.int_to_str(fine), res))
            )
            key = (hour_ts, cell, r["icao24"])
            a = acc.setdefault(key, [0, 0, r["min_nic"], "", False, 0, 0, 0, 0, [0] * NIC_BINS])
            a[0] += r["n_reports"]
            a[1] += r["n_bad_reports"]
            a[2] = min(a[2], r["min_nic"])
            a[3] = r["callsign"] or a[3]
            a[4] = a[4] or r["anonymous"]
            # Eski arşivlerde irtifa sütunları yok: 0 = "alçak irtifa bilinmiyor".
            a[5] += r.get("n_low_reports") or 0
            a[6] += r.get("n_low_bad_reports") or 0
            a[7] += r.get("n_nacp_reports") or 0
            a[8] += r.get("n_low_nacp_reports") or 0
            # Eski arşivlerde dağılım yok: sıfır kalır ("bilinmiyor").
            for k, n in enumerate(r.get("nic_hist") or ()):
                a[9][k] += n

        out_rows = [
            {
                "hour": datetime.fromtimestamp(hour_ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S"),
                "resolution": res,
                "h3": cell,
                "icao24": icao,
                "callsign": callsign,
                "n_reports": n_reports,
                "n_bad_reports": n_bad_reports,
                "min_nic": min_nic,
                "anonymous": anonymous,
                "n_low_reports": n_low,
                "n_low_bad_reports": n_low_bad,
                "n_nacp_reports": n_nacp,
                "n_low_nacp_reports": n_low_nacp,
                "nic_hist": nic_hist,
            }
            for (hour_ts, cell, icao), (
                n_reports,
                n_bad_reports,
                min_nic,
                callsign,
                anonymous,
                n_low,
                n_low_bad,
                n_nacp,
                n_low_nacp,
                nic_hist,
            ) in acc.items()
        ]
        for i in range(0, len(out_rows), batch_size):
            n_inserted += ch.insert("jamming_ac_hourly", out_rows[i : i + batch_size])
    return n_inserted
