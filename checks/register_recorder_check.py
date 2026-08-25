"""Isolation checks for the register recorder: the two surfaces of the legacy
client, the store, the exchange that is absent, and the promise that a failure
inside the recorder never reaches the site.

No site, no sitedaemon, no site database — a throwaway run archive stands in for
the real one. The real `SiteRegisterClient` is used —
built past its `__init__`, with a fake Pyro proxy in place of the wire — so the
retry loop under test is genropy's own and not a copy of it.

Needs the bench venv, because the recorder imports genropy.
Run: python -m checks.register_recorder_check — needs genropy importable
"""

import json
import os
import sys
import time

from gnrprobe import register_recorder
from gnr.core.gnrbag import Bag
from gnr.web.daemon.siteregister import MAX_RETRY_ATTEMPTS
from gnr.web.daemon.siteregister_client import SiteRegisterClient
from gnrprobe.register_recorder import EXCHANGE_HEADER, RegisterRecorder, StoreRecorder
from gnrprobe.archive import RunArchive

# what FakeWire answers to get_item, so the checks name it once
ITEM_ANSWER = {"register_item_id": "p1", "data": None, "register_name": "page"}

ARCHIVE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "temp", "register_recorder_check.sqlite")

CONDITIONS = {"label": "dev", "recorders": ["register"]}


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


class FakeWire:
    """Stands in for the Pyro proxy: answers, or fails as many times as asked."""

    def __init__(self, failing=False):
        self.failing = failing
        self.calls = []

    def get_item(self, register_item_id, include_data=False, register_name=None):
        self.calls.append("get_item")
        if self.failing:
            raise ConnectionError("no daemon")
        return {"register_item_id": register_item_id, "data": None,
                "register_name": register_name or "page"}

    def drop_page(self, page_id, **kwargs):
        self.calls.append("drop_page")
        if self.failing:
            raise ConnectionError("no daemon")
        return page_id

    def lock_item(self, register_item_id, reason=None, register_name=None):
        self.calls.append("lock_item")
        return True

    def unlock_item(self, register_item_id, reason=None, register_name=None):
        self.calls.append("unlock_item")
        return True

    def set_datachange(self, *args, **kwargs):
        self.calls.append("set_datachange")
        return "changed"


class FakeSite:
    """Only what the recorder reads: the request of the current thread."""

    def __init__(self):
        self.currentRequest = None

    def enter_exchange(self, exchange_id):
        self.currentRequest = FakeRequest({EXCHANGE_HEADER: exchange_id})

    def leave_exchange(self):
        self.currentRequest = None


class FakeRequest:
    def __init__(self, headers):
        self.headers = headers


class ClientBuilder:
    """Hands the recorder a real client whose wire is the fake one."""

    def __init__(self, wire):
        self.wire = wire

    def __call__(self, site):
        client = SiteRegisterClient.__new__(SiteRegisterClient)
        client.site = site
        client.siteregister = self.wire
        client.locked_exception = RuntimeError
        # the lazy read of a register item builds a RemoteStoreBag from these;
        # without them the property raises AttributeError, which Python turns
        # into a fall-through to ServerStore.__getattr__ and a puzzling message.
        client.remotebag_uri = "PYRO:remotebag@localhost:1"
        client.hmac_key = None
        return client


def lines(archive):
    rows = archive.connection.execute(
        "SELECT line FROM record WHERE kind = 'register' ORDER BY id").fetchall()
    return [json.loads(row[0]) for row in rows]


def drop_archive():
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(ARCHIVE + suffix):
            os.remove(ARCHIVE + suffix)


def fresh(failing=False):
    """A throwaway archive, a real client on a fake wire, and the recorder."""
    drop_archive()
    archive = RunArchive(ARCHIVE, run_id="check", conditions=CONDITIONS)
    wire = FakeWire(failing=failing)
    site = FakeSite()
    register_recorder.SiteRegisterClient = ClientBuilder(wire)
    recorder = RegisterRecorder(site, archive=archive)
    register_recorder.SiteRegisterClient = SiteRegisterClient
    return recorder, site, wire, archive


failures = []


def check(label, condition):
    print(f"{'ok  ' if condition else 'FAIL'} {label}")
    if not condition:
        failures.append(label)


# 1. a method declared on the legacy class: surface `client`, one wire attempt
rec, site, wire, arc = fresh()
site.enter_exchange("ex1")
answer = rec.get_item("p1")
recorded = lines(arc)
check("an explicit method answers through the recorder",
      answer == ITEM_ANSWER)
check("an explicit method is recorded on the client surface",
      len(recorded) == 1 and recorded[0]["surface"] == "client"
      and recorded[0]["verb"] == "get_item")
check("its arguments and answer are in the line",
      recorded[0]["args"] == ["p1"] and "register_item_id" in recorded[0]["result"])
check("one round trip is counted", recorded[0]["wire_calls"] == 1)
check("the exchange is on the line", recorded[0]["exchange_id"] == "ex1")
check("the ordinal starts at one", recorded[0]["ordinal"] == 1)
check("a successful call records no error",
      recorded[0]["error"] is None and recorded[0]["wire_error"] is None)

# 2. a name the legacy __getattr__ forwards: surface `passthrough`
rec, site, wire, arc = fresh()
site.enter_exchange("ex2")
check("a forwarded verb answers", rec.drop_page("p9") == "p9")
check("it is recorded on the passthrough surface",
      lines(arc)[0]["surface"] == "passthrough" and lines(arc)[0]["verb"] == "drop_page")

# 3. the legacy retry loop: four attempts, swallowed, and the trace says so
rec, site, wire, arc = fresh(failing=True)
site.enter_exchange("ex3")
check("the legacy funnel swallows the failure and answers None",
      rec.drop_page("p9") is None)
recorded = lines(arc)[0]
check(f"the {MAX_RETRY_ATTEMPTS} retried round trips are counted",
      recorded["wire_calls"] == MAX_RETRY_ATTEMPTS)
check("the swallowed error class is recorded",
      recorded["wire_error"].startswith("ConnectionError"))

# 4. an explicit method does not retry: the exception reaches the site, recorded
rec, site, wire, arc = fresh(failing=True)
site.enter_exchange("ex4")
raised = None
try:
    rec.get_item("p1")
except Exception as exc:
    raised = exc
check("an explicit method lets the exception through",
      isinstance(raised, ConnectionError))
check("the exception is recorded on the line",
      lines(arc)[0]["error"].startswith("ConnectionError"))

# 5. no exchange: the key is absent, never faked and never stale
rec, site, wire, arc = fresh()
site.enter_exchange("ex5")
rec.get_item("p1")
site.leave_exchange()
rec.get_item("p2")
recorded = lines(arc)
check("the call inside the exchange carries it",
      recorded[0]["exchange_id"] == "ex5")
check("the call outside any exchange omits the key",
      "exchange_id" not in recorded[1])
check("the ordinal of the exchangeless call is its own sequence",
      recorded[0]["ordinal"] == 1 and recorded[1]["ordinal"] == 1)

# 6. the store: wrapped, recorded, and naming its register and item
rec, site, wire, arc = fresh()
site.enter_exchange("ex6")
store = rec.pageStore("p1")
check("the store handed back is wrapped", isinstance(store, StoreRecorder))
with store as opened:
    check("the with block yields the wrapper, so inner calls are recorded",
          opened is store)
    opened.set_datachange("a.b", value=1)
recorded = lines(arc)
verbs = [line["verb"] for line in recorded]
check("the store call and the lock are recorded",
      verbs == ["pageStore", "__enter__", "set_datachange", "__exit__"], )
store_lines = [line for line in recorded if line["surface"] == "store"]
check("every store line names its register and item",
      all(line["register_name"] == "page"
          and line["register_item_id"] == "p1" for line in store_lines))
check("the store lines belong to the exchange",
      all(line["exchange_id"] == "ex6" for line in store_lines))

# 7. a register read that is a property is recorded too
rec, site, wire, arc = fresh()
site.enter_exchange("ex7")
rec.pageStore("p1").register_item
check("a property read on the store is recorded",
      [line["verb"] for line in lines(arc)] == ["pageStore", "register_item"])

# 8. what is not a routine is handed back untouched
rec, site, wire, arc = fresh()
check("an exception class stays a class, so `except` keeps matching",
      rec.locked_exception is RuntimeError)
check("the wire object is handed back, not wrapped into a call",
      rec.siteregister.proxy is wire)
check("reading attributes writes no line", lines(arc) == [])

# 9. a failure inside the recorder never reaches the site
rec, site, wire, arc = fresh()
site.enter_exchange("ex9")
rec.get_comparable_value = lambda value: (_ for _ in ()).throw(RuntimeError("boom"))
check("the site gets its answer even when the recorder fails",
      rec.get_item("p1") == ITEM_ANSWER)
recorded = lines(arc)
check("the recorder failure is recorded instead of the line",
      len(recorded) == 1
      and recorded[0]["recorder_error"].startswith("RuntimeError"))

# 10. long values are truncated with their real length
rec, site, wire, arc = fresh()
site.enter_exchange("ex10")
rec.drop_page("x" * 5000)
recorded = lines(arc)[0]
check("a long value is truncated with its real length",
      recorded["args"][0].endswith("chars>")
      and len(recorded["args"][0]) < 5000)

# 11. values are comparable between runs: Bags as XML, no memory addresses
rec, site, wire, arc = fresh()
site.enter_exchange("ex11")
bag = Bag()
bag["a.b"] = 1
store = rec.pageStore("p1")
store.set_datachange("rootenv", value=bag)
recorded = lines(arc)
written = json.dumps(recorded)
check("a Bag argument is recorded as its XML",
      "<GenRoBag>" in recorded[1]["kwargs"]["value"]
      and "Bag object" not in recorded[1]["kwargs"]["value"])
check("no memory address reaches the trace", " at 0x" not in written)
check("the store handed back is still named in the answer",
      "ServerStore" in recorded[0]["result"])

# 12. the duration is the call alone, not the call plus our serialisation
rec, site, wire, arc = fresh()
site.enter_exchange("ex12")
plain = rec.get_comparable_value


def slow_value(value):
    time.sleep(0.05)
    return plain(value)


rec.get_comparable_value = slow_value
rec.get_item("p1")
rec.get_comparable_value = plain
check("duration_ms excludes the recorder's own serialisation",
      lines(arc)[0]["duration_ms"] < 50)

# 13. a failure inside the ARCHIVE WRITER never reaches the site either
rec, site, wire, arc = fresh()
site.enter_exchange("ex13")
rec.archive = FailingOnce(arc)
check("the site gets its answer even when the archive writer fails",
      rec.get_item("p1") == ITEM_ANSWER)
recorded = lines(arc)
check("the writer failure is recorded once the writer answers again",
      len(recorded) == 1
      and recorded[0]["recorder_error"].startswith("RuntimeError"))

drop_archive()
print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
