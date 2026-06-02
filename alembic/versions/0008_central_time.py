"""Run the database in Central Time

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-02

The Port of Port Arthur operates on US Central Time, so the data layer is Central
end to end (see ``app/tz.py``). This sets ``America/Chicago`` as the **database**
default ``TimeZone`` — the property of the database itself, so a plain ``psql``
session, a report tool, or anything connecting without our app settings still
reads/writes in Central. ``timestamptz`` columns continue to store absolute
instants; this only changes the wall-clock they render in and how a *naive*
timestamp is interpreted on input.

The app also pins each connection to the same zone in ``app/db.py`` (belt and
braces — it does not depend on this default). ``ALTER DATABASE ... SET`` takes
effect on **new** connections, so existing sessions keep their zone until they
reconnect.

Idempotent and reversible: ``downgrade`` resets the database to UTC (the previous
implicit behaviour), not to the cluster default, so the round-trip is explicit.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TZ_NAME = "America/Chicago"


def _dbname() -> str:
    # ALTER DATABASE needs a literal name (no current_database() function form);
    # take it from the live connection so this works against any deployment DB.
    return op.get_bind().engine.url.database


def upgrade() -> None:
    op.execute(f'ALTER DATABASE "{_dbname()}" SET TimeZone TO \'{TZ_NAME}\'')


def downgrade() -> None:
    op.execute(f'ALTER DATABASE "{_dbname()}" SET TimeZone TO \'UTC\'')
