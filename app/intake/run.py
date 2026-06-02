"""Runnable berth-request intake entrypoint.

Lands a SharePoint-list CSV export of Adobe Sign berth requests into
``intake_event`` (idempotent — safe to re-run on each fresh export).

  python -m app.intake.run path/to/BerthRequests.csv
  python -m app.intake.run path/to/BerthRequests.csv --dry-run   # parse + report only, no DB

``--dry-run`` is the "parse → human review" step from the project conventions:
it parses every row and reports the warnings (junk IMO, blank/odd berths,
missing names, unparseable dates) without touching the database.
"""
from __future__ import annotations

import argparse
import logging

from app.intake.ingest import IntakeIngestor
from app.intake.source import SharePointCsvSource

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("app.intake.run")


def _dry_run(source: SharePointCsvSource, sample: int = 20) -> None:
    seen = warned = 0
    samples: list[str] = []
    for rec in source.stream():
        seen += 1
        if rec.warnings:
            warned += 1
            if len(samples) < sample:
                label = rec.vessel_name or "(no vessel)"
                samples.append(f"  row {seen} [{label}]: {'; '.join(rec.warnings)}")
    print(f"DRY RUN: {seen} rows parsed, {warned} with warnings")
    if samples:
        print(f"first {len(samples)} rows with warnings:")
        print("\n".join(samples))


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest a berth-request CSV export.")
    ap.add_argument("csv_path", help="path to the SharePoint-list CSV export")
    ap.add_argument(
        "--source", default="form", help="intake_event.source (default: form)"
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="parse + report only; do not touch the DB"
    )
    args = ap.parse_args()

    source = SharePointCsvSource(args.csv_path)

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
