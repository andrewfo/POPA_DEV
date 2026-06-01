"""Land normalized AIS messages into the database.

Source-agnostic: it consumes ``AISPosition`` / ``AISStatic`` from any
``AISSource``. It upserts ``vessel`` keyed on MMSI (merging static detail as it
arrives, never clobbering known values with nulls) and appends raw
``position_report`` rows. Occupancy derivation is a LATER step — this only
lands clean data.
"""
from __future__ import annotations

import logging

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.ais.messages import AISPosition, AISStatic
from app.ais.source import AISMessage, AISSource
from app.models import PositionReport, Vessel

logger = logging.getLogger(__name__)


class Ingestor:
    def __init__(self, session: Session, commit_every: int = 50) -> None:
        self.session = session
        self.commit_every = commit_every
        self._pending = 0
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
            "draft": msg.draft,
            "destination": msg.destination,
        }
        ins = pg_insert(Vessel).values(**values)
        # COALESCE(new, existing): keep prior detail if the new field is null.
        set_ = {
            k: func.coalesce(getattr(ins.excluded, k), getattr(Vessel, k))
            for k in values
            if k != "mmsi"
        }
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
        if self._pending >= self.commit_every:
            self.flush()

    def flush(self) -> None:
        if self._pending:
            self.session.commit()
            self._pending = 0

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
