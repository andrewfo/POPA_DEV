"""Application configuration, loaded from environment / .env.

Everything that varies between environments (DB connection, AIS key, the
wharf bounding box) lives here so nothing is hard-coded deeper in the stack.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Database ---
    postgres_user: str = "popa"
    postgres_password: str = "popa"
    postgres_db: str = "popa_wharf"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    # If set, overrides the assembled URL above.
    database_url: str | None = None
    # Fail fast instead of hanging the driver default (~tens of seconds) when
    # Postgres is down/unreachable. Tunable for slow/remote PostGIS or CI bring-up.
    db_connect_timeout: int = 3

    # --- Operator auth (HTTP Basic, whole app) ---
    # One operator credential gating the whole app (map + reads + writes). Auth
    # is active ONLY when BOTH are set; either blank => disabled (open), so local
    # dev and the TestClient suite run with no credentials. A deployment turns it
    # on purely by setting these two env vars. Basic auth must run behind TLS.
    operator_user: str = Field(default="", repr=False)
    operator_password: str = Field(default="", repr=False)

    # --- AIS ingestion ---
    aisstream_api_key: str = Field(default="", repr=False)
    aisstream_url: str = "wss://stream.aisstream.io/v0/stream"

    # Bounding box around the POPA public wharf (Sabine-Neches waterway).
    # Snug to the quay on the city side (centerline spans lat 29.8541..29.8638,
    # lon -93.9445..-93.9351; NE corner kept off the city to the N/E) and pushed
    # SW out into the navigation channel — the water side where vessels
    # transit/berth — so the SW corner reaches well into the waterway.
    ais_bbox_sw_lat: float = 29.823
    ais_bbox_sw_lon: float = -93.9586
    ais_bbox_ne_lat: float = 29.866
    ais_bbox_ne_lon: float = -93.930

    # --- Conflict detection ---
    # Minimum clear separation required between two CONFIRMED vessels along the
    # wharf face (feet) — a mooring/standoff gap, not just no-overlap. Enforced
    # by the no_wharf_overlap exclusion constraint, which pads each station range
    # by half this gap on each side before the && test (migration 0007). Two
    # boats with a real gap of exactly this many feet are allowed; anything
    # tighter is a conflict. NOTE: the DB constraint bakes in the literal value;
    # changing it here is documentation only — author a new migration to change
    # what the constraint enforces.
    min_vessel_gap_ft: float = 75.0

    # --- Occupancy derivation (step 5) ---
    # "Alongside" buffer: a vessel within this many metres of the wharf
    # centerline counts as at the quay. Sized for the widest expected beam plus
    # fender/standoff allowance. This is the Option-3 swappable buffer; replace
    # the predicate in app/occupancy/alongside.py with an apron polygon later
    # without touching the detector.
    berth_buffer_m: float = 75.0
    # Berthed-state detector hysteresis. Enter when SOG <= enter knots and
    # alongside; require a sustained dwell of >= dwell minutes. Exit only after
    # a real departure (not alongside, or SOG > depart knots) sustained for
    # >= depart-gap minutes — two thresholds so it doesn't flap.
    berth_enter_sog_kn: float = 0.5
    berth_depart_sog_kn: float = 1.0
    berth_dwell_min: float = 20.0
    berth_depart_gap_min: float = 10.0

    # --- AIS verification auto-expiry (step 7 auto status-mutation) ---
    # How long after a planned reservation's window has *fully ended* it lingers
    # in the AIS-verification panel before being auto-archived. A row whose window
    # just closed still shows (as arrived / no_show) so the operator can act; once
    # it has been past its ETD by this many minutes it is swept to a terminal
    # status — `completed` if AIS saw the vessel berth, else `cancelled` (no-show)
    # — so it drops out of the live panel and lands in History instead. The sweep
    # is the deferred "auto status-mutation" half of step 7. 0 disables expiry.
    verification_grace_minutes: int = 60

    # --- Sidebar "Vessels" stat ---
    # The headline "Vessels" count is vessels *present* — those with an AIS fix
    # within this many hours — not every vessel row ever ingested (which only
    # grows, since a vessel row is never removed when a ship leaves). A departed
    # vessel stops broadcasting in the bbox, so its latest fix ages past this
    # window and it drops out of the count.
    vessel_present_window_h: float = 24.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def ais_bounding_box(self) -> list[list[list[float]]]:
        """aisstream.io subscription format: list of boxes, each [[SW],[NE]] as [lat, lon]."""
        return [
            [
                [self.ais_bbox_sw_lat, self.ais_bbox_sw_lon],
                [self.ais_bbox_ne_lat, self.ais_bbox_ne_lon],
            ]
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
