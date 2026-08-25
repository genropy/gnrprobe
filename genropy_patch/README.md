# The flag on genropy's side

gnrprobe works with no change to genropy at all — that is what `gnrprobe serve`
is for. This directory is the OTHER half: the fifteen lines that let the
development server turn collection on, which is where a developer actually
reproduces a slow page.

Nothing here is applied automatically. It is a proposal for a PR on genropy,
kept beside the tool so the two stay in step.

## Why it cannot be a plugin

genropy's dispatcher discovers commands by walking the `gnr` package directory
itself (`gnr/core/cli/gnr.py`, `load_framework_script_tree`), and `gnr` is a
regular package, not a namespace one. So no external distribution can add a
`gnr web probe`. The flag has to live in genropy, and the reading command lives
in gnrprobe's own console script.

## The import is lazy, and the dependency is not taken

The import sits inside the branch the flag opens, so genropy without the flag
imports nothing, and genropy without gnrprobe installed prints one line saying
what to install. No entry in genropy's dependencies.

## The two install points

The order is not a preference:

- `Probe.start` must run BEFORE `GnrWsgiSite` is constructed. The site's
  `__init__` forces its register into existence — genropy's own comment there
  reads *"this is needed, don't remove"* — so an assignment made afterwards
  patches a name the site has already read.
- `probe.wrap` must be the OUTERMOST wrapper. The debugger goes inside it, so
  the exchange id is in the environ before anything reads the request.

## The diff

Against `gnr/web/serverwsgi.py`, in `Server.__init__` (beside the other
`add_argument` calls) and in `Server.serve()` (around the `GnrWsgiSite(...)`
construction). See `serverwsgi.diff` in this directory.
