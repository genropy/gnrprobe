"""The `gnrprobe` command: serve with collection on, and read a run back.

Two verbs, and they are deliberately not one. `serve` records; `report` reads.
Merging them would mean reading a file that is still being written — while the
server is up the browser's idle pings keep landing in the archive, and any
census taken earlier stops matching.

`gnr web probe` is NOT available: genropy's dispatcher discovers its commands by
walking the `gnr` package directory itself, so no external distribution can add
one. The console script is the whole CLI.
"""

import argparse
import os
import sys

from gnrprobe import VERSION, report as report_module


def build_parser():
    parser = argparse.ArgumentParser(
        prog="gnrprobe", description="what a GenroPy request actually costs")
    parser.add_argument("--version", action="version", version=VERSION)
    verbs = parser.add_subparsers(dest="verb", required=True)

    read = verbs.add_parser("report", help="read a run archive back")
    read.add_argument("which", nargs="?", default="summary",
                      choices=sorted(report_module.REPORTS) + ["exchange"],
                      help="which report (default: summary)")
    read.add_argument("archive", nargs="?",
                      help="the run archive (default: the newest one)")
    read.add_argument("--exchange", help="the exchange id, for `exchange`")
    read.add_argument("--limit", type=int, default=15)

    serve = verbs.add_parser(
        "serve", help="start a production site with both recorders installed",
        description="Everything after the instance name is genropy's own "
                    "`serveprod` command line: this adds nothing to it and "
                    "takes nothing away.")
    serve.add_argument("instance_name")
    serve.add_argument("--label", default="prod",
                       help="names the run and separates it from other runs")
    serve.add_argument("rest", nargs=argparse.REMAINDER)
    return parser


def run_report(options):
    path = options.archive or report_module.latest_archive()
    run = report_module.Run(path)
    print(f"{path}\n")
    if options.which == "exchange":
        if not options.exchange:
            raise SystemExit("the exchange report needs --exchange <id>")
        print(report_module.exchange(run, options.exchange))
        return
    reporter = report_module.REPORTS[options.which]
    if options.which == "slow":
        print(reporter(run, options.limit))
    else:
        print(reporter(run))


def run_serve(options):
    """Mint the run, install both recorders, then hand over to genropy.

    The register recorder installs here and nowhere later: `serveprod` builds
    the site before it reads the gunicorn `-c` file, in the master process and
    before the fork, so no hook is early enough. The HTTP recorder needs the
    loaded application instead, which only exists in the worker — that is what
    the shipped gunicorn config is for, and it is added only when the caller
    named no `-c` of their own.
    """
    from gnr.web.cli.gnrserveprod import main
    from gnrprobe.collector import Probe

    argv = [options.instance_name] + [a for a in options.rest if a != "--"]
    probe = Probe.start(label=options.label, sitename=options.instance_name,
                        forking=True, server="gunicorn", command_line=list(argv))
    print(f"gnrprobe: recording run {probe.run_id} into {probe.path}")
    if not ({"-c", "--config"} & set(argv)):
        argv += ["-c", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "gunicorn_conf.py")]
    else:
        print("gnrprobe: a -c was given, so the HTTP recorder is NOT installed — "
              "call post_worker_init yourself, or drop the -c")
    sys.argv = [sys.argv[0]] + argv
    main()


def main():
    options = build_parser().parse_args()
    if options.verb == "report":
        run_report(options)
    else:
        run_serve(options)


if __name__ == "__main__":
    main()
