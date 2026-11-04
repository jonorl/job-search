"""The Google Sheet where applications are tracked: who has been applied to, and recording new ones.

Access is through a service account the sheet is shared with, so it works unattended from cron.
"""

import logging
import os
import re
from datetime import date, datetime, timedelta

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

log = logging.getLogger(__name__)

API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
# Legal suffixes differ between the advert and what gets typed into the sheet.
SUFFIXES = r"\b(ltd|limited|inc|llc|plc|gmbh|corp|corporation|co|company|group|sa|bv|ag)\b"


class SheetError(Exception):
    pass


def company_key(name: str) -> str:
    name = re.sub(r"[^a-z0-9]+", " ", name.lower())
    return re.sub(r"\s+", " ", re.sub(SUFFIXES, " ", name)).strip()


class Sheet:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        key_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
        self.sheet_id = os.environ.get("APPLICATIONS_SHEET_ID")
        if not key_file or not self.sheet_id:
            raise SheetError("GOOGLE_SERVICE_ACCOUNT_FILE and APPLICATIONS_SHEET_ID must be set")
        creds = service_account.Credentials.from_service_account_file(key_file, scopes=SCOPES)
        self.session = AuthorizedSession(creds)
        self._tab: str | None = None

    def _get(self, url: str, **params) -> dict:
        r = self.session.get(url, params=params, timeout=30)
        if not r.ok:
            raise SheetError(f"{r.status_code}: {r.text[:300]}")
        return r.json()

    def tab(self) -> str:
        # The URL names the tab by gid, but the values API wants its title.
        if self._tab is None:
            meta = self._get(f"{API}/{self.sheet_id}", fields="sheets.properties(sheetId,title)")
            gid = int(self.cfg.get("sheet_gid", 0))
            titles = {s["properties"]["sheetId"]: s["properties"]["title"] for s in meta["sheets"]}
            if gid not in titles:
                raise SheetError(f"no tab with gid {gid}")
            self._tab = titles[gid]
        return self._tab

    def _rows(self) -> list[list]:
        data = self._get(
            f"{API}/{self.sheet_id}/values/'{self.tab()}'",
            valueRenderOption="UNFORMATTED_VALUE",
            dateTimeRenderOption="SERIAL_NUMBER",
        )
        return data.get("values", [])

    def _columns(self, header: list) -> dict[str, int]:
        names = {str(h).strip().lower(): i for i, h in enumerate(header)}
        columns = {}
        for field, title in self.cfg["columns"].items():
            if title.lower() not in names:
                raise SheetError(f"column {title!r} not in the header row: {header}")
            columns[field] = names[title.lower()]
        return columns

    def recent_companies(self) -> set[str]:
        """Companies applied to within the configured window, normalised with company_key."""
        rows = self._rows()
        if not rows:
            return set()
        cols = self._columns(rows[0])
        cutoff = date.today() - timedelta(days=30 * self.cfg.get("exclude_months", 12))
        recent = set()
        for row in rows[1:]:
            cell = lambda f: row[cols[f]] if cols[f] < len(row) else ""
            company, applied = str(cell("company")).strip(), _parse_date(cell("date"))
            # An undated row is more likely recent than not, so it still excludes the company.
            if company and (applied is None or applied >= cutoff):
                recent.add(company_key(company))
        return recent

    def append(self, values: dict[str, str]) -> None:
        rows = self._rows()
        if not rows:
            raise SheetError("the sheet has no header row")
        cols = self._columns(rows[0])
        row = [""] * len(rows[0])
        for field, value in values.items():
            if field in cols:
                row[cols[field]] = value
        # Bounded to the tracked columns, so summary cells beside the table don't shift where the row lands.
        last = _column_letter(len(rows[0]))
        r = self.session.post(
            f"{API}/{self.sheet_id}/values/'{self.tab()}'!A:{last}:append",
            params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
            json={"values": [row]},
            timeout=30,
        )
        if not r.ok:
            raise SheetError(f"{r.status_code}: {r.text[:300]}")


def _column_letter(n: int) -> str:
    letters = ""
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _parse_date(value) -> date | None:
    if isinstance(value, (int, float)) and value > 0:
        # Sheets serial dates count days from 30 Dec 1899.
        return date(1899, 12, 30) + timedelta(days=int(value))
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    return None


def recent_companies(cfg: dict | None) -> set[str]:
    """As Sheet.recent_companies, but an unconfigured or unreachable sheet excludes nothing."""
    if not cfg:
        return set()
    try:
        return Sheet(cfg).recent_companies()
    except Exception as e:
        log.warning("couldn't read the applications sheet, so no companies are excluded: %s", e)
        return set()
