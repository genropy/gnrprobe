"""Collection on genropy's development server, with genropy untouched.

The `--collect` flag proposed in `genropy_patch/` is the tidy way. This is the
way that works today, on an unmodified checkout, and it is worth having anyway:
a tool that needs a framework patch before it can be tried does not get tried.

Two interceptions, both plain assignments made before genropy's entry point
runs:

- `gnr.web.gnrwsgisite.SiteRegisterClient` — the recorder in place of the
  register client. It must be assigned BEFORE the site is built, because
  `GnrWsgiSite.__init__` forces its register into existence under genropy's own
  comment "this is needed, don't remove".

- `gnr.web.serverwsgi.make_server` — werkzeug's, called at the end of
  `Server.serve()` with the finished application. Wrapping it there puts the
  HTTP recorder OUTERMOST, after the debugger, which is exactly where it has to
  be: the exchange id must be in the environ before anything reads the request.

Choosing `make_server` and not the site constructor is what makes the wrapper
outermost without knowing anything about the middleware between them.

The development server is one process — `threaded=True, processes=1` — so the
fork constraint that stops the gunicorn path does not exist here. And its debug
is on unless `--nodebug` says otherwise, which is why `X-GnrSqlTime` and
`X-GnrSqlCount` arrive with real numbers.

Under `--reload` the parent process builds no site at all (it serves `FakeApp`),
so only the reloader child records, and each restart is a new process attaching
to the same archive by path.
"""

from __future__ import annotations

import sys

from gnrprobe.collector import Probe


def install(probe):
    """Put the HTTP recorder around whatever application werkzeug is handed."""
    from gnr.web import serverwsgi

    original = serverwsgi.make_server

    def recording_make_server(host, port, app, *args, **kwargs):
        # 'FakeApp' is the string the reloader's parent hands over; it never
        # serves a request, and wrapping it would only hide what it is.
        if not isinstance(app, str):
            app = probe.wrap(app)
        return original(host, port, app, *args, **kwargs)

    serverwsgi.make_server = recording_make_server


def serve(instance_name, rest, label="dev", archive_dir=None):
    """Mint the run, install both recorders, hand over to `gnr web serve`."""
    from gnr.web.cli.gnrwsgiserve import main

    argv = [instance_name] + list(rest)
    probe = Probe.start(label=label, sitename=instance_name,
                        archive_dir=archive_dir, server="development",
                        command_line=list(argv))
    print(f"gnrprobe: recording run {probe.run_id} into {probe.path}")
    install(probe)
    sys.argv = [sys.argv[0]] + argv
    main()
