"""Dataverse intake worker orchestration tests.

Pure: the Dataverse client and the LLM call are faked, and ``record_manual_request``
is injected, so ``process_batch`` is exercised without a DB or network. The live
``DataverseClient`` HTTP and the poll loop are not exercised here.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from app.intake.dataverse_run import process_batch


class _FakeClient:
    id_field = "popa_berthrequestid"

    def __init__(self, rows):
        self._rows = rows
        self.marked: list[tuple[str, int]] = []

    def fetch_new(self, *, status_new, limit):
        return self._rows[:limit]

    def mark_triaged(self, row_id, status_triaged):
        self.marked.append((row_id, status_triaged))


def _fake_complete(text):
    return lambda messages: text


def _canned(**over):
    base = {"vessel": "SAGA ADVENTURE", "imo": 9317406, "confidence": 0.8}
    base.update(over)
    return json.dumps(base)


def test_process_batch_records_and_marks_each_row():
    rows = [
        {"popa_berthrequestid": "id-1", "popa_vesselname": "SAGA ADVENTURE"},
        {"popa_berthrequestid": "id-2", "popa_vesselname": "OTHER"},
    ]
    client = _FakeClient(rows)
    session = MagicMock()
    recorded = []

    def fake_record(sess, form):
        recorded.append(form)
        return {"duplicate": False, "reservation_id": 1}

    summary = process_batch(
        session,
        client,
        complete=_fake_complete(_canned()),
        source="email",
        status_new=1,
        status_triaged=2,
        limit=25,
        record=fake_record,
    )

    assert summary["fetched"] == 2
    assert summary["recorded"] == 2
    assert summary["errors"] == 0
    # each row committed and marked triaged with the source row preserved
    assert session.commit.call_count == 2
    assert client.marked == [("id-1", 2), ("id-2", 2)]
    assert recorded[0].source == "email"
    assert recorded[0].source_raw == rows[0]
    # AI provenance rides onto the card
    assert recorded[0].notes is not None and "AI-parsed" in recorded[0].notes


def test_process_batch_counts_skipped_separately():
    client = _FakeClient([{"popa_berthrequestid": "id-1"}])
    session = MagicMock()

    summary = process_batch(
        session,
        client,
        complete=_fake_complete(_canned()),
        source="email",
        status_new=1,
        status_triaged=2,
        limit=25,
        record=lambda s, f: {"skipped": True},
    )
    assert summary["skipped"] == 1
    assert summary["recorded"] == 0


def test_process_batch_isolates_a_failing_row():
    rows = [{"popa_berthrequestid": "id-1"}, {"popa_berthrequestid": "id-2"}]
    client = _FakeClient(rows)
    session = MagicMock()
    calls = {"n": 0}

    def flaky_record(sess, form):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return {"duplicate": False}

    summary = process_batch(
        session,
        client,
        complete=_fake_complete(_canned()),
        source="email",
        status_new=1,
        status_triaged=2,
        limit=25,
        record=flaky_record,
    )
    assert summary["errors"] == 1
    assert summary["recorded"] == 1
    # the failing row was rolled back and never marked; the good one was marked
    session.rollback.assert_called_once()
    assert client.marked == [("id-2", 2)]
