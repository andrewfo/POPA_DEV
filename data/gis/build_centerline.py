"""Derive the canonical wharf centerline (quay face) + POPA stationing from the
berth polygons.

Method (validated against the ``width`` / ``berth_leng`` attributes):
  * Each berth polygon's WATER-SIDE edge is the edge whose length matches the
    berth ``width`` and which lies on the channel side (away from the inland
    warehouses). That edge IS the quay face for that berth.
  * Adjacent berths share their water-corner vertices, so the per-berth face
    edges chain end-to-end into one continuous face line, SW -> NE.
  * POPA station = running footage along that face. The berth ``berth_leng``
    chain anchors the Berth 5/4 junction at station 351 (Berth 4 then runs
    351->1108->...->3450 at the Berth 1 NE end), and the planar edge lengths
    match those station deltas to ~1 ft, so station = 351 + signed distance
    along the face from that anchor.

Outputs:
  * data/gis/centerline_vertices.json   -> [[lon, lat, M], ...] for the seed
  * app/static/gis/centerline.geojson   -> line + station ticks for the map

Run:  python data/gis/build_centerline.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import shapefile  # pyshp
from pyproj import CRS, Transformer

GIS_DIR = Path(__file__).parent
STATIC_GIS = Path(__file__).parents[2] / "app" / "static" / "gis"

BERTHS = GIS_DIR / "berths"
WAREHOUSES = GIS_DIR / "warehouses"

ANCHOR_BERTH = "Berth 4"      # its SW water corner is the stationing anchor
ANCHOR_STATION = 351.0        # running footage at that corner (from berth_leng)


def _reader(path: Path):
    r = shapefile.Reader(str(path))
    crs = CRS.from_wkt((path.with_suffix(".prj")).read_text())
    return r, crs


def _edges(points):
    return list(zip(points[:-1], points[1:]))


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def main() -> None:
    berths, bcrs = _reader(BERTHS)
    whs, wcrs = _reader(WAREHOUSES)

    # Everything in the berths' planar US-ft CRS (true feet -> good for stationing).
    wh_to_b = Transformer.from_crs(wcrs, bcrs, always_xy=True)
    to_wgs = Transformer.from_crs(bcrs, CRS.from_epsg(4326), always_xy=True)

    recs = list(berths.iterShapeRecords())

    # --- axes: along-shore (u) from berth-centroid spread, water normal (w) ---
    cents = []
    for sr in recs:
        p = sr.shape.points
        cents.append((sum(x for x, _ in p) / len(p), sum(y for _, y in p) / len(p)))
    sw = min(cents, key=lambda c: c[0])
    ne = max(cents, key=lambda c: c[0])
    ux, uy = ne[0] - sw[0], ne[1] - sw[1]
    ul = math.hypot(ux, uy)
    u = (ux / ul, uy / ul)                       # along-shore, SW->NE
    n = (-u[1], u[0])                            # a perpendicular

    wmean = []
    for sr in whs.iterShapeRecords():
        p = [wh_to_b.transform(x, y) for x, y in sr.shape.points]
        wmean.append((sum(x for x, _ in p) / len(p), sum(y for _, y in p) / len(p)))
    wm = (sum(c[0] for c in wmean) / len(wmean), sum(c[1] for c in wmean) / len(wmean))
    bc = (sum(c[0] for c in cents) / len(cents), sum(c[1] for c in cents) / len(cents))
    inland = (wm[0] - bc[0], wm[1] - bc[1])      # points toward warehouses
    if n[0] * inland[0] + n[1] * inland[1] > 0:  # make w point to the WATER
        n = (-n[0], -n[1])
    w = n

    along = lambda p: p[0] * u[0] + p[1] * u[1]
    water = lambda p: p[0] * w[0] + p[1] * w[1]

    # --- per-berth water-side face edge -----------------------------------
    faces = {}  # berth name -> (sw_pt, ne_pt) in ft
    for sr in recs:
        name = sr.record["name"]
        width = float(sr.record["width"])
        cands = [
            e for e in _edges(sr.shape.points)
            if abs(_dist(*e) - width) <= 0.10 * width
        ]
        if not cands:
            continue
        face = max(cands, key=lambda e: water(((e[0][0] + e[1][0]) / 2,
                                               (e[0][1] + e[1][1]) / 2)))
        pts = sorted(face, key=along)            # order SW -> NE
        faces[name] = (pts[0], pts[1])

    # --- chain face edges into one ordered vertex list --------------------
    verts = []
    for f in faces.values():
        verts.extend(f)
    verts.sort(key=along)
    chain = []
    for p in verts:
        if not chain or _dist(chain[-1], p) > 15.0:  # dedupe shared corners
            chain.append(p)

    # --- stationing: anchor Berth 4 SW corner at ANCHOR_STATION -----------
    anchor_pt = faces[ANCHOR_BERTH][0]
    cum = [0.0]
    for i in range(1, len(chain)):
        cum.append(cum[-1] + _dist(chain[i - 1], chain[i]))
    ai = min(range(len(chain)), key=lambda i: _dist(chain[i], anchor_pt))
    stations = [ANCHOR_STATION + (cum[i] - cum[ai]) for i in range(len(chain))]

    # --- emit -------------------------------------------------------------
    vertices = []
    for (x, y), m in zip(chain, stations):
        lon, lat = to_wgs.transform(x, y)
        vertices.append([round(lon, 7), round(lat, 7), round(m, 2)])

    (GIS_DIR / "centerline_vertices.json").write_text(json.dumps(vertices, indent=2))

    # Interpolate a lat/lon for any station along the (monotonic) face line.
    def interp(t: float):
        for i in range(len(stations) - 1):
            a, b = stations[i], stations[i + 1]
            if a <= t <= b:
                r = 0.0 if b == a else (t - a) / (b - a)
                x = chain[i][0] + r * (chain[i + 1][0] - chain[i][0])
                y = chain[i][1] + r * (chain[i + 1][1] - chain[i][1])
                return to_wgs.transform(x, y)
        return None

    # Feet markers every 100 ft (labelled every 500), interpolated on the line.
    lo = math.ceil(stations[0] / 100) * 100
    hi = math.floor(stations[-1] / 100) * 100
    marks = []
    for s in range(int(lo), int(hi) + 1, 100):
        ll = interp(float(s))
        if ll is None:
            continue
        marks.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(ll[0], 7), round(ll[1], 7)]},
            "properties": {"station": s, "major": s % 500 == 0},
        })
    (STATIC_GIS).mkdir(parents=True, exist_ok=True)
    (STATIC_GIS / "feet_markers.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": marks})
    )

    # Per-berth station range, taken from the SAME geometry as the line so the
    # berth extents and the feet markers always agree.
    def nearest_station(pt):
        i = min(range(len(chain)), key=lambda i: _dist(chain[i], pt))
        return stations[i]

    berth_st = {
        name: sorted([round(nearest_station(f[0]), 1), round(nearest_station(f[1]), 1)])
        for name, f in faces.items()
    }
    (STATIC_GIS / "berth_stations.json").write_text(json.dumps(berth_st, indent=2))

    line = {
        "type": "Feature",
        "geometry": {"type": "LineString",
                     "coordinates": [[lon, lat] for lon, lat, _ in vertices]},
        "properties": {"name": "POPA Public Wharf centerline",
                       "stations": [m for *_, m in vertices]},
    }
    ticks = [
        {"type": "Feature",
         "geometry": {"type": "Point", "coordinates": [lon, lat]},
         "properties": {"station": m}}
        for lon, lat, m in vertices
    ]
    STATIC_GIS.mkdir(parents=True, exist_ok=True)
    (STATIC_GIS / "centerline.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [line, *ticks]})
    )

    print(f"axis u={u} water_normal={w}")
    print(f"faces found: {list(faces)}")
    for lon, lat, m in vertices:
        print(f"  station {m:>8.1f} ft  ->  ({lat:.6f}, {lon:.6f})")
    print(f"span {vertices[0][2]} .. {vertices[-1][2]} ft over {len(vertices)} vertices")
    print(f"-> wrote centerline_vertices.json and app/static/gis/centerline.geojson")


if __name__ == "__main__":
    main()
