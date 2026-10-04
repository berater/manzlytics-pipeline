"""Hücre seviye sınıflaması.

`packages/map/src/jamming.ts` ile aynı eşikler ve aynı hesap — iki taraf değişirse birlikte
güncellenmeli (test: tests/test_levels.py ile jamming.test.ts aynı örnek tabloyu kullanır).

Kural: hücrede en az MIN_AIRCRAFT uçak varsa, etkilenen uçak sayısı arka plan oranına
(BACKGROUND_RATE) göre anlamlı derecede yüksek mi diye tek taraflı binom testine bakılır.
Anlamlı değilse ya da oran LOW_MAX_RATIO altındaysa "low"; değilse orana göre medium/high.
Böylece eşik trafik ve periyot uzunluğuyla kendiliğinden ölçeklenir ve tek bir uçak
(n_bad = 1) hiçbir hücreyi tek başına işaretleyemez.
"""

from __future__ import annotations

import math
from typing import Literal

Level = Literal["noData", "low", "medium", "high"]

MIN_AIRCRAFT = 5
LOW_MAX_RATIO = 0.025
HIGH_MIN_RATIO = 0.1
BACKGROUND_RATE = 0.015  # temiz bölgelerde etkilenen uçak payı (örneklemlerde %1,0–1,7)
SIGNIFICANCE = 0.01


def binomial_tail(k: int, n: int, p: float) -> float:
    """P(X ≥ k), X ~ Binom(n, p). Log uzayında; büyük n'de taşma olmaz."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    log_pmf = n * math.log1p(-p)
    step = math.log(p) - math.log1p(-p)
    below = 0.0
    for i in range(k):
        below += math.exp(log_pmf)
        log_pmf += math.log((n - i) / (i + 1)) + step
    return max(0.0, 1.0 - below)


def jamming_level(n_total: int, n_bad: int) -> Level:
    if n_total < MIN_AIRCRAFT:
        return "noData"
    ratio = n_bad / n_total
    if ratio <= LOW_MAX_RATIO or binomial_tail(n_bad, n_total, BACKGROUND_RATE) >= SIGNIFICANCE:
        return "low"
    if ratio >= HIGH_MIN_RATIO:
        return "high"
    return "medium"
