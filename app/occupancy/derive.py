"""Orchestrate occupancy derivation: position samples -> observed reservations.

For each vessel: classify its track (alongside via the swappable buffer),
detect berthing events (hysteresis), project bow/stern to a POPA station range,
and upsert an ``observed`` reservation keyed on a stable ``derived_key`` so
re-running over the same window UPDATEs instead of duplicating.

These rows are always status ``observed`` / source ``ais`` and are *allowed* to
overlap planned reservations — that overlap is the signal step 6 surfaces, not
something to block (the exclusion constraint is confirmed-only by design).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crosswalk import geo_to_station
from app.occupancy.alongside import PosRow, load_samples
from app.occupancy.detect import Sample, detect_berthings
from app.occupancy.project import FEET_PER_M, project_bow_stern

# Direction convention: POPA station increases in one geographic direction along
# the wharf. We label a vessel whose bow points toward INCREASING station as
# heading upstream. TODO: verify this against the Sabine-Neches channel axis and
# the port's convention; flip this single flag if it's backwards — no other code
# encodes the direction sense.
UPSTREAM_TOWARD_INCREASING_STATION = True

# Default LOA (m) for the no-dimensions fallback so a derived range is never
# zero-width. Roughly a small coaster; flagged low-confidence regardless.
_FALLBACK_LOA_M = 100.0


@dataclass(frozen=True)
class DerivedReservation:
    vessel_id: int
    station_lo: float
    station_hi: float
    t_start: dt.datetime
    t_end: dt.datetime
    direction: str | None
    confident: bool
    open_ended: bool
    reservation_id: int
    inserted: bool


def _vessel_dims(session: Session, vessel_ids: list[int]) -> dict[int, dict]:
    if not vessel_ids:
        return {}
    rows = session.execute(
        text(
            "SELECT id, loa, dim_a, dim_b FROM vessel WHERE id = ANY(:ids)"
        ),
        {"ids": vessel_ids},
    )
    return {
        r.id: {
            "loa": None if r.loa is None else float(r.loa),
            "dim_a": None if r.dim_a is None else float(r.dim_a),
            "dim_b": None if r.dim_b is None else float(r.dim_b),
        }
        for r in rows
    }


def _representative(track: list[PosRow], t_start: dt.datetime, t_end: dt.datetime) -> PosRow:
    """Pick the in-window fix nearest the midpoint, preferring one with a real
    heading, then one with a COG, then anything."""
    mid = t_start + (t_end - t_start) / 2
    window = [p for p in track if t_start <= p.ts <= t_end] or track

    def key(p: PosRow):
        return abs((p.ts - mid).total_seconds())

    for predicate in (
        lambda p: p.heading is not None,
        lambda p: p.cog is not None,
        lambda p: True,
    ):
        candidates = [p for p in window if predicate(p)]
        if candidates:
            return min(candidates, key=key)
    return window[0]


def _station_range_and_direction(
    session: Session,
    rep: PosRow,
    dims: dict,
    segment_id: int | None,
) -> tuple[float, float, str | None, bool]:
    """Return (station_lo, station_hi, direction, confident)."""
    heading = rep.heading if rep.heading is not None else rep.cog
    bs = project_bow_stern(
        rep.lat,
        rep.lon,
        heading_deg=heading,
        dim_a_m=dims.get("dim_a"),
        dim_b_m=dims.get("dim_b"),
        loa_m=dims.get("loa"),
    )

    if not bs.confident:
        # No usable orientation: centre an LOA-wide interval on the antenna.
        sta = geo_to_station(session, rep.lat, rep.lon, segment_id)
        loa_ft = (dims.get("loa") or _FALLBACK_LOA_M) * FEET_PER_M
        half = loa_ft / 2.0
        return sta - half, sta + half, None, False

    bow_sta = geo_to_station(session, bs.bow_lat, bs.bow_lon, segment_id)
    stern_sta = geo_to_station(session, bs.stern_lat, bs.stern_lon, segment_id)

    bow_toward_increasing = bow_sta >= stern_sta
    upstream = bow_toward_increasing == UPSTREAM_TOWARD_INCREASING_STATION
    direction = "upstream" if upstream else "downstream"

    lo, hi = sorted((bow_sta, stern_sta))
    return lo, hi, direction, True


def _upsert(
    session: Session,
    *,
    vessel_id: int,
    lo: float,
    hi: float,
    t_start: dt.datetime,
    t_end: dt.datetime,
    direction: str | None,
    confident: bool,
    open_ended: bool,
) -> tuple[int, bool]:
    derived_key = f"v{vessel_id}:{t_start.isoformat()}"
    note_bits = ["derived from AIS"]
    if not confident:
        note_bits.append("low confidence (no heading/dims)")
    if open_ended:
        note_bits.append("still alongside (open-ended)")
    notes = "; ".join(note_bits)

    row = session.execute(
        text(
            """
            INSERT INTO reservation
                (vessel_id, type, station_range, time_range, direction,
                 status, source, derived_key, notes, created_at)
            VALUES
                (:vessel_id, 'vessel',
                 numrange(:lo, :hi, '[]'),
                 tstzrange(:t_start, :t_end, '[)'),
                 :direction, 'observed', 'ais', :derived_key, :notes, now())
            ON CONFLICT (derived_key) WHERE derived_key IS NOT NULL
            DO UPDATE SET
                station_range = EXCLUDED.station_range,
                time_range = EXCLUDED.time_range,
                direction = EXCLUDED.direction,
                notes = EXCLUDED.notes
            RETURNING id, (xmax = 0) AS inserted
            """
        ),
        {
            "vessel_id": vessel_id,
            "lo": lo,
            "hi": hi,
            "t_start": t_start,
            "t_end": t_end,
            "direction": direction,
            "derived_key": derived_key,
            "notes": notes,
        },
    ).one()
    return int(row.id), bool(row.inserted)


def derive_observed(
    session: Session,
    *,
    segment_id: int | None = None,
    since: dt.datetime | None = None,
) -> list[DerivedReservation]:
    """Derive observed reservations from landed position reports. Idempotent.

    Does NOT commit — the caller owns the transaction boundary (``run.py``
    commits; tests roll back). Idempotency holds within a transaction too: the
    ``ON CONFLICT`` upsert sees rows inserted earlier in the same transaction.
    """
    settings = get_settings()
    grouped = load_samples(
        session,
        buffer_m=settings.berth_buffer_m,
        segment_id=segment_id,
        since=since,
    )
    dims_by_vessel = _vessel_dims(session, list(grouped))

    enter = settings.berth_enter_sog_kn
    depart = settings.berth_depart_sog_kn
    dwell = dt.timedelta(minutes=settings.berth_dwell_min)
    gap = dt.timedelta(minutes=settings.berth_depart_gap_min)

    results: list[DerivedReservation] = []
    for vessel_id, track in grouped.items():
        events = detect_berthings(
            [Sample(p.ts, p.alongside, p.sog, p.nav_status) for p in track],
            enter_sog=enter,
            depart_sog=depart,
            dwell=dwell,
            depart_gap=gap,
        )
        dims = dims_by_vessel.get(vessel_id, {})
        for ev in events:
            rep = _representative(track, ev.t_start, ev.t_end)
            lo, hi, direction, confident = _station_range_and_direction(
                session, rep, dims, segment_id
            )
            res_id, inserted = _upsert(
                session,
                vessel_id=vessel_id,
                lo=lo,
                hi=hi,
                t_start=ev.t_start,
                t_end=ev.t_end,
                direction=direction,
                confident=confident,
                open_ended=ev.open_ended,
            )
            results.append(
                DerivedReservation(
                    vessel_id=vessel_id,
                    station_lo=lo,
                    station_hi=hi,
                    t_start=ev.t_start,
                    t_end=ev.t_end,
                    direction=direction,
                    confident=confident,
                    open_ended=ev.open_ended,
                    reservation_id=res_id,
                    inserted=inserted,
                )
            )

    return results
