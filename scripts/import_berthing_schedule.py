"""One-time importer: a berth-level berthing-schedule CSV -> reservations.

Loads a columnar schedule (one row per booking) into the POPA wharf data layer
through the *existing* HTTP API — no direct DB writes, so every row gets the
normal audit trail, and berth assignment / confirm run the real gates.

Per row (two API calls, because there is no "create vessel" endpoint):
  1. POST /intake/berth-request   -> creates the vessel (by name; IMO if present)
     + a `requested` reservation carrying the time window, lands a raw
     intake_event (audit), and dedupes on re-run.
  2. PATCH /reservations/{id}      -> assign the catalog berth (fills the station
     range), set cargo, and promote. Tries `confirmed`; if the mooring-gap
     exclusion constraint rejects it (409 — an adjacent berth is occupied the
     same day and both footprints fill their whole berth), falls back to
     `tentative` so the booking still lands, berth-assigned, for an operator to
     place precisely later.

CSV columns: berth,ship,start_date,end_date,cargo,loa_ft,imo,notes
Dates are YYYY-MM-DD (date-only granularity -> a booking occupies its calendar
days inclusive: etb = start 00:00, etd = end 23:59, Central).

Dry-run by default (prints the plan, writes nothing). Pass --commit to POST.
Idempotent: intake dedupes by content hash, so re-running does not double-create.

Usage:
  # dry run against the deployment (through the TLS reverse proxy)
  python scripts/import_berthing_schedule.py \
      --csv "berthing_schedule_july_2026.csv" \
      --base https://wharf.example.org --user OPERATOR --password 'SECRET'
  # then commit
  python scripts/import_berthing_schedule.py --csv ... --base ... --user ... --password ... --commit

Base URL / credentials may also come from env: BASE_URL, OPERATOR_USER,
OPERATOR_PASSWORD. Credentials are optional (omit when auth is open, e.g. dev).
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys

import httpx

# --- Known-bad rows in the July 2026 sheet (per operator review) -------------
# Corrections keyed by ship name: (start, end, note). Rows still missing usable
# dates after this are skipped and reported; a `Cancelled` note is skipped too.
# On a clean/corrected CSV these simply don't apply.
DATE_OVERRIDES = {
    "Star Minerva": ("2026-07-29", "2026-08-02",
                     "end-date best-guess 8/2 (raw '7/29-4/2/26' malformed)"),
    "Yasa Magnolia": ("2026-07-28", "2026-07-29",
                      "end-date best-guess 7/29 (raw '7/28-19/2026' malformed)"),
    "Barge UMS": ("2026-07-10", "2026-07-11",
                  "PLACEHOLDER WINDOW - raw 'Barge UMS-7/TBA/26', exact July day "
                  "TBA; correct before relying on this"),
}
NOTE_TAG = "[imported from berthing schedule sheet]"


def berth_map(client: httpx.Client, base: str) -> dict[int, int]:
    """schedule berth number -> catalog berth_id, matched by the digits in name
    ('Berth 5' -> 5). Robust to differing ids between dev and live."""
    out = {}
    for b in client.get(f"{base}/berths").raise_for_status().json():
        m = re.search(r"(\d+)", b["name"])
        if m:
            out[int(m.group(1))] = b["id"]
    return out


def plan(rows: list[dict]) -> tuple[list[dict], list[tuple]]:
    actions, skipped = [], []
    for i, r in enumerate(rows, start=2):
        ship = r["ship"].strip()
        note = (r.get("notes") or "").strip()
        if "cancel" in note.lower():
            skipped.append((i, ship, "cancelled"))
            continue
        sd, ed = r["start_date"].strip(), r["end_date"].strip()
        extra = None
        if ship in DATE_OVERRIDES:
            sd, ed, extra = DATE_OVERRIDES[ship]
        if not sd or not ed:
            skipped.append((i, ship, "no usable dates"))
            continue
        loa = (r.get("loa_ft") or "").strip()
        imo = (r.get("imo") or "").strip()
        cargo = (r.get("cargo") or "").strip()
        notes = NOTE_TAG + (f" src-note: {note}" if note else "")
        if extra:
            notes += f" | {extra}"
        actions.append({
            "row": i, "berth": int(r["berth"].strip()), "ship": ship,
            "loa_ft": float(loa) if loa else None,
            "imo": int(imo) if imo else None,
            "etb": f"{sd}T00:00", "etd": f"{ed}T23:59",
            "cargo": cargo or None, "notes": notes,
        })
    return actions, skipped


def run(actions, bmap, client, base, commit):
    confirmed = tentative = fail = 0
    for a in actions:
        berth_id = bmap.get(a["berth"])
        tag = f"row{a['row']:>2} B{a['berth']} {a['ship']!r}"
        if berth_id is None:
            print(f"  FAIL {tag}: no catalog berth for #{a['berth']}")
            fail += 1
            continue
        if not commit:
            print(f"  PLAN {tag} -> berth_id={berth_id} {a['etb']}..{a['etd']}"
                  f" cargo={a['cargo']!r}")
            continue
        # 1. intake -> vessel + requested reservation
        form = {"source": "operator", "vessel": a["ship"],
                "etb": a["etb"], "etd": a["etd"], "notes": a["notes"]}
        if a["loa_ft"] is not None:
            form["length_ft"] = a["loa_ft"]
        if a["imo"] is not None:
            form["imo"] = a["imo"]
        ir = client.post(f"{base}/intake/berth-request", json=form)
        if ir.status_code >= 400:
            print(f"  FAIL {tag}: intake {ir.status_code} {ir.text[:160]}")
            fail += 1
            continue
        j = ir.json()
        res_id = j.get("reservation_id")
        if j.get("skipped") or res_id is None:
            print(f"  FAIL {tag}: intake made no reservation ({j.get('warnings')})")
            fail += 1
            continue
        dup = " (dedup)" if j.get("duplicate") else ""
        # 2. assign berth + cargo, try confirmed then fall back to tentative
        base_patch = {"berth_id": berth_id, "notes": a["notes"]}
        if a["cargo"]:
            base_patch["cargo"] = a["cargo"]
        pr = client.patch(f"{base}/reservations/{res_id}",
                          json={**base_patch, "status": "confirmed"})
        if pr.status_code == 409:
            pr = client.patch(f"{base}/reservations/{res_id}",
                              json={**base_patch, "status": "tentative"})
            if pr.status_code < 400:
                print(f"  TENT {tag} -> res {res_id}{dup} tentative @ berth {berth_id}"
                      f" (confirmed 409: adjacent berth occupied)")
                tentative += 1
                continue
        if pr.status_code >= 400:
            print(f"  FAIL {tag}: confirm {pr.status_code} {pr.text[:180]}")
            fail += 1
            continue
        print(f"  OK   {tag} -> res {res_id}{dup} confirmed @ berth {berth_id}")
        confirmed += 1
    return confirmed, tentative, fail


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True, help="path to the schedule CSV")
    ap.add_argument("--base", default=os.environ.get("BASE_URL", "http://localhost:8000"),
                    help="API base URL (env BASE_URL)")
    ap.add_argument("--user", default=os.environ.get("OPERATOR_USER", ""),
                    help="HTTP-Basic user (env OPERATOR_USER); omit if auth is open")
    ap.add_argument("--password", default=os.environ.get("OPERATOR_PASSWORD", ""),
                    help="HTTP-Basic password (env OPERATOR_PASSWORD)")
    ap.add_argument("--commit", action="store_true", help="actually POST/PATCH")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    auth = (args.user, args.password) if args.user else None
    with open(args.csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    with httpx.Client(timeout=30, auth=auth, follow_redirects=True) as client:
        try:
            bmap = berth_map(client, base)
        except httpx.HTTPStatusError as e:
            sys.exit(f"cannot read {base}/berths: {e.response.status_code} "
                     f"(check URL / credentials)")
        print(f"target: {base}   auth: {'on' if auth else 'open'}")
        print(f"berth map (schedule# -> id): {bmap}\n")
        actions, skipped = plan(rows)
        print(f"=== SKIPPED ({len(skipped)}) ===")
        for i, ship, why in skipped:
            print(f"  row{i:>2} {ship!r}: {why}")
        print(f"\n=== {'COMMITTING' if args.commit else 'DRY-RUN'} "
              f"({len(actions)} rows) ===")
        conf, tent, fail = run(actions, bmap, client, base, args.commit)
        if args.commit:
            print(f"\nconfirmed: {conf}   tentative: {tent}   "
                  f"failures: {fail}   skipped: {len(skipped)}")
            if fail:
                sys.exit(1)
        else:
            print(f"\nplanned: {len(actions)}   skipped: {len(skipped)}   "
                  f"(re-run with --commit to load)")


if __name__ == "__main__":
    main()
