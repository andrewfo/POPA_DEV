"""HTTP surface, split by concern into APIRouter modules.

``app/main.py`` owns app assembly (middleware, the OperationalError handler, the
static map mount, ``/`` and ``/health``) and includes these routers:

- ``read_only`` — reads over the data layer (segments, positions, history,
  vessels, stats, geo->station, live occupancy, reservations, berths, bbox/db
  health). Never mutates.
- ``intake`` — manual berth-request entry/edit/delete + the intake audit list
  (the phone/email channel; see ``app/intake/manual.py``).
- ``edit`` — the manual edit surface for ship data + scheduling (create/edit/
  cancel/delete vessels & reservations; see ``app/edit.py``).
- ``analysis`` — read-only conflict (``GET /conflicts``) and AIS verification
  (``GET /verification``) surfaces, plus the verification sweep write companion.
- ``depth`` — depth-survey upload (``POST /depth/surveys``) + the
  controlling-depth profile reads that back the draft gate (``app/depth/``).

Write paths route DB-layer failures through ``common.do_write`` so a bad
enum/range -> 422 and a constraint violation (incl. the confirmed-only
``no_wharf_overlap`` exclusion) -> 409 instead of a 500.
"""
from app.routers.analysis import router as analysis_router
from app.routers.depth import router as depth_router
from app.routers.edit import router as edit_router
from app.routers.intake import router as intake_router
from app.routers.read_only import router as read_only_router

__all__ = [
    "read_only_router",
    "intake_router",
    "edit_router",
    "analysis_router",
    "depth_router",
]
