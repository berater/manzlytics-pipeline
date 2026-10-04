"""Veri güveni (N15): risk seviyesinden bağımsız, "bu sayıya ne kadar güvenilir" kademesi.

`packages/map/src/quality.ts` ile aynı eşikler ve aynı hesap — iki taraf değişirse birlikte
güncellenmeli (test: tests/test_quality.py ile quality.test.ts aynı örnek tabloyu kullanır).
Tasarım: docs/research/veri-kalite-endeksi-tasarimi.md. Eşikler başlangıç değeridir;
gerçek `n_total` dağılımına ve #24 verisine göre kalibre edilecek.
"""

from __future__ import annotations

import math
from typing import Literal

from .levels import MIN_AIRCRAFT

Confidence = Literal["none", "low", "medium", "high"]

LOW_MAX_AIRCRAFT = 20  # bunun altı "düşük"
HIGH_MIN_AIRCRAFT = 100  # "yüksek" için en az
HIGH_MAX_WIDTH = 0.08  # "yüksek" için Wilson aralığı en çok bu geniş
LOW_MAX_COVERAGE = 0.5  # saat kapsaması bunun altındaysa "düşük"
HIGH_MIN_COVERAGE = 0.75  # "yüksek" için en az
WILSON_Z = 1.96  # %95


def wilson_interval(n_total: int, n_bad: int, z: float = WILSON_Z) -> tuple[float, float]:
    """Etkilenen uçak oranının Wilson güven aralığı (alt, üst). Uçak yoksa (0, 1)."""
    if n_total <= 0:
        return (0.0, 1.0)
    p = n_bad / n_total
    z2 = z * z
    denom = 1 + z2 / n_total
    center = (p + z2 / (2 * n_total)) / denom
    half = z * math.sqrt(p * (1 - p) / n_total + z2 / (4 * n_total * n_total)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def data_confidence(n_total: int, n_bad: int, coverage: float | None = None) -> Confidence:
    """`coverage`: verinin bulunduğu saat sayısı / istenen saat sayısı (0–1); bilinmiyorsa None."""
    if n_total < MIN_AIRCRAFT:
        return "none"
    if n_total < LOW_MAX_AIRCRAFT or (coverage is not None and coverage < LOW_MAX_COVERAGE):
        return "low"
    lo, hi = wilson_interval(n_total, n_bad)
    if (
        n_total >= HIGH_MIN_AIRCRAFT
        and hi - lo <= HIGH_MAX_WIDTH
        and (coverage is None or coverage >= HIGH_MIN_COVERAGE)
    ):
        return "high"
    return "medium"
