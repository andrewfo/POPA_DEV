"""AIS verification — checking operator placements against observed reality.

Reframed from the original "request→AIS reconciliation" idea (see
``reconciliation-verification`` in the project memory / CLAUDE.md step 7): AIS
cannot *place* a not-yet-arrived ship — it only knows where a vessel **is now /
was**, and its derived station ranges are **approximate** (a ``confident`` flag,
``observed`` never blocks ``confirmed``), not survey-grade. So **placement stays
the operator's job** (and a future optimizer's). This module *verifies* those
placements after the fact: once a vessel shows up on AIS, did the plan match
reality?

Like ``find_conflicts`` it only **surfaces** findings as a query — it does **not**
mutate status (there is also no ``arrived`` status to advance into; the lifecycle
is observed/requested/tentative/confirmed/cancelled/completed). The operator acts
on the findings through the edit surface.

The matching key is **vessel identity + TIME overlap**, deliberately NOT the
step-6 station-``&&`` conflict join: a ``requested`` row carries an **empty**
``station_range`` that can never match ``&&``, so conflict detection structurally
can't see arrivals. Identity is the shared ``vessel`` row (AIS upserts by MMSI,
intake by IMO; a vessel with both reconciles to one row). A planned row with no
``vessel_id`` (name-only intake) can't be matched and is left out.

Two layers, mirroring ``app/conflicts.py``:

* **Pure helpers** (``classify_planned`` / ``where_planned``) — unit-tested, no
  database. States per planned reservation (``vessel_id`` set, non-empty window):
    - ``arrived``  — an ``observed`` AIS berthing for the same vessel overlaps the
      planned window;
    - ``no_show``  — the window has fully passed (``t_end <= as_of``) with none seen;
    - ``awaiting`` — window current or future, nothing observed yet.
  A *placed* planned row also carries ``where_planned``: does the observed station
  range overlap the planned one (``True``/``False``), or ``None`` when unplaced —
  the same signal as step 6's observed-vs-planned conflict, surfaced inline.
* **The DB query** (``verify``) — a LATERAL join from each planned row to its best
  matching ``observed`` row, plus a second pass for ``unplanned`` observed
  berthings (a vessel alongside that no planned row covers — a walk-in the office
  never logged). Dock No. is converted server-side, like the other endpoints.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.conflicts import station_overlaps
from app.crosswalk import segment_dockno_params

# Reservations an operator put on the board — the placements we verify against AIS.
PLANNED_STATUSES = ("requested", "tentative", "confirmed")


# ---------------------------------------------------------------------------
# Pure helpers — no database; model the verification states exactly
# ---------------------------------------------------------------------------
def classify_planned(
    t_start: Any, t_end: Any, has_observed: bool, as_of: datetime
) -> str:
    """Classify one planned reservation against AIS reality.

    * ``arrived``  — a matching ``observed`` berthing exists (``has_observed``).
    * ``no_show``  — no berthing and the window has fully elapsed
      (``t_end`` is set and ``<= as_of``). An open-ended window (``t_end is None``)
      is never a no-show.
    * ``awaiting`` — otherwise (the window is current/future, nothing seen yet).
    """
    if has_observed:
        return "arrived"
    if t_end is not None and t_end <= as_of:
        return "no_show"
    return "awaiting"


def where_planned(
    p_sta_lo: float | None,
    p_sta_hi: float | None,
    o_sta_lo: float | None,
    o_sta_hi: float | None,
) -> bool | None:
    """Did the vessel berth where it was planned? ``True``/``False`` when both the
    planned row is placed and an observed range exists, else ``None`` (can't tell —
    an unplaced ``requested`` row has no station range to compare). Reuses the
    step-6 closed-interval ``station_overlaps`` so the inclusivity matches."""
    if p_sta_lo is None or o_sta_lo is None:
        return None
    return station_overlaps(p_sta_lo, p_sta_hi, o_sta_lo, o_sta_hi)


def expiry_action(arrived: bool, feed_alive: bool, status: str) -> str | None:
    """What to do with a *stale* planned row (window past + grace). Returns the
    action, deliberately split so a missing AIS feed can never be read as a no-show:

    * ``"completed"`` — AIS observed the vessel berth (``arrived``); the booking ran
      its course. Evidence-backed, so it applies to **any** planned status.
    * ``None`` — no berthing **and** the AIS feed showed no traffic at all during the
      window (``not feed_alive``). A dead feed is indistinguishable from a no-show on
      a per-vessel basis, so this is *not negative evidence*: leave the row untouched
      and let a later sweep (once the feed is back) judge it. **No data ≠ no-show.**
    * ``"cancelled"`` — a genuine no-show (the feed was live, the vessel just never
      came) of a ``requested``/``tentative`` row. These are cheap to re-create, so
      auto-archiving them is safe.
    * ``"flagged"`` — a genuine no-show of a **confirmed** booking. Marine ETAs slip
      by hours; auto-cancelling a confirmed row on a slipped ETA destroys an operator
      commitment. So it is *not* cancelled — it is left ``confirmed`` and flagged
      (a one-time audit note) for the operator to resolve.

    The terminal statuses (``completed``/``cancelled``) sit outside the
    confirmed-only no-overlap exclusion constraint, so archiving can never raise an
    IntegrityError (it only ever *removes* a row from the constraint's set)."""
    if arrived:
        return "completed"
    if not feed_alive:
        return None
    if status == "confirmed":
        return "flagged"
    return "cancelled"


# ---------------------------------------------------------------------------
# DB query — Postgres is the source of truth
# ---------------------------------------------------------------------------
# Each planned row LEFT JOINed to its best-matching observed berthing (same
# vessel, time-overlapping). "Best" = prefer a still-ongoing berthing (open upper),
# then the most recent — the exact pick only affects which observed window we echo
# back; arrival is a boolean. Empty-time / vessel-less / non-planned rows drop out.
_PLANNED_SQL = text(
    """
    SELECT
        p.id AS p_id, p.type AS p_type, p.status AS p_status,
        lower(p.time_range)    AS p_t_start, upper(p.time_range)    AS p_t_end,
        isempty(p.station_range) AS p_sta_empty,
        lower(p.station_range) AS p_sta_lo, upper(p.station_range) AS p_sta_hi,
        p.berth_id AS p_berth_id, pb.name AS p_berth_name,
        pv.name AS p_vessel_name, pv.imo AS p_vessel_imo,
        o.obs_id, o.obs_t_start, o.obs_t_end, o.obs_sta_lo, o.obs_sta_hi
    FROM reservation p
    JOIN vessel pv ON pv.id = p.vessel_id
    LEFT JOIN berth pb ON pb.id = p.berth_id
    LEFT JOIN LATERAL (
        SELECT o2.id AS obs_id,
               lower(o2.time_range)    AS obs_t_start, upper(o2.time_range)    AS obs_t_end,
               lower(o2.station_range) AS obs_sta_lo,  upper(o2.station_range) AS obs_sta_hi
        FROM reservation o2
        WHERE o2.status::text = 'observed'
          AND o2.vessel_id = p.vessel_id
          AND o2.time_range && p.time_range
        ORDER BY (upper(o2.time_range) IS NULL) DESC, lower(o2.time_range) DESC
        LIMIT 1
    ) o ON TRUE
    WHERE p.status::text IN ('requested', 'tentative', 'confirmed')
      AND p.vessel_id IS NOT NULL
      AND NOT isempty(p.time_range)
      AND ((CAST(:t_from AS timestamptz) IS NULL AND CAST(:t_to AS timestamptz) IS NULL)
           OR p.time_range && tstzrange(:t_from, :t_to, '[]'))
    ORDER BY lower(p.time_range)
    LIMIT :limit
    """
)

# Observed berthings with no planned row covering the same vessel+window — a
# vessel alongside that the office never logged a request for.
_UNPLANNED_SQL = text(
    """
    SELECT
        o.id AS o_id,
        lower(o.time_range)    AS o_t_start, upper(o.time_range)    AS o_t_end,
        lower(o.station_range) AS o_sta_lo,  upper(o.station_range) AS o_sta_hi,
        o.berth_id AS o_berth_id, ob.name AS o_berth_name,
        ov.name AS o_vessel_name, ov.imo AS o_vessel_imo
    FROM reservation o
    JOIN vessel ov ON ov.id = o.vessel_id
    LEFT JOIN berth ob ON ob.id = o.berth_id
    WHERE o.status::text = 'observed'
      AND NOT EXISTS (
          SELECT 1 FROM reservation p
          WHERE p.status::text IN ('requested', 'tentative', 'confirmed')
            AND p.vessel_id = o.vessel_id
            AND p.time_range && o.time_range
      )
      AND ((CAST(:t_from AS timestamptz) IS NULL AND CAST(:t_to AS timestamptz) IS NULL)
           OR o.time_range && tstzrange(:t_from, :t_to, '[]'))
    ORDER BY lower(o.time_range) DESC
    LIMIT :limit
    """
)


def verify(
    session: Session,
    *,
    t_from: datetime | None = None,
    t_to: datetime | None = None,
    limit: int = 200,
) -> dict:
    """Verify operator placements against observed AIS occupancy.

    Returns ``{"as_of", "planned": [...], "unplanned": [...]}``. Each ``planned``
    entry carries its ``state`` (``arrived``/``no_show``/``awaiting``), a
    ``where_planned`` flag, and the matched ``observed`` window (or ``None``).
    ``unplanned`` lists observed berthings with no covering plan. ``t_from``/
    ``t_to`` (optional) keep only rows whose window overlaps that span, like the
    other endpoints; ``as_of`` is the DB clock the states were judged against.
    """
    # Judge no_show/awaiting against the DB clock (Central-pinned, see app/db.py),
    # not the app process clock, so it matches the stored timestamptz exactly.
    as_of = session.execute(text("SELECT now()")).scalar_one()

    dock = segment_dockno_params(session)

    def dk(v: Any) -> float | None:
        return float(dock.from_popa(float(v))) if v is not None else None

    def f(v: Any) -> float | None:
        return float(v) if v is not None else None

    def iso(v: Any) -> str | None:
        return v.isoformat() if v is not None else None

    planned_rows = session.execute(
        _PLANNED_SQL, {"t_from": t_from, "t_to": t_to, "limit": limit}
    ).all()
    planned: list[dict] = []
    for r in planned_rows:
        has_obs = r.obs_id is not None
        observed = None
        if has_obs:
            observed = {
                "id": r.obs_id,
                "t_start": iso(r.obs_t_start),
                "t_end": iso(r.obs_t_end),
                "station_lo": f(r.obs_sta_lo),
                "station_hi": f(r.obs_sta_hi),
                "station_lo_dock": dk(r.obs_sta_lo),
                "station_hi_dock": dk(r.obs_sta_hi),
            }
        planned.append(
            {
                "id": r.p_id,
                "type": r.p_type,
                "status": r.p_status,
                "vessel_name": r.p_vessel_name,
                "vessel_imo": r.p_vessel_imo,
                "berth_id": r.p_berth_id,
                "berth_name": r.p_berth_name,
                "t_start": iso(r.p_t_start),
                "t_end": iso(r.p_t_end),
                "station_unassigned": bool(r.p_sta_empty),
                "station_lo": f(r.p_sta_lo),
                "station_hi": f(r.p_sta_hi),
                "station_lo_dock": dk(r.p_sta_lo),
                "station_hi_dock": dk(r.p_sta_hi),
                "state": classify_planned(r.p_t_start, r.p_t_end, has_obs, as_of),
                "where_planned": where_planned(
                    f(r.p_sta_lo), f(r.p_sta_hi), f(r.obs_sta_lo), f(r.obs_sta_hi)
                ),
                "observed": observed,
            }
        )

    unplanned_rows = session.execute(
        _UNPLANNED_SQL, {"t_from": t_from, "t_to": t_to, "limit": limit}
    ).all()
    unplanned = [
        {
            "id": r.o_id,
            "vessel_name": r.o_vessel_name,
            "vessel_imo": r.o_vessel_imo,
            "berth_id": r.o_berth_id,
            "berth_name": r.o_berth_name,
            "t_start": iso(r.o_t_start),
            "t_end": iso(r.o_t_end),
            "station_lo": f(r.o_sta_lo),
            "station_hi": f(r.o_sta_hi),
            "station_lo_dock": dk(r.o_sta_lo),
            "station_hi_dock": dk(r.o_sta_hi),
            "ongoing": r.o_t_end is None,
        }
        for r in unplanned_rows
    ]

    return {"as_of": iso(as_of), "planned": planned, "unplanned": unplanned}


# ---------------------------------------------------------------------------
# Auto-expiry — the deferred step-7 "auto status-mutation" half
# ---------------------------------------------------------------------------
# A planned row whose window has been fully past for longer than the grace period
# is swept so it stops lingering in the live verification panel. Unlike ``verify``
# (read-only), this MUTATES, but only on evidence (see ``expiry_action``):
#   * arrived (an observed AIS berthing overlapped the window) -> ``completed``
#   * no-show (feed was live, vessel unseen) of requested/tentative -> ``cancelled``
#   * no-show of a CONFIRMED booking -> FLAGGED (note only; stays ``confirmed`` for
#     the operator — a slipped ETA must not auto-destroy a commitment)
#   * no berthing AND no AIS traffic at all in the window -> LEFT ALONE (a dead feed
#     is not a no-show; ``feed_alive`` distinguishes the two — `position_report`
#     carries the whole bbox's traffic, so any landed fix in the window proves the
#     feed was up). A later sweep judges it once the feed returns.
# The grace period (``config.verification_grace_minutes``) is why a row whose
# window *just* closed still shows — the operator gets a window to react before it
# auto-archives. An audit line is appended to ``notes`` (operator notes are kept,
# not clobbered) so History records why/when. The confirmed-flag note is guarded by
# a ``[no-show flag]`` marker so repeat sweeps don't spam it. Open-ended windows
# (NULL upper) never expire — they have not "ended".
_EXPIRE_SQL = text(
    """
    WITH stale AS (
        SELECT p.id,
               p.status::text AS prev_status,
               p.notes        AS prev_notes,
               upper(p.time_range) AS t_end,
               v.name AS vessel_name,
               v.imo  AS vessel_imo,
               EXISTS (
                   SELECT 1 FROM reservation o
                   WHERE o.status::text = 'observed'
                     AND o.vessel_id = p.vessel_id
                     AND o.time_range && p.time_range
               ) AS arrived,
               EXISTS (
                   SELECT 1 FROM position_report pr
                   WHERE p.time_range @> COALESCE(pr.msg_ts, pr.created_at)
               ) AS feed_alive
        FROM reservation p
        LEFT JOIN vessel v ON v.id = p.vessel_id
        WHERE p.status::text IN ('requested', 'tentative', 'confirmed')
          AND NOT isempty(p.time_range)
          AND upper(p.time_range) IS NOT NULL
          AND upper(p.time_range) <= now() - make_interval(mins => :grace)
    ),
    decided AS (
        SELECT s.*,
               CASE
                   WHEN s.arrived THEN 'completed'
                   WHEN NOT s.feed_alive THEN NULL
                   WHEN s.prev_status = 'confirmed' THEN 'flagged'
                   ELSE 'cancelled'
               END AS action
        FROM stale s
    )
    UPDATE reservation r
    SET status = CASE d.action
                     WHEN 'completed' THEN 'completed'::reservation_status
                     WHEN 'cancelled' THEN 'cancelled'::reservation_status
                     ELSE r.status                     -- 'flagged' keeps confirmed
                 END,
        notes = COALESCE(r.notes || E'\\n', '')
                || '[auto ' || to_char(now(), 'YYYY-MM-DD HH24:MI') || '] '
                || CASE d.action
                       WHEN 'completed' THEN
                            'completed — vessel berthed (AIS) and window elapsed'
                       WHEN 'cancelled' THEN
                            'cancelled — no-show; AIS feed live but vessel not seen; '
                            || 'window elapsed ' || to_char(d.t_end, 'YYYY-MM-DD HH24:MI')
                       ELSE
                            '[no-show flag] confirmed booking — AIS feed live but '
                            || 'vessel not seen; window elapsed '
                            || to_char(d.t_end, 'YYYY-MM-DD HH24:MI')
                            || '; operator action required (not auto-cancelled)'
                   END
    FROM decided d
    WHERE r.id = d.id
      AND d.action IS NOT NULL
      -- A confirmed no-show is flagged once; don't re-append the note each sweep.
      AND NOT (d.action = 'flagged'
               AND COALESCE(d.prev_notes, '') LIKE '%[no-show flag]%')
    RETURNING r.id, r.status::text AS status, d.action AS action, d.arrived,
              d.t_end, d.vessel_name AS vessel_name, d.vessel_imo AS vessel_imo
    """
)


def expire_stale(session: Session, *, grace_minutes: int) -> list[dict]:
    """Sweep planned rows whose window has been past for > ``grace_minutes``.

    Acts only on evidence (see ``expiry_action``): ``completed`` when AIS observed a
    berthing, ``cancelled`` for a no-show of a ``requested``/``tentative`` row,
    ``flagged`` (note only, status unchanged) for a no-show of a **confirmed**
    booking. A stale row with **no AIS traffic at all in its window** is left
    untouched — a dead feed is not a no-show. Each touched row gets an audit line in
    ``notes``. Returns the rows it acted on (id, current ``status``, the ``action``
    taken, ``arrived``, vessel name/imo, ``t_end``) so the caller can report. Does
    **not** commit — the endpoint / worker owns the transaction boundary, like the
    rest of the write surface.

    ``grace_minutes <= 0`` expires as soon as the window ends (no grace). The SQL
    anchors on ``now()`` (the Central-pinned DB clock) so the comparison matches
    the stored timestamptz exactly."""
    rows = session.execute(
        _EXPIRE_SQL, {"grace": max(0, int(grace_minutes))}
    ).all()
    return [
        {
            "id": r.id,
            "status": r.status,
            "action": r.action,
            "arrived": bool(r.arrived),
            "vessel_name": r.vessel_name,
            "vessel_imo": r.vessel_imo,
            "t_end": r.t_end.isoformat() if r.t_end is not None else None,
        }
        for r in rows
    ]
