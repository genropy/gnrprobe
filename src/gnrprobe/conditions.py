"""The declared conditions of a run, read from where each one is true.

Two runs are comparable only under the same declaration, so the declaration is
not a comment in a README: it is stored as data in the archive's own `run` row.
Nothing here is assumed — the bind and the flags come from the command line that
was given, the database from the instance's own `instanceconfig.xml`, the
versions from the installed distributions.

`genropy_commit` is asked for as well, and it earns its place: an editable
install reports the version of the moment it was installed, not the code that
ran. When the working tree is not a git checkout the key is simply absent.
"""

import importlib.metadata
import os
import platform
import sqlite3
import subprocess
from datetime import datetime

from gnrprobe import VERSION


def new_run_id(label):
    return f"{label}-{datetime.now().strftime('%Y%m%dT%H%M%S')}"


def distribution_version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def genropy_path():
    import gnr
    return os.path.dirname(os.path.dirname(os.path.abspath(gnr.__file__)))


def git_commit(path):
    """The short commit of the tree at `path`, or None when it is not one."""
    try:
        done = subprocess.run(["git", "-C", path, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None


# Newer instances keep their configuration under `config/`; older ones keep it
# at the instance root. Both are looked for, in that order, and the first that
# exists wins — measured on sandboxpg, which is the newer shape.
INSTANCE_CONFIG_PATHS = ("config/instanceconfig.xml", "instanceconfig.xml")


def instance_database(sitename):
    """The db the instance actually points at, not the one we remember."""
    from gnr.app.pathresolver import PathResolver
    from gnr.core.gnrbag import Bag
    path = PathResolver().instance_name_to_path(sitename)
    for relative in INSTANCE_CONFIG_PATHS:
        config = os.path.join(path, relative)
        if os.path.exists(config):
            return dict(Bag(config).getAttr("db") or {})
    raise FileNotFoundError(f"no instanceconfig.xml under {path}")


def declare(label, sitename=None, **conditions):
    """The common half of every declaration, plus whatever the caller knows."""
    declared = {"label": label,
                "sitename": sitename,
                "recorders": ["http", "register"],
                "genropy": distribution_version("genropy"),
                "gnrprobe": VERSION,
                "python": platform.python_version(),
                "sqlite": sqlite3.sqlite_version,
                "platform": platform.platform()}
    try:
        declared["genropy_commit"] = git_commit(genropy_path())
    except Exception:
        declared["genropy_commit"] = None
    if sitename:
        try:
            declared["database"] = instance_database(sitename)
        except Exception as exc:
            declared["database"] = f"unreadable: {type(exc).__name__}: {exc}"
    declared.update(conditions)
    return declared
