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
import argparse
import json
import time
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.intake.llm import ParseResult, openrouter_complete, parse_request
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
        # Tolerate a scheme-less host in DATAVERSE_URL (e.g.
        # "org.crm.dynamics.com"): the OAuth scope and the Web API base both
        # need an absolute https URL, and a bare host yields AADSTS70011
        # ("scope ... is not valid"). Default to https when no scheme is given.
        url = url.strip().rstrip("/")
        if url and "://" not in url:
            url = f"https://{url}"
        self.url = url
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
        # One pooled client so a poll's token + fetch + per-row marks reuse a
        # keep-alive connection instead of a fresh TLS handshake per call.
        self._http = httpx.Client(timeout=timeout)

    # --- auth ---
    def _bearer(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        resp = self._http.post(
            f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": f"{self.url}/.default",
            },
            timeout=self._timeout,
        )
        # Azure returns the real reason (an AADSTS code + description) in the
        # JSON body; raise_for_status() drops it, leaving a bare 400. Surface it
        # so a bad tenant/client-id/secret/scope is diagnosable from the log.
        self._raise_for_error(resp, "Azure AD token request")
        body = resp.json()
        self._token = body["access_token"]
        self._token_exp = time.time() + float(body.get("expires_in", 3600))
        return self._token

    @staticmethod
    def _raise_for_error(resp: httpx.Response, what: str) -> None:
        """Raise with the response body if ``resp`` is an error. Both Azure AD
        and Dataverse put the actionable reason (AADSTS code / Dataverse error
        message) in the body, which raise_for_status() discards — leaving an
        opaque 400/403. Prefer a JSON ``error`` payload, fall back to raw text."""
        if not resp.is_error:
            return
        detail = resp.text
        try:
            err = resp.json()
            if isinstance(err, dict):
                # Azure: {error, error_description}; Dataverse: {error: {message}}
                inner = err.get("error")
                if isinstance(inner, dict):
                    detail = inner.get("message", detail)
                elif inner:
                    detail = f"{inner}: {err.get('error_description', detail)}"
        except Exception:  # noqa: BLE001 - non-JSON body; fall back to text
            pass
        raise RuntimeError(f"{what} failed ({resp.status_code}): {detail}")

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
        resp = self._http.get(
            f"{self._base}/{self.table}",
            params={
                "$filter": f"{self.status_field} eq {status_new}",
                "$orderby": "createdon asc",
                "$top": str(limit),
            },
            headers=self._headers(),
        )
        self._raise_for_error(resp, f"Dataverse read of {self.table}")
        return resp.json().get("value", [])

    def mark_triaged(self, row_id: str, status_triaged: int) -> None:
        resp = self._http.patch(
            f"{self._base}/{self.table}({row_id})",
            json={self.status_field: status_triaged},
            headers={**self._headers(), "Content-Type": "application/json"},
        )
        self._raise_for_error(resp, f"Dataverse update of {self.table}")


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
    recorded_ids: set | None = None,
    dry_run: bool = False,
) -> dict:
    """Fetch a batch of new requests, parse + record each, then mark it triaged.

    One transaction per row: a row that records cleanly is committed and marked
    before the next is touched, so one bad row can't lose the batch.

    A row that records but then fails to *mark* triaged is left ``New`` and
    reappears next poll. To avoid re-billing the LLM for it on every poll (a real
    cost if the mark keeps failing, e.g. a missing write permission), its id is
    remembered in ``recorded_ids`` (hoist this across polls — see ``main``); on a
    later poll such a row is only re-marked, never re-parsed/re-recorded. The
    intake dedupe is still the backstop if the id cache is lost (process
    restart). Returns a summary dict.

    ``dry_run`` parses + prints each row's extraction and writes nothing — no
    record, no commit, no triage-mark — so you can eyeball the LLM output against
    real rows before turning the loop on. ``session`` is unused in that mode (pass
    ``None``)."""
    if recorded_ids is None:
        recorded_ids = set()
    rows = client.fetch_new(status_new=status_new, limit=limit)
    summary = {
        "fetched": len(rows),
        "recorded": 0,
        "skipped": 0,
        "remarked": 0,
        "errors": 0,
        "previewed": 0,
    }
    for row in rows:
        row_id = row.get(client.id_field)
        if dry_run:
            # Parse + show, never write. A parse failure here is just reported.
            try:
                result = parse_request(row, complete=complete, source=source)
                _print_parse(row_id, result)
                summary["previewed"] += 1
            except Exception:  # noqa: BLE001 - report and keep previewing
                summary["errors"] += 1
                logger.exception("dry-run parse failed for %s", row_id)
            continue
        # Already recorded on a prior poll but its triage-mark failed: retry only
        # the mark, never re-bill the LLM or re-record.
        if row_id is not None and row_id in recorded_ids:
            try:
                client.mark_triaged(row_id, status_triaged)
                recorded_ids.discard(row_id)
                summary["remarked"] += 1
            except Exception:  # noqa: BLE001 - keep going; retry again next poll
                summary["errors"] += 1
                logger.exception("retry mark_triaged failed for %s", row_id)
            continue
        try:
            result = parse_request(row, complete=complete, source=source)
            result.form.source_raw = row  # preserve the verbatim Dataverse row
            outcome = record(session, result.form)
            session.commit()
            # A duplicate (dedupe no-op) is a skip, not a fresh record — its path
            # returns ``duplicate`` rather than ``skipped``, so count both here.
            no_op = outcome.get("skipped") or outcome.get("duplicate")
            summary["skipped" if no_op else "recorded"] += 1
            if row_id is not None:
                try:
                    client.mark_triaged(row_id, status_triaged)
                except Exception:  # noqa: BLE001 - recorded; just remember to re-mark
                    recorded_ids.add(row_id)
                    summary["errors"] += 1
                    logger.exception(
                        "recorded row %s but mark_triaged failed; will re-mark "
                        "next poll without re-parsing",
                        row_id,
                    )
        except Exception:  # noqa: BLE001 - one bad row must not kill the batch
            session.rollback()
            summary["errors"] += 1
            logger.exception("failed to process Dataverse row %s", row_id)
    return summary


def _print_parse(label, result: ParseResult) -> None:
    """Print one parse for human eyeballing (dry-run / --input). Shows only the
    fields the model actually populated, plus confidence and any caveats."""
    fields = {
        k: v
        for k, v in result.form.model_dump(mode="json").items()
        if v not in (None, False, "") and k != "source_raw"
    }
    print(f"\n=== {label} ===")
    print(f"confidence: {result.confidence:.0%}")
    print("parsed fields:")
    print(json.dumps(fields, indent=2, ensure_ascii=False, default=str))
    if result.notes:
        print("notes:")
        for n in result.notes:
            print(f"  - {n}")


def run_input_file(path: str, *, complete, source: str) -> None:
    """Parse rows from a local JSON file (a single object or a list of objects)
    through the LLM and print each result. Needs only OpenRouter — no Dataverse,
    no DB — so you can validate the prompt/model against sample rows today."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data if isinstance(data, list) else [data]
    print(f"parsing {len(rows)} row(s) from {path}")
    for i, row in enumerate(rows):
        label = row.get("Vessel Name") or row.get("vessel") or f"row[{i}]"
        try:
            result = parse_request(row, complete=complete, source=source)
            _print_parse(label, result)
        except Exception:  # noqa: BLE001 - report and continue to the next sample
            logger.exception("parse failed for %s", label)


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


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.intake.dataverse_run",
        description="Poll the Power Pages / Dataverse berth-request table, "
        "LLM-normalize each new row, and record it. Default: loop forever.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single batch then exit (default: poll forever)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="parse and PRINT each row; write nothing (no record, no triage-mark). "
        "Needs Dataverse read + OpenRouter, but no DB.",
    )
    parser.add_argument(
        "--input",
        metavar="FILE",
        help="parse rows from a local JSON file (one object or a list) instead of "
        "polling Dataverse; prints results, writes nothing. Needs only OpenRouter "
        "- no Dataverse config, no DB. Good for testing the prompt/model today.",
    )
    parser.add_argument(
        "--model",
        help="override INTAKE_LLM_MODEL for this run (e.g. a different OpenRouter id)",
    )
    return parser.parse_args(argv)


def main(argv=None) -> None:
    args = _parse_args(argv)
    settings = get_settings()
    model = args.model or settings.intake_llm_model

    # Every mode calls the LLM, so OpenRouter is always required.
    if not settings.openrouter_api_key:
        raise SystemExit("OPENROUTER_API_KEY is not set.")
    complete = openrouter_complete(
        api_key=settings.openrouter_api_key,
        model=model,
        base_url=settings.openrouter_base_url,
    )

    # --input: parse local sample rows; no Dataverse, no DB.
    if args.input:
        run_input_file(args.input, complete=complete, source=settings.intake_llm_source)
        return

    # All other modes talk to Dataverse — validate that config now.
    if not settings.dataverse_configured:
        raise SystemExit(
            "Dataverse is not configured (need DATAVERSE_URL, DATAVERSE_TENANT_ID, "
            "DATAVERSE_CLIENT_ID, DATAVERSE_CLIENT_SECRET). Use --input to test the "
            "LLM without Dataverse."
        )
    if settings.dataverse_status_new == settings.dataverse_status_triaged:
        raise SystemExit(
            "DATAVERSE_STATUS_NEW and DATAVERSE_STATUS_TRIAGED must differ (and be "
            "the choice option values from your Request Status column); equal values "
            "mean every row stays in the polled state and is re-parsed each poll."
        )

    client = _build_client(settings)
    common = dict(
        complete=complete,
        source=settings.intake_llm_source,
        status_new=settings.dataverse_status_new,
        status_triaged=settings.dataverse_status_triaged,
        limit=settings.dataverse_batch_limit,
    )

    # --dry-run: fetch + parse + print, write nothing (so no DB session needed).
    if args.dry_run:
        logger.info("dry-run: model=%s table=%s (no writes)", model, settings.dataverse_table)
        summary = process_batch(None, client, dry_run=True, **common)
        logger.info("dry-run batch: %s", summary)
        return

    def _one_batch(recorded_ids: set) -> dict:
        session = SessionLocal()
        try:
            return process_batch(session, client, recorded_ids=recorded_ids, **common)
        finally:
            session.close()

    if args.once:
        logger.info("single batch: model=%s table=%s", model, settings.dataverse_table)
        logger.info("batch: %s", _one_batch(set()))
        return

    poll = settings.dataverse_poll_seconds
    logger.info(
        "Dataverse intake worker up: model=%s table=%s poll=%ss",
        model,
        settings.dataverse_table,
        poll,
    )
    # Ids of rows recorded but not yet successfully marked triaged, carried across
    # polls so a stuck mark is retried without re-billing the LLM (see
    # process_batch). Bounded by the count of genuinely stuck rows.
    recorded_ids: set = set()
    while True:
        try:
            summary = _one_batch(recorded_ids)
            if summary["fetched"]:
                logger.info("batch: %s", summary)
        except Exception:  # noqa: BLE001 - keep the long-running worker alive
            logger.exception("poll failed; retrying next interval")
        time.sleep(poll)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
