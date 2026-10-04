"""PositionReport → ClickHouse `positions` tablosu."""

from __future__ import annotations

from collections.abc import Iterable

import h3
from manzlytics_common.clickhouse import ClickHouse

from manzlytics_ingest.models import PositionReport

# Saklanan en ince çözünürlük; daha kaba hücreler h3ToParent ile türetilir.
# manzlytics_detector.jamming.FINEST_RESOLUTION ile aynı olmalı.
STORED_RESOLUTION = 5


def to_row(r: PositionReport) -> dict:
    return {
        "ts": r.ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "icao24": r.icao24[:6].rjust(6, "0"),
        "lat": r.lat,
        "lon": r.lon,
        "h3_r5": h3.str_to_int(h3.latlng_to_cell(r.lat, r.lon, STORED_RESOLUTION)),
        "alt_baro_ft": r.alt_baro_ft,
        "alt_geom_ft": r.alt_geom_ft,
        "callsign": r.callsign,
        "nic": r.nic,
        "nac_p": r.nac_p,
        "sil": r.sil,
        "on_ground": r.on_ground,
        "source": r.source,
        "anonymous": r.anonymous,
    }


def insert_positions(ch: ClickHouse, reports: Iterable[PositionReport]) -> int:
    return ch.insert("positions", (to_row(r) for r in reports))
