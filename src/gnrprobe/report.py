"""Reading an archive back: where a request's cost actually goes.

Every number here comes out of lines already recorded — nothing is collected a
second time, and nothing is inferred that the trace does not carry.

Two honesty rules the output states rather than hides. The recorders add one
Python indirection per HTTP exchange and one per register call, and a session
makes roughly 1800 register calls, so ABSOLUTE latencies read high; what is
sound is the comparison between rows of the same run. And `X-GnrSqlTime` and
`X-GnrSqlCount` only carry real numbers when the site runs in debug — with debug
off they arrive as a measured zero, which is why `other_ms` swallows everything
in that case.

Read a run only once it is closed. While the server is up the browser's idle
pings keep landing in the archive and any census taken earlier stops matching.
Fold the WAL in first, so the `.sqlite` alone is the whole archive:

    sqlite3 <run>.sqlite 'PRAGMA wal_checkpoint(TRUNCATE);'
"""

import json
import os
import sqlite3
import statistics

from gnrprobe.archive import ARCHIVE_DIR_ENV, DEFAULT_ARCHIVE_DIR

UNJOINABLE = """
SELECT count(*) FROM record r
 WHERE r.kind = 'register' AND r.exchange_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM record h WHERE h.kind = 'http'
                     AND h.run_id = r.run_id AND h.exchange_id = r.exchange_id)
"""

TIMING_NOTE = ("timings include the recorders' own overhead — compare rows "
               "with each other, never with an unrecorded run")


def latest_archive(directory=None):
    """The newest run in the archive directory, for a bare `report` command."""
    directory = (directory or os.environ.get(ARCHIVE_DIR_ENV)
                 or DEFAULT_ARCHIVE_DIR)
    runs = [os.path.join(directory, name) for name in os.listdir(directory)
            if name.endswith(".sqlite")]
    if not runs:
        raise SystemExit(f"no run archive in {directory}")
    return max(runs, key=os.path.getmtime)


class Run:
    """One archive, opened read-only."""

    def __init__(self, path):
        self.path = path
        self.connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    @property
    def conditions(self):
        row = self.connection.execute(
            "SELECT run_id, started, conditions FROM run ORDER BY started").fetchone()
        return {"run_id": row[0], "started": row[1], **json.loads(row[2])}

    def lines(self, kind, where="", parameters=()):
        query = f"SELECT line FROM record WHERE kind = ?{where}"
        for (line,) in self.connection.execute(query, (kind,) + tuple(parameters)):
            yield json.loads(line)

    def count(self, query, parameters=()):
        return self.connection.execute(query, parameters).fetchone()[0]

    @property
    def register_calls_by_exchange(self):
        counts = {}
        for exchange_id, in self.connection.execute(
                "SELECT exchange_id FROM record "
                "WHERE kind = 'register' AND exchange_id IS NOT NULL"):
            counts[exchange_id] = counts.get(exchange_id, 0) + 1
        return counts


def number(value):
    """A header value that should be a number, whatever the wire carried."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def percentile(values, fraction):
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def table(headers, rows, note=None, title=None):
    """A report IS data. Rendering it is somebody else's job.

    The CLI draws it as a text table; the companion's MCP application hands the
    same dict to an agent. One source of truth for what a report contains, and
    no second implementation of the queries behind it.
    """
    return {"title": title, "headers": list(headers),
            "rows": [list(row) for row in rows], "note": note}


def render(report):
    """Draw a report as an aligned text table."""
    headers, rows, note = report["headers"], report["rows"], report.get("note")
    out = [report["title"]] if report.get("title") else []
    if not rows:
        return "\n".join(out + ["  (nothing to show)"])
    columns = [[str(h)] + [str(r[i]) for r in rows] for i, h in enumerate(headers)]
    widths = [max(len(cell) for cell in column) for column in columns]
    def draw(cells):
        return "  ".join(str(cell).ljust(widths[i]) if i == 0
                         else str(cell).rjust(widths[i])
                         for i, cell in enumerate(cells))
    out += [draw(headers), "  ".join("-" * w for w in widths)]
    out += [draw(row) for row in rows]
    if note:
        out.append(f"\n  {note}")
    return "\n".join(out)


def summary(run):
    """The declared conditions, and the census that says the run is whole."""
    conditions = run.conditions
    census = run.connection.execute(
        "SELECT kind, count(*) FROM record GROUP BY kind").fetchall()
    filtered = run.connection.execute(
        "SELECT coalesce(json_extract(line, '$.filtered'), 'recorded'), count(*) "
        "FROM record WHERE kind = 'http' GROUP BY 1").fetchall()
    surfaces = run.connection.execute(
        "SELECT coalesce(json_extract(line, '$.surface'), 'none'), count(*) "
        "FROM record WHERE kind = 'register' GROUP BY 1").fetchall()
    census_report = table(["what", "n"], [list(r) for r in census + filtered + surfaces],
                          title="census")
    unjoinable = run.count(UNJOINABLE)
    orphans = run.count("SELECT count(*) FROM record WHERE kind = 'register' "
                        "AND exchange_id IS NULL")
    faults = run.count("SELECT count(*) FROM record "
                       "WHERE json_extract(line, '$.recorder_error') IS NOT NULL")
    errors = run.count("SELECT count(*) FROM record "
                       "WHERE json_extract(line, '$.error') IS NOT NULL")
    swallowed = run.count("SELECT count(*) FROM record "
                          "WHERE json_extract(line, '$.wire_error') IS NOT NULL")
    integrity = table(["what", "n"],
                  [["register lines naming an unknown exchange", unjoinable],
                   ["register calls belonging to no exchange", orphans],
                   ["recorder faults", faults],
                   ["errors that reached the site", errors],
                   ["failures the retry loop swallowed", swallowed]],
                  note="the first must be 0; the second is the site's own boot",
                  title="integrity")
    return {"conditions": conditions, "census": census_report,
            "integrity": integrity}


def rpc(run):
    """Per RPC method: what it costs, and where the cost goes.

    `other_ms` is what is left once SQL and XML are taken off the wall time —
    the register conversation and the page's own Python. It is the column that
    says a slow call is not a slow query.
    """
    per_exchange = run.register_calls_by_exchange
    grouped = {}
    for line in run.lines("http", " AND json_extract(line, '$.filtered') IS NULL"):
        key = line.get("rpc_method") or f"[{line.get('path')}]"
        grouped.setdefault(key, []).append(line)
    rows = []
    for key, lines in grouped.items():
        durations = [line.get("duration_ms") or 0 for line in lines]
        headers = [line.get("gnr_headers") or {} for line in lines]
        def header(name, source):
            return number({k.lower(): v for k, v in source.items()}.get(name.lower()))
        sql_ms = [header("X-GnrSqlTime", h) * 1000 for h in headers]
        xml_ms = [header("X-GnrXMLTime", h) * 1000 for h in headers]
        sql_n = [header("X-GnrSqlCount", h) for h in headers]
        other = [max(0.0, d - s - x) for d, s, x in zip(durations, sql_ms, xml_ms)]
        calls = [per_exchange.get(line.get("exchange_id"), 0) for line in lines]
        rows.append([key, len(lines),
                     round(percentile(durations, .5), 1),
                     round(percentile(durations, .95), 1),
                     round(percentile(sql_ms, .5), 1),
                     int(percentile(sql_n, .5)),
                     round(percentile(xml_ms, .5), 1),
                     round(percentile(other, .5), 1),
                     percentile(calls, .5), max(calls or [0]),
                     percentile([line.get("resp_len") or 0 for line in lines], .5)])
    rows.sort(key=lambda row: row[1] * row[2], reverse=True)
    return table(["rpc method", "n", "ms p50", "ms p95", "sql ms", "sql n",
                  "xml ms", "other ms", "reg p50", "reg max", "bytes"],
                 rows, note=TIMING_NOTE, title="per RPC method")


def register(run):
    """Per register verb: how often, how long, and what it cost on the wire."""
    grouped = {}
    for line in run.lines("register"):
        key = (line.get("surface") or "none", line.get("verb"))
        grouped.setdefault(key, []).append(line)
    rows = []
    for (surface, verb), lines in grouped.items():
        durations = [line.get("duration_ms") or 0 for line in lines]
        wire = [line.get("wire_calls") or 0 for line in lines]
        rows.append([verb, surface, len(lines), round(sum(durations), 1),
                     round(percentile(durations, .5), 2),
                     sum(wire), max(wire or [0]),
                     sum(1 for line in lines if line.get("wire_error")),
                     sum(1 for line in lines if line.get("error"))])
    rows.sort(key=lambda row: row[3], reverse=True)
    return table(["verb", "surface", "n", "total ms", "ms p50", "wire",
                  "wire max", "swallowed", "errors"], rows, note=TIMING_NOTE,
                 title="per register verb")


def wire(run):
    """The failures that are invisible anywhere else.

    The register's retry loop calls the proxy up to four times and swallows
    every exception without logging or re-raising it, so from outside a fourfold
    failure and a legitimate `None` are the same thing. These lines are the
    difference.
    """
    rows = []
    for line in run.lines("register",
                          " AND (json_extract(line, '$.wire_error') IS NOT NULL"
                          " OR json_extract(line, '$.error') IS NOT NULL)"):
        rows.append([line.get("verb"), line.get("surface"),
                     line.get("wire_calls"), line.get("exchange_id") or "-",
                     line.get("wire_error") or line.get("error")])
    return table(["verb", "surface", "wire", "exchange", "failure"], rows,
                 title="failures the retry loop swallowed")


def slow(run, limit=15):
    """The slowest recorded exchanges, with the register traffic each caused."""
    per_exchange = run.register_calls_by_exchange
    rows = []
    for line in run.lines("http", " AND json_extract(line, '$.filtered') IS NULL"):
        rows.append([line.get("exchange_id"),
                     line.get("rpc_method") or line.get("path"),
                     round(line.get("duration_ms") or 0, 1),
                     per_exchange.get(line.get("exchange_id"), 0),
                     line.get("status")])
    rows.sort(key=lambda row: row[2], reverse=True)
    return table(["exchange", "what", "ms", "register calls", "status"],
                 rows[:limit], note=TIMING_NOTE, title="the slowest exchanges")


def exchange(run, exchange_id):
    """One exchange and the register conversation it caused, in order."""
    http = list(run.lines("http", " AND exchange_id = ?", (exchange_id,)))
    if not http:
        raise SystemExit(f"no exchange {exchange_id} in {run.path}")
    line = http[0]
    rows = []
    for call in sorted(run.lines("register", " AND exchange_id = ?", (exchange_id,)),
                       key=lambda c: c.get("ordinal") or 0):
        rows.append([call.get("ordinal"), call.get("surface"), call.get("verb"),
                     call.get("wire_calls"), round(call.get("duration_ms") or 0, 2),
                     str(call.get("args"))[:60]])
    return {"exchange": line,
            "calls": table(["#", "surface", "verb", "wire", "ms", "args"], rows,
                           title="the register conversation it caused")}


REPORTS = {"summary": summary, "rpc": rpc, "register": register,
           "wire": wire, "slow": slow}
