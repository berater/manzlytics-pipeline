# /// script
# dependencies = ["pyshp>=2.3"]
# ///
"""Odak bölgedeki FIR (Flight Information Region) poligonlarını üretir.

    uv run services/detector/scripts/build_fir_places.py

Kaynak: EUROCONTROL Network Manager — "FirUir_NM" shapefile, MIT lisanslı
(github.com/euctrl-pru/eurocontrol-atlas). Resmi, kurumsal bir kaynak;
VATSIM'in sanal ATC ağı için tuttuğu önceki geçici kaynağın (#22) yerini alır
— bkz. docs/yol-haritasi.md #22 ve docs/veri-stratejisi.md T3.

Not: Şebeke verisi 2015 tarihli; FIR sınırları siyasi sınırlara göre çok
seyrek değişir, ama güncel bir kaynak bulununca (resmi AIP/eAIP) tekrar
değiştirilebilir.

Çıktı: src/manzlytics_detector/data/fir_places.json — yalnız odak bölgeye değen
poligon parçaları, koordinatlar 0,01°'ye yuvarlanmış ve ~2 km'de sadeleştirilmiş.
Betik çıktıyı sıkıştırılmış (minified) yazar; commit'lemeden önce
`pnpm exec prettier --write src/manzlytics_detector/data/fir_places.json`
çalıştırın (CI `format:check` bunu ister).
"""

from __future__ import annotations

import io
import json
import urllib.request
import zipfile
from pathlib import Path

import shapefile

ZIP_URL = "https://raw.githubusercontent.com/euctrl-pru/eurocontrol-atlas/master/zip/FirUir_NM.zip"
# Odak bölge (28–48°K, 20–52°D) + kenar payı — build_places.py ile aynı
BBOX = (15.0, 24.0, 57.0, 52.0)
OUT = Path(__file__).resolve().parents[1] / "src/manzlytics_detector/data/fir_places.json"
TOLERANCE = 0.02

# Şebeke modelinin gerçek FIR karşılığı olmayan yer tutucu/kurgusal alanları
NOT_REAL_FIRS = {"UUUUFIR", "OOOOFIR", "PPPPFIR"}


def fetch_shapefile() -> shapefile.Reader:
    with urllib.request.urlopen(ZIP_URL, timeout=60) as r:  # noqa: S310
        data = r.read()
    zf = zipfile.ZipFile(io.BytesIO(data))
    parts = {}
    for name in ("shp", "dbf", "shx"):
        member = next(n for n in zf.namelist() if n.lower().endswith(f".{name}"))
        parts[name] = io.BytesIO(zf.read(member))
    return shapefile.Reader(shp=parts["shp"], dbf=parts["dbf"], shx=parts["shx"])


def ring_bbox(ring: list) -> tuple[float, float, float, float]:
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def intersects(b: tuple, c: tuple = BBOX) -> bool:
    return b[2] >= c[0] and b[0] <= c[2] and b[3] >= c[1] and b[1] <= c[3]


def round_ring(ring: list) -> list:
    out: list = []
    for x, y in ring:
        p = [round(x, 2), round(y, 2)]
        if not out or max(abs(p[0] - out[-1][0]), abs(p[1] - out[-1][1])) >= TOLERANCE:
            out.append(p)
    if out and out[-1] != out[0]:
        out.append(out[0])
    return out


def signed_area(ring: list) -> float:
    return sum(
        x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1], strict=True)
    )


def shape_polygons(shape) -> list[list[list[float]]]:
    """Shapefile parçalarını dış halka + iç halka (delik) gruplarına ayırır.

    ESRI Shapefile kuralı: saat yönünde halka = dış sınır, saat yönü tersi = delik.
    """
    parts = list(shape.parts) + [len(shape.points)]
    rings = [shape.points[parts[i] : parts[i + 1]] for i in range(len(parts) - 1)]
    polygons: list[list[list[float]]] = []
    for ring in rings:
        ring = [[p[0], p[1]] for p in ring]
        if signed_area(ring) < 0:  # saat yönü = dış halka
            polygons.append([ring])
        elif polygons:  # saat yönü tersi = önceki dış halkanın deliği
            polygons[-1].append(ring)
        else:
            polygons.append([ring])
    return polygons


def main() -> None:
    sf = fetch_shapefile()

    out = []
    for sr in sf.shapeRecords():
        rec = sr.record.as_dict()
        code = rec["AV_AIRSPAC"]
        if not code.endswith("FIR") or code in NOT_REAL_FIRS:
            continue
        icao = code[:-3]
        name = rec["AV_NAME"].removesuffix(" FIR").removesuffix(" UIR").title().strip()

        polys = []
        for poly in shape_polygons(sr.shape):
            if not intersects(ring_bbox(poly[0])):
                continue
            rings = [round_ring(r) for r in poly]
            rings = [r for r in rings if len(r) >= 4]
            if rings:
                polys.append(rings)
        if not polys:
            continue

        bb = [ring_bbox(p[0]) for p in polys]
        out.append(
            {
                "icao": icao,
                "name": name,
                "bbox": [
                    min(b[0] for b in bb),
                    min(b[1] for b in bb),
                    max(b[2] for b in bb),
                    max(b[3] for b in bb),
                ],
                "polygons": polys,
            }
        )

    OUT.write_text(
        json.dumps(
            {
                "source": (
                    "EUROCONTROL Network Manager, FirUir_NM (MIT) — "
                    "github.com/euctrl-pru/eurocontrol-atlas"
                ),
                "firs": out,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    print(f"{OUT}: {len(out)} FIR, {OUT.stat().st_size / 1024:.0f} KiB")


if __name__ == "__main__":
    main()
