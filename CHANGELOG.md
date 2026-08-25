# Changelog

## 0.1.0 — unreleased

Extracted from the legacy/bridge comparison bench in `genropy-asgi`
(`benchmarks/compare/`, commit a6a53c0), where the two recorders and the archive
were built and where they paid for four traps that are documented rather than
rediscovered.

What changed on the way out:

- `run_archive.py` → `archive.py`; the promoted column `stack` became `label`,
  since a generic run has no second stack to be separated from;
- the environment channel is `GNR_PROBE_RUN` / `GNR_PROBE_ARCHIVE_DIR`, and runs
  land in `~/genro_probe/runs/`;
- the seam header is `X-Gnrprobe-Exchange-Id`;
- `serve_legacy.py` split in two: the conditions became `conditions.py`, read
  from where each one is true, and the install became `collector.py`, whose
  whole surface is two calls;
- new: `report.py`, the reading layer — the bench had raw SQL in a README;
- the three check scripts came across whole, imports apart.

What was left behind, because it is the software under test rather than the
instrument: the bridge recipe, the recording worker, the register recorder
mixin, the drift check.
