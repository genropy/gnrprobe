"""The checks, run as pytest cases.

They are kept as standalone scripts on purpose: they are the machine evidence
that a fault inside a recorder never reaches the response or the site, and
rewriting 81 assertions into a different shape is how evidence gets lost. Each
script prints one line per assertion and exits non-zero on the first failure, so
pytest only has to run it and read the code.

`register_recorder_check` needs genropy importable, because the recorder
subclasses its register client. Where genropy is absent the case skips rather
than failing: gnrprobe's archive and HTTP halves are stdlib-only and must stay
testable on their own.
"""

import pathlib
import subprocess
import sys

import pytest

CHECKS = pathlib.Path(__file__).resolve().parent.parent / "checks"


def genropy_available():
    try:
        import gnr.web.daemon.siteregister_client  # noqa: F401
    except Exception:
        return False
    return True


@pytest.mark.parametrize("name", ["archive_check", "http_recorder_check",
                                  "register_recorder_check"])
def test_check_script(name):
    if name == "register_recorder_check" and not genropy_available():
        pytest.skip("genropy is not importable in this environment")
    done = subprocess.run([sys.executable, str(CHECKS / f"{name}.py")],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
