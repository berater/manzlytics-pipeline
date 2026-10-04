"""Algoritma sürümü (N16): çıktının hangi eşik/kural setiyle üretildiğini işaretler.

`levels.py`, `jamming.py`, `quality.py`, `spoofing.py` ve `events.py` içindeki eşikler ya da
kurallar değişince **elle** artırılır (semver: eşik/kural değişimi → minor, şema kırılması → major).
Olay kimliği (`event_id`) sürümü içerir: sürüm değişince aynı veriden yeni kimlikler çıkar, eski
ve yeni sonuçlar karışmaz. `tests/test_version.py` eşik parmak izi değişip sürüm değişmediyse
başarısız olur.
"""

from __future__ import annotations

import hashlib
import json

ALGORITHM_VERSION = "1.2.0"


def threshold_fingerprint() -> str:
    """Eşik sabitlerinin kısa özeti; değişirse `ALGORITHM_VERSION` artırılmalı."""
    from manzlytics_detector import events, jamming, levels, quality, spoofing

    values: dict[str, object] = {}
    for mod in (levels, jamming, quality, spoofing, events):
        for name, value in vars(mod).items():
            if name.isupper() and isinstance(value, int | float | str | tuple):
                values[f"{mod.__name__}.{name}"] = value
    blob = json.dumps(values, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]
