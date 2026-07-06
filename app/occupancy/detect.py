"""Berthed-state detection — pure logic, no database.

Input is a time-ordered sequence of position samples for ONE vessel, each
already classified as ``alongside`` (inside the wharf buffer — see
``alongside.py``; the spatial test is done in SQL so this stays pure and
unit-testable). Output is the list of berthing events the track contains.

Hysteresis (two thresholds, not one) so the state doesn't flap:

  * ENTER a berthing when the vessel is alongside and SOG <= ``enter_sog`` and
    that condition is *sustained* for at least ``dwell``.
  * EXIT only after a real departure — not alongside, or SOG > ``depart_sog`` —
    *sustained* for at least ``depart_gap``. Speeds between the two thresholds,
    or brief blips, keep the vessel berthed.

AIS ``nav_status`` (5 = moored, 1 = at anchor) is used as a mild assist toward
entering, never as sole truth — many vessels set it wrong.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

NAV_MOORED = 5
NAV_AT_ANCHOR = 1


@dataclass(frozen=True)
class Sample:
    ts: dt.datetime
    alongside: bool
    sog: float | None = None
    nav_status: int | None = None


@dataclass(frozen=True)
class BerthingEvent:
    """A detected alongside interval. ``open_ended`` means the track was still
    berthed at its last sample, so ``t_end`` will extend as more data lands."""

    t_start: dt.datetime
    t_end: dt.datetime
    open_ended: bool


def detect_berthings(
    samples: list[Sample],
    *,
    enter_sog: float = 0.5,
    depart_sog: float = 1.0,
    dwell: dt.timedelta = dt.timedelta(minutes=20),
    depart_gap: dt.timedelta = dt.timedelta(minutes=10),
    as_of: dt.datetime | None = None,
    stale_after: dt.timedelta | None = None,
) -> list[BerthingEvent]:
    """Reduce a single vessel's track to its berthing events.

    A trailing alongside segment is normally ``open_ended`` (still berthed at the
    last sample). But a vessel that *departs while coverage is down* (or leaves
    the receiver's range) just stops sampling — its segment would stay "ongoing"
    forever. When ``as_of``/``stale_after`` are given, a trailing segment whose
    last sample is older than ``stale_after`` relative to ``as_of`` is emitted
    CLOSED at that last sample instead. Pass the *feed clock* (the newest sample
    across the whole feed) as ``as_of``, never the wall clock — silence of the
    entire feed is an outage, not a departure."""
    ordered = sorted(samples, key=lambda s: s.ts)

    events: list[BerthingEvent] = []
    seg_start: dt.datetime | None = None  # first still sample of the open segment
    seg_last_still: dt.datetime | None = None  # most recent confirmation it's there

    def _can_enter(s: Sample) -> bool:
        if not s.alongside or s.sog is None:
            return s.alongside and s.sog is None  # no speed but alongside: trust it
        if s.sog <= enter_sog:
            return True
        # Moored/anchored hint relaxes the speed bar a little, but never beyond
        # the departure threshold.
        return s.nav_status in (NAV_MOORED, NAV_AT_ANCHOR) and s.sog <= depart_sog

    def _departed(s: Sample) -> bool:
        return (not s.alongside) or (s.sog is not None and s.sog > depart_sog)

    def _emit(start: dt.datetime, end: dt.datetime, open_ended: bool) -> None:
        if end - start >= dwell:
            events.append(BerthingEvent(start, end, open_ended))

    for s in ordered:
        if seg_start is None:
            if _can_enter(s):
                seg_start = s.ts
                seg_last_still = s.ts
            continue

        # In an open segment.
        if _departed(s):
            # Tolerate brief departures; only close once sustained past the gap.
            assert seg_last_still is not None
            if s.ts - seg_last_still >= depart_gap:
                _emit(seg_start, seg_last_still, open_ended=False)
                seg_start = None
                seg_last_still = None
        else:
            # Alongside and not departing (still or neutral speed) -> keep alive.
            seg_last_still = s.ts

    if seg_start is not None and seg_last_still is not None:
        # Trailing open segment: still berthed at the last sample we have —
        # unless that sample has gone stale against the feed clock (see above).
        stale = (
            as_of is not None
            and stale_after is not None
            and as_of - seg_last_still >= stale_after
        )
        _emit(seg_start, seg_last_still, open_ended=not stale)

    return events
