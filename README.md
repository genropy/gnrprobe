# gnrprobe

What a GenroPy request actually costs.

Two recorders and one archive. The HTTP recorder writes one line per exchange;
the register recorder writes one line per call the site makes to its site
register — verb, arguments, answer, and how many round trips it cost on the
wire. A request header joins the two, so **every register call is attributable
to the exchange that caused it**. A reading layer then answers the questions
worth asking.

Extracted from the legacy/bridge comparison bench in `genropy-asgi`, where it
was built and where it paid for four traps. See `CHANGELOG.md`.

## Why it exists

On a normal session — log in, open a table, change one field, save — a GenroPy
site makes about **1800 calls to its site register**. The median RPC exchange
costs 25 of them and the worst costs 92. Serving statics and idle pings adds 501
more that no HTTP log mentions at all.

And when a register call fails, the retry loop calls the proxy up to four times
and swallows every exception without logging or re-raising it. From outside that
funnel a fourfold failure and a legitimate `None` are indistinguishable. This is
the only place they are told apart.

## Install

Into the environment that runs the site:

```bash
pip install -e .
```

No runtime dependencies. The archive and the HTTP recorder are stdlib only; the
register recorder imports genropy, which is there by construction.

## Collect

Development server — needs the fifteen-line flag in `genropy_patch/`, not yet
proposed as a PR:

```bash
gnr web serve myinstance --collect
```

The development server is the target this tool is built around, and not for
convenience: it is ONE process. See the fork constraint below.

Production stack under gunicorn, no change to genropy:

```bash
gnrprobe serve myinstance -b 127.0.0.1:8080 -w 1 -k gthread --threads 16
```

Everything after the instance name is genropy's own `serveprod` command line:
this adds nothing to it and takes nothing away. It prints the archive it records
into — or it refuses, for the reason below.

On macOS, `PGGSSENCMODE=disable` is mandatory whenever a process forks: libpq
negotiating Kerberos in a forked child segfaults the worker on its first
request.

## The fork constraint

**On sqlite 3.51.0 a forked child cannot open SQLite at all, once its parent
has.** Not the same file — the library. Measured on macOS, python 3.13.2 and the
system 3.13: 0 successes out of 10 with the parent having connected, 6 out of 6
with a parent that never did. It arrives as SIGSEGV inside the C call, with no
exception to catch. sqlite 3.50.4 is clean.

Nothing in this code can work around it: closing the parent's handle before the
fork does not help, and neither does giving the child a file of its own.

**And it is intermittent, which is the worst part.** Forking five children that
each open their own database, three times in a row on the same machine: 5/5,
5/5, 0/5. So the gate is NOT a probe — a probe would let a doomed run start
about a third of the time, and the failure would then arrive as a worker dying
on its first recorded line and read as a defect of the recorder. `gnrprobe
serve` decides on the library version, where the measurement is stable (3.50.4
clean, 3.51.0 not), and refuses with the two ways out:

- run the site on a python whose sqlite is older, or
- collect on the development server, which does not fork.

Check where you stand — the verdict, and the evidence behind it:

```bash
python -c "from gnrprobe.archive import is_fork_safe, fork_probe; print(is_fork_safe(), fork_probe(), '/5 children survived')"
```

Run the second number a few times. If it moves, that is the intermittence, and
it is why the first number does not come from it. `GNR_PROBE_FORCE_FORK=1`
overrides the verdict for someone who has measured their own platform properly.

## What it found the first time it was pointed at a real site

sandboxpg, development server, one login and three pages opened. 88 HTTP
exchanges, 1144 register calls, no recorder faults.

- Opening the application index costs **211 register calls** and 716 ms, and
  **none of it is SQL or XML** — it is all register conversation and Python.
- A test page with **nothing on it** costs **167** of them.
- Across the whole run, `getItem` on a store was called **527 times to read 13
  distinct keys**. In one empty page render, one single key —
  `globalServices_lastChangedConfigTS.storage_gnr` — was read **54 times**, and
  every store read costs two Pyro round trips: **108 IPC round trips for one
  value that cannot change during a request.**

None of that is visible in an HTTP log, in a profiler, or in the SQL trace. It
is visible here because a register call is recorded next to the exchange that
caused it.

## Read

Stop the server first. While it runs, the browser's idle pings keep landing in
the archive and any census taken earlier stops matching. Then fold the WAL in,
so the `.sqlite` alone is the whole archive:

```bash
sqlite3 ~/genro_probe/runs/<run_id>.sqlite 'PRAGMA wal_checkpoint(TRUNCATE);'
```

With no path, every report reads the newest run.

| Command | What it answers |
|---|---|
| `gnrprobe report summary` | the declared conditions, the census, and whether the run is whole |
| `gnrprobe report rpc` | per RPC method: wall time, SQL time and count, XML time, **what is left over**, and the register calls each exchange caused |
| `gnrprobe report register` | per register verb: how often, how long, how many round trips |
| `gnrprobe report wire` | the failures the retry loop swallowed, and the errors that reached the site |
| `gnrprobe report slow` | the slowest exchanges, with the register traffic each caused |
| `gnrprobe report exchange --exchange <id>` | one exchange and its register conversation, in order |

The column that earns the tool is `other ms` in the `rpc` report: wall time minus
SQL minus XML. When it is large, the slow page is not a slow query.

## What the numbers are worth

The recorders add one Python indirection per exchange and one per register call.
**Absolute latencies read high; rows of the same run compare soundly with each
other.** Every report that shows a time says so in its own footer.

Under gunicorn, wrapping the application also costs the `wsgi.file_wrapper` fast
path — the recorder returns a generator, and gunicorn only takes that path for a
file wrapper.

`X-GnrSqlTime` and `X-GnrSqlCount` only carry real numbers when the site runs in
debug. With debug off they arrive as a measured zero and `other ms` swallows
everything.

## Where the archive lives

One SQLite file per run, `~/genro_probe/runs/<run_id>.sqlite`, or under
`GNR_PROBE_ARCHIVE_DIR`. **Keep it out of any git tree and off network mounts**:
the lines carry whole request and response bodies — the login form, the session
cookies — and WAL does not work over a network filesystem.

One table, one JSON column holding the whole line, plus a few promoted ones,
each promoted because it has a job: `run_id` and `exchange_id` to JOIN, `label`
to SEPARATE, `ts` and `thread` to ORDER, `kind`, `subject` and `status` to
FILTER. A promoted column is always a copy of what the JSON still holds. No
schema version: a line of a new shape needs no migration.

## What is filtered

Statics — recognised by the **response content type**, never by guessing from
the URL — and pings that rendered nothing get an id-only **stub**: what the
exchange was and why it was filtered, never a body. Not silence, because the
register recorder stamps those exchanges too, and with no line at all their
calls would name an exchange the trace does not contain.

Everything else is recorded whole, with no truncation anywhere.

## The traps, so nobody pays for them twice

- **`PGGSSENCMODE=disable` on macOS** — libpq negotiating Kerberos in a forked
  child segfaults. No exception to catch.
- **`sqlite3.connect` in a forked child** — see the fork constraint above. The
  bench this came from recorded it as a WAL-mode, same-file problem; it is
  neither. It does not bite where nothing forks.
- **A surviving register keeps you logged in.** The cookie plus the register are
  the whole identity, and the server holds no session state — so restarting the
  server alone leaves you inside the application and the trace contains no
  login. The register's own pickle has to go too.
- **The identity travels in two places at login.** `login_checkAvatar` carries
  flat `user=` / `password=` fields; `login_doLogin` carries them inside an XML
  Bag. A driver rewriting only the first logs every session in as the same user.

## Checks

```bash
pytest
```

81 assertions, kept as three standalone scripts under `checks/` and run by
pytest. They cover both recorders, the archive, and the four guarantees about
failure: a fault inside a recorder never reaches the response, never reaches the
site, and a site that dies before returning a body still leaves its line.
`register_recorder_check` needs genropy importable and skips where it is absent.
