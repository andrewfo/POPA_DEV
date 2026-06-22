"""Runnable AIS ingestion entrypoint.

Connects the live aisstream.io source to the database ingestor and reconnects
with backoff if the websocket drops.

Run:  python -m app.ais.run
"""
from __future__ import annotations

import asyncio
import logging

from app.ais.ingest import Ingestor
from app.ais.source import AisStreamSource
from app.config import get_settings
from app.db import SessionLocal
from app.workers import beat

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("app.ais.run")


async def main() -> None:
    settings = get_settings()
    source = AisStreamSource(
        api_key=settings.aisstream_api_key,
        bounding_boxes=settings.ais_bounding_box,
        url=settings.aisstream_url,
    )

    # Beat on each real commit, so liveness tracks the cadence the feed lands
    # data (a busy waterway commits every few seconds).
    def _heartbeat(ing: Ingestor) -> None:
        beat("ais", "ok", {"positions": ing.positions, "statics": ing.statics})

    backoff = 1.0
    while True:
        session = SessionLocal()
        ingestor = Ingestor(session, on_commit=_heartbeat)
        try:
            logger.info("connecting to aisstream...")
            beat("ais", "ok", {"event": "connecting"})
            await ingestor.run(source)
            backoff = 1.0  # clean end (rare for a live stream)
        except asyncio.CancelledError:
            ingestor.flush()
            raise
        except Exception as exc:  # noqa: BLE001 - keep the long-running ingest alive
            logger.exception("ingestion loop error; reconnecting in %.0fs", backoff)
            beat("ais", "error", {"error": str(exc)})
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
        finally:
            session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
