# Stationing Crosswalk & Wharf Geometry

*Reference for `app/crosswalk.py`, `app/seed/wharf_seed.py`, `data/gis/build_centerline.py`, `data/gis/to_geojson.py`, `data/gis/` shapefiles. POPA wharf data layer — roadmap → [`../PLAN.md`](../PLAN.md); design contract → [`../../CLAUDE.md`](../../CLAUDE.md).*

## Purpose

The wharf is referenced **linearly**: every position is a distance in feet along the quay face, measured in the canonical **POPA stationing** system. Three external systems (Corps/USACE station numbers, painted Dock No. markers, and the raw geographic lat/lon of AIS fixes) each require a reversible conversion to that canonical measure.

This section owns all of that math. `app/crosswalk.py` is the single module all position I/O goes through — no other module may inline a stationing transform. The `data/gis/` scripts derive the real measured centerline geometry from the port's ArcGIS exports, and `app/seed/wharf_seed.py` seeds that geometry (plus the berth catalog) into the database.

## Files

| File | What it does |
| --- | --- |
| `app/crosswalk.py` | Defines `AffineParams(scale, offset)` and default constants for Corps and Dock No. Exposes `from_popa` / `to_popa` on the dataclass; convenience wrappers `popa_to_corps`, `corps_to_popa`, `popa_to_dockno`, `dockno_to_popa`. DB-backed `segment_dockno_params` / `segment_corps_params` load per-segment affine params (falling back to published defaults when no segment is seeded). `parse_station` / `format_station` handle `NN+NN` stationing notation. `geo_to_station` calls PostGIS `ST_InterpolatePoint` to project lat/lon → POPA station. `project_to_station` is the pure-Python offline counterpart for tests and tooling. |
| `app/seed/wharf_seed.py` | Reads `data/gis/centerline_vertices.json` (or a placeholder), `data/gis/apron_polygon.json`, and `app/static/gis/berth_stations.json`; upserts one `wharf_segment` row (measured LINESTRINGM + apron polygon + affine params) and all named berths. Idempotent — safe to re-run after a finer survey. |
| `data/gis/build_centerline.py` | Derives the canonical quay-face geometry and POPA stationing from the ArcGIS shapefiles. Produces: `centerline_vertices.json`, `apron_polygon.json`, `app/static/gis/centerline.geojson`, `app/static/gis/feet_markers.geojson`, `app/static/gis/yellow_markers.geojson`, `app/static/gis/berth_stations.json`. Uses `app.crosswalk` for all Dock No. ↔ POPA math — no inline transforms. |
| `data/gis/to_geojson.py` | Reads `berths.shp` and `warehouses.shp`, reprojects each from its native CRS to WGS84, and writes `app/static/gis/berths.geojson` and `app/static/gis/warehouses.geojson` for the Leaflet map. |
| `data/gis/berths.shp` + sidecar files | ArcGIS export of berth polygons (Texas South Central State Plane US-ft, EPSG:2278). Each polygon is a berthing-WATER rectangle; attributes include `name`, `width` (along-shore feet), `berth_leng` (running POPA footage at the SW edge). Source of stationing reference and berth catalog. |
| `data/gis/lines.shp` + sidecar files | ArcGIS "Distance And Direction" annotation: two records, each a straight segment of the real bulkhead. These carry the actual quay-face **geometry** (verified against the orthomosaic). Used by `build_centerline.py` to place the centerline on the real concrete/water edge. |
| `data/gis/warehouses.shp` + sidecar files | Warehouse footprints, used only to derive the water-side normal direction (which side of the quay is the channel). |
| `data/gis/centerline_vertices.json` | Output of `build_centerline.py`. `[[lon, lat, M], ...]` where `M` is POPA station in feet. Consumed by `wharf_seed.py` to build the `LINESTRINGM`. |
| `data/gis/apron_polygon.json` | Output of `build_centerline.py`. Closed ring `[[lon, lat], ...]` of the berthing-zone polygon (40 ft inland + 250 ft waterward of the quay face). Seeded into `wharf_segment.apron`. |

## Key concepts & invariants

**Linear stationing model.** The wharf has a single canonical measure: POPA station, in feet, increasing SW→NE along the quay face. Every reservation is a `[stern_sta, bow_sta]` interval in POPA feet. "Berths" are named convenience labels for station ranges — the allocation unit is always the interval, never the berth id.

**Affine reconciliation.** Each external system is an exact affine function of POPA station:

```
external_value = scale × popa_station + offset
```

Published defaults (verified against the port's crosswalk exhibit):

| System | Scale | Offset | Notes |
| --- | --- | --- | --- |
| Corps/USACE | 1.0 | +12 040.65 ft | Same direction as POPA; constant offset |
| Dock No. | −1.0 | +3 365 ft | **Reversed** — Dock No. 0 is at POPA ≈ 3365, increases toward SW |

The parameters are stored per `wharf_segment` (`corps_scale`, `corps_offset`, `dockno_scale`, `dockno_offset`) so a future segment with different offsets generalises without code changes. `segment_dockno_params` / `segment_corps_params` in `crosswalk.py` load them from the DB; all callers use these, never hard-coded literals.

**Because Dock No. is reversed**, the stern end (larger Dock No.) maps to the lower POPA bound. The edit surface enters station ranges as Dock No. (`station_lo` = stern / larger Dock No., `station_hi` = bow / smaller Dock No.) and converts server-side via `AffineParams.to_popa`. The canonical store and all conflict logic stay in POPA.

**ALL stationing math stays in `app/crosswalk.py`.** No other module may inline a `scale × x + offset` transform or hard-code a numeric offset. `build_centerline.py` imports `app.crosswalk` for its Dock No. tick math rather than repeating the constants. This is the project's strictest single-module invariant.

**Measured `M`-linestring and `ST_InterpolatePoint`.** The centerline geometry stored in `wharf_segment.geom` is a PostGIS `LINESTRINGM` (SRID 4326) where the `M` coordinate of each vertex equals its POPA station in feet. `ST_InterpolatePoint(geom, point)` returns the interpolated `M` at the foot of the closest point on the line — this is how a raw AIS lat/lon becomes a canonical POPA station with no additional arithmetic. `geo_to_station` in `crosswalk.py` wraps this call; `project_to_station` is the pure-Python offline equivalent (accurate to sub-foot at wharf scale via equirectangular projection).

**Centerline construction method.** The ArcGIS berth polygons are water-side rectangles; their edges give correct along-shore **stationing** (the `berth_leng` chain anchored at Berth 4 SW = 351 ft) but are geometrically displaced from the real quay face. `build_centerline.py` separates these two concerns:
1. *Stationing reference*: chain the berth water-side edges and anchor at `ANCHOR_STATION = 351.0` → monotonic POPA assignments.
2. *Quay-face geometry*: use `lines.shp` (two straight bulkhead segments verified on the orthomosaic, joined through the step face) as the actual drawn line.
3. *Combine*: project each `lines.shp` vertex onto the stationing reference to get its POPA station; densify with berth-corner nodes. The resulting polyline sits on the real stepped dock edge and carries canonical, monotonic POPA.

**Apron polygon.** The occupancy "alongside" test uses `wharf_segment.apron` (a polygon strip: 40 ft inland, 250 ft waterward of the quay face) rather than the older symmetric centerline buffer. Built by `build_centerline.py` from the water-side unit normal; seeded by `wharf_seed.py`. When the apron is NULL (not yet seeded) the occupancy code falls back to the centerline buffer — see `app/occupancy/alongside.py`.

**Station-range input convention.** Operators enter station ranges in Dock No. feet (the yellow markers painted on the wharf deck). Conversion to canonical POPA happens server-side in `app/edit.py` via `segment_dockno_params(session).to_popa(...)`. The UI never converts. `/reservations` returns both `station_lo/hi` (POPA) and `station_lo/hi_dock` so the edit forms can pre-fill Dock No. values.

## Connections

- **Upstream of everything positional**: `app/occupancy/project.py` calls `geo_to_station` to derive bow/stern POPA from AIS lat/lon. `app/edit.py` calls `segment_dockno_params` to convert operator-entered Dock No. to POPA on create/edit. `app/feasibility.py` uses POPA ranges for the free-space calculation.
- **Downstream of `data/gis/build_centerline.py`**: `app/seed/wharf_seed.py` consumes its JSON outputs; the Leaflet map in `app/static/index.html` loads the GeoJSON files it writes to `app/static/gis/`.
- **Tested by**: `tests/test_crosswalk.py` (pure affine math + notation parsing) and `tests/test_geo_station_real.py` (PostGIS `ST_InterpolatePoint` against the seeded centerline — requires a live DB).
- See also: [`core.md`](core.md) (DB session used by the crosswalk DB helpers), [`migrations.md`](migrations.md) (migration 0001 creates `wharf_segment`; migration 0005 adds `apron`; migration 0006 creates `berth`).
