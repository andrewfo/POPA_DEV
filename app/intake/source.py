"""Pluggable intake sources.

An ``IntakeSource`` is an iterator of normalized ``BerthRequest`` records, each
carrying its verbatim source row in ``.raw``. Two sources read the same
BerthRequests SharePoint list: ``SharePointCsvSource`` (a manual CSV export of
the Adobe Sign forms harvested by Power Automate) and ``SharePointGraphSource``
(the **live** list, read directly over the Microsoft Graph API). Both — and any
future source (a different export shape, operator entry) — feed the identical
ingestion path, mirroring the AIS ``AISSource`` design.
"""
from __future__ import annotations

import abc
import csv
import io
import logging
from collections.abc import Iterator

from app.intake.records import BerthRequest, parse_row

logger = logging.getLogger(__name__)


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


# --- Microsoft Graph (live SharePoint list) --------------------------------

def _stringify(value) -> str | None:
    """Render a Graph field value as the string the CSV path would have carried,
    so ``parse_row`` sees a uniform shape. Integral floats lose the ``.0`` (so a
    numeric IMO like ``9415777.0`` doesn't read as a noisy value); everything
    else is ``str()``'d. Dates arrive as ISO strings and pass through untouched —
    ``records._parse_date`` already understands ISO."""
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    s = str(value).strip()
    return s or None


def graph_item_to_row(fields: dict, column_map: dict[str, str]) -> dict:
    """Remap a Graph list-item ``fields`` object (keyed by SharePoint *internal*
    column names) onto the display-name keys ``parse_row`` expects, stringifying
    values so the row looks like a CSV export row.

    ``column_map`` is ``{internal_name: display_name}`` from the list's column
    definitions. Internal names with no mapping (Graph system fields like ``id``,
    ``ContentType``) are kept verbatim — harmless extra keys that ``parse_row``
    ignores but preserves in ``raw`` for the audit trail.
    """
    row: dict = {}
    for internal, value in fields.items():
        key = column_map.get(internal, internal)
        row[key] = _stringify(value)
    return row


class SharePointGraphSource(IntakeSource):
    """Live source: the BerthRequests SharePoint list read directly over the
    Microsoft Graph API (app-only / client-credentials).

    Yields the same normalized ``BerthRequest`` records as the CSV export — it
    only remaps Graph field *internal* names onto the display-name keys
    ``parse_row`` expects (via the list's own column definitions), so the whole
    downstream pipeline (parse, dedupe, raw landing) is unchanged.

    Requires an Azure AD app registration in the tenant with Graph
    ``Sites.Selected`` (or ``Sites.Read.All``); credentials come from config.
    The ``http`` client is injectable for testing.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        site_hostname: str,
        site_path: str,
        list_name: str,
        graph_base_url: str = "https://graph.microsoft.com/v1.0",
        login_url: str = "https://login.microsoftonline.com",
        page_size: int = 200,
        http=None,
    ) -> None:
        missing = [
            n
            for n, v in (
                ("SHAREPOINT_TENANT_ID", tenant_id),
                ("SHAREPOINT_CLIENT_ID", client_id),
                ("SHAREPOINT_CLIENT_SECRET", client_secret),
            )
            if not v
        ]
        if missing:
            raise ValueError(
                "SharePointGraphSource needs " + ", ".join(missing)
            )
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.site_hostname = site_hostname
        self.site_path = site_path.strip("/")
        self.list_name = list_name
        self.graph_base_url = graph_base_url.rstrip("/")
        self.login_url = login_url.rstrip("/")
        self.page_size = page_size
        self._http = http
        self._token: str | None = None

    # -- HTTP plumbing -------------------------------------------------------
    @property
    def http(self):
        if self._http is None:
            import httpx

            self._http = httpx.Client(timeout=30.0)
        return self._http

    def _access_token(self) -> str:
        if self._token:
            return self._token
        resp = self.http.post(
            f"{self.login_url}/{self.tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        return self._token

    def _get(self, url: str, params: dict | None = None) -> dict:
        # Absolute @odata.nextLink URLs are followed as-is; relative paths are
        # resolved against the Graph base.
        if not url.startswith("http"):
            url = f"{self.graph_base_url}/{url.lstrip('/')}"
        resp = self.http.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {self._access_token()}"},
        )
        resp.raise_for_status()
        return resp.json()

    # -- Graph resource resolution ------------------------------------------
    def _site_id(self) -> str:
        site = self._get(f"sites/{self.site_hostname}:/{self.site_path}")
        return site["id"]

    def _list_id(self, site_id: str) -> str:
        data = self._get(
            f"sites/{site_id}/lists",
            params={"$filter": f"displayName eq '{self.list_name}'"},
        )
        items = data.get("value", [])
        if not items:
            raise ValueError(
                f"SharePoint list {self.list_name!r} not found on site"
            )
        return items[0]["id"]

    def _column_map(self, site_id: str, list_id: str) -> dict[str, str]:
        """Build {internal_name: display_name} so Graph field keys map onto the
        canonical display-name keys parse_row uses."""
        data = self._get(f"sites/{site_id}/lists/{list_id}/columns")
        return {
            c["name"]: c["displayName"]
            for c in data.get("value", [])
            if c.get("name") and c.get("displayName")
        }

    def _iter_items(self, site_id: str, list_id: str) -> Iterator[dict]:
        url: str | None = f"sites/{site_id}/lists/{list_id}/items"
        params: dict | None = {"expand": "fields", "$top": self.page_size}
        while url:
            page = self._get(url, params=params)
            yield from page.get("value", [])
            url = page.get("@odata.nextLink")
            params = None  # nextLink already carries the query

    def stream(self) -> Iterator[BerthRequest]:
        site_id = self._site_id()
        list_id = self._list_id(site_id)
        column_map = self._column_map(site_id, list_id)
        logger.info(
            "graph intake: site=%s list=%s (%d columns mapped)",
            site_id, list_id, len(column_map),
        )
        for item in self._iter_items(site_id, list_id):
            fields = item.get("fields", {})
            yield parse_row(graph_item_to_row(fields, column_map))
