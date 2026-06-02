"""Occupancy derivation (step 5).

Reads landed ``position_report`` rows, decides when a vessel is *berthed*, and
writes ``observed`` reservations with a ``[stern_sta, bow_sta]`` station range
and ``[ETB, ETD]`` time range. No human intake involved.

Module shape:
  alongside.py  - the single swappable "is this point at the quay?" predicate
                  (a centerline buffer now; an apron polygon later).
  project.py    - pure geodesic bow/stern projection (no DB).
  detect.py     - pure hysteresis berthing-event detector (no DB).
  derive.py     - orchestrates: samples -> events -> idempotent observed rows.
  run.py        - python -m app.occupancy.run  (periodic batch).
"""
