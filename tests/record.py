"""Record one live run's replies for test_golden: uv run python -m tests.record --roster tests/golden/world.csv"""

import argparse
import gzip
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx

from citeforge import curated, publish, report, roster, state
from citeforge.cli import _parser
from citeforge.http import Http
from citeforge.run import Settings, run
from tests.conftest import FIXTURES, fixture_path

_EMAIL = re.compile(rb"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_DROPPED = ("abstract", "reference")  # Crossref fields no parser reads, and the bulk of each reply
KEPT_HEADERS = ("content-type", "retry-after", "location")


def _scrub(host: str, body: bytes) -> bytes:
    if host == "api.crossref.org" and body.startswith(b"{"):
        doc = json.loads(body)
        message = doc.get("message") if isinstance(doc, dict) else None
        items = message.get("items", [message]) if isinstance(message, dict) else []
        for item in items if isinstance(items, list) else []:
            for key in _DROPPED if isinstance(item, dict) else ():
                item.pop(key, None)
        body = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()
    return _EMAIL.sub(b"redacted", body)


class Recorder(httpx.BaseTransport):
    """Serve a recorded reply when one exists, else ask the host and keep every reply except a transient error."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.live = httpx.HTTPTransport()
        self.misses = 0
        self.served: set[Path] = set()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = fixture_path(self.root, request.method, request.url.copy_remove_param("api_key"))
        self.served.add(path)
        if path.exists():
            data = json.loads(gzip.decompress(path.read_bytes()))
            return httpx.Response(data["status"], headers=data["headers"], content=data["body"].encode())
        self.misses += 1
        reply = self.live.handle_request(request)
        reply.read()
        headers = {k: reply.headers[k] for k in KEPT_HEADERS if k in reply.headers}
        if reply.status_code == 429 or reply.status_code >= 500:
            return httpx.Response(reply.status_code, headers=headers, content=reply.content)
        body = _scrub(request.url.host, reply.content).decode("utf-8", "replace")
        url = str(request.url.copy_remove_param("api_key"))
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"url": url, "status": reply.status_code, "headers": headers, "body": body}
        path.write_bytes(gzip.compress(json.dumps(payload, ensure_ascii=False).encode(), mtime=0))
        return httpx.Response(reply.status_code, headers=headers, content=body.encode())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roster", type=Path, required=True)
    parser.add_argument("--curated", type=Path, default=Path("data/curated.toml"))
    parser.add_argument("--group", type=Path, default=Path("tests/golden/a2i2.csv"))
    parser.add_argument("--fixtures", type=Path, default=FIXTURES / "golden")
    parser.add_argument("--work", type=Path, required=True, help="output/, state/ and run/; a fresh one is a cold run")
    args = parser.parse_args()
    ns = _parser().parse_args(["--input", str(args.roster), "--output", str(args.work / "output")])
    ns.curated, ns.group = str(args.curated), str(args.group)
    ns.state, ns.run = str(args.work / "state"), str(args.work / "run")
    settings = Settings.from_args(ns, {"OPENALEX_API_KEY": ""}, datetime.now(UTC).date(), keyless=True)
    members = roster.load(settings.input)
    cur = curated.load(settings.curated)
    cur.check_members(members)
    settings.run.mkdir(parents=True, exist_ok=True)
    recorder = Recorder(args.fixtures)
    with (settings.run / "requests.jsonl").open("w", encoding="utf-8") as log:
        http = Http(log, {}, transport=recorder)
        try:
            group = roster.load_group(settings.group, members)
            folders = [*(m.dirname for m in members), publish.GROUP_DIR]
            existing = publish.on_disk(settings.output, folders)
            result = run(settings, members, group, cur, http, state.load(settings.state), existing)
        finally:
            http.close()
    publish.write(settings.output, result.files, result.stale)
    report.write(result.report, settings.run, settings.state)
    state.save(settings.state, result.state)
    stale = [p for p in args.fixtures.rglob("*.json.gz") if p not in recorder.served]
    for path in stale:  # a reply this run no longer asks for would only hide a missing fixture
        path.unlink()
    print(f"recorded on {settings.today}, exit {result.exit_code}, {recorder.misses} fetched live, {len(stale)} pruned")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
