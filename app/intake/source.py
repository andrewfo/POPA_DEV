"""Pluggable intake sources.

An ``IntakeSource`` is an iterator of normalized ``BerthRequest`` records, each
carrying its verbatim source row in ``.raw``. The current source is the
SharePoint-list CSV export (Adobe Sign forms harvested by Power Automate); a
future source (direct SharePoint REST, a different export shape, operator entry)
implements the same interface and feeds the identical ingestion path — mirroring
the AIS ``AISSource`` design.
"""
from __future__ import annotations

import abc
import csv
import io
from collections.abc import Iterator

from app.intake.records import BerthRequest, parse_row


class IntakeSource(abc.ABC):
    """Yields normalized ``BerthRequest`` records. Implementations own their
    input format."""

    @abc.abstractmethod
    def stream(self) -> Iterator[BerthRequest]:
        raise NotImplementedError


class SharePointCsvSource(IntakeSource):
    """The SharePoint-list CSV export of Adobe Sign berth requests.

    These exports come out of Excel / Power Automate as either UTF-8 (sometimes
    BOM-prefixed) or a Windows codepage. We try ``utf-8-sig`` first, then fall
    back to ``cp1252`` (which decodes any byte) so a stray non-UTF-8 character
    never aborts the import. Each ``DictReader`` row is preserved verbatim as the
    record's ``raw`` payload.
    """

    def __init__(self, path: str, encoding: str | None = None) -> None:
        self.path = path
        # If the caller pins an encoding, honour it; otherwise try in order.
        self.encodings = [encoding] if encoding else ["utf-8-sig", "cp1252"]

    def _decode(self) -> str:
        # Decode the whole file up front so a fallback never re-yields rows that
        # an earlier encoding already emitted before failing mid-stream.
        with open(self.path, "rb") as fh:
            data = fh.read()
        last_err: UnicodeDecodeError | None = None
        for enc in self.encodings:
            try:
                return data.decode(enc)
            except UnicodeDecodeError as exc:
                last_err = exc
        raise last_err  # type: ignore[misc]

    def stream(self) -> Iterator[BerthRequest]:
        reader = csv.DictReader(io.StringIO(self._decode(), newline=""))
        for row in reader:
            # DictReader yields OrderedDict; normalize to a plain dict and drop
            # any None key from short/ragged rows.
            clean = {k: v for k, v in row.items() if k is not None}
            yield parse_row(clean)
