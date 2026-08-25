"""Turning collection on: mint the run, install the two recorders.

The whole surface a host has to call is two lines — one before the site is
built, one after — which is what keeps the change inside genropy down to a flag:

    probe = Probe.start(label='dev', sitename=sitename, port=port)
    ...
    application = probe.wrap(application)

Order is not a preference. `Probe.start` must run BEFORE `GnrWsgiSite` is
constructed, because the site's `__init__` forces its register into existence
and an assignment made afterwards patches a name already read. `wrap` must run
AFTER every other middleware, outermost, so the exchange id is in the environ
before anything reads the request — a debugger wrapping the site goes inside,
not outside.

The archive path is published in the environment as well as held here, because
the environment is the only channel that reaches a worker born by fork or by
spawn. The register recorder is handed the archive object directly, through a
`partial`: genropy builds its client as `SiteRegisterClient(site)`, with no room
for a second argument.
"""

import functools
import os
import sqlite3

from gnrprobe import conditions as conditions_module
from gnrprobe.archive import (ARCHIVE_DIR_ENV, DEFAULT_ARCHIVE_DIR, RUN_ENV,
                              RunArchive, is_fork_safe)
from gnrprobe.http_recorder import HttpRecorder


class Probe:
    """One collection run: the archive, and the two recorders writing into it."""

    def __init__(self, archive):
        self.archive = archive

    @classmethod
    def start(cls, label="dev", sitename=None, archive_dir=None, forking=False,
              **declared):
        """`forking` says the host will fork workers off this process.

        It is asked for rather than guessed, and it is checked rather than
        assumed: on some sqlite builds a forked child cannot open the library at
        all once its parent has, and the worker would die on its first recorded
        line. Refusing here costs a message; finding out later costs the run.
        """
        if forking and not is_fork_safe():
            raise SystemExit(
                "gnrprobe: this python cannot record from forked workers — "
                "sqlite crashes in a child once the parent has opened it "
                f"(sqlite {sqlite3.sqlite_version}, measured now, not guessed).\n"
                "  Either run the site on a python whose sqlite is older "
                "(3.50.4 is clean), or collect on the development server, "
                "which is one process: gnr web serve <instance> --collect")
        archive = cls.mint(label, sitename, archive_dir,
                           fork_safe=not forking or is_fork_safe(), **declared)
        cls.install_register_recorder(archive)
        return cls(archive)

    @classmethod
    def mint(cls, label, sitename=None, archive_dir=None, **declared):
        run_id = conditions_module.new_run_id(label)
        directory = (archive_dir or os.environ.get(ARCHIVE_DIR_ENV)
                     or DEFAULT_ARCHIVE_DIR)
        archive = RunArchive(os.path.join(directory, f"{run_id}.sqlite"),
                             run_id=run_id,
                             conditions=conditions_module.declare(
                                 label, sitename=sitename, **declared))
        os.environ[RUN_ENV] = archive.path
        return archive

    @staticmethod
    def install_register_recorder(archive):
        """Assign the recorder over the name every domain builds its client from.

        Imported here and not at module level: a host that only wants the HTTP
        recorder must not be made to import genropy's register client.
        """
        from gnr.web import gnrwsgisite
        from gnrprobe.register_recorder import RegisterRecorder
        gnrwsgisite.SiteRegisterClient = functools.partial(RegisterRecorder,
                                                           archive=archive)

    def wrap(self, application):
        return HttpRecorder(application, archive=self.archive)

    @property
    def path(self):
        return self.archive.path

    @property
    def run_id(self):
        return self.archive.run_id
