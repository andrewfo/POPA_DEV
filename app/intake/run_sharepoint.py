"""Runnable live berth-request intake entrypoint (Microsoft Graph).

Pulls the BerthRequests SharePoint list directly over the Graph API and lands
each item into ``intake_event`` (idempotent — safe to re-run / schedule, since
the content-hash ``dedupe_key`` makes re-polling the whole list a no-op for
unchanged rows). This replaces the manual "export CSV, then ``app.intake.run``"
step with a live connection to the same list.

  python -m app.intake.run_sharepoint
  python -m app.intake.run_sharepoint --dry-run   # parse + report only, no DB

Credentials and site/list identity come from config/.env (SHAREPOINT_* +
GRAPH_*); the source raises a clear error if they are not yet provisioned.
Like the CSV path it lands raw only (source 'form'); request→reservation
reconciliation is the deferred later layer.
"""
from __future__ import annotations

import argparse
import logging

from app.config import get_settings
from app.intake.ingest import IntakeIngestor
from app.intake.source import SharePointGraphSource

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("app.intake.run_sharepoint")


def _build_source() -> SharePointGraphSource:
    s = get_settings()
    return SharePointGraphSource(
        tenant_id=s.sharepoint_tenant_id,
        client_id=s.sharepoint_client_id,
        client_secret=s.sharepoint_client_secret,
        site_hostname=s.sharepoint_site_hostname,
        site_path=s.sharepoint_site_path,
        list_name=s.sharepoint_list_name,
        graph_base_url=s.graph_base_url,
        login_url=s.graph_login_url,
    )


def _dry_run(source: SharePointGraphSource, sample: int = 20) -> None:
    seen = warned = 0
    samples: list[str] = []
    for rec in source.stream():
        seen += 1
        if rec.warnings:
            warned += 1
            if len(samples) < sample:
                label = rec.vessel_name or "(no vessel)"
                samples.append(f"  row {seen} [{label}]: {'; '.join(rec.warnings)}")
    print(f"DRY RUN: {seen} live rows parsed, {warned} with warnings")
    if samples:
        print(f"first {len(samples)} rows with warnings:")
        print("\n".join(samples))


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Ingest the live BerthRequests SharePoint list via Graph."
    )
    ap.add_argument(
        "--source", default="form", help="intake_event.source (default: form)"
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="parse + report only; do not touch the DB"
    )
    args = ap.parse_args()

    source = _build_source()

    if args.dry_run:
        _dry_run(source)
        return

    # Imported lazily so --dry-run needs no DB configured.
    from app.db import SessionLocal

    session = SessionLocal()
    ingestor = IntakeIngestor(session, source=args.source)
    try:
        ingestor.run(source)
    finally:
        session.close()
    print(
        f"ingested: seen={ingestor.seen} inserted={ingestor.inserted} "
        f"skipped(dupe)={ingestor.skipped} rows_with_warnings={ingestor.warnings}"
    )


if __name__ == "__main__":
    main()
