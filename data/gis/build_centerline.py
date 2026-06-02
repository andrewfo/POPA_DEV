"""Derive the canonical wharf centerline (quay face) + POPA stationing.

The berth polygons are berthing-WATER rectangles: their landward long edge is
the quay face, their water-side long edge is the outer limit of the berth
pocket, ~200-270 ft (the berth depth) out into the channel. So the berths give
clean STATIONING (running footage along the wharf), independent of geometry.

``lines.*`` (an ArcGIS "Distance And Direction" annotation) holds the real
quay-face GEOMETRY: its two records are the two STRAIGHT PARTS of the bulkhead,
each verified on the orthomosaic to lie on the concrete/water edge. The parts
are near-parallel but laterally offset ~80 ft — a real STEP where the NE part
juts waterward of the SW part (clearly visible in the aerial: the bulk-carrier
berth sits on the outboard NE part, the small-boat berth on the inboard SW part).

Method:
  * STATIONING REFERENCE: each berth's along-shore edge length matches its
    ``width`` attribute, and the ``berth_leng`` chain (351 -> 1108 -> ... ->
    3450) gives the running POPA footage. Chaining the (water-side) edges and
    anchoring the Berth 5/4 junction at station 351 reproduces those deltas to
    ~1 ft. This chain carries correct ALONG-wharf stationing (independent of
    which side it sits on).
  * QUAY-FACE GEOMETRY: the centerline IS the two-part bulkhead — the four
    ``lines`` endpoints ordered SW->NE and joined into one polyline (the middle
    segment is the step face). Each vertex is stationed by projecting it onto the
    reference chain, so POPA is canonical and monotonic while the drawn line sits
    on the real, stepped dock edge. The NE terminus lands at POPA ~3360 ~= Dock
    No. 0 (the physical start of the dock stationing).

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
from app.crosswalk import dockno_to_popa, format_station, popa_to_dockno

BERTHS = GIS_DIR / "berths"
WAREHOUSES = GIS_DIR / "warehouses"
LINES = GIS_DIR / "lines"   # the two straight parts of the stepped bulkhead (ArcGIS)

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

    # --- chain face edges into one ordered STATIONING REFERENCE ----------
    # (berth water-side edges; correct along-wharf footage, wrong side — used
    # only to carry stationing onto the surveyed quay line below.)
    verts = []
    for f in faces.values():
        verts.extend(f)
    verts.sort(key=along)
    ref_chain = []
    for p in verts:
        if not ref_chain or _dist(ref_chain[-1], p) > 15.0:  # dedupe shared corners
            ref_chain.append(p)

    # --- stationing: anchor Berth 4 SW corner at ANCHOR_STATION -----------
    anchor_pt = faces[ANCHOR_BERTH][0]
    cum = [0.0]
    for i in range(1, len(ref_chain)):
        cum.append(cum[-1] + _dist(ref_chain[i - 1], ref_chain[i]))
    ai = min(range(len(ref_chain)), key=lambda i: _dist(ref_chain[i], anchor_pt))
    ref_stations = [ANCHOR_STATION + (cum[i] - cum[ai]) for i in range(len(ref_chain))]

    # --- dock-edge (quay face) geometry: the two-part stepped bulkhead --------
    # data/gis/lines.* is an ArcGIS "Distance And Direction" annotation whose two
    # records are the two STRAIGHT PARTS of the real bulkhead — verified against
    # the orthomosaic, each part lies on the concrete/water edge. The parts are
    # near-parallel but laterally offset by ~80 ft: the NE part juts that far
    # waterward of the SW part, a real STEP in the quay (a vessel berths against
    # whichever part it sits along). Order the four endpoints SW->NE and connect
    # them into one polyline — the middle segment, between the two inner
    # endpoints, is the step face. The centerline IS this bulkhead, stationed by
    # projecting each vertex onto the berth reference chain (so POPA is canonical
    # and monotonic, while the drawn geometry sits on the real two-part edge).
    quay, qcrs = _reader(LINES)
    q_to_b = Transformer.from_crs(qcrs, bcrs, always_xy=True)
    # Order each part's two points SW->NE, then place the SW part before the NE
    # part: ...far_SW, SW-inner, NE-inner (the step face), far_NE... A naive
    # global along-sort would swap the two inner step corners (their along values
    # differ by <1 ft across the near-perpendicular step) and cut a diagonal.
    parts = [sorted((q_to_b.transform(x, y) for x, y in sr.shape.points), key=along)
             for sr in quay.iterShapeRecords()]
    parts.sort(key=lambda seg: along(seg[0]))   # SW part first
    quay_pts = parts[0] + parts[1]

    # Station of any point = M at its foot on the reference chain.
    ref_edges = list(zip(zip(ref_chain[:-1], ref_stations[:-1]),
                         zip(ref_chain[1:], ref_stations[1:])))

    def _ref_station(P):
        best = None
        for (a, ma), (b, mb) in ref_edges:
            dx, dy = b[0] - a[0], b[1] - a[1]
            seg2 = dx * dx + dy * dy
            t = 0.0 if seg2 == 0 else ((P[0] - a[0]) * dx + (P[1] - a[1]) * dy) / seg2
            t = max(0.0, min(1.0, t))
            fx, fy = a[0] + t * dx, a[1] + t * dy
            d2 = (P[0] - fx) ** 2 + (P[1] - fy) ** 2
            if best is None or d2 < best[0]:
                best = (d2, ma + t * (mb - ma))
        return best[1]

    # Base geometry = the bulkhead polyline itself (SW terminus, the two inner
    # step corners, NE terminus — two straight parts joined by the step face),
    # each vertex stationed off the berth reference chain. The step face is
    # near-perpendicular, so its two corners sit at ~the same station; clamp to a
    # running max so POPA is non-decreasing SW->NE (the <1 ft blip is noise).
    base_chain = quay_pts
    base_st = []
    for p in base_chain:
        s = _ref_station(p)
        base_st.append(s if not base_st else max(s, base_st[-1]))

    # Densify: drop a stationing node at every berth reference station, placed by
    # interpolating ALONG the bulkhead (so it stays exactly on the straight
    # part), giving canonical nodes at the berth corners (351, 1107, ...) on top
    # of the four bulkhead points. The step face spans ~0 station, so nothing
    # interpolates across it — every added node lands cleanly on one part.
    def _interp_on(t):
        for i in range(len(base_st) - 1):
            a, b = base_st[i], base_st[i + 1]
            if b > a and a <= t <= b:
                r = (t - a) / (b - a)
                return (base_chain[i][0] + r * (base_chain[i + 1][0] - base_chain[i][0]),
                        base_chain[i][1] + r * (base_chain[i + 1][1] - base_chain[i][1]))
        return None

    nodes = list(zip(base_chain, base_st))
    for m in ref_stations:
        if base_st[0] + 1.0 < m < base_st[-1] - 1.0:
            xy = _interp_on(m)
            if xy is not None:
                nodes.append((xy, m))
    nodes.sort(key=lambda cs: cs[1])
    chain = [c for c, _ in nodes]
    stations = [s for _, s in nodes]

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

    # Yellow quay-face ticks — the exhibit's SECOND tick series: the port's
    # **Dock No.** stationing, the ruler painted on the wharf deck. Anchored at
    # Dock No. 0 (= POPA 3365 via the crosswalk, the physical NE start of the
    # dock stationing) and increasing SW, one tick every 50 ft. Distinct from the
    # POPA series above: these sit ON the wharf — each tick runs from the quay
    # edge landward onto the apron (water_ft=0, no part in the channel), vs. the
    # long POPA lines that run out into the water. Labelled round-hundred ticks
    # run nearly the full berth depth and carry their Dock No. at the landward
    # (top) edge; the minor 50s are short unlabelled ticks at the quay. All
    # Dock No. <-> POPA math goes through app.crosswalk — never inline here.
    BERTH_DEPTH_FT = 255.0              # tick reach landward from the quay face

    def yellow_feature(popa, dockno, *, major):
        """One Dock No. tick: interpolate the quay point at POPA ``popa``, draw it
        landward, and label it with its ``dockno``. Returns None if the point
        falls off the line. Single source of the Feature schema so the loop and
        the end-cap tick can't drift apart."""
        xy = interp_xy(popa)
        if xy is None:
            return None
        line = marker_line(xy[0], xy[1],
                           land_ft=BERTH_DEPTH_FT if major else 45,
                           water_ft=0)
        return {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": line},
            "properties": {
                "dockno": round(dockno, 1),
                "major": major,
                "label": str(int(round(dockno))) if major else None,
            },
        }

    # Dock No. grows as POPA shrinks, so the NE end is the smallest Dock No.
    dn_ne = popa_to_dockno(popa_hi)
    dn_sw = popa_to_dockno(popa_lo)
    first_dn = int(math.ceil(dn_ne / 50.0)) * 50      # first round 50 inside coverage
    last_dn = int(math.floor(dn_sw / 50.0)) * 50

    yellow_marks = []
    for dn in range(first_dn, last_dn + 1, 50):
        feat = yellow_feature(dockno_to_popa(float(dn)), float(dn), major=(dn % 100 == 0))
        if feat is not None:
            yellow_marks.append(feat)

    # Close the ruler exactly on the SW quay terminus, even though it is not a
    # round 50 ft of Dock No.; without this the last tick stops short of the end.
    if dn_sw - last_dn > 1.0:
        if yellow_marks and dn_sw - yellow_marks[-1]["properties"]["dockno"] < 50.0:
            yellow_marks.pop()    # avoid an end-cap landing on top of the last 50
        feat = yellow_feature(popa_lo, dn_sw, major=True)
        if feat is not None:
            yellow_marks.append(feat)
    (STATIC_GIS / "yellow_markers.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": yellow_marks})
    )

    # Per-berth station range, from the stationing REFERENCE (the berth-edge
    # chain that carries canonical berth_leng footage), so berth extents stay
    # tied to the published lengths even though the drawn line is the quay face.
    def nearest_station(pt):
        i = min(range(len(ref_chain)), key=lambda i: _dist(ref_chain[i], pt))
        return ref_stations[i]

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
