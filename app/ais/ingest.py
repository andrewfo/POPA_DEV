"""Land normalized AIS messages into the database.

Source-agnostic: it consumes ``AISPosition`` / ``AISStatic`` from any
``AISSource``. It upserts ``vessel`` keyed on MMSI (merging static detail as it
arrives, never clobbering known values with nulls) and appends raw
``position_report`` rows. Occupancy derivation is a LATER step — this only
lands clean data.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import case, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.ais.messages import AISPosition, AISStatic
from app.ais.source import AISMessage, AISSource
from app.models import PositionReport, Vessel

logger = logging.getLogger(__name__)

# Dimension columns AIS owns for an MMSI-keyed vessel, but which an operator can
# pin via ``vessel.dims_locked`` (migration 0015). When locked, the ingestor
# keeps the stored value instead of overwriting it from the feed. loa/dim_a/dim_b
# are frozen together so ``loa = dim_a + dim_b`` can't drift while pinned.
_LOCKED_DIMS = {"loa", "beam", "draft", "dim_a", "dim_b"}


class Ingestor:
    def __init__(
        self,
        session: Session,
        commit_every: int = 50,
        commit_interval_s: float = 10.0,
        on_commit=None,
    ) -> None:
        self.session = session
        self.commit_every = commit_every
        # Optional callback fired after each real commit (passed self), so a
        # caller can stamp a liveness heartbeat on the same cadence the feed
        # actually lands data — without coupling this source-agnostic class to
        # the worker layer. See app/ais/run.py.
        self.on_commit = on_commit
        # Also flush when this many seconds have elapsed since the last commit,
        # even if the batch isn't full. Snug bounding boxes (e.g. a single wharf)
        # see only a trickle of messages, so a count-only threshold could leave
        # rows uncommitted for a long time — they'd never reach the map.
        self.commit_interval_s = commit_interval_s
        self._pending = 0
        self._last_commit = time.monotonic()
        self.positions = 0
        self.statics = 0

    # --- vessel upserts -----------------------------------------------------
    def _ensure_vessel(self, mmsi: int) -> int:
        """Make sure a vessel row exists for this MMSI; return its id."""
        stmt = (
            pg_insert(Vessel)
            .values(mmsi=mmsi)
            # No-op update so RETURNING fires even on conflict.
            .on_conflict_do_update(index_elements=["mmsi"], set_={"mmsi": mmsi})
            .returning(Vessel.id)
        )
        return int(self.session.execute(stmt).scalar_one())

    def _upsert_vessel_static(self, msg: AISStatic) -> int:
        values = {
            "mmsi": msg.mmsi,
            "imo": msg.imo,
            "name": msg.name,
            "callsign": msg.callsign,
            "ship_type": msg.ship_type,
            "loa": msg.loa,
            "beam": msg.beam,
            "dim_a": msg.dim_a,
            "dim_b": msg.dim_b,
            "draft": msg.draft,
            "destination": msg.destination,
        }
        ins = pg_insert(Vessel).values(**values)
        # COALESCE(new, existing): keep prior detail if the new field is null.
        # Dimension columns are additionally gated on ``dims_locked``: when an
        # operator has pinned a corrected value (AIS was wrong — migration 0015),
        # keep the stored value even though the feed carries one, so the override
        # is persistent instead of reverting on the next ShipStaticData. Identity
        # / non-dimension fields keep merging from AIS regardless of the lock.
        set_ = {}
        for k in values:
            if k == "mmsi":
                continue
            merged = func.coalesce(getattr(ins.excluded, k), getattr(Vessel, k))
            if k in _LOCKED_DIMS:
                set_[k] = case((Vessel.dims_locked, getattr(Vessel, k)), else_=merged)
            else:
                set_[k] = merged
        set_["updated_at"] = func.now()
        stmt = ins.on_conflict_do_update(index_elements=["mmsi"], set_=set_).returning(
            Vessel.id
        )
        return int(self.session.execute(stmt).scalar_one())

    # --- position ----------------------------------------------------------
    def _insert_position(self, msg: AISPosition, vessel_id: int) -> None:
        self.session.execute(
            pg_insert(PositionReport).values(
                vessel_id=vessel_id,
                mmsi=msg.mmsi,
                lat=msg.lat,
                lon=msg.lon,
                geom=func.ST_SetSRID(func.ST_MakePoint(msg.lon, msg.lat), 4326),
                sog=msg.sog,
                cog=msg.cog,
                heading=msg.heading,
                nav_status=msg.nav_status,
                source="ais",
                msg_ts=msg.msg_ts,
                raw=msg.raw,
            )
        )

    # --- dispatch ----------------------------------------------------------
    def handle(self, msg: AISMessage) -> None:
        if isinstance(msg, AISStatic):
            self._upsert_vessel_static(msg)
            self.statics += 1
        elif isinstance(msg, AISPosition):
            vessel_id = self._ensure_vessel(msg.mmsi)
            self._insert_position(msg, vessel_id)
            self.positions += 1
        else:  # pragma: no cover - defensive
            return
        self._pending += 1
        if (
            self._pending >= self.commit_every
            or time.monotonic() - self._last_commit >= self.commit_interval_s
        ):
            self.flush()

    def flush(self) -> None:
        if self._pending:
            self.session.commit()
            self._pending = 0
            # Heartbeat ties to a real commit (data actually landed), and runs
            # AFTER the commit so the ingestor's transaction is closed first.
            if self.on_commit is not None:
                self.on_commit(self)
        self._last_commit = time.monotonic()

    async def run(self, source: AISSource) -> None:
        """Consume a source until it ends, committing periodically."""
        try:
            async for msg in source.stream():
                self.handle(msg)
                if (self.positions + self.statics) % 200 == 0:
                    logger.info(
                        "ingested positions=%d statics=%d",
                        self.positions, self.statics,
                    )
        finally:
            self.flush()
