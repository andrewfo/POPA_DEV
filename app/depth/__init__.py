"""Controlling-depth data layer (step 6's draft gate).

The port's periodic hydrographic condition surveys arrive as ``.XYZ`` point
clouds (soundings in Texas South Central State Plane ftUS, EPSG:2278). This
package turns one into a versioned, per-station controlling-depth profile and
gates reservation confirmation on it:

* ``parse`` — tolerant ``.XYZ`` -> ``(x, y, z)`` reader (pure).
* ``ingest`` — project soundings onto the measured centerline (PostGIS),
  clip to the berthing zone, bin by POPA station, store the shallowest depth per
  bin as a new ``depth_survey`` + ``depth_segment`` rows. Re-runnable: each
  upload is a new dated survey (depths change constantly).
* ``gate`` — ``controlling_depth_over`` (DB lookup) + ``depth_shortfall`` (pure
  comparison) used by the confirm path in ``app/edit.py``.
"""
