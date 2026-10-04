"""ClickHouse üzerinde jamming agregasyonu ve sorgusu.

`jamming.py` ile aynı metodoloji; çevrimdışı analiz Python'da, servis ClickHouse'ta çalışır.
tests/test_store_integration.py ikisinin aynı sonucu verdiğini gerçek ClickHouse'ta doğrular.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from manzlytics_common.clickhouse import ClickHouse

from manzlytics_detector.jamming import (
    BAD_NACP_BELOW,
    BAD_NIC_BELOW,
    BAD_SHARE,
    LOW_ALT_FT,
    MAX_NIC,
    NIC_BINS,
)

RESOLUTIONS = (3, 4, 5)

# Agregasyona giren raporlar: havada, NIC'li, yalnız ADS-B kaynaklı (jamming.py ile aynı).
REPORT_FILTER = """NOT on_ground
  AND nic IS NOT NULL
  AND (endsWith(source, ':adsb_icao') OR endsWith(source, ':adsb_icao_nt') OR source = 'unknown')"""

# BAD_SHARE = 0.5 → "bad_reports / reports >= 0.5" tamsayı olarak "2 * b >= r".
assert BAD_SHARE == 0.5

AGGREGATE_SQL = f"""
INSERT INTO jamming_ac_hourly
    (hour, resolution, h3, icao24, callsign, n_reports, n_bad_reports, min_nic, anonymous,
     n_low_reports, n_low_bad_reports, n_nacp_reports, n_low_nacp_reports, nic_hist)
SELECT
    toStartOfHour(ts) AS hour,
    {{res:UInt8}} AS resolution,
    h3ToParent(h3_r5, {{res:UInt8}}) AS h3c,
    icao24,
    ifNull(anyLast(callsign), '') AS callsign,
    count() AS n_reports,
    countIf(nic < {BAD_NIC_BELOW}) AS n_bad_reports,
    min(assumeNotNull(nic)) AS min_nic,
    max(anonymous) AS anonymous,
    countIf(alt_baro_ft IS NOT NULL AND alt_baro_ft < {LOW_ALT_FT}) AS n_low_reports,
    countIf(alt_baro_ft IS NOT NULL AND alt_baro_ft < {LOW_ALT_FT} AND nic < {BAD_NIC_BELOW})
        AS n_low_bad_reports,
    countIf(nac_p IS NOT NULL) AS n_nacp_reports,
    countIf(nac_p IS NOT NULL AND nac_p < {BAD_NACP_BELOW}) AS n_low_nacp_reports,
    sumForEach(arrayMap(k -> toUInt32(least(assumeNotNull(nic), {MAX_NIC}) = k), range({NIC_BINS})))
        AS nic_hist
FROM positions FINAL
WHERE ts >= {{start:DateTime64(3, 'UTC')}} AND ts < {{end:DateTime64(3, 'UTC')}}
  AND {REPORT_FILTER}
GROUP BY hour, h3c, icao24
"""

QUERY_SQL = """
SELECT
    h3ToString(h3) AS h3,
    count() AS n_total,
    countIf(2 * b >= r) AS n_bad,
    -- N3: yalnız seyir irtifasındaki (alçak irtifa dışı) raporlarla yeniden sayım
    countIf(r > lr) AS n_total_cruise,
    countIf(r > lr AND 2 * (b - lb) >= r - lr) AS n_bad_cruise,
    arraySlice(
        arraySort(
            x -> tupleElement(x, 3),
            -- gizlilik programındaki uçakların kimliği sorgudan hiç çıkmaz
            groupArrayIf((if(anon, '', icao24), if(anon, '', cs), mn, anon), 2 * b >= r)
        ),
        1, {max_aircraft:UInt32}
    ) AS bad
FROM
(
    SELECT h3, icao24, sum(n_bad_reports) AS b, sum(n_reports) AS r,
           sum(n_low_bad_reports) AS lb, sum(n_low_reports) AS lr,
           anyLast(callsign) AS cs, min(min_nic) AS mn, max(anonymous) AS anon
    FROM jamming_ac_hourly FINAL
    WHERE resolution = {res:UInt8}
      AND hour >= {start:DateTime('UTC')} AND hour < {end:DateTime('UTC')}
    GROUP BY h3, icao24
)
GROUP BY h3
ORDER BY h3
"""

# Saatlik zaman çizelgesi: uçak, o saatteki tüm hücrelerdeki raporlarıyla değerlendirilir.
# Çözünürlükten bağımsızdır (her çözünürlük aynı raporları içerir); res 4 satırları okunur.
TIMELINE_SQL = """
SELECT hour, count() AS n_aircraft, countIf(2 * b >= r) AS n_affected
FROM
(
    SELECT hour, icao24, sum(n_bad_reports) AS b, sum(n_reports) AS r
    FROM jamming_ac_hourly FINAL
    WHERE resolution = 4
      AND hour >= {start:DateTime('UTC')} AND hour < {end:DateTime('UTC')}
    GROUP BY hour, icao24
)
GROUP BY hour
ORDER BY hour
"""


def _fmt(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def floor_hour(t: datetime) -> datetime:
    return t.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def aggregate(
    ch: ClickHouse, start: datetime, end: datetime, resolutions: tuple[int, ...] = RESOLUTIONS
) -> None:
    """[start, end) aralığındaki tam saatleri (yeniden) hesaplar. İdempotent."""
    start, end = floor_hour(start), floor_hour(end - timedelta(microseconds=1)) + timedelta(hours=1)
    for res in resolutions:
        ch.command(AGGREGATE_SQL, {"res": res, "start": _fmt(start), "end": _fmt(end)})


@dataclass(frozen=True, slots=True)
class BadAircraft:
    icao24: str | None
    callsign: str | None
    min_nic: int
    anonymous: bool = False


@dataclass(frozen=True, slots=True)
class CellResult:
    h3: str
    n_total: int
    n_bad: int
    bad_aircraft: tuple[BadAircraft, ...]
    # Yalnız seyir irtifasıyla sayım (N3); alçak irtifa sütunsuz eski saatlerde == n_total/n_bad.
    n_total_cruise: int = 0
    n_bad_cruise: int = 0

    @property
    def ratio_bad(self) -> float:
        return self.n_bad / self.n_total if self.n_total else 0.0


def query_jamming(
    ch: ClickHouse, start: datetime, end: datetime, resolution: int, max_aircraft: int = 50
) -> list[CellResult]:
    rows = ch.query(
        QUERY_SQL,
        {"res": resolution, "start": _fmt(start), "end": _fmt(end), "max_aircraft": max_aircraft},
    )
    return [
        CellResult(
            h3=row["h3"],
            n_total=int(row["n_total"]),
            n_bad=int(row["n_bad"]),
            n_total_cruise=int(row["n_total_cruise"]),
            n_bad_cruise=int(row["n_bad_cruise"]),
            bad_aircraft=tuple(
                BadAircraft(
                    None if anon else icao.strip("\x00"),
                    None if anon else (cs or None),
                    int(mn),
                    bool(anon),
                )
                for icao, cs, mn, anon in row["bad"]
            ),
        )
        for row in rows
    ]


@dataclass(frozen=True, slots=True)
class TimelineHour:
    hour: datetime
    n_aircraft: int
    n_affected: int


def query_timeline(ch: ClickHouse, start: datetime, end: datetime) -> list[TimelineHour]:
    """[start, end) içindeki her saat; verisi olmayan saatler 0 ile doldurulur."""
    rows = ch.query(TIMELINE_SQL, {"start": _fmt(start), "end": _fmt(end)})
    by_hour = {
        datetime.fromisoformat(r["hour"]).replace(tzinfo=UTC): (
            int(r["n_aircraft"]),
            int(r["n_affected"]),
        )
        for r in rows
    }
    out: list[TimelineHour] = []
    h = floor_hour(start)
    while h < end:
        n, a = by_hour.get(h, (0, 0))
        out.append(TimelineHour(hour=h, n_aircraft=n, n_affected=a))
        h += timedelta(hours=1)
    return out


# Rota kontrolü (N12): her hücrenin saatlik serisi, tek sorguda. Uçak o saatte bu hücredeki
# raporlarıyla değerlendirilir (CELL_HOURS_SQL ile aynı kural).
ROUTE_HOURS_SQL = """
SELECT h3ToString(h3) AS h3, hour, count() AS n_aircraft, countIf(2 * b >= r) AS n_affected
FROM
(
    SELECT h3, hour, icao24, sum(n_bad_reports) AS b, sum(n_reports) AS r
    FROM jamming_ac_hourly FINAL
    WHERE resolution = {res:UInt8}
      AND hour >= {start:DateTime('UTC')} AND hour < {end:DateTime('UTC')}
    GROUP BY h3, hour, icao24
)
GROUP BY h3, hour
ORDER BY h3, hour
"""


def query_cell_hours(
    ch: ClickHouse, start: datetime, end: datetime, resolution: int
) -> dict[str, list[tuple[int, int]]]:
    """Hücre → [start, end) içindeki her saat için (uçak, etkilenen); boş saatler (0, 0)."""
    n_hours = int((end - floor_hour(start)).total_seconds() // 3600)
    base = floor_hour(start)
    out: dict[str, list[tuple[int, int]]] = {}
    for row in ch.query(
        ROUTE_HOURS_SQL, {"res": resolution, "start": _fmt(start), "end": _fmt(end)}
    ):
        idx = int(
            (datetime.fromisoformat(row["hour"]).replace(tzinfo=UTC) - base).total_seconds() // 3600
        )
        if 0 <= idx < n_hours:
            series = out.setdefault(row["h3"], [(0, 0)] * n_hours)
            series[idx] = (int(row["n_aircraft"]), int(row["n_affected"]))
    return out


# --- Tek hücrenin ayrıntısı ("neden kırmızı?") ---

# Uçak bazında periyot özeti; etkilenenler önce, sonra en düşük NIC'e göre.
CELL_AIRCRAFT_SQL = """
SELECT if(anon, '', icao24) AS icao24, if(anon, '', cs) AS callsign,
       r AS n_reports, b AS n_bad_reports, mn AS min_nic, anon AS anonymous,
       2 * b >= r AS affected, count() OVER () AS n_total,
       countIf(2 * b >= r) OVER () AS n_bad
FROM
(
    SELECT icao24, sum(n_reports) AS r, sum(n_bad_reports) AS b, anyLast(callsign) AS cs,
           min(min_nic) AS mn, max(anonymous) AS anon
    FROM jamming_ac_hourly FINAL
    WHERE resolution = {res:UInt8} AND h3 = stringToH3({h3:String})
      AND hour >= {start:DateTime('UTC')} AND hour < {end:DateTime('UTC')}
    GROUP BY icao24
)
ORDER BY affected DESC, min_nic ASC, n_reports DESC
LIMIT {max_aircraft:UInt32}
"""

# Hücrenin saatlik serisi: uçak o saatte bu hücredeki raporlarıyla değerlendirilir.
CELL_HOURS_SQL = """
SELECT hour, count() AS n_aircraft, countIf(2 * b >= r) AS n_affected
FROM
(
    SELECT hour, icao24, sum(n_bad_reports) AS b, sum(n_reports) AS r
    FROM jamming_ac_hourly FINAL
    WHERE resolution = {res:UInt8} AND h3 = stringToH3({h3:String})
      AND hour >= {start:DateTime('UTC')} AND hour < {end:DateTime('UTC')}
    GROUP BY hour, icao24
)
GROUP BY hour
ORDER BY hour
"""

# Rapor düzeyinde NIC dağılımı, saatlik özetten (tüm arşivlenmiş günler için geçerli yol).
CELL_NIC_HIST_SQL = """
SELECT sumForEach(nic_hist) AS hist
FROM jamming_ac_hourly FINAL
WHERE resolution = {res:UInt8} AND h3 = stringToH3({h3:String})
  AND hour >= {start:DateTime('UTC')} AND hour < {end:DateTime('UTC')}
"""

# Yedek yol: dağılım sütunu olmayan eski saatler için ham tablodan (canlı hat; arşivlenmiş
# günlerde `positions` boştur). Bölüm = gün, en fazla 7 bölüm okunur.
CELL_NIC_SQL = f"""
SELECT assumeNotNull(nic) AS nic, count() AS n
FROM positions FINAL
WHERE ts >= {{start:DateTime64(3, 'UTC')}} AND ts < {{end:DateTime64(3, 'UTC')}}
  AND h3ToParent(h3_r5, {{res:UInt8}}) = stringToH3({{h3:String}})
  AND {REPORT_FILTER}
GROUP BY nic
ORDER BY nic
"""

# Alçak irtifa kırılımı: uçaklar yalnız seyir irtifasındaki (LOW_ALT_FT ve üstü) raporlarıyla
# yeniden değerlendirilir; hiç seyir raporu olmayan uçak sayılmaz. Düşük NIC raporlarının kaçı
# alçak irtifada, ayrıca döner.
CELL_ALTITUDE_SQL = """
SELECT countIf(r > lr) AS n_total_cruise,
       countIf(r > lr AND 2 * (b - lb) >= r - lr) AS n_bad_cruise,
       sum(b) AS bad_reports, sum(lb) AS low_bad_reports
FROM
(
    SELECT icao24, sum(n_reports) AS r, sum(n_bad_reports) AS b,
           sum(n_low_reports) AS lr, sum(n_low_bad_reports) AS lb
    FROM jamming_ac_hourly FINAL
    WHERE resolution = {res:UInt8} AND h3 = stringToH3({h3:String})
      AND hour >= {start:DateTime('UTC')} AND hour < {end:DateTime('UTC')}
    GROUP BY icao24
)
"""

# NACp sinyali: NACp bildiren uçaklar arasında NACp < 8 olanlar (raporlarının en az yarısı) ve
# bunlardan NIC ile de etkilenenler. NIC belirleyici kalır; NACp doğrulama içindir. NACp sütunsuz
# eski saatlerde n_total_nacp == 0.
CELL_NACP_SQL = """
SELECT countIf(rp > 0) AS n_total_nacp,
       countIf(rp > 0 AND 2 * lp >= rp) AS n_bad_nacp,
       countIf(rp > 0 AND 2 * lp >= rp AND 2 * b >= r) AS n_bad_both
FROM
(
    SELECT icao24, sum(n_reports) AS r, sum(n_bad_reports) AS b,
           sum(n_nacp_reports) AS rp, sum(n_low_nacp_reports) AS lp
    FROM jamming_ac_hourly FINAL
    WHERE resolution = {res:UInt8} AND h3 = stringToH3({h3:String})
      AND hour >= {start:DateTime('UTC')} AND hour < {end:DateTime('UTC')}
    GROUP BY icao24
)
"""


@dataclass(frozen=True, slots=True)
class CellAircraft:
    icao24: str | None
    callsign: str | None
    n_reports: int
    n_bad_reports: int
    min_nic: int
    affected: bool
    anonymous: bool = False


@dataclass(frozen=True, slots=True)
class CellDetail:
    h3: str
    resolution: int
    n_total: int
    n_bad: int
    hours: tuple[TimelineHour, ...]
    nic_histogram: tuple[int, ...]  # indeks = NIC (0..11), değer = rapor sayısı
    aircraft: tuple[CellAircraft, ...]
    # Alçak irtifa kırılımı (N3): seyir irtifasındaki raporlarla yeniden sayım ve düşük NIC
    # raporlarının alçak irtifadaki payı. Alçak irtifa sütunları olmayan eski saatlerde
    # cruise == tümü, low_bad_reports == 0.
    n_total_cruise: int = 0
    n_bad_cruise: int = 0
    bad_reports: int = 0
    low_bad_reports: int = 0
    # NACp sinyali: NACp bildiren uçak sayısı, bunlardan NACp < 8 olanlar, NIC ile de etkilenenler
    n_total_nacp: int = 0
    n_bad_nacp: int = 0
    n_bad_both: int = 0


def query_cell(
    ch: ClickHouse,
    cell: str,
    resolution: int,
    start: datetime,
    end: datetime,
    max_aircraft: int = 100,
) -> CellDetail:
    params = {"res": resolution, "h3": cell, "start": _fmt(start), "end": _fmt(end)}
    ac_rows = ch.query(CELL_AIRCRAFT_SQL, {**params, "max_aircraft": max_aircraft})
    by_hour = {
        datetime.fromisoformat(r["hour"]).replace(tzinfo=UTC): (
            int(r["n_aircraft"]),
            int(r["n_affected"]),
        )
        for r in ch.query(CELL_HOURS_SQL, params)
    }
    hours: list[TimelineHour] = []
    h = floor_hour(start)
    while h < end:
        n, a = by_hour.get(h, (0, 0))
        hours.append(TimelineHour(hour=h, n_aircraft=n, n_affected=a))
        h += timedelta(hours=1)
    hist = _nic_histogram(ch, params)
    aircraft = tuple(
        CellAircraft(
            icao24=None if _truthy(r["anonymous"]) else r["icao24"].strip("\x00"),
            callsign=None if _truthy(r["anonymous"]) else (r["callsign"] or None),
            n_reports=int(r["n_reports"]),
            n_bad_reports=int(r["n_bad_reports"]),
            min_nic=int(r["min_nic"]),
            affected=_truthy(r["affected"]),
            anonymous=_truthy(r["anonymous"]),
        )
        for r in ac_rows
    )
    first = ac_rows[0] if ac_rows else {}
    alt = (ch.query(CELL_ALTITUDE_SQL, params) or [{}])[0]
    nacp = (ch.query(CELL_NACP_SQL, params) or [{}])[0]
    return CellDetail(
        h3=cell,
        resolution=resolution,
        n_total=int(first.get("n_total", 0)),
        n_bad=int(first.get("n_bad", 0)),
        hours=tuple(hours),
        nic_histogram=tuple(hist),
        aircraft=aircraft,
        n_total_cruise=int(alt.get("n_total_cruise", 0)),
        n_bad_cruise=int(alt.get("n_bad_cruise", 0)),
        bad_reports=int(alt.get("bad_reports", 0)),
        low_bad_reports=int(alt.get("low_bad_reports", 0)),
        n_total_nacp=int(nacp.get("n_total_nacp", 0)),
        n_bad_nacp=int(nacp.get("n_bad_nacp", 0)),
        n_bad_both=int(nacp.get("n_bad_both", 0)),
    )


def _nic_histogram(ch: ClickHouse, params: dict) -> list[int]:
    """Önce saatlik özetteki dağılım; boşsa (eski satırlar) ham tablodan."""
    rows = ch.query(CELL_NIC_HIST_SQL, params)
    hist = [int(n) for n in (rows[0]["hist"] if rows else [])]
    hist = (hist + [0] * NIC_BINS)[:NIC_BINS]
    if any(hist):
        return hist
    for r in ch.query(CELL_NIC_SQL, params):
        hist[min(int(r["nic"]), MAX_NIC)] += int(r["n"])
    return hist


def _truthy(v: object) -> bool:
    return v in (True, 1, "1", "true")


def event_row(e: dict) -> dict:
    """Ortak şemadaki olay → `events` tablosu satırı (zamanlar UTC, ayrıntılar JSON metni)."""

    def ts(iso: str) -> str:
        return iso.replace("T", " ").removesuffix("Z")

    return {
        **{k: e[k] for k in ("event_id", "type", "duration_hours", "cells", "region_h3", "fir")},
        **{k: e[k] for k in ("firs", "affected_aircraft", "scale", "confidence", "source")},
        "algorithm_version": e["algorithm_version"],
        "anonymous": e["anonymous"],
        "first_seen": ts(e["first_seen"]),
        "last_seen": ts(e["last_seen"]),
        "data_coverage": json.dumps(e["data_coverage"], separators=(",", ":"))
        if e["data_coverage"]
        else "",
        "evidence": json.dumps(e["evidence"], separators=(",", ":")) if e["evidence"] else "",
    }


def load_events(ch: ClickHouse, events: list[dict], batch_size: int = 5_000) -> int:
    """Olayları `events` tablosuna yazar (idempotent: aynı `event_id` yerine geçer)."""
    n = 0
    for i in range(0, len(events), batch_size):
        n += ch.insert("events", [event_row(e) for e in events[i : i + batch_size]])
    return n
