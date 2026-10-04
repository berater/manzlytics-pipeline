"""Hücre merkezine ülke ya da deniz adı (Natural Earth, kamu malı).

Veri: data/places.json — scripts/build_places.py ile üretilir. Ülke önceliklidir; kıyıya
çok yakın ve hiçbir poligona düşmeyen noktalar için None döner.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import h3

DATA = Path(__file__).parent / "data" / "places.json"
FIR_DATA = Path(__file__).parent / "data" / "fir_places.json"


@dataclass(frozen=True, slots=True)
class Place:
    kind: Literal["country", "sea"]
    name_tr: str
    name_en: str


@dataclass(frozen=True, slots=True)
class Fir:
    """EUROCONTROL Network Manager'dan (MIT) — bkz. build_fir_places.py başlığı."""

    icao: str
    name: str


@dataclass(frozen=True, slots=True)
class _Shape:
    place: Place
    bbox: tuple[float, float, float, float]
    polygons: tuple
    area: float


def _ring_area(ring: list) -> float:
    return (
        abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(ring, ring[1:], strict=False))) / 2
    )


@lru_cache(maxsize=1)
def _shapes() -> tuple[_Shape, ...]:
    raw = json.loads(DATA.read_text())["places"]
    return tuple(
        _Shape(
            place=Place(p["kind"], p["tr"], p["en"]),
            bbox=tuple(p["bbox"]),
            polygons=tuple(p["polygons"]),
            area=sum(_ring_area(poly[0]) for poly in p["polygons"]),
        )
        for p in raw
    )


def _in_ring(x: float, y: float, ring: list) -> bool:
    inside = False
    for (x0, y0), (x1, y1) in zip(ring, ring[1:], strict=False):
        if (y0 > y) != (y1 > y) and x < (x1 - x0) * (y - y0) / (y1 - y0) + x0:
            inside = not inside
    return inside


def _contains(shape: _Shape, x: float, y: float) -> bool:
    x0, y0, x1, y1 = shape.bbox
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    for outer, *holes in shape.polygons:
        if _in_ring(x, y, outer) and not any(_in_ring(x, y, h) for h in holes):
            return True
    return False


@lru_cache(maxsize=4096)
def place_for(lat: float, lon: float) -> Place | None:
    hits = [s for s in _shapes() if _contains(s, lon, lat)]
    countries = [s for s in hits if s.place.kind == "country"]
    # Deniz poligonları iç içe olabilir (Girit Denizi ⊂ Akdeniz): en küçüğü en belirgin ad.
    pick = countries or sorted(hits, key=lambda s: s.area)
    return pick[0].place if pick else None


def place_for_cell(cell: str) -> Place | None:
    """Hücre merkezinin adı; merkez boşluğa düşerse (kıyı, sınır hattı) köşelerin çoğunluğu."""
    lat, lon = h3.cell_to_latlng(cell)
    if (p := place_for(round(lat, 4), round(lon, 4))) is not None:
        return p
    corners = [place_for(round(a, 4), round(o, 4)) for a, o in h3.cell_to_boundary(cell)]
    found = Counter(c for c in corners if c is not None)
    return found.most_common(1)[0][0] if found else None


@dataclass(frozen=True, slots=True)
class _FirShape:
    fir: Fir
    bbox: tuple[float, float, float, float]
    polygons: tuple


@lru_cache(maxsize=1)
def _fir_shapes() -> tuple[_FirShape, ...]:
    raw = json.loads(FIR_DATA.read_text())["firs"]
    return tuple(
        _FirShape(
            fir=Fir(f["icao"], f["name"]), bbox=tuple(f["bbox"]), polygons=tuple(f["polygons"])
        )
        for f in raw
    )


@lru_cache(maxsize=4096)
def fir_for(lat: float, lon: float) -> Fir | None:
    hits = [s.fir for s in _fir_shapes() if _contains(s, lon, lat)]
    return hits[0] if hits else None


def fir_for_cell(cell: str) -> Fir | None:
    """Hücre merkezinin FIR'ı; merkez boşluğa düşerse köşelerin çoğunluğu."""
    lat, lon = h3.cell_to_latlng(cell)
    if (f := fir_for(round(lat, 4), round(lon, 4))) is not None:
        return f
    corners = [fir_for(round(a, 4), round(o, 4)) for a, o in h3.cell_to_boundary(cell)]
    found = Counter(c for c in corners if c is not None)
    return found.most_common(1)[0][0] if found else None
