import argparse
import os
import sys
import traceback
from collections.abc import Sequence
from datetime import UTC, datetime

from citeforge import curated as curated_mod
from citeforge import identify, publish, report, roster, state
from citeforge.http import Http
from citeforge.run import ConfigError, Settings, run


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="citeforge", description="Build per-author BibTeX from open registries.")
    parser.add_argument("--input", default="data/input.csv")
    parser.add_argument("--output", default="output")
    parser.set_defaults(command="build", group="data/a2i2.csv", curated="data/curated.toml", state="state", run="run")
    sub = parser.add_subparsers(dest="command")
    ident = sub.add_parser("identify", help="print ORCID and OpenAlex proposals for the roster; writes nothing")
    ident.add_argument("--input", default=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    ns = _parser().parse_args(argv)
    try:
        return _identify(ns) if ns.command == "identify" else _build(ns)
    except (roster.RosterError, curated_mod.CuratedError, ConfigError) as exc:
        print(f"citeforge: {exc}", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1


def _identify(ns: argparse.Namespace) -> int:
    settings = Settings.from_args(ns, os.environ, datetime.now(UTC).date())
    members = roster.load(settings.input, strict=False)
    lab = curated_mod.load(settings.curated).lab_rors
    with open(os.devnull, "w", encoding="utf-8") as log:
        http = Http(log, {"api.openalex.org": settings.openalex_key})
        try:
            identify.print_table(identify.propose(members, http, lab), sys.stdout)
        finally:
            http.close()
    return 0


def _build(ns: argparse.Namespace) -> int:
    settings = Settings.from_args(ns, os.environ, datetime.now(UTC).date())
    members = roster.load(settings.input)
    group = roster.load_group(settings.group, members)
    curated = curated_mod.load(settings.curated)
    curated.check_members(members)
    previous = state.load(settings.state)
    folders = [*(m.dirname for m in members), publish.GROUP_DIR]
    existing = publish.on_disk(settings.output, folders)
    settings.run.mkdir(parents=True, exist_ok=True)
    with (settings.run / "requests.jsonl").open("w", encoding="utf-8") as log:
        http = Http(log, {"api.openalex.org": settings.openalex_key})
        try:
            result = run(settings, members, group, curated, http, previous, existing)
        finally:
            http.close()
    publish.write(settings.output, result.files, result.stale)
    report.write(result.report, settings.run, settings.state)
    state.save(settings.state, result.state)
    return result.exit_code
