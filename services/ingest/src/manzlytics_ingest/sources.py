"""Veri kaynağı kayıt defteri (docs/veri-stratejisi.md §1).

Her satırın `source` sütunu buradaki bir anahtarla başlar. Sınıflar:
- open: kamu malı veya ticari kullanıma izin veren lisans → toplanır
- agreement: ticari olmayan kullanım / izin gerekiyor → anlaşma yapılana kadar toplanmaz
- closed: otomatik toplama yasak → yalnızca lisans anlaşmasıyla
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SourceClass = Literal["open", "agreement", "closed"]


@dataclass(frozen=True, slots=True)
class Source:
    key: str
    name: str
    data_type: str  # veri-stratejisi.md §3: T1…T5
    source_class: SourceClass
    license: str
    attribution: str
    share_alike: bool
    commercial_use: bool
    backfill: str  # geriye dönük erişim
    agreement: str | None = None  # anlaşma durumu ve tarihi


SOURCES: dict[str, Source] = {
    s.key: s
    for s in [
        Source(
            key="adsblol",
            name="adsb.lol globe_history (günlük arşiv) ve canlı API",
            data_type="T1",
            source_class="open",
            license="ODbL-1.0",
            attribution="Veri: adsb.lol (ODbL)",
            share_alike=True,
            commercial_use=True,
            backfill="2023-02-16'dan beri, günlük (github.com/adsblol/globe_history_YYYY)",
        ),
        Source(
            key="opensky",
            name="OpenSky Network",
            data_type="T1",
            source_class="agreement",
            license="Ticari olmayan kullanım; ticari lisans ayrı",
            attribution="",
            share_alike=False,
            commercial_use=False,
            backfill="Trino arşivi (araştırma başvurusuyla)",
            agreement="Görüşülmedi (#49)",
        ),
        Source(
            key="adsbfi",
            name="adsb.fi",
            data_type="T1",
            source_class="agreement",
            license="Kişisel/ticari olmayan kullanım (doğrulanacak)",
            attribution="",
            share_alike=False,
            commercial_use=False,
            backfill="Yok",
            agreement="Görüşülmedi (#49)",
        ),
        Source(
            key="airplaneslive",
            name="airplanes.live",
            data_type="T1",
            source_class="agreement",
            license="Kişisel/ticari olmayan kullanım (doğrulanacak)",
            attribution="",
            share_alike=False,
            commercial_use=False,
            backfill="Yok",
            agreement="Görüşülmedi (#49)",
        ),
        Source(
            key="adsbx",
            name="ADS-B Exchange",
            data_type="T1",
            source_class="closed",
            license="Ücretli / kısıtlayıcı şartlar (doğrulanacak)",
            attribution="",
            share_alike=False,
            commercial_use=False,
            backfill="Ayın 1'ine ait örnek günler",
            agreement="Görüşülmedi (#49)",
        ),
    ]
}


def source_key(source: str) -> str:
    """`adsblol:adsb_icao` → `adsblol`."""
    return source.split(":", 1)[0]


def collectable(key: str) -> bool:
    """Bu kaynaktan bugün veri toplanabilir mi? (açık ya da anlaşması yapılmış)"""
    s = SOURCES[key]
    return s.source_class == "open" or (s.agreement or "").startswith("Anlaşma yapıldı")
