"""Isolation checks for the HTTP recorder: filters, whole bodies, and the
promise that a failure inside the recorder — the archive writer included —
never reaches the response.

No site, no server, no site database — a minimal WSGI app, a recorder wrapping
it, and a throwaway run archive.
This is the machine evidence behind the recorder's two guarantees, so it lives
here rather than in a scratch file: evidence that is deleted is not evidence.

Run: python -m checks.http_recorder_check (or via `pytest`)
"""

import io
import json
import os
import sys

from gnrprobe.http_recorder import EXCHANGE_ENVIRON_KEY, HttpRecorder
from gnrprobe.archive import RunArchive

ARCHIVE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "temp", "http_recorder_check.sqlite")

CONDITIONS = {"label": "dev", "recorders": ["http"]}


class FailingOnce:
    """An archive whose first write fails, so the failure itself is recorded."""

    def __init__(self, archive):
        self.archive = archive
        self.failed = False

    def append_record(self, kind, record):
        if not self.failed:
            self.failed = True
            raise RuntimeError("archive down")
        self.archive.append_record(kind, record)


def serve(recorder, path, body=b"", content_type="text/xml", answer=b"<answer/>",
          method="POST"):
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "",
               "CONTENT_LENGTH": str(len(body)) if body else "",
               "CONTENT_TYPE": "application/x-www-form-urlencoded" if body else "",
               "HTTP_COOKIE": "session=abc", "wsgi.input": io.BytesIO(body)}
    seen = {}

    def app(env, start_response):
        seen["read"] = env["wsgi.input"].read()
        seen["header"] = env.get(EXCHANGE_ENVIRON_KEY)
        start_response("200 OK", [("Content-Type", content_type)])
        return [answer[:3], answer[3:]]

    def start_response(status, headers, exc_info=None):
        seen["status"] = status

    recorder.application = app
    served = b"".join(recorder(environ, start_response))
    return served, seen


def lines(archive):
    rows = archive.connection.execute(
        "SELECT line FROM record WHERE kind = 'http' ORDER BY id").fetchall()
    return [json.loads(row[0]) for row in rows]


def drop_archive():
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(ARCHIVE + suffix):
            os.remove(ARCHIVE + suffix)


def fresh():
    """A throwaway archive and a recorder writing into it."""
    drop_archive()
    archive = RunArchive(ARCHIVE, run_id="check", conditions=CONDITIONS)
    return HttpRecorder(lambda e, s: [], archive=archive), archive


failures = []


def check(label, condition):
    print(f"{'ok  ' if condition else 'FAIL'} {label}")
    if not condition:
        failures.append(label)


# 1. the happy path: whole bodies, the injected header, a distinct exchange_id
rec, arc = fresh()
served, seen = serve(rec, "/test_invoice_pg_legacy/index",
                     body=b"method=login_doLogin&login=%3Clogin%3E%3C%2Flogin%3E")
check("response relayed intact", served == b"<answer/>")
check("app could still read the request body",
      seen["read"] == b"method=login_doLogin&login=%3Clogin%3E%3C%2Flogin%3E")
check("exchange id injected into the request", bool(seen["header"]))
served2, seen2 = serve(rec, "/other", body=b"method=x")
recorded = lines(arc)
check("two lines written", len(recorded) == 2)
check("distinct exchange ids",
      recorded[0]["exchange_id"] != recorded[1]["exchange_id"])
first = recorded[0]
check("exchange id visible among the request headers",
      first["req_headers"].get("X-Gnrprobe-Exchange-Id") == seen["header"])
check("whole request body recorded",
      first["req_body"] == "method=login_doLogin&login=%3Clogin%3E%3C%2Flogin%3E")
check("whole response body recorded", first["resp_body"] == "<answer/>")
check("rpc method parsed", first["rpc_method"] == "login_doLogin")
check("form payload parsed", first["form"]["login"] == "<login></login>")
check("thread and duration recorded",
      isinstance(first["thread"], int) and first["duration_ms"] >= 0)
check("status recorded", first["status"] == 200)

# 2. the filters
rec, arc = fresh()
serve(rec, "/_rsrc/js/gnr.js", content_type="application/javascript",
      answer=b"var a=1")
serve(rec, "/favicon.ico", content_type="application/octet-stream", answer=b"icon")
serve(rec, "/_ping", content_type="text/xml",
      answer=b"<?xml version='1.0' encoding='UTF-8'?>\n<GenRoBag></GenRoBag>")
# the real idle answer on the wire: handle_ping's bare envelope
serve(rec, "/_ping", content_type="text/xml",
      answer=b"<?xml version='1.0' encoding='UTF-8'?>\n"
             b"<GenRoBag><result _T=\"NN\"></result></GenRoBag>")
serve(rec, "/_ping", content_type="text/xml",
      answer=b"<?xml version='1.0' encoding='UTF-8'?>\n"
             b"<GenRoBag><result _T=\"NN\"/></GenRoBag>")
serve(rec, "/_ping", content_type="text/xml",
      answer=b"<?xml version='1.0' encoding='UTF-8'?>\n<GenRoBag><result _T=\"NN\">"
             b"</result><dataChanges><sc_0>x</sc_0></dataChanges></GenRoBag>")
recorded = lines(arc)
stubs = [line for line in recorded if line.get("filtered")]
full = [line for line in recorded if not line.get("filtered")]
check("statics, favicon and every empty ping shape are filtered",
      len(stubs) == 5 and len(full) == 1)
check("a filtered exchange still names itself",
      all(line["exchange_id"] and line["path"] for line in stubs))
check("a filtered exchange carries no body and no headers",
      all("resp_body" not in line and "req_body" not in line
          and "resp_headers" not in line for line in stubs))
check("the filter reason is on the stub",
      [line["filtered"] for line in stubs]
      == ["static", "static", "empty_ping", "empty_ping", "empty_ping"])
check("the ping carrying a datachange is recorded whole",
      len(full) == 1 and "dataChanges" in full[0]["resp_body"])

# 3. the X-Gnr* breakdown
rec, arc = fresh()
environ = {"REQUEST_METHOD": "GET", "PATH_INFO": "/p", "QUERY_STRING": "",
           "wsgi.input": io.BytesIO(b"")}


def gnr_app(env, start_response):
    start_response("200 OK", [("Content-Type", "text/plain"),
                              ("X-GnrTime", "0.12"), ("X-GnrSqlCount", "7")])
    return [b"body"]


rec.application = gnr_app
b"".join(rec(environ, lambda s, h, e=None: None))
check("X-Gnr* headers harvested",
      lines(arc)[0]["gnr_headers"] == {"X-GnrTime": "0.12", "X-GnrSqlCount": "7"})

# 4. a failure on the reply side is recorded and does not reach the response
rec, arc = fresh()
rec.is_static = lambda path, headers: (_ for _ in ()).throw(RuntimeError("boom"))
served, _ = serve(rec, "/p", answer=b"<intact/>")
recorded = lines(arc)
check("response intact when the recorder fails on the reply",
      served == b"<intact/>")
check("the failure is recorded",
      recorded and recorded[0].get("recorder_error", "").startswith("RuntimeError"))

# 5. a failure on the request side is recorded and does not reach the response
rec, arc = fresh()
rec.read_body = lambda environ: (_ for _ in ()).throw(ValueError("nope"))
served, _ = serve(rec, "/p", body=b"method=x", answer=b"<intact/>")
recorded = lines(arc)
check("response intact when the recorder fails on the request",
      served == b"<intact/>")
check("the request-side failure is recorded",
      recorded and recorded[0].get("recorder_error", "").startswith("ValueError"))

# 6. a failure inside the ARCHIVE WRITER is recorded and does not reach the
# response — the writer is the one part of the recorder every line goes through
rec, arc = fresh()
rec.archive = FailingOnce(arc)
served, _ = serve(rec, "/p", answer=b"<intact/>")
recorded = lines(arc)
check("response intact when the archive writer fails",
      served == b"<intact/>")
check("the writer failure is recorded once the writer answers again",
      recorded and recorded[0].get("recorder_error", "").startswith("RuntimeError"))

# 7. a site that dies before returning a body still leaves its line
#
# The exchange id is injected into the environ BEFORE the application is
# called, so the register recorder is already stamping calls with it. An
# exchange that produced no HTTP line at all would leave those calls naming an
# exchange the trace does not contain — the unjoinable line the stub exists to
# prevent, on the one case macro-phase 2 most wants to compare: the request
# that errored.
rec, arc = fresh()


def exploding_app(environ, start_response):
    raise RuntimeError("the site died before there was a body")


rec.application = exploding_app
raised = None
try:
    rec({"REQUEST_METHOD": "POST", "PATH_INFO": "/sys/rpc", "QUERY_STRING": ""},
        lambda *args, **kwargs: None)
except RuntimeError as exc:
    raised = exc
recorded = lines(arc)
check("the site's failure reaches the caller untouched", isinstance(raised, RuntimeError))
check("the exchange still leaves exactly one line", len(recorded) == 1)
check("the line says no reply was ever produced",
      recorded and recorded[0]["status"] is None and recorded[0]["resp_body"] == "")
check("a request that died is not filed as a filtered exchange",
      recorded and "filtered" not in recorded[0])

# ...and the ping shape does not fool it: a /_ping that dies has no reply, so
# the empty-ping filter has nothing to read and must not claim the exchange.
rec, arc = fresh()
rec.application = exploding_app
try:
    rec({"REQUEST_METHOD": "POST", "PATH_INFO": "/_ping", "QUERY_STRING": ""},
        lambda *args, **kwargs: None)
except RuntimeError:
    pass
recorded = lines(arc)
check("a ping that died is a record, not an empty-ping stub",
      len(recorded) == 1 and "filtered" not in recorded[0])

drop_archive()
print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
