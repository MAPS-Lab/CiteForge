import dataclasses
import json
import random
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Literal, TextIO

import httpx

from citeforge.model import Fetch, Record
from citeforge.tables import CSL_HOSTS

USER_AGENT = "CiteForge/2.0 (+https://github.com/gabrielspadon/CiteForge)"
MAX_ATTEMPTS = 4
BACKOFF_S = (1.0, 2.0, 4.0)
MAX_RETRY_AFTER_S = 120.0
MAX_RETRY_WAIT_S = 300.0
MAX_REDIRECTS = 3
CIRCUIT_FAILURES = 5
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
# Statuses that answer for one record (a malformed query, an ORCID deprecated or locked) and say nothing of host health.
RECORD_STATUS = frozenset({400, 409})
_SECRET_QS = re.compile(r"(?i)([?&](?:access_token|api_key|apikey|api-key|token|key)=)[^&#\s'\"\\)]+")


@dataclass(frozen=True, slots=True)
class Host:
    name: str
    base: str
    min_interval_s: float
    budget: int
    expect: Literal["json", "xml", "any"]
    key_param: str | None = None


HOSTS: Mapping[str, Host] = {
    h.name: h
    for h in (
        Host("api.openalex.org", "https://api.openalex.org", 0.1, 1500, "json", "api_key"),
        Host("pub.orcid.org", "https://pub.orcid.org", 0.125, 600, "json"),
        Host("dblp.org", "https://dblp.org", 1.0, 300, "xml"),
        Host("api.crossref.org", "https://api.crossref.org", 1.0, 1500, "json"),
        Host("api.datacite.org", "https://api.datacite.org", 0.35, 300, "json"),
        Host("doi.org", "https://doi.org", 0.5, 200, "any"),
        Host("export.arxiv.org", "https://export.arxiv.org", 3.0, 60, "xml"),
        Host("eutils.ncbi.nlm.nih.gov", "https://eutils.ncbi.nlm.nih.gov", 0.34, 50, "json"),
    )
}
ALLOWED = frozenset(HOSTS) | CSL_HOSTS
FIXED_PARAMS: Mapping[str, Mapping[str, str]] = {
    "dblp.org": {"app": "citeforge"},
    "eutils.ncbi.nlm.nih.gov": {"tool": "citeforge"},
}


REDIRECT = "redirect"
REDIRECT_BLOCKED = "redirect_blocked"
REDIRECT_LIMIT = "redirect_limit"


@dataclass(frozen=True, slots=True)
class Reply:
    fetch: Fetch
    status: int | None
    headers: Mapping[str, str]
    body: bytes
    reason: str
    url: str = ""

    @property
    def location(self) -> str | None:
        return self.headers.get("location")


class ParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Result[T]:
    fetch: Fetch
    value: T | None
    reason: str = "ok"


@dataclass(frozen=True, slots=True)
class Batch:
    found: Mapping[str, Record]
    missing: frozenset[str]
    failed: frozenset[str]


def _scrub_secrets(text: str) -> str:
    return _SECRET_QS.sub(r"\1REDACTED", text)


def _parse_retry_after(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except TypeError, ValueError:
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _content_ok(expect: str, content_type: str) -> bool:
    match expect:
        case "json":
            return "json" in content_type
        case "xml":
            return "xml" in content_type
        case _:
            return True


class _HostState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.next_at = 0.0
        self.used = 0
        self.failures = 0
        self.down: str | None = None
        self.counts: Counter[str] = Counter()
        self.max_retry_after = 0.0
        self.waited = 0.0


class Http:
    def __init__(
        self,
        log: TextIO,
        keys: Mapping[str, str],
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._log = log
        self._log_lock = threading.Lock()
        self._keys = dict(keys)
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random(0)
        self._states = {name: _HostState() for name in HOSTS}
        self._client = httpx.Client(
            transport=transport,
            follow_redirects=False,
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"User-Agent": USER_AGENT},
        )

    def close(self) -> None:
        self._client.close()

    def down(self, host: Host) -> str | None:
        return self._states[host.name].down

    def stats(self) -> dict[str, dict[str, int | float]]:
        out: dict[str, dict[str, int | float]] = {}
        for name, st in sorted(self._states.items()):
            row: dict[str, int | float] = {
                k: st.counts[k]
                for k in ("requests", "ok", "not_found", "429", "5xx", "timeouts", "challenges", "retries")
            }
            row["max_retry_after"] = st.max_retry_after
            out[name] = row
        return out

    def get(
        self,
        host: Host,
        path: str,
        params: Mapping[str, str] = {},
        headers: Mapping[str, str] = {},
        follow_redirects: bool = False,
    ) -> Reply:
        """GET host.base + path; with follow_redirects, Location is followed up to MAX_REDIRECTS hops."""
        st = self._states[host.name]
        query = {**params, **FIXED_PARAMS.get(host.name, {})}
        if host.key_param and (key := self._keys.get(host.name)):
            query[host.key_param] = key
        blocked = Reply(Fetch.FAILED, None, {}, b"", REDIRECT_BLOCKED)
        try:
            target = httpx.URL(host.base + path).copy_merge_params(query)
        except httpx.InvalidURL:
            return blocked
        reply = blocked
        with st.lock:
            for hop in range(MAX_REDIRECTS + 1):
                if target.scheme != "https" or target.host not in ALLOWED or target.port is not None:
                    reply = dataclasses.replace(blocked, url=str(target))
                    break
                reply = dataclasses.replace(self._attempts(host, st, target, headers), url=str(target))
                if reply.reason != REDIRECT or not reply.location or not follow_redirects:
                    break
                if hop == MAX_REDIRECTS:
                    reply = dataclasses.replace(reply, reason=REDIRECT_LIMIT)
                    break
                target = target.join(reply.location)
                if target.host == host.name:
                    target = target.copy_merge_params(query)
            if (
                reply.fetch is Fetch.FAILED
                and reply.reason not in (REDIRECT, REDIRECT_BLOCKED, REDIRECT_LIMIT)
                and reply.status not in RECORD_STATUS
            ):
                st.failures += 1
                if st.failures >= CIRCUIT_FAILURES and st.down is None:
                    st.down = "circuit"
            else:
                st.failures = 0
            return reply

    def _attempts(self, host: Host, st: _HostState, target: httpx.URL, headers: Mapping[str, str]) -> Reply:
        reply = Reply(Fetch.FAILED, None, {}, b"", "down")
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if st.down is not None:
                return Reply(Fetch.FAILED, reply.status, reply.headers, b"", "down")
            if st.used >= host.budget:
                st.down = "budget"
                return Reply(Fetch.FAILED, None, {}, b"", "budget")
            wait = st.next_at - self._clock()
            if wait > 0:
                self._sleep(wait)
            st.used += 1
            st.counts["requests"] += 1
            if attempt > 1:
                st.counts["retries"] += 1
            started = self._clock()
            reply, retry_after = self._once(host, st, target, headers)
            ms = (self._clock() - started) * 1000
            st.next_at = self._clock() + host.min_interval_s
            self._write_log(host, target, reply, attempt, retry_after, ms)
            retryable = reply.reason in ("timeout", "conn_error", "decode_error") or (
                reply.status is not None and reply.status in RETRY_STATUS
            )
            if not retryable:
                return reply
            if retry_after > MAX_RETRY_AFTER_S:
                st.down = f"retry_after {retry_after:.0f}s"
                return reply
            backoff = BACKOFF_S[attempt - 1] * self._rng.uniform(0.75, 1.25) if attempt < MAX_ATTEMPTS else 0.0
            delay = retry_after or backoff
            # The wait outlives this call, so the next request to the host also honours it.
            st.next_at = max(st.next_at, self._clock() + delay)
            st.waited += delay
            if st.waited > MAX_RETRY_WAIT_S:
                st.down = f"retry_wait {st.waited:.0f}s"
                return reply
            if attempt == MAX_ATTEMPTS:
                break
        return reply

    def _once(self, host: Host, st: _HostState, target: httpx.URL, headers: Mapping[str, str]) -> tuple[Reply, float]:
        try:
            resp = self._client.get(target, headers=dict(headers))
        except httpx.TimeoutException:
            st.counts["timeouts"] += 1
            return Reply(Fetch.FAILED, None, {}, b"", "timeout"), 0.0
        except httpx.DecodingError:
            return Reply(Fetch.FAILED, None, {}, b"", "decode_error"), 0.0
        except httpx.RequestError:
            return Reply(Fetch.FAILED, None, {}, b"", "conn_error"), 0.0
        status = resp.status_code
        hdrs = {k.lower(): v for k, v in resp.headers.items()}
        body = resp.content
        ctype = hdrs.get("content-type", "").lower()
        retry_after = 0.0
        if status == 200:
            if "html" in ctype or body[:15].lower().startswith((b"<!doctype html", b"<html")):
                st.counts["challenges"] += 1
                return Reply(Fetch.FAILED, status, hdrs, body, "challenge"), 0.0
            if not _content_ok(host.expect, ctype):
                return Reply(Fetch.FAILED, status, hdrs, body, "parse_error"), 0.0
            st.counts["ok"] += 1
            return Reply(Fetch.OK, status, hdrs, body, "ok"), 0.0
        if status == 202:
            st.counts["challenges"] += 1
            return Reply(Fetch.FAILED, status, hdrs, body, "challenge"), 0.0
        if status in (404, 410):
            st.counts["not_found"] += 1
            return Reply(Fetch.NOT_FOUND, status, hdrs, body, "not_found"), 0.0
        if 300 <= status < 400:
            return Reply(Fetch.FAILED, status, hdrs, body, REDIRECT), 0.0
        if status == 429:
            st.counts["429"] += 1
        elif status >= 500:
            st.counts["5xx"] += 1
        if status in RETRY_STATUS:
            retry_after = _parse_retry_after(hdrs.get("retry-after"))
            st.max_retry_after = max(st.max_retry_after, retry_after)
        return Reply(Fetch.FAILED, status, hdrs, body, "http_error"), retry_after

    def _write_log(
        self, host: Host, target: httpx.URL, reply: Reply, attempt: int, retry_after: float, ms: float
    ) -> None:
        line = {
            "t": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "host": host.name if target.host == host.name else target.host,
            "path": _scrub_secrets(target.raw_path.decode("ascii", "replace")),
            "status": reply.status,
            "attempt": attempt,
            "ms": round(ms, 1),
            "bytes": len(reply.body),
            "retry_after": retry_after,
            "reason": reply.reason,
        }
        with self._log_lock:
            self._log.write(json.dumps(line, sort_keys=True) + "\n")
