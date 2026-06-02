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

    # --- AIS ingestion ---
    aisstream_api_key: str = Field(default="", repr=False)
    aisstream_url: str = "wss://stream.aisstream.io/v0/stream"

    # Bounding box around the POPA public wharf (Sabine-Neches waterway).
    # Snug to the quay (centerline spans lat 29.8541..29.8638, lon
    # -93.9445..-93.9351) with margin pushed SE onto the channel — the water
    # side where vessels transit/berth — and kept off the city to the N/E.
    ais_bbox_sw_lat: float = 29.850
    ais_bbox_sw_lon: float = -93.946
    ais_bbox_ne_lat: float = 29.866
    ais_bbox_ne_lon: float = -93.930

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
