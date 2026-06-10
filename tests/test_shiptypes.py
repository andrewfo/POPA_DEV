"""Unit tests for the service-craft classification (app/shiptypes.py).

The set must mirror the UI's tug/pilot buckets (app/static/js/api.js
shipTypeCategory) — these tests pin the membership so a drift in either place
shows up as a failing expectation, not silent panel noise.
"""
from __future__ import annotations

from app.shiptypes import SERVICE_CRAFT_SQL, SERVICE_CRAFT_TYPES, is_service_craft


def test_membership_mirrors_ui_tug_and_pilot_buckets():
    # 31/32 towing, 52 tug, 56/57 "spare — local vessel" (the Sabine-Neches
    # towboat fleet), 50 pilot — and nothing else.
    assert SERVICE_CRAFT_TYPES == frozenset({31, 32, 50, 52, 56, 57})


def test_service_craft_codes():
    for t in (31, 32, 50, 52, 56, 57):
        assert is_service_craft(t)


def test_commercial_traffic_is_not_service_craft():
    for t in (70, 79, 80, 89, 60):       # cargo / tanker / passenger
        assert not is_service_craft(t)


def test_dredger_is_not_service_craft():
    # Dredging occupancy is this system's other half — never filtered.
    assert not is_service_craft(33)


def test_unknown_type_is_not_service_craft():
    # When in doubt, show the vessel.
    assert not is_service_craft(None)
    assert not is_service_craft(0)


def test_sql_rendering_matches_the_set():
    rendered = {int(x) for x in SERVICE_CRAFT_SQL.split(", ")}
    assert rendered == set(SERVICE_CRAFT_TYPES)
