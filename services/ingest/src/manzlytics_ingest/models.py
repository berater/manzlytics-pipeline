"""Kaynaktan bağımsız normalize konum kaydı.

Her veri kaynağı (adsb.lol, ileride anlaşmalı kaynaklar) bu yapıya dönüştürülür;
detector ve ClickHouse şeması yalnızca bunu bilir.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class PositionReport:
    ts: datetime
    icao24: str
    lat: float
    lon: float
    alt_baro_ft: float | None = None
    alt_geom_ft: float | None = None
    callsign: str | None = None
    nic: int | None = None
    nac_p: int | None = None
    sil: int | None = None
    on_ground: bool = False
    source: str = "unknown"
    # Gizlilik programındaki uçak (FAA LADD / PIA): sayılır ama kimliği hiç gösterilmez.
    anonymous: bool = False
    gs_kt: float | None = None
    track: float | None = None
    # NIC/NACp/SIL'in geldiği ayrıntı kaydının yaşı (arşivde her noktada yok, bkz. globe_history)
    nic_age_s: float | None = None
