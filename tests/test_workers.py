"""Pure worker-liveness classifier tests — no database.

The health rule the /workers endpoint applies: offline when never beat, error
when the last cycle raised, stale when the beat is older than three cadences
(with a floor), ok otherwise. The DB upsert path is covered implicitly by the
endpoint; these lock the classification.
"""
from __future__ import annotations

from app.workers import KNOWN_WORKERS, classify


def test_offline_when_never_beat():
    assert classify(None, None, 60) == "offline"
    # A cadence with no age is still offline (no heartbeat row).
    assert classify("ok", None, 60) == "offline"


def test_error_status_wins_regardless_of_age():
    assert classify("error", 1, 60) == "error"
    assert classify("error", 100000, 60) == "error"


def test_fresh_beat_is_ok():
    assert classify("ok", 5, 60) == "ok"
    assert classify("ok", 180, 60) == "ok"  # exactly 3 cadences, still ok


def test_stale_when_past_three_cadences():
    assert classify("ok", 181, 60) == "stale"  # just over 3 * 60
    assert classify("ok", 800, 300) == "ok"  # 3 * 300 = 900 not yet reached
    assert classify("ok", 901, 300) == "stale"


def test_floor_protects_fast_workers():
    # A 15s-cadence worker uses the 45s floor, not 3 * 15 = 45 — same here, but
    # a smaller cadence still gets the floor rather than flapping on one slow tick.
    assert classify("ok", 44, 10) == "ok"
    assert classify("ok", 46, 10) == "stale"


def test_registry_shape():
    # Every known worker has a label and a positive cadence.
    for name, (label, cadence) in KNOWN_WORKERS.items():
        assert isinstance(name, str) and name
        assert isinstance(label, str) and label
        assert isinstance(cadence, int) and cadence > 0
