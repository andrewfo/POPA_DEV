"""Pluggable AIS sources.

An ``AISSource`` is an async iterator of normalized ``AISPosition`` /
``AISStatic`` objects. The live source is aisstream.io over a websocket; a
historical Marine Cadastre CSV source can implement the same interface later
and feed the identical ingestion path.
"""
from __future__ import annotations

import abc
import json
import logging
from collections.abc import AsyncIterator

import websockets

from app.ais.messages import AISPosition, AISStatic, parse_aisstream

logger = logging.getLogger(__name__)

AISMessage = AISPosition | AISStatic


class AISSource(abc.ABC):
    """Yields normalized AIS messages. Implementations own their wire format."""

    @abc.abstractmethod
    def stream(self) -> AsyncIterator[AISMessage]:
        """Async-iterate normalized AIS messages until the source is exhausted
        or the connection drops."""
        raise NotImplementedError


class AisStreamSource(AISSource):
    """Live source: aisstream.io websocket.

    The subscription message MUST be sent within 3s of connecting or the server
    drops the connection — we send it immediately after the handshake.
    """

    def __init__(
        self,
        api_key: str,
        bounding_boxes: list[list[list[float]]],
        url: str = "wss://stream.aisstream.io/v0/stream",
        message_types: tuple[str, ...] = ("PositionReport", "ShipStaticData"),
    ) -> None:
        if not api_key:
            raise ValueError("AISSTREAM_API_KEY is required for AisStreamSource")
        self.api_key = api_key
        self.bounding_boxes = bounding_boxes
        self.url = url
        self.message_types = list(message_types)

    def _subscription(self) -> str:
        return json.dumps(
            {
                "APIKey": self.api_key,
                "BoundingBoxes": self.bounding_boxes,
                "FilterMessageTypes": self.message_types,
            }
        )

    async def stream(self) -> AsyncIterator[AISMessage]:
        async with websockets.connect(self.url, ping_interval=20) as ws:
            await ws.send(self._subscription())  # within 3s of connect
            logger.info("aisstream subscribed: bbox=%s", self.bounding_boxes)
            async for raw in ws:
                try:
                    envelope = json.loads(raw)
                except (ValueError, TypeError):
                    logger.warning("non-JSON frame from aisstream, skipping")
                    continue
                if "error" in envelope:
                    logger.error("aisstream error frame: %s", envelope.get("error"))
                    continue
                parsed = parse_aisstream(envelope)
                if parsed is not None:
                    yield parsed
