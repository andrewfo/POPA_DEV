"""Vessel-particulars lookup by IMO — the data behind IMO auto-fill on intake.

The berth-request form pre-populates a vessel's name and dimensions from its IMO
*before submit* (PLAN.md item A). This module is the single lookup seam behind that
feature, structured for graceful decomposition across two tiers:

- **Tier 1 (``lookup_onfile``)** — read our own ``vessel`` table, which the AIS
  ingestor already fills with IMO + dimensions from ``ShipStaticData`` (see
  ``app/ais/ingest.py``). Free, instant, and authoritative for any ship the port has
  seen broadcast static data inside the AIS bounding box.
- **Tier 2 (``lookup_external``)** — an external vessel-reference provider
  (MarineTraffic / VesselFinder / Datalastic / Equasis, …) for a ship not yet on
  file (never-arrived, or seen only via position reports with no static broadcast
  yet). **Stubbed today**; wiring it in touches only this module.

``lookup`` is the orchestrator the HTTP endpoint calls — Tier 1 first, Tier 2 as
fallback. Everything returns the same normalized :class:`VesselParticulars`, so the
endpoint and the form never need to know which tier answered (mirrors the
source-agnostic ``AISSource`` contract on the ingestion side).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

# Same constant as the rest of the tree (app/intake/manual, app/edit, …). Station
# "M" is feet; vessel dimensions are stored in metres. The UI works in feet, so we
# convert here — the client never converts.
FEET_PER_M = 3.280839895


@dataclass
class VesselParticulars:
    """A vessel's identity + dimensions in the shape the intake form consumes.

    Dimensions are **feet** (the form's unit); the canonical store is metres, so the
    conversion happens at lookup time. ``ais_tracked`` (has an MMSI) drives the
    form's AIS-dims-authority handling — the ingestor owns those dims, so the form
    fills + flags them (editable; overtyping one arms a confirm-to-pin
    ``dims_locked`` override on save). ``source`` records which tier answered.
    """

    imo: int
    mmsi: int | None
    name: str | None
    loa_ft: float | None
    beam_ft: float | None
    draft_ft: float | None
    ship_type: int | None
    callsign: str | None
    ais_tracked: bool
    dims_locked: bool
    source: str  # "onfile" | "external"

    def to_payload(self) -> dict:
        """The ``found`` response body for ``GET /vessels/lookup``."""
        return {
            "found": True,
            "source": self.source,
            "imo": self.imo,
            "mmsi": self.mmsi,
            "name": self.name,
            "loa_ft": self.loa_ft,
            "beam_ft": self.beam_ft,
            "draft_ft": self.draft_ft,
            "ship_type": self.ship_type,
            "callsign": self.callsign,
            "ais_tracked": self.ais_tracked,
            "dims_locked": self.dims_locked,
        }


def _to_ft(m: Any) -> float | None:
    return round(float(m) * FEET_PER_M, 1) if m is not None else None


def lookup_onfile(session: Session, imo: int) -> VesselParticulars | None:
    """Tier 1: find a vessel already on file by IMO.

    ``imo`` is indexed but **not unique** (AIS can mis-report it, and the key is
    MMSI), so the most-recently-updated matching row wins. Returns ``None`` when no
    row carries this IMO — a normal miss the caller falls through to Tier 2 on.
    """
    row = session.execute(
        text(
            """
            SELECT id, mmsi, imo, name, callsign, ship_type,
                   loa, beam, draft, dims_locked
            FROM vessel
            WHERE imo = :imo
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """
        ),
        {"imo": imo},
    ).one_or_none()
    if row is None:
        return None
    return VesselParticulars(
        imo=imo,
        mmsi=row.mmsi,
        name=row.name,
        loa_ft=_to_ft(row.loa),
        beam_ft=_to_ft(row.beam),
        draft_ft=_to_ft(row.draft),
        ship_type=row.ship_type,
        callsign=row.callsign,
        ais_tracked=row.mmsi is not None,
        dims_locked=bool(row.dims_locked),
        source="onfile",
    )


def _like_escape(s: str) -> str:
    """Escape LIKE metacharacters so an operator's ``%``/``_`` match literally."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_onfile(session: Session, q: str, limit: int = 8) -> list[dict]:
    """Name search over on-file vessels — the type-ahead behind the intake form's
    Vessel field, for when the operator knows the ship's name but not its IMO.

    ``vessel`` already holds every AIS ship *and* every past manual request with an
    IMO (``_upsert_vessel``), so this covers repeat callers without scanning
    ``intake_event``. Only rows with a **structurally valid IMO** are returned
    (picking one fills the form's required IMO, which then drives :func:`lookup`). One row per IMO, the
    most-recently-updated winning — the same rule as :func:`lookup_onfile`, so the
    row shown is the row auto-fill returns. An all-digit ``q`` also matches an IMO
    prefix. Name-prefix matches rank first, then most recently seen on AIS.

    ``q`` under 2 chars returns ``[]``. A plain ``ILIKE`` (no trigram index) is
    fine: ``vessel`` is thousands of rows. Read-only — proposes, never places.
    """
    q = q.strip()
    if len(q) < 2:
        return []
    pat = _like_escape(q)
    rows = session.execute(
        text(
            r"""
            WITH latest AS (
                SELECT DISTINCT ON (imo)
                       id, mmsi, imo, name, ship_type, loa, beam
                FROM vessel
                -- Structurally valid IMOs only (7 digits, check digit = the
                -- weighted sum of the first six mod 10; mirrors valid_imo). AIS
                -- mis-reports IMO, and picking a bad one would fill an IMO the
                -- form and intake both reject.
                WHERE imo BETWEEN 1000000 AND 9999999
                  AND ((imo / 1000000) * 7 + (imo / 100000 % 10) * 6
                       + (imo / 10000 % 10) * 5 + (imo / 1000 % 10) * 4
                       + (imo / 100 % 10) * 3 + (imo / 10 % 10) * 2) % 10 = imo % 10
                  AND (name ILIKE :contains ESCAPE '\'
                       OR (:digits AND imo::text LIKE :prefix ESCAPE '\'))
                ORDER BY imo, updated_at DESC, id DESC
            ), ranked AS (
                SELECT l.*, p.msg_ts AS last_seen,
                       COALESCE(l.name ILIKE :prefix ESCAPE '\', false) AS name_prefix
                FROM latest l
                LEFT JOIN LATERAL (
                    SELECT msg_ts FROM position_report
                    WHERE l.mmsi IS NOT NULL AND mmsi = l.mmsi
                    ORDER BY msg_ts DESC NULLS LAST
                    LIMIT 1
                ) p ON true
            )
            SELECT * FROM ranked
            ORDER BY name_prefix DESC, last_seen DESC NULLS LAST, name
            LIMIT :limit
            """
        ),
        {
            "contains": f"%{pat}%",
            "prefix": f"{pat}%",
            "digits": q.isdigit(),
            "limit": limit,
        },
    ).all()
    return [
        {
            "imo": r.imo,
            "mmsi": r.mmsi,
            "name": r.name,
            "loa_ft": _to_ft(r.loa),
            "beam_ft": _to_ft(r.beam),
            "ship_type": r.ship_type,
            "ais_tracked": r.mmsi is not None,
            "last_seen": r.last_seen.isoformat() if r.last_seen else None,
        }
        for r in rows
    ]


def lookup_external(imo: int) -> VesselParticulars | None:
    """Tier 2 (STUB): resolve an IMO against an external vessel-reference provider.

    Returns ``None`` today — no provider is wired. When one is added it must yield a
    :class:`VesselParticulars` with ``source="external"`` (dimensions already in
    feet, ``ais_tracked=False`` since these come from a registry, not our AIS feed),
    so the endpoint and form need no change — same source-agnostic pattern as
    ``AISSource`` on the ingestion side.

    TODO(tier2): add a pluggable provider gated by ``VESSEL_REF_PROVIDER`` /
    ``VESSEL_REF_API_KEY`` settings in ``app/config.py`` (candidates: MarineTraffic
    ``vesselmasterdata``, VesselFinder MASTERDATA, Datalastic, Equasis). Cache a hit
    back into ``vessel`` so it becomes a Tier-1 answer next time.
    """
    return None


def lookup(session: Session, imo: int) -> VesselParticulars | None:
    """Resolve vessel particulars for ``imo``: on-file first, external as fallback.

    Callers (the ``GET /vessels/lookup`` endpoint) use only this. ``None`` means
    neither tier knows the IMO — the operator enters dimensions by hand.
    """
    return lookup_onfile(session, imo) or lookup_external(imo)
