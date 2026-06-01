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
    CheckConstraint,
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
RESERVATION_SOURCES = ("ais", "form", "phone", "operator")
DIRECTIONS = ("upstream", "downstream")
INTAKE_SOURCES = ("ais", "form", "phone", "operator")

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
    """

    __tablename__ = "reservation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vessel_id: Mapped[int | None] = mapped_column(
        ForeignKey("vessel.id", ondelete="SET NULL")
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
    received_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed: Mapped[bool] = mapped_column(nullable=False, default=False)
    reservation_id: Mapped[int | None] = mapped_column(
        ForeignKey("reservation.id", ondelete="SET NULL")
    )


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


Index("ix_position_report_mmsi_ts", PositionReport.mmsi, PositionReport.msg_ts)
Index("ix_reservation_status", Reservation.status)
