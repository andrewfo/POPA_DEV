"""Runnable occupancy derivation (periodic batch).

Reads landed position reports and writes/updates ``observed`` reservations.
Idempotent: safe to run on a cron or by hand repeatedly.

Run:
    python -m app.occupancy.run                 # all history
    python -m app.occupancy.run --since 2026-06-01T00:00:00+00:00
    python -m app.occupancy.run --segment-id 1
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging

from app.crosswalk import format_station
from app.db import SessionLocal
from app.occupancy.derive import derive_observed

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("app.occupancy.run")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Derive observed reservations from AIS.")
    p.add_argument(
        "--since",
        type=lambda s: dt.datetime.fromisoformat(s),
        default=None,
        help="ISO-8601 lower bound on position timestamps (e.g. 2026-06-01T00:00:00+00:00).",
    )
    p.add_argument(
        "--segment-id",
        type=int,
        default=None,
        help="Restrict to one wharf_segment id (default: nearest segment per fix).",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    session = SessionLocal()
    try:
        results = derive_observed(
            session, segment_id=args.segment_id, since=args.since
        )
        session.commit()
    finally:
        session.close()

    inserted = sum(1 for r in results if r.inserted)
    updated = len(results) - inserted
    logger.info(
        "derived %d observed reservation(s): %d new, %d updated",
        len(results), inserted, updated,
    )
    for r in results:
        logger.info(
            "  vessel=%d sta=[%s, %s] %s..%s dir=%s%s%s",
            r.vessel_id,
            format_station(r.station_lo),
            format_station(r.station_hi),
            r.t_start.isoformat(),
            r.t_end.isoformat(),
            r.direction or "?",
            " open" if r.open_ended else "",
            " low-conf" if not r.confident else "",
        )


if __name__ == "__main__":
    main()
