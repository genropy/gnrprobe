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

Found on the way out, and it is not a detail. The bench recorded the SQLite fork
trap as a WAL-mode problem on the same file, dodged by a venv carrying sqlite
3.50.4. Measured properly here, the scope is wider: on sqlite 3.51.0 a forked
child cannot open the library AT ALL once its parent has, whatever file it aims
at, and closing the parent's handle first does not help. It is also intermittent — five children
surviving 5/5, 5/5, then 0/5 on the same machine — so the gate reads the library
version rather than probing, because a probe would let a doomed run start about
a third of the time. `fork_probe()` stays as evidence, `gnrprobe serve` refuses
where the version says it must, and the development server — one process —
became the primary target rather than the convenient one.

What was left behind, because it is the software under test rather than the
instrument: the bridge recipe, the recording worker, the register recorder
mixin, the drift check.
