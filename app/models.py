"""ORM models for the wharf data layer.

The wharf is referenced LINEARLY: positions are POPA stations (feet) along a
measured PostGIS centerline (``M`` = POPA station). "Berths" are just named
station ranges. Every reservation is a rectangle in (time) x (station) space;
a conflict is "time ranges overlap AND station ranges overlap" — one primitive
covers vessel-vs-vessel and vessel-vs-dredge alike.

Enum / range / constraint definitions here are mirrored by the hand-written
Alembic migration. Keep the two in sync; migrations remain the source of truth
for the live schema.
"""
from __future__ import annotations

import datetime as dt

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, NUMRANGE, TSTZRANGE
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# ---------------------------------------------------------------------------
# Enum value sets (also created as Postgres ENUM types in the migration).
# ---------------------------------------------------------------------------
RESERVATION_TYPES = ("vessel", "dredge", "layberth")
RESERVATION_STATUSES = (
    "observed",
    "requested",
    "tentative",
    "confirmed",
    "cancelled",
    "completed",
)
# 'email' added in migration 0004 (manual berth-request entry channel); 'ai'
# added in migration 0009 (the AI-assisted normalizer's own provenance tag —
# distinct from the human 'email' channel it used to borrow).
RESERVATION_SOURCES = ("ais", "form", "phone", "operator", "email", "ai")
DIRECTIONS = ("upstream", "downstream")
INTAKE_SOURCES = ("ais", "form", "phone", "operator", "email", "ai")

reservation_type_enum = Enum(*RESERVATION_TYPES, name="reservation_type")
reservation_status_enum = Enum(*RESERVATION_STATUSES, name="reservation_status")
reservation_source_enum = Enum(*RESERVATION_SOURCES, name="reservation_source")
direction_enum = Enum(*DIRECTIONS, name="direction")
intake_source_enum = Enum(*INTAKE_SOURCES, name="intake_source")


class Base(DeclarativeBase):
    pass


class WharfSegment(Base):
    """A named stretch of wharf with a measured centerline and the affine
    parameters that reconcile external stationing systems to canonical POPA
    station.

    External system value = ``scale * popa_station + offset``. Stored per
    segment so the crosswalk math generalizes instead of hard-coding one
    transform.
    """

    __tablename__ = "wharf_segment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)

    # Measured centerline: LINESTRING with an M coordinate carrying POPA station
    # (feet). lat/lon vertices are real geographic coordinates (SRID 4326).
    geom: Mapped[object] = mapped_column(
        Geometry(geometry_type="LINESTRINGM", srid=4326, spatial_index=True),
        nullable=False,
    )

    # Digitized berthing-zone polygon (water side of the quay). NULL until
    # seeded; the occupancy "alongside" test falls back to the centerline buffer
    # when absent. Added in migration 0005. See app/occupancy/alongside.py.
    apron: Mapped[object | None] = mapped_column(
        Geometry(geometry_type="POLYGON", srid=4326, spatial_index=False),
    )

    # Canonical POPA station span this segment covers (feet). Informational /
    # for routing a lat/lon to the right segment.
    popa_sta_start: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    popa_sta_end: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)

    # Affine params per external stationing system.
    # Corps/USACE = scale*popa + offset  (default scale 1, offset 12040.65)
    corps_scale: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=1)
    corps_offset: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False, default=0)
    # Dock No. = scale*popa + offset  (default scale -1, offset 3365)
    dockno_scale: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=-1)
    dockno_offset: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False, default=0)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Berth(Base):
    """A named canonical station range — the operator's handle for a stretch of
    wharf. Per the core model "berths are just named station ranges": the berth
    is a label, not the allocation unit. Assigning a berth to a reservation
    copies this range onto its canonical ``station_range`` (see app/edit.py), so
    conflict detection keeps running on the range, not on the berth id.
    """

    __tablename__ = "berth"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    # Canonical POPA station span (feet); start < end (CHECK in migration 0006).
    popa_sta_start: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    popa_sta_end: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "popa_sta_start < popa_sta_end", name="berth_station_ordered"
        ),
    )


class Vessel(Base):
    """A physical vessel. MMSI is the canonical AIS key; IMO is the stable
    long-term identity. Names are non-unique and frequently misspelled, so they
    are never used as a key.
    """

    __tablename__ = "vessel"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mmsi: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True)
    imo: Mapped[int | None] = mapped_column(BigInteger, index=True)
    name: Mapped[str | None] = mapped_column(String(120))
    callsign: Mapped[str | None] = mapped_column(String(32))
    ship_type: Mapped[int | None] = mapped_column(Integer)  # AIS numeric type code
    loa: Mapped[float | None] = mapped_column(Numeric(8, 2))  # length overall, m
    beam: Mapped[float | None] = mapped_column(Numeric(8, 2))  # m
    # AIS position-reference offsets: A = antenna->bow, B = antenna->stern (m).
    # Kept individually (not just LOA=A+B) so bow/stern projection can place the
    # antenna correctly within the hull.
    dim_a: Mapped[float | None] = mapped_column(Numeric(8, 2))
    dim_b: Mapped[float | None] = mapped_column(Numeric(8, 2))
    draft: Mapped[float | None] = mapped_column(Numeric(6, 2))  # m
    destination: Mapped[str | None] = mapped_column(String(120))

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "mmsi IS NOT NULL OR imo IS NOT NULL",
            name="vessel_requires_mmsi_or_imo",
        ),
    )

    reservations: Mapped[list["Reservation"]] = relationship(back_populates="vessel")


class Reservation(Base):
    """A rectangle in (time) x (station) space: a vessel or dredge op occupying
    a station interval over a time window. The no-overlap exclusion constraint
    (confirmed-only) is added in the migration via btree_gist.

    That constraint enforces a **minimum mooring gap** (75 ft, baked into the
    constraint as migration 0007's ``GAP_FT`` — the single source of truth; there
    is deliberately no app-config mirror), not bare no-overlap: migration 0007
    pads each station range by half the gap on each side before the ``&&`` test,
    so two confirmed vessels closer than the gap collide. Empty ranges (an
    unassigned ``requested`` row) stay empty and never conflict.
    """

    __tablename__ = "reservation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vessel_id: Mapped[int | None] = mapped_column(
        ForeignKey("vessel.id", ondelete="SET NULL")
    )
    # Named berth this booking is assigned to (operator's handle). NULL = berth
    # not yet assigned. The canonical position stays station_range; assigning a
    # berth copies its range here (app/edit.py). ondelete SET NULL: dropping a
    # berth from the catalog must not delete bookings.
    berth_id: Mapped[int | None] = mapped_column(
        ForeignKey("berth.id", ondelete="SET NULL"), index=True
    )

    type: Mapped[str] = mapped_column(reservation_type_enum, nullable=False)
    # Canonical POPA station interval [stern_sta, bow_sta], feet.
    station_range: Mapped[object] = mapped_column(NUMRANGE, nullable=False)
    time_range: Mapped[object] = mapped_column(TSTZRANGE, nullable=False)

    direction: Mapped[str | None] = mapped_column(direction_enum)
    status: Mapped[str] = mapped_column(reservation_status_enum, nullable=False)
    source: Mapped[str] = mapped_column(reservation_source_enum, nullable=False)
    priority: Mapped[int | None] = mapped_column(Integer)
    cargo: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    # Stable identity for a derived (observed/AIS) reservation so re-deriving the
    # same berthing window UPDATEs instead of duplicating. NULL for planned rows;
    # uniqueness is enforced by a partial index (see migration 0002).
    derived_key: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    vessel: Mapped[Vessel | None] = relationship(back_populates="reservations")


class IntakeEvent(Base):
    """Raw inbound request exactly as received, before normalization — the
    audit + reconciliation trail. (Intake itself is a later layer; the table
    exists now so nothing is lost once it lands.)
    """

    __tablename__ = "intake_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(intake_source_enum, nullable=False)
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Stable content hash of the raw row, so an identical re-submission of a
    # manual request is deduped instead of duplicating. NULL is allowed (an
    # intake with no stable payload); uniqueness is enforced by a partial index
    # (see migration 0003), mirroring reservation.derived_key.
    dedupe_key: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed: Mapped[bool] = mapped_column(nullable=False, default=False)
    reservation_id: Mapped[int | None] = mapped_column(
        ForeignKey("reservation.id", ondelete="SET NULL")
    )
    # Soft-delete marker (migration 0010). NULL = live; set = removed by an
    # operator but kept for audit ("the evidence that a request ever arrived").
    # The dedupe unique index is partial on ``deleted_at IS NULL`` so a deleted
    # row neither blocks a re-submission nor is seen by the live API.
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class PositionReport(Base):
    """Landed AIS PositionReport. Kept raw and source-agnostic so historical
    Marine Cadastre data can feed the same table. Occupancy derivation (a later
    step) reads from here.
    """

    __tablename__ = "position_report"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vessel_id: Mapped[int | None] = mapped_column(
        ForeignKey("vessel.id", ondelete="SET NULL"), index=True
    )
    mmsi: Mapped[int | None] = mapped_column(BigInteger, index=True)

    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lon: Mapped[float] = mapped_column(Float, nullable=False)
    geom: Mapped[object] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=True), nullable=False
    )
    sog: Mapped[float | None] = mapped_column(Float)  # speed over ground, knots
    cog: Mapped[float | None] = mapped_column(Float)  # course over ground, deg
    heading: Mapped[float | None] = mapped_column(Float)  # true heading, deg
    nav_status: Mapped[int | None] = mapped_column(Integer)

    source: Mapped[str] = mapped_column(String(32), nullable=False, default="ais")
    msg_ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AuditLog(Base):
    """Append-only record of who changed what, written by every mutating
    endpoint (migration 0010).

    The whole app sits behind a single shared HTTP-Basic credential, so there was
    no record of which writes happened or by whom — a stray edit/delete left no
    trail. ``actor`` is the Basic username (NULL when auth is disabled, e.g. dev /
    tests run open); ``detail`` is free-form JSONB carrying the useful context for
    the action (the pre-edit/pre-delete ``raw`` payload, the changed field list,
    the swept reservation ids). No FK to the touched row: a reservation may be
    hard-deleted and an ``intake_event`` soft-deleted, but the log must outlive
    both. Append-only and constraint-free, so logging never blocks the write it
    records.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor: Mapped[str | None] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    entity: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict | None] = mapped_column(JSONB)
    at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkerHeartbeat(Base):
    """Per-worker liveness (migration 0011). One row per background worker
    ('ais', 'occupancy', 'intake-dataverse'), upserted each cycle — never
    appended, so the table stays tiny. ``GET /workers`` reads it back and derives
    each worker's health from how stale ``beat_at`` is against the worker's
    nominal cadence (see app/workers.py). This is liveness telemetry the console
    renders as a dot per worker; it is NOT the audit trail (that's audit_log).
    """

    __tablename__ = "worker_heartbeat"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSONB)
    beat_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DepthSurvey(Base):
    """One hydrographic condition survey (migration 0012). Surveys are
    VERSIONED, never overwritten — depths change constantly, so each upload is a
    new dated row and the draft gate reads the *latest active* survey covering a
    station range. The raw soundings aren't stored (a survey is ~400k points);
    only the reduced per-station profile (``DepthSegment``) lives in the DB.
    See app/depth/ingest.py for the reduction.
    """

    __tablename__ = "depth_survey"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    surveyed_at: Mapped[dt.date] = mapped_column(Date, nullable=False)
    source_file: Mapped[str | None] = mapped_column(Text)
    # Planar CRS the uploaded soundings were in (PostGIS transforms to 4326).
    # EPSG:2278 = Texas South Central State Plane ftUS (POPA's survey frame).
    srid: Mapped[int] = mapped_column(Integer, nullable=False, default=2278)
    datum: Mapped[str | None] = mapped_column(Text)  # vertical datum, e.g. "MLLW"
    point_count: Mapped[int | None] = mapped_column(Integer)  # soundings binned
    station_min: Mapped[float | None] = mapped_column(Numeric(12, 4))  # POPA ft
    station_max: Mapped[float | None] = mapped_column(Numeric(12, 4))  # POPA ft
    min_depth_ft: Mapped[float | None] = mapped_column(Numeric(6, 2))
    bin_ft: Mapped[float | None] = mapped_column(Numeric(8, 2))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    segments: Mapped[list["DepthSegment"]] = relationship(
        back_populates="survey", cascade="all, delete-orphan"
    )


class DepthSegment(Base):
    """A station bin of a survey, carrying the controlling (shallowest) depth in
    that bin within the berthing zone (migration 0012). ``popa_range`` is a
    half-open POPA ``numrange`` [lo, hi) in feet; the GiST index on it drives the
    draft gate's ``&&`` overlap against a reservation's ``station_range``.
    """

    __tablename__ = "depth_segment"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    survey_id: Mapped[int] = mapped_column(
        ForeignKey("depth_survey.id", ondelete="CASCADE"), nullable=False, index=True
    )
    station_range: Mapped[object] = mapped_column("popa_range", NUMRANGE, nullable=False)
    controlling_depth_ft: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    point_count: Mapped[int | None] = mapped_column(Integer)

    survey: Mapped[DepthSurvey] = relationship(back_populates="segments")


Index("ix_position_report_mmsi_ts", PositionReport.mmsi, PositionReport.msg_ts)
Index("ix_reservation_status", Reservation.status)
