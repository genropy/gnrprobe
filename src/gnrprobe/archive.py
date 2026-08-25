"""The per-run archive: one SQLite file every recorder writes its lines into.

This is the recording TARGET, not a place lines are loaded into afterwards. A
separate load step is a step that can be forgotten, and a session measured and
unrecoverable a minute later is what happens when the window exists. Writing
straight into the archive removes it. A truncated JSONL line is also possible
when a process dies mid-write, while a half-written row is not.

One file per run, OUTSIDE any git tree — the lines carry whole bodies, the login
form and the session cookies — and on a LOCAL filesystem, because WAL does not
work over network mounts.

The `record` table is ONE JSON column holding the whole line, plus a few
promoted ones, each promoted because it has a job: `run_id` and `exchange_id`
to JOIN, `label` to SEPARATE, `ts` and `thread` to ORDER, `kind`, `subject` and
`status` to FILTER. A promoted column is a COPY of what the JSON already holds,
never the only place a value lives — otherwise the blob stops being the record
and this is a schema again. A field is promoted once it is queried often; an
occasional query reads inside the JSON with SQLite's own `json_extract`.

The one declared exception is `label`, which is a copy of the run's declared
conditions rather than of the record: adding it to the record itself would
change a record shape that has to stay identical across runs one wants to
compare.

`exchange_id` goes in as NULL when the record has no such key — the faithful
copy of an absence. The calls the site makes while building itself happen before
any exchange exists, and that is information, not a loss.

Two modes, one constructor. With a `run_id` the process MINTS the run: it
creates the file, the schema and the run row carrying the declared conditions.
Without, it ATTACHES to an existing archive and reads the run id back from it.
That is what lets a launcher mint the run in the parent and every recorder — in
a forked worker under gunicorn, in a spawned one elsewhere — attach to it by
path.

The connection is opened lazily PER PID: a handle inherited across a fork is
the same class of invisible defect as a shared file descriptor. WAL serialises
the writers, and a lock serialises the threads inside one process.

THE FORK CONSTRAINT, measured rather than inherited. On sqlite 3.51.0 (macOS,
python 3.13.2 and the system 3.13): once a process has opened ANY SQLite
connection, `sqlite3.connect` in a child it forks afterwards dies with SIGSEGV —
inside the C call, with no exception to catch. Measured 0 successes out of 10,
and 6 out of 6 when the parent had never connected. Closing the parent's handle
before the fork does NOT save it, and neither does giving the child a file of
its own: what poisons the child is that the parent ever touched the library.

Two consequences, and they shape the whole tool:

- where nothing forks — the development server is one process — this does not
  exist, and that is the target the tool is built around;
- where something forks, the parent must NEVER open the archive. `is_fork_safe`
  below answers the question by measurement, once, and the launcher refuses
  rather than handing back a run whose workers die on their first recorded line.

sqlite 3.50.4 does all of it cleanly, which is what hid the problem while this
code lived in a bench with a venv of its own.

The fork hook below closes what the parent holds before every fork. It is not
the fix — nothing in this process can be — but it keeps the parent from also
holding a half-shared handle, and it costs nothing.

What no hook can save is a worker killed outright: a child that dies without
closing loses the tail of its WAL. Measured — `os._exit` straight after a write
and the row is gone.
"""

import json
import os
import sqlite3
import threading
import weakref
from datetime import datetime

# The path of the archive of the run in progress, published by the launcher that
# minted it. The channel has to survive a fork here and a spawn on the bridge,
# so it is the environment and not an inherited object.
RUN_ENV = "GNR_PROBE_RUN"

# Where a minted run lands, unless the launcher is told otherwise.
ARCHIVE_DIR_ENV = "GNR_PROBE_ARCHIVE_DIR"
DEFAULT_ARCHIVE_DIR = os.path.join(os.path.expanduser("~"), "genro_probe", "runs")

SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
    run_id     TEXT PRIMARY KEY,
    started    TEXT,
    conditions TEXT
);
CREATE TABLE IF NOT EXISTS record (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT,
    label       TEXT,
    kind        TEXT,
    exchange_id TEXT,
    ts          TEXT,
    thread      INTEGER,
    subject     TEXT,
    status      INTEGER,
    line        TEXT
);
CREATE INDEX IF NOT EXISTS record_exchange ON record (run_id, exchange_id);
CREATE INDEX IF NOT EXISTS record_kind ON record (run_id, kind, subject);
"""

# What each kind of line answers `subject` with: the path for an HTTP exchange,
# the verb for a register call. One column, because reading the archive by hand
# filters on "what is this line about" without caring which recorder wrote it.
SUBJECT_FIELD = {"http": "path", "register": "verb"}


def is_fork_safe():
    """Can a forked child open SQLite here, once this process already has?

    Asked by running it, never by comparing version strings: the answer depends
    on the library, the platform and the build, and a version table would be
    wrong somewhere. One fork, one temporary file, and the child's exit code is
    the answer. Cheap enough to ask at startup and never cached across runs.
    """
    import subprocess
    import sys
    probe = ("import os,sqlite3,tempfile,sys\n"
             "sqlite3.connect(':memory:').execute('select 1')\n"
             "d=tempfile.mkdtemp()\n"
             "c=os.fork()\n"
             "if c==0:\n"
             "    try:\n"
             "        k=sqlite3.connect(os.path.join(d,'p.sqlite'))\n"
             "        k.execute('PRAGMA journal_mode=WAL')\n"
             "        k.execute('CREATE TABLE t (v)')\n"
             "        k.close()\n"
             "        os._exit(0)\n"
             "    except BaseException:\n"
             "        os._exit(3)\n"
             "sys.exit(0 if os.waitpid(c,0)[1]==0 else 1)\n")
    try:
        done = subprocess.run([sys.executable, "-c", probe], timeout=30,
                              capture_output=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


# Every archive alive in this process, so the fork hook can reach them. Weak, so
# holding an archive open is the caller's business and not this module's.
LIVE_ARCHIVES = weakref.WeakSet()


def close_before_fork():
    for archive in list(LIVE_ARCHIVES):
        archive.close()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(before=close_before_fork)


class RunArchive:
    """One SQLite file per run; both recorders append their lines to it."""

    def __init__(self, path, run_id=None, conditions=None):
        self.path = path
        self.lock = threading.Lock()
        self.connections = {}
        LIVE_ARCHIVES.add(self)
        if run_id is None:
            self.run_id, self.conditions = self.read_run()
        else:
            self.run_id = run_id
            self.conditions = conditions
            self.create_run()

    @property
    def label(self):
        return self.conditions.get("label")

    @property
    def connection(self):
        """The connection of THIS process, opened on first use.

        Never a handle inherited across the fork: the register recorder is born
        in the master and every worker records afterwards.
        """
        pid = os.getpid()
        connection = self.connections.get(pid)
        if connection is None:
            connection = sqlite3.connect(self.path, isolation_level=None,
                                         check_same_thread=False)
            connection.execute("PRAGMA journal_mode=WAL")
            self.connections = {pid: connection}
        return connection

    def close(self):
        """Close what this process holds; the next use opens a new connection.

        The lock is taken rather than skipped: closing a handle a thread is
        writing on is exactly the invisible defect this file exists to avoid.
        Called before a fork, and safe to call at any other time.
        """
        with self.lock:
            for connection in self.connections.values():
                connection.close()
            self.connections = {}

    def create_run(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with self.lock:
            self.connection.executescript(SCHEMA)
            self.connection.execute(
                "INSERT INTO run (run_id, started, conditions) VALUES (?, ?, ?)",
                (self.run_id, datetime.now().isoformat(),
                 json.dumps(self.conditions, ensure_ascii=False)))

    def read_run(self):
        row = self.connection.execute(
            "SELECT run_id, conditions FROM run ORDER BY started LIMIT 1").fetchone()
        return row[0], json.loads(row[1])

    def append_record(self, kind, record):
        """One row: the line as JSON, plus the columns queries need."""
        line = json.dumps(record, ensure_ascii=False, default=repr)
        with self.lock:
            self.connection.execute(
                "INSERT INTO record (run_id, label, kind, exchange_id, ts, "
                "thread, subject, status, line) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.run_id, self.label, kind, record.get("exchange_id"),
                 record.get("ts"), record.get("thread"),
                 record.get(SUBJECT_FIELD[kind]), record.get("status"), line))
