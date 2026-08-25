"""Gunicorn config installing the HTTP recorder in every worker.

`post_worker_init` runs right after gunicorn's own `load_wsgi()`, so
`worker.wsgi` is already the site application and wrapping it here is enough
(verified on gunicorn 26.1.0, `Worker.init_process`).

The hook holds no logic of its own: installing the recorder is one call. Its
companion, the register recorder, cannot use a hook at all — see `collector.py`.

The import lives inside the hook so that merely reading this file costs nothing.
"""


def post_worker_init(worker):
    from gnrprobe.http_recorder import HttpRecorder
    worker.wsgi = HttpRecorder(worker.wsgi)
