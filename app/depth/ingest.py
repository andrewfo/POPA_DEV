"""Ingest a hydrographic ``.XYZ`` survey into a per-station controlling-depth
profile.

The reduction runs in **PostGIS**, not Python — the soundings are in a planar
CRS (Texas South Central State Plane ftUS, EPSG:2278) and PostGIS already knows
that CRS and carries the authoritative geo->station path
(``ST_InterpolatePoint`` on the measured centerline, exactly as
``crosswalk.geo_to_station``). So we:

1. ``COPY`` the parsed soundings into a TEMP table (fast for ~400k rows).
2. In one set-based pass: transform each sounding 2278->4326, project it onto the
   nearest ``wharf_segment`` centerline to get its POPA station, clip to the
   berthing zone (the digitized apron, plus a perpendicular-offset band that
   drops wall-toe shallows and far-channel points), bin by station, and keep the
   **shallowest** sounding per bin (controlling = governing depth).
3. Store the bins as ``depth_segment`` rows under a new dated ``depth_survey``,
   and (same pass) a 2-D ``depth_cell`` grid binned by station AND offset-from-quay
   for the map's cross-section overlay — the gate reads ``depth_segment``, the
   cells are visualization only (a segment's depth is the min over its cells).

Surveys are VERSIONED: every call inserts a NEW survey (depths change
constantly), and the draft gate reads the latest active one. This keeps no raw
soundings — only the reduced profile.

Runnable for the initial load:  ``python -m app.depth.ingest <file.XYZ>``
(the operator-facing path is the browser upload -> ``POST /depth/surveys``).
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.depth.gate import FEET_PER_M
from app.depth.parse import parse_survey_date, parse_xyz


def import_survey(
    session: Session,
    rows: Iterable[tuple[float, float, float]],
    *,
    source_file: str | None = None,
    surveyed_at: dt.date,
    datum: str | None = None,
    srid: int = 2278,
    bin_ft: float | None = None,
    toe_offset_ft: float | None = None,
    max_offset_ft: float | None = None,
) -> dict:
    """Reduce ``rows`` of ``(x, y, z)`` soundings to a new active depth survey.

    Does NOT commit — the caller (the upload endpoint via ``do_write``, or the
    CLI) owns the transaction. Raises ``ValueError`` (-> 422) when no sounding
    survives the berthing-zone clip (wrong CRS, extent off the wharf, or an empty
    file), so a useless survey is never recorded.
    """
    s = get_settings()
    bin_ft = float(bin_ft if bin_ft is not None else s.depth_bin_ft)
    toe_ft = float(toe_offset_ft if toe_offset_ft is not None else s.depth_toe_offset_ft)
    max_ft = float(max_offset_ft if max_offset_ft is not None else s.depth_max_offset_ft)
    off_bin_ft = float(s.depth_offset_bin_ft)
    toe_m = toe_ft / FEET_PER_M
    max_m = max_ft / FEET_PER_M

    # 1) New survey row (aggregates filled in after the reduction).
    sid = session.execute(
        text(
            """
            INSERT INTO depth_survey
                (surveyed_at, source_file, srid, datum, bin_ft, active)
            VALUES (:surveyed_at, :source_file, :srid, :datum, :bin_ft, true)
            RETURNING id
            """
        ),
        {
            "surveyed_at": surveyed_at,
            "source_file": source_file,
            "srid": srid,
            "datum": datum,
            "bin_ft": bin_ft,
        },
    ).scalar_one()

    # 2) COPY soundings into a transaction-scoped temp table (psycopg3 fast path).
    raw = session.connection().connection.driver_connection
    n_parsed = 0
    with raw.cursor() as cur:
        # IF NOT EXISTS + TRUNCATE so a second import within one uncommitted
        # transaction (e.g. a test) reuses the table rather than colliding; real
        # runs commit between imports, so ON COMMIT DROP clears it each time.
        cur.execute(
            "CREATE TEMP TABLE IF NOT EXISTS tmp_sounding "
            "(x double precision, y double precision, z double precision) "
            "ON COMMIT DROP"
        )
        cur.execute("TRUNCATE tmp_sounding")
        with cur.copy("COPY tmp_sounding (x, y, z) FROM STDIN") as copy:
            for x, y, z in rows:
                copy.write_row((x, y, z))
                n_parsed += 1

    # 3) Project -> clip, once, into a temp table carrying each surviving
    #    sounding's POPA station, perpendicular offset (feet), and depth. Both the
    #    per-station controlling profile (depth_segment) and the 2-D cross-section
    #    grid (depth_cell) are then binned from this same set, so they can never
    #    disagree (a segment's controlling depth is the min over its cells) and
    #    the expensive nearest-segment projection runs only once.
    session.execute(
        text(
            """
            CREATE TEMP TABLE IF NOT EXISTS tmp_proj
            (sta double precision, off_ft double precision, z double precision)
            ON COMMIT DROP
            """
        )
    )
    session.execute(text("TRUNCATE tmp_proj"))
    session.execute(
        text(
            """
            INSERT INTO tmp_proj (sta, off_ft, z)
            SELECT p.sta, p.off_m * :ft_per_m, p.z
            FROM (
                SELECT
                    t.z AS z,
                    ST_InterpolatePoint(seg.geom, seg.pt) AS sta,
                    ST_Distance(seg.geom2d::geography, seg.pt::geography) AS off_m,
                    seg.apron AS apron,
                    seg.pt AS pt
                FROM tmp_sounding t
                CROSS JOIN LATERAL (
                    SELECT ws.geom AS geom,
                           ST_Force2D(ws.geom) AS geom2d,
                           ws.apron AS apron,
                           ST_Transform(
                               ST_SetSRID(ST_MakePoint(t.x, t.y), :srid), 4326
                           ) AS pt
                    FROM wharf_segment ws
                    ORDER BY ws.geom <-> ST_Transform(
                        ST_SetSRID(ST_MakePoint(t.x, t.y), :srid), 4326
                    )
                    LIMIT 1
                ) seg
            ) p
            WHERE p.sta IS NOT NULL
              AND (p.apron IS NULL OR ST_Contains(p.apron, p.pt))
              AND p.off_m BETWEEN :toe_m AND :max_m
            """
        ),
        {"srid": srid, "ft_per_m": FEET_PER_M, "toe_m": toe_m, "max_m": max_m},
    )

    # 3a) Per-station controlling profile (the draft gate reads this).
    result = session.execute(
        text(
            """
            INSERT INTO depth_segment
                (survey_id, popa_range, controlling_depth_ft, point_count)
            SELECT
                :sid,
                numrange(CAST(floor(sta / :bin) * :bin AS numeric),
                         CAST(floor(sta / :bin) * :bin + :bin AS numeric), '[)'),
                min(z),
                count(*)
            FROM tmp_proj
            GROUP BY floor(sta / :bin)
            """
        ),
        {"sid": sid, "bin": bin_ft},
    )
    segment_count = result.rowcount

    # 3b) 2-D cross-section grid (station × offset), for the map overlay only.
    session.execute(
        text(
            """
            INSERT INTO depth_cell
                (survey_id, popa_range, offset_range, controlling_depth_ft, point_count)
            SELECT
                :sid,
                numrange(CAST(floor(sta / :bin) * :bin AS numeric),
                         CAST(floor(sta / :bin) * :bin + :bin AS numeric), '[)'),
                numrange(CAST(floor(off_ft / :obin) * :obin AS numeric),
                         CAST(floor(off_ft / :obin) * :obin + :obin AS numeric), '[)'),
                min(z),
                count(*)
            FROM tmp_proj
            GROUP BY floor(sta / :bin), floor(off_ft / :obin)
            """
        ),
        {"sid": sid, "bin": bin_ft, "obin": off_bin_ft},
    )

    if not segment_count:
        # Nothing survived the clip: almost always a CRS mismatch or a survey that
        # doesn't overlap the wharf. Roll back the empty survey via ValueError.
        raise ValueError(
            "no soundings fell in the berthing zone — check the survey CRS "
            f"(expected EPSG:{srid}) and that it covers the wharf"
        )

    # 4) Backfill the survey's headline aggregates from its segments.
    session.execute(
        text(
            """
            UPDATE depth_survey s SET
                point_count = agg.n,
                station_min = agg.lo,
                station_max = agg.hi,
                min_depth_ft = agg.mind
            FROM (
                SELECT sum(point_count) AS n,
                       min(lower(popa_range)) AS lo,
                       max(upper(popa_range)) AS hi,
                       min(controlling_depth_ft) AS mind
                FROM depth_segment WHERE survey_id = :sid
            ) agg
            WHERE s.id = :sid
            """
        ),
        {"sid": sid},
    )

    summary = session.execute(
        text(
            """
            SELECT surveyed_at, point_count, station_min, station_max, min_depth_ft
            FROM depth_survey WHERE id = :sid
            """
        ),
        {"sid": sid},
    ).first()

    return {
        "id": int(sid),
        "surveyed_at": summary.surveyed_at.isoformat(),
        "source_file": source_file,
        "parsed_points": n_parsed,
        "binned_points": int(summary.point_count) if summary.point_count else 0,
        "segment_count": int(segment_count),
        "station_min": float(summary.station_min) if summary.station_min is not None else None,
        "station_max": float(summary.station_max) if summary.station_max is not None else None,
        "min_depth_ft": float(summary.min_depth_ft) if summary.min_depth_ft is not None else None,
        "bin_ft": bin_ft,
    }


def main() -> None:
    """Initial-load CLI:  python -m app.depth.ingest <file.XYZ> [--date MM/DD/YYYY]
    [--datum MLLW]. The operator-facing path is the browser upload; this exists
    to seed the first survey after a deploy."""
    import argparse

    from app.db import SessionLocal

    ap = argparse.ArgumentParser(description="Ingest a .XYZ depth survey.")
    ap.add_argument("path", help="path to the .XYZ sounding file")
    ap.add_argument("--date", help="survey date MM/DD/YYYY (else parsed from filename)")
    ap.add_argument("--datum", default=None, help="vertical datum, e.g. MLLW")
    ap.add_argument("--srid", type=int, default=2278, help="planar CRS of the soundings")
    args = ap.parse_args()

    if args.date:
        surveyed_at = dt.datetime.strptime(args.date, "%m/%d/%Y").date()
    else:
        surveyed_at = parse_survey_date(args.path) or dt.date.today()

    session = SessionLocal()
    try:
        with open(args.path, encoding="utf-8", errors="ignore") as fh:
            summary = import_survey(
                session,
                parse_xyz(fh),
                source_file=args.path.replace("\\", "/").split("/")[-1],
                surveyed_at=surveyed_at,
                datum=args.datum,
                srid=args.srid,
            )
        session.commit()
        print(
            f"Imported survey id={summary['id']} ({summary['surveyed_at']}): "
            f"{summary['parsed_points']} soundings -> {summary['segment_count']} "
            f"station bins, shallowest {summary['min_depth_ft']} ft, "
            f"POPA {summary['station_min']}..{summary['station_max']}"
        )
    finally:
        session.close()


if __name__ == "__main__":
    main()
