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

import sys

GIS_DIR = Path(__file__).parent
STATIC_GIS = Path(__file__).parents[2] / "app" / "static" / "gis"

# Pull the canonical Dock No. crosswalk params (no inline stationing math).
sys.path.insert(0, str(Path(__file__).parents[2]))
from app.crosswalk import format_station

BERTHS = GIS_DIR / "berths"
WAREHOUSES = GIS_DIR / "warehouses"

ANCHOR_BERTH = "Berth 4"      # its SW water corner is the stationing anchor
ANCHOR_STATION = 351.0        # running footage at that corner (from berth_leng)

# Apron / berthing-zone polygon: a strip along the WATER side of the quay face.
# A berthed vessel's AIS antenna floats off the quay (NOT on the landward berth
# rectangle), so the "alongside" zone is water-side. Widths are in true feet
# (the berths' planar CRS). Water reach covers a large beam + standoff + AIS
# noise; the small inland reach absorbs fender slop / antennas just shy of the
# digitized face. This supersedes the symmetric centerline buffer
# (Settings.berth_buffer_m) once seeded — see app/occupancy/alongside.py.
APRON_WATER_FT = 250.0
APRON_INLAND_FT = 40.0


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

    # Interpolate a planar (x, y) in true feet for any POPA station on the
    # (monotonic) face line; lon/lat is just that point through to_wgs.
    def interp_xy(t: float):
        for i in range(len(stations) - 1):
            a, b = stations[i], stations[i + 1]
            if a <= t <= b:
                r = 0.0 if b == a else (t - a) / (b - a)
                x = chain[i][0] + r * (chain[i + 1][0] - chain[i][0])
                y = chain[i][1] + r * (chain[i + 1][1] - chain[i][1])
                return (x, y)
        return None

    # Station ticks, to match the port's "Wharf Stationing with Aerial" exhibit:
    # thin uniform lines perpendicular to the quay, extending out into the
    # channel, labelled in canonical POPA stationing (STA "X+YY") every 100 ft
    # from 0+00 at the SW end to the NE end of the public wharf (~34+50). POPA is
    # the canonical measure, so each tick reads the same value the exhibit shows.
    # Each marker is a LineString from a short landward nub on the concrete to a
    # point out in the water; the frontend draws the line + a label at the water
    # end. `w` is the unit water normal in the planar US-ft CRS, so the offsets
    # below are in true feet.
    def marker_line(x, y, land_ft, water_ft):
        a = to_wgs.transform(x - w[0] * land_ft, y - w[1] * land_ft)   # on the dock
        b = to_wgs.transform(x + w[0] * water_ft, y + w[1] * water_ft)  # into water
        return [[round(a[0], 7), round(a[1], 7)], [round(b[0], 7), round(b[1], 7)]]

    # Every round POPA hundred across the WHOLE wharf face (so the ticks span its
    # full length, including the SW berths that sit at negative POPA), plus the
    # NE end of the face as the exhibit's final tick (~34+50). The 0+00 .. 34+50
    # run matches the exhibit exactly; the SW extension is labelled in the same
    # POPA reference (negative stationing, e.g. "-4+00").
    popa_lo, popa_hi = stations[0], stations[-1]
    first = int(math.ceil(popa_lo / 100.0)) * 100   # smallest hundred on the line
    last = int(math.floor(popa_hi / 100.0)) * 100   # largest hundred on the line
    tick_popas = [float(p) for p in range(first, last + 1, 100)]
    if popa_hi - last >= 25:
        tick_popas.append(round(popa_hi / 50.0) * 50.0)

    marks = []
    for popa in tick_popas:
        xy = interp_xy(popa)
        if xy is None:
            continue
        line = marker_line(xy[0], xy[1], land_ft=10, water_ft=300)
        marks.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": line},
            "properties": {"popa": round(popa, 1), "label": format_station(popa)},
        })
    (STATIC_GIS).mkdir(parents=True, exist_ok=True)
    (STATIC_GIS / "feet_markers.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": marks})
    )

    # Yellow quay-face ticks — the exhibit's SECOND tick series. Referenced from
    # the NE end of the public wharf (Berth 1): 0 ft at the far-NE quay corner,
    # increasing SW down the face, one tick every 50 ft, the full length of the
    # wharf (so every berth is covered). Distinct from the POPA series above:
    # these sit ON the wharf — each tick runs from the quay edge landward onto
    # the concrete apron (water_ft=0, no part in the channel), vs. the long POPA
    # lines that run out into the water. The berth rectangles are ~270 ft deep,
    # so the labelled round-hundred ticks run nearly that full depth and carry
    # their label at the landward (top) edge of the berth rectangle; the minor
    # 50s are short unlabelled ticks at the quay. Result: a ruler with the yellow
    # numbers along the top of the berths instead of a label on every tick.
    BERTH_DEPTH_FT = 255.0              # just inside the ~270 ft berth rectangles
    popa_ne = stations[-1]              # Berth 1 NE quay corner = yellow 0+00

    def yellow_feature(interp_station, dist, *, major):
        """One quay-face tick: interpolate the quay point at ``interp_station``
        (POPA ft), draw it landward, and label it with ``dist`` (ft from Berth 1).
        Returns None if the point falls off the line. Single source of the
        Feature schema so the loop and the end-cap tick can't drift apart."""
        xy = interp_xy(interp_station)
        if xy is None:
            return None
        line = marker_line(xy[0], xy[1],
                           land_ft=BERTH_DEPTH_FT if major else 45,
                           water_ft=0)
        return {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": line},
            "properties": {
                "dist_ne": round(dist, 1),
                "major": major,
                "label": str(int(round(dist))) if major else None,
            },
        }

    yellow_marks = []
    y = 0.0
    while popa_ne - y >= popa_lo - 1e-6:
        feat = yellow_feature(popa_ne - y, y, major=(round(y) % 100 == 0))
        if feat is not None:
            yellow_marks.append(feat)
        y += 50.0

    # Close the ruler exactly on the SW (leftmost) edge of Berth 6 — the end of
    # the wharf face — even though it is not a round 50 ft from Berth 1. Without
    # this the last 50-ft tick stops ~20 ft inside the berth; the port wants the
    # final marker lined up with that edge.
    y_end = popa_ne - popa_lo
    if y_end - (y - 50.0) > 1.0:        # last placed tick fell short of the edge
        # Drop the final round-hundred if the edge tick would land almost on top
        # of it (4600 vs the 4620 terminus), leaving just the edge marker.
        if yellow_marks and y_end - yellow_marks[-1]["properties"]["dist_ne"] < 50.0:
            yellow_marks.pop()
        feat = yellow_feature(popa_lo, y_end, major=True)
        if feat is not None:
            yellow_marks.append(feat)
    (STATIC_GIS / "yellow_markers.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": yellow_marks})
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

    # --- apron / berthing-zone polygon (water-side strip of the quay face) -----
    # Offset the face chain inland by APRON_INLAND_FT and waterward by
    # APRON_WATER_FT (both along the unit water normal w, in true feet), then walk
    # the inland edge SW->NE and the water edge back NE->SW to close one ring.
    inland_xy = [(x - w[0] * APRON_INLAND_FT, y - w[1] * APRON_INLAND_FT) for x, y in chain]
    water_xy = [(x + w[0] * APRON_WATER_FT, y + w[1] * APRON_WATER_FT) for x, y in chain]
    ring_xy = inland_xy + water_xy[::-1] + [inland_xy[0]]
    ring_wgs = [[round(lon, 7), round(lat, 7)] for lon, lat in (to_wgs.transform(x, y) for x, y in ring_xy)]

    (GIS_DIR / "apron_polygon.json").write_text(json.dumps(ring_wgs, indent=2))
    (STATIC_GIS / "apron.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [ring_wgs]},
            "properties": {
                "name": "POPA Public Wharf apron (berthing zone)",
                "water_ft": APRON_WATER_FT, "inland_ft": APRON_INLAND_FT,
            },
        }],
    }))

    print(f"axis u={u} water_normal={w}")
    print(f"faces found: {list(faces)}")
    for lon, lat, m in vertices:
        print(f"  station {m:>8.1f} ft  ->  ({lat:.6f}, {lon:.6f})")
    print(f"span {vertices[0][2]} .. {vertices[-1][2]} ft over {len(vertices)} vertices")
    print(f"apron strip: {APRON_INLAND_FT} ft inland .. {APRON_WATER_FT} ft water, {len(ring_wgs)} ring pts")
    print(f"-> wrote centerline_vertices.json, apron_polygon.json, and app/static/gis/{{centerline,apron}}.geojson")


if __name__ == "__main__":
    main()
