"""Shared write plumbing for the router modules."""
from __future__ import annotations

from typing import Callable

from fastapi import HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_audit


def actor(request: Request) -> str | None:
    """The authenticated operator to attribute a write to, for the audit_log
    (``None`` when auth is open — dev / tests). Set by
    ``app/auth.BasicAuthMiddleware`` on ``request.state``."""
    return getattr(request.state, "operator", None)


def do_write(session: Session, fn, *, audit: Callable[[object], dict | None] | None = None):
    """Run a write function and commit, translating DB-layer failures into clean
    HTTP errors instead of 500s.

    A bad enum / range raises ``ValueError`` -> 422. The exclusion constraint
    ``no_wharf_overlap`` (confirmed-only, time x station) and the unique-MMSI /
    "mmsi or imo required" checks raise ``IntegrityError`` -> 409. The raw
    INSERT/UPDATE executes inside ``fn`` (before commit), so both the call and
    the commit are wrapped, and the poisoned transaction is rolled back.

    ``audit`` (optional) is called with ``fn``'s result and returns the
    ``record_audit`` kwargs to log (or ``None`` to skip — e.g. a no-op 404 or a
    deduped re-submission). The audit row lands in the SAME transaction as the
    write, so the trail commits atomically with the change it records."""
    try:
        result = fn()
        if audit is not None:
            spec = audit(result)
            if spec:
                record_audit(session, **spec)
        session.commit()
        return result
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        session.rollback()
        text_ = str(getattr(exc, "orig", exc))
        if "no_wharf_overlap" in text_:
            detail = (
                "confirmed reservation overlaps another confirmed booking "
                "(time x station). Adjust the window, the berth, or keep it "
                "tentative."
            )
        elif "uq_intake_event_dedupe_key" in text_:
            detail = "those exact details already exist on another berth request"
        elif "mmsi" in text_ and "key" in text_.lower():
            detail = "another vessel already uses that MMSI"
        elif "vessel_requires_mmsi_or_imo" in text_:
            detail = "a vessel needs at least one of MMSI or IMO"
        else:
            detail = "write violates a database constraint"
        raise HTTPException(status_code=409, detail=detail) from exc
