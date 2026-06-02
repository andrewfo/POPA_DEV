---
name: add-stationing-system
description: Add a new external stationing system (an affine reconciliation to canonical POPA station) to the wharf data layer. Use when a new reference frame must convert to/from POPA station — e.g. another agency's chainage, a new dock-numbering scheme, or revised crosswalk offsets. All stationing math stays in app/crosswalk.py with affine params on wharf_segment; never inline a transform elsewhere.
---

# Adding an external stationing system

Canonical position is **POPA station** (feet) along the measured wharf
centerline. Every external system is an **affine function** of POPA station:

```
external_value = scale * popa_station + offset
```

Existing systems (verified against the port crosswalk):
- **Corps/USACE:** scale `1`, offset `12040.65` (constant offset, same direction)
- **Dock No.:** scale `-1`, offset `3365` (reversed)

**Rule from CLAUDE.md: new stationing systems = new affine params on
`wharf_segment`, never new inline math. Extend `crosswalk.py` and its tests
together.** Params are stored **per segment** so the math generalizes — do not
hard-code one transform in business logic.

## Steps

1. **Schema:** add `<sys>_scale` / `<sys>_offset` columns to `wharf_segment` in
   `app/models.py` and a new Alembic migration (use the
   [new-migration](../new-migration/SKILL.md) skill). Match the
   `Numeric(12, 6)` scale / `Numeric(12, 4)` offset precision of the existing
   `corps_*` / `dockno_*` columns. Default them to the published crosswalk
   values.

2. **Crosswalk module (`app/crosswalk.py`):**
   - Add `DEFAULT_<SYS>_SCALE` / `DEFAULT_<SYS>_OFFSET` constants and a
     `DEFAULT_<SYS> = AffineParams(...)`.
   - Add the convenience wrappers `popa_to_<sys>(...)` and `<sys>_to_popa(...)`
     mirroring `popa_to_corps` / `corps_to_popa`. Reuse `AffineParams.from_popa`
     / `to_popa` — do not write the arithmetic by hand. The `AffineParams`
     dataclass already rejects a zero scale (non-invertible).

3. **Seed / per-segment params:** update `app/seed/wharf_seed.py` so seeded
   segments carry the new params, and ensure any code that builds
   `AffineParams` from a segment row reads the new columns.

4. **Tests (`tests/test_crosswalk.py`):** add round-trip tests
   (`to_popa(from_popa(x)) ≈ x`), known reference points from the port
   crosswalk, and the reversed-direction case if `scale < 0`. Stationing-
   notation helpers (`parse_station` / `format_station`) are unaffected unless
   the new system uses different notation.

## Don't

- Don't add stationing arithmetic outside `crosswalk.py`.
- Don't assume the global default params for a segment — read the segment's own
  `*_scale` / `*_offset`. Defaults are only a convenience for the published
  values.
