"""Aero katmanı için odak bölgedeki tarifeli havalimanlarını üretir.

    uv run services/detector/scripts/build_airports.py

Kaynak: OurAirports (davidmegginson.github.io/ourairports-data) — kamu malı (public domain),
bkz. docs/veri-stratejisi.md T3. OpenAIP (CC BY-NC) bilerek KULLANILMAZ.

Çıktı: apps/web/public/data/aero-airports.geojson — yalnız `large_airport` ve `medium_airport`
türünde, tarifeli sefer yapılan ve odak bölge kutusundaki havalimanları (build_fir_places.py ile
aynı kutu); koordinatlar 4 ondalığa yuvarlanır. Betik çıktıyı sıkıştırılmış yazar; commit'lemeden
önce `pnpm exec prettier --write apps/web/public/data/aero-airports.geojson` çalıştırın.
"""

from __future__ import annotations

import csv
import io
import json
import urllib.request
from pathlib import Path

CSV_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
# build_fir_places.py ile aynı: (minLon, minLat, maxLon, maxLat)
BBOX = (15.0, 24.0, 57.0, 52.0)
TYPES = {"large_airport": "large", "medium_airport": "medium"}
OUT = Path(__file__).resolve().parents[3] / "apps/web/public/data/aero-airports.geojson"


def features(rows: list[dict[str, str]]) -> list[dict]:
    out = []
    for r in rows:
        kind = TYPES.get(r["type"])
        if kind is None or r["scheduled_service"] != "yes":
            continue
        lon, lat = float(r["longitude_deg"]), float(r["latitude_deg"])
        if not (BBOX[0] <= lon <= BBOX[2] and BBOX[1] <= lat <= BBOX[3]):
            continue
        out.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(lon, 4), round(lat, 4)]},
                "properties": {
                    "icao": r["icao_code"] or r["ident"],
                    "iata": r["iata_code"],
                    "name": r["name"],
                    "kind": kind,
                    "country": r["iso_country"],
                },
            }
        )
    # Büyükler önce, sonra ICAO sırası: çıktı deterministik, etiket çakışmasında büyük kazanır.
    out.sort(key=lambda f: (f["properties"]["kind"] != "large", f["properties"]["icao"]))
    return out


def main() -> None:
    with urllib.request.urlopen(CSV_URL, timeout=60) as r:  # noqa: S310
        text = r.read().decode("utf-8")
    fc = {
        "type": "FeatureCollection",
        "features": features(list(csv.DictReader(io.StringIO(text)))),
    }
    OUT.write_text(
        json.dumps(fc, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    print(f"{len(fc['features'])} havalimanı → {OUT}")


if __name__ == "__main__":
    main()
