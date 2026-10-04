"""Bölgenin ülke ve deniz poligonlarını Natural Earth'ten (kamu malı) üretir.

    uv run python services/detector/scripts/build_places.py

Çıktı: src/manzlytics_detector/data/places.json — yalnız odak bölgeye değen poligon
parçaları, koordinatlar 0,01°'ye yuvarlanmış ve ~2 km'de sadeleştirilmiş. Hücre merkezine
ad vermek için yeterli.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
# Türkiye bakış açısı (POV) sürümü: sınırlar Türkiye'nin resmi tutumuna göre (ör. Kırım → Ukrayna).
COUNTRIES = BASE + "ne_10m_admin_0_countries_tur.geojson"
MARINE = BASE + "ne_10m_geography_marine_polys.geojson"
# Odak bölge (28–48°K, 20–52°D) + kenar payı
BBOX = (15.0, 24.0, 57.0, 52.0)
OUT = Path(__file__).resolve().parents[1] / "src/manzlytics_detector/data/places.json"

# Tartışmalı bölgelerde tek, coğrafi ad (ada) kullanılır; resmi ad değişiklikleri.
MERGE = {
    "N. Cyprus": ("Kıbrıs", "Cyprus"),
    "Cyprus": ("Kıbrıs", "Cyprus"),
    "Akrotiri": ("Kıbrıs", "Cyprus"),
    "Dhekelia": ("Kıbrıs", "Cyprus"),
    "Turkey": ("Türkiye", "Türkiye"),  # resmi İngilizce ad
}
# Radyal sadeleştirme: bir önceki noktaya bu kadar yakın noktalar atılır (~2 km).
TOLERANCE = 0.02


def fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


def polygons(geom: dict) -> list:
    return [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]


def ring_bbox(ring: list) -> tuple[float, float, float, float]:
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def intersects(b: tuple, c: tuple = BBOX) -> bool:
    return b[2] >= c[0] and b[0] <= c[2] and b[3] >= c[1] and b[1] <= c[3]


def round_ring(ring: list) -> list:
    out: list = []
    for x, y, *_ in ring:
        p = [round(x, 2), round(y, 2)]
        if not out or max(abs(p[0] - out[-1][0]), abs(p[1] - out[-1][1])) >= TOLERANCE:
            out.append(p)
    if out and out[-1] != out[0]:
        out.append(out[0])
    return out


def places(features: list, kind: str, name_key: str, tr_key: str, en_key: str) -> list:
    out = []
    for f in features:
        props = f["properties"]
        polys = []
        for poly in polygons(f["geometry"]):
            if intersects(ring_bbox(poly[0])):
                rings = [round_ring(r) for r in poly]
                if len(rings[0]) >= 4:
                    polys.append([r for r in rings if len(r) >= 4])
        if not polys:
            continue
        tr, en = MERGE.get(props[name_key], (props.get(tr_key), props.get(en_key)))
        bb = [ring_bbox(p[0]) for p in polys]
        out.append(
            {
                "kind": kind,
                "tr": tr or en,
                "en": en,
                "bbox": [
                    min(b[0] for b in bb),
                    min(b[1] for b in bb),
                    max(b[2] for b in bb),
                    max(b[3] for b in bb),
                ],
                "polygons": polys,
            }
        )
    return out


def main() -> None:
    data = places(fetch(COUNTRIES)["features"], "country", "NAME", "NAME_TR", "NAME_EN")
    data += places(fetch(MARINE)["features"], "sea", "name", "name_tr", "name_en")
    OUT.write_text(
        json.dumps(
            {"source": "Natural Earth (public domain), naturalearthdata.com", "places": data},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    print(f"{OUT}: {len(data)} yer, {OUT.stat().st_size / 1024:.0f} KiB")


if __name__ == "__main__":
    main()
