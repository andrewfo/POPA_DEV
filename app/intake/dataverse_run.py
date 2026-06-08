"""Runnable AI-intake worker: Power Pages / Dataverse berth requests -> data layer.

Polls the Power Pages "Berth Request" Dataverse table over the Dataverse Web API,
hands each new row to the LLM normalizer (``app/intake/llm.py``), and records the
result through the same ``record_manual_request`` pipeline as a hand-typed
request — raw row preserved in ``intake_event`` (deduped), a low-stakes
``requested`` reservation projected with an empty station range. Then it marks the
Dataverse row "triaged" so it is parsed exactly once.

It authenticates with an Azure AD **app-registration** (client-credentials) and
only ever makes **outbound** calls to ``login.microsoftonline.com`` and the
org's ``*.crm.dynamics.com`` — so it needs no inbound exposure of the api (which
binds to 127.0.0.1) and no HTTP-connector DLP exception. The model proposes;
placement/confirm stay the operator's job.

Run:  python -m app.intake.dataverse_run
"""
from __future__ import annotations

import logging
import time

import httpx
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.intake.llm import openrouter_complete, parse_request
from app.intake.manual import record_manual_request

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("app.intake.dataverse_run")


class DataverseClient:
    """Minimal Dataverse Web API client: client-credentials token + read new rows
    + mark a row triaged. Token is cached until shortly before expiry."""

    def __init__(
        self,
        *,
        url: str,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        api_version: str,
        table: str,
        id_field: str,
        status_field: str,
        timeout: float = 30.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.table = table
        self.id_field = id_field
        self.status_field = status_field
        self._base = f"{self.url}/api/data/{api_version}"
        self._timeout = timeout
        self._token: str | None = None
        self._token_exp = 0.0

    # --- auth ---
    def _bearer(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        resp = httpx.post(
            f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": f"{self.url}/.default",
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        self._token_exp = time.time() + float(body.get("expires_in", 3600))
        return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._bearer()}",
            "Accept": "application/json",
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
        }

    # --- data ---
    def fetch_new(self, *, status_new: int, limit: int) -> list[dict]:
        """Rows whose Request Status = ``status_new``, oldest first, capped at
        ``limit``."""
        resp = httpx.get(
            f"{self._base}/{self.table}",
            params={
                "$filter": f"{self.status_field} eq {status_new}",
                "$orderby": "createdon asc",
                "$top": str(limit),
            },
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json().get("value", [])

    def mark_triaged(self, row_id: str, status_triaged: int) -> None:
        resp = httpx.patch(
            f"{self._base}/{self.table}({row_id})",
            json={self.status_field: status_triaged},
            headers={**self._headers(), "Content-Type": "application/json"},
            timeout=self._timeout,
        )
        resp.raise_for_status()


def process_batch(
    session: Session,
    client: DataverseClient,
    *,
    complete,
    source: str,
    status_new: int,
    status_triaged: int,
    limit: int,
    record=record_manual_request,
) -> dict:
    """Fetch a batch of new requests, parse + record each, then mark it triaged.

    One transaction per row: a row that records cleanly is committed and marked
    before the next is touched, so one bad row can't lose the batch. A row that
    fails to *mark* (but recorded) is left ``New`` and reappears next poll — the
    intake dedupe makes the re-record a no-op (only one wasted LLM call), so the
    failure mode is safe, not a duplicate. Returns a summary dict."""
    rows = client.fetch_new(status_new=status_new, limit=limit)
    summary = {"fetched": len(rows), "recorded": 0, "skipped": 0, "errors": 0}
    for row in rows:
        row_id = row.get(client.id_field)
        try:
            result = parse_request(row, complete=complete, source=source)
            result.form.source_raw = row  # preserve the verbatim Dataverse row
            outcome = record(session, result.form)
            session.commit()
            summary["skipped" if outcome.get("skipped") else "recorded"] += 1
            if row_id is not None:
                client.mark_triaged(row_id, status_triaged)
        except Exception:  # noqa: BLE001 - one bad row must not kill the batch
            session.rollback()
            summary["errors"] += 1
            logger.exception("failed to process Dataverse row %s", row_id)
    return summary


def _build_client(settings: Settings) -> DataverseClient:
    return DataverseClient(
        url=settings.dataverse_url,
        tenant_id=settings.dataverse_tenant_id,
        client_id=settings.dataverse_client_id,
        client_secret=settings.dataverse_client_secret,
        api_version=settings.dataverse_api_version,
        table=settings.dataverse_table,
        id_field=settings.dataverse_id_field,
        status_field=settings.dataverse_status_field,
    )


def main() -> None:
    settings = get_settings()

    # Fail fast on missing config rather than silently looping over nothing.
    if not settings.dataverse_configured:
        raise SystemExit(
            "Dataverse is not configured (need DATAVERSE_URL, DATAVERSE_TENANT_ID, "
            "DATAVERSE_CLIENT_ID, DATAVERSE_CLIENT_SECRET). Worker not started."
        )
    if not settings.openrouter_api_key:
        raise SystemExit("OPENROUTER_API_KEY is not set. Worker not started.")
    if not (settings.dataverse_status_new and settings.dataverse_status_triaged):
        raise SystemExit(
            "DATAVERSE_STATUS_NEW and DATAVERSE_STATUS_TRIAGED must be the choice "
            "option values from your Request Status column. Worker not started."
        )

    client = _build_client(settings)
    complete = openrouter_complete(
        api_key=settings.openrouter_api_key,
        model=settings.intake_llm_model,
        base_url=settings.openrouter_base_url,
    )
    poll = settings.dataverse_poll_seconds
    logger.info(
        "Dataverse intake worker up: model=%s table=%s poll=%ss",
        settings.intake_llm_model,
        settings.dataverse_table,
        poll,
    )

    while True:
        session = SessionLocal()
        try:
            summary = process_batch(
                session,
                client,
                complete=complete,
                source=settings.intake_llm_source,
                status_new=settings.dataverse_status_new,
                status_triaged=settings.dataverse_status_triaged,
                limit=settings.dataverse_batch_limit,
            )
            if summary["fetched"]:
                logger.info("batch: %s", summary)
        except Exception:  # noqa: BLE001 - keep the long-running worker alive
            logger.exception("poll failed; retrying next interval")
        finally:
            session.close()
        time.sleep(poll)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
