# Hydrographic depth surveys

Raw condition-survey artifacts land here. They are **git-ignored** (large; see
`.gitignore`) — only the *reduced* per-station controlling-depth profile is kept,
in the database (`depth_survey` / `depth_segment`, migration 0012).

## Format

POPA condition surveys arrive as an `.XYZ` point cloud — whitespace-separated
`X Y Z` per line:

- **X, Y** — easting / northing in **Texas South Central State Plane, NAD83(2011),
  US survey feet (EPSG:2278)**.
- **Z** — water depth in **feet below the survey datum** (e.g. MLLW).

Filenames lead with the survey date as `MMDDYYYY`, e.g.
`07092023CND_POPA_B1-6_2x2.XYZ` = 2023-07-09, Berths 1–6, 2×2 ft grid. The
plan-view PDFs (`*_planviews.zip`) are the vendor's contoured reference — use
them to sanity-check the computed profile; they are not ingested.

## Loading a survey

Depths change constantly, so each survey is a **new versioned row** and the draft
gate reads the latest active one.

- **Operators:** upload the `.XYZ` from the map UI (the *Depth surveys* panel →
  drag-and-drop), which `POST`s to `/depth/surveys`.
- **Initial load / ops:** `python -m app.depth.ingest data/surveys/<file>.XYZ
  [--date MM/DD/YYYY] [--datum MLLW]`.

The reduction (project each sounding onto the wharf centerline → POPA station,
clip to the berthing zone, bin, keep the shallowest = controlling depth) runs in
PostGIS — see `app/depth/ingest.py`. Tune the bin width and berthing-zone clip
via the `DEPTH_*` settings (`app/config.py`).
