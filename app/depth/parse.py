"""Tolerant ``.XYZ`` sounding parser (pure, unit-tested without a DB).

A condition-survey ``.XYZ`` is whitespace-separated ``X Y Z`` per line — easting,
northing (planar feet, the survey's CRS) and depth (feet below datum). Real files
carry blank lines, trailing whitespace, occasional comment lines, and sometimes
extra columns; this parser skips what it can't read rather than failing the whole
import, mirroring the project's "tolerant ingest" rule for messy inputs.
"""
from __future__ import annotations

from typing import Iterable, Iterator


def parse_xyz(lines: Iterable[str]) -> Iterator[tuple[float, float, float]]:
    """Yield ``(x, y, z)`` float triples from ``.XYZ`` lines.

    Blank lines, comment lines (``#`` / ``;``), and lines whose first three
    tokens aren't all numeric are skipped. Extra columns past the third are
    ignored.
    """
    for line in lines:
        s = line.strip()
        if not s or s[0] in "#;":
            continue
        parts = s.split()
        if len(parts) < 3:
            continue
        try:
            x, y, z = float(parts[0]), float(parts[1]), float(parts[2])
        except ValueError:
            continue
        yield (x, y, z)


def parse_survey_date(filename: str):
    """Best-effort survey date from a filename. POPA names its surveys with a
    leading ``MMDDYYYY`` (e.g. ``07092023CND_POPA_B1-6_2x2.XYZ`` -> 2023-07-09).
    Returns a ``date`` or ``None`` if no leading 8-digit MMDDYYYY is present (the
    caller then asks the operator or falls back to today).
    """
    import datetime as dt
    import os
    import re

    base = os.path.basename(filename or "")
    m = re.match(r"(\d{8})", base)
    if not m:
        return None
    digits = m.group(1)
    try:
        return dt.datetime.strptime(digits, "%m%d%Y").date()
    except ValueError:
        return None
