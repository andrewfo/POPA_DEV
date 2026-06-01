"""Convert the port's ArcGIS shapefile exports to WGS84 lat/lon GeoJSON.

Each source layer may carry its own coordinate system (the berths are in Texas
South Central State Plane US-ft; the warehouses came out in Web Mercator), so we
read the CRS from each ``.prj`` and reproject to EPSG:4326 individually.

Run:  python data/gis/to_geojson.py
Output: app/static/gis/<layer>.geojson  (consumed by the Leaflet map)
"""
from __future__ import annotations

import json
from pathlib import Path

import shapefile  # pyshp
from pyproj import CRS, Transformer

GIS_DIR = Path(__file__).parent
OUT_DIR = Path(__file__).parents[2] / "app" / "static" / "gis"

# layer name -> properties to keep (shapefile field -> output key)
LAYERS = {
    "berths": {"name": "name", "berth_leng": "berth_leng", "width": "width"},
    "warehouses": {"Name": "name", "Label": "label"},
}


def _rings_to_wgs(shape, transform) -> list[list[list[float]]]:
    """Split a shapefile polygon into rings and reproject each to [lon, lat]."""
    pts = shape.points
    parts = list(shape.parts) + [len(pts)]
    rings = []
    for a, b in zip(parts[:-1], parts[1:]):
        ring = [list(transform.transform(x, y)) for x, y in pts[a:b]]
        rings.append(ring)
    return rings


def convert(layer: str, keep: dict[str, str]) -> dict:
    crs = CRS.from_wkt((GIS_DIR / f"{layer}.prj").read_text())
    transform = Transformer.from_crs(crs, CRS.from_epsg(4326), always_xy=True)
    reader = shapefile.Reader(str(GIS_DIR / layer))
    features = []
    for sr in reader.iterShapeRecords():
        rec = sr.record.as_dict()
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": _rings_to_wgs(sr.shape, transform),
                },
                "properties": {out: rec.get(src) for src, out in keep.items()},
            }
        )
    print(f"{layer}: {len(features)} features  ({crs.name})")
    return {"type": "FeatureCollection", "features": features}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for layer, keep in LAYERS.items():
        fc = convert(layer, keep)
        (OUT_DIR / f"{layer}.geojson").write_text(json.dumps(fc))
    print(f"-> wrote GeoJSON to {OUT_DIR}")


if __name__ == "__main__":
    main()
