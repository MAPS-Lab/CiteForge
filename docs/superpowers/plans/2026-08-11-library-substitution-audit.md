# Library Substitution Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace proven handwritten infrastructure with stronger maintained libraries, repair adjacent demonstrated defects, and leave a verified evidence record with no planning artifacts in the final tree.

**Architecture:** Keep CiteForge's public `parse_bibtex_to_dict()` contract, deterministic serializer, shared Requests and Tenacity transport, semantic cache, token bucket, and domain policies. Introduce narrow adapters for BibTeX parsing and LaTeX decoding, enforce offline tests at the pytest boundary, and keep every unrelated repair independently testable and committable.

**Tech Stack:** Python 3.10 through 3.14, `bibtexparser==1.4.4`, `pylatexenc==2.11`, `pytest-socket==0.8.0`, pytest, Ruff, mypy, setuptools, uv-generated hashed requirements locks, GitHub Actions.

## Global Constraints

- Repository `CLAUDE.md` is authoritative.
- Preserve byte-identical BibTeX output unless a focused test proves the old output malformed and records the intended correction.
- Preserve the `{type, key, fields}` return shape of `parse_bibtex_to_dict()`.
- Preserve Python 3.10 compatibility and validate Python 3.14.
- Keep `main.py` as the documented compatibility launcher.
- Never add parallel parser, cache, transport, rate-limiter, or identity implementations.
- Regenerate universal hashed locks with the Python 3.10 target after dependency changes.
- Write tests first and observe each new regression test fail before production changes.
- Commit each task separately and verify `git log --oneline -1` after every commit.
- Delete `docs/superpowers/` in the final task, as requested.

---

### Task 1: Replace handwritten BibTeX parsing

**Files:**
- Modify: `pyproject.toml`
- Modify: `requirements.lock`
- Modify: `requirements-dev.lock`
- Modify: `citeforge/bibtex_utils.py:28-224`
- Modify: `tests/test_core.py:167-201`
- Modify: `tests/test_bibtex_serialize.py`

**Interfaces:**
- Consumes: `bibtexparser.loads(text: str) -> BibDatabase`
- Produces: `parse_bibtex_to_dict(bibtex: str) -> dict[str, Any] | None` with keys `type`, `key`, and `fields`

- [ ] **Step 1: Add failing standards-compliance tests**

```python
def test_parser_resolves_macros_concatenation_and_leading_comments() -> None:
    parsed = bt.parse_bibtex_to_dict(
        '@comment{ignored}\n@string{journal_name = "Journal of Tests"}\n'
        '@article{k, title = {A {Nested} Title}, journal = journal_name, month = jan # " 2024"}'
    )
    assert parsed == {
        "type": "article",
        "key": "k",
        "fields": {
            "title": "A {Nested} Title",
            "journal": "Journal of Tests",
            "month": "January 2024",
        },
    }


def test_parser_uses_first_entry_for_single_entry_contract() -> None:
    parsed = bt.parse_bibtex_to_dict("@article{first,title={One}}\n@book{second,title={Two}}")
    assert parsed == {"type": "article", "key": "first", "fields": {"title": "One"}}
```

- [ ] **Step 2: Prove the tests fail on the handwritten parser**

Run: `pytest tests/test_core.py::test_parser_resolves_macros_concatenation_and_leading_comments tests/test_core.py::test_parser_uses_first_entry_for_single_entry_contract -v --tb=short`

Expected: both tests fail because macros, concatenation, comments, or multiple entries are mishandled.

- [ ] **Step 3: Add the dependency and minimal adapter**

```python
import bibtexparser


def parse_bibtex_to_dict(bibtex: str) -> dict[str, Any] | None:
    """Parse the first BibTeX entry into CiteForge's stable entry shape."""
    try:
        entries = bibtexparser.loads(bibtex).entries
    except (TypeError, ValueError):
        entries = []
    if not entries:
        logger.debug(f"header_fail | input={bibtex[:60]}", category=LogCategory.PARSE)
        return None
    raw = dict(entries[0])
    entry_type = str(raw.pop("ENTRYTYPE", "")).lower()
    key = str(raw.pop("ID", ""))
    if not entry_type or not key:
        return None
    fields = {str(name).lower(): str(value).strip() for name, value in raw.items()}
    return {"type": entry_type, "key": key, "fields": fields}
```

Delete `_ENTRY_HEAD_RE`, `_SINGLE_LINE_ENTRY_RE`, `_FIELD_ASSIGN_RE`, `_QUOTED_VALUE_RE`, `_parse_bibtex_head()`, `_extract_balanced_braces()`, and `_assign_field_value()`.

- [ ] **Step 4: Regenerate affected locks**

Run:

```bash
uv pip compile pyproject.toml --universal --python-version 3.10 --generate-hashes -o requirements.lock
uv pip compile pyproject.toml --extra dev --universal --python-version 3.10 --generate-hashes -o requirements-dev.lock
```

Expected: both locks contain `bibtexparser==1.4.4` and remain installable on the Python 3.10 target.

- [ ] **Step 5: Verify focused and corpus parity**

Run:

```bash
pytest tests/test_core.py tests/test_bibtex_serialize.py tests/test_bibtex_build.py -v --tb=short
python - <<'PY'
from pathlib import Path
from citeforge.bibtex_utils import parse_bibtex_to_dict
paths = sorted(Path("output").rglob("*.bib"))
failures = [str(path) for path in paths if parse_bibtex_to_dict(path.read_text()) is None]
assert not failures, failures[:10]
print(f"parsed={len(paths)} failures=0")
PY
```

Expected: focused tests pass and all 3,669 committed BibTeX files parse.

- [ ] **Step 6: Commit the parser migration**

```bash
git add pyproject.toml requirements.lock requirements-dev.lock citeforge/bibtex_utils.py tests/test_core.py tests/test_bibtex_serialize.py
git commit -m "refactor: adopt standards-compliant BibTeX parsing"
git log --oneline -1
```

---

### Task 2: Consolidate LaTeX decoding

**Files:**
- Create: `citeforge/latex_utils.py`
- Create: `tests/test_latex_utils.py`
- Modify: `pyproject.toml`
- Modify: `requirements.lock`
- Modify: `requirements-dev.lock`
- Modify: `citeforge/bibtex_utils.py:227-383`
- Modify: `citeforge/text_utils.py:121-151`
- Modify: `tests/test_bibtex_serialize.py`
- Modify: `tests/test_core.py`

**Interfaces:**
- Consumes: `pylatexenc.latex2text.LatexNodes2Text`
- Produces: `latex_to_ascii(text: str, *, math_mode: Literal["remove", "verbatim"]) -> str`

- [ ] **Step 1: Write failing conversion tests**

```python
import pytest

from citeforge.latex_utils import latex_to_ascii


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (r'M{\"u}ller', "Muller"),
        (r"Fran\c{c}ois", "Francois"),
        (r"\textbf{Nested \emph{Title}}", "Nested Title"),
        (r"{\it Signal} and \& Noise", "Signal and & Noise"),
    ],
)
def test_latex_to_ascii_decodes_standard_forms(source: str, expected: str) -> None:
    assert latex_to_ascii(source, math_mode="remove") == expected


def test_latex_to_ascii_math_modes_are_explicit() -> None:
    assert latex_to_ascii(r"Analysis $\phi$ result", math_mode="remove") == "Analysis  result"
    assert latex_to_ascii(r"Analysis $\phi$ result", math_mode="verbatim") == r"Analysis $\phi$ result"
```

- [ ] **Step 2: Prove the new adapter does not exist**

Run: `pytest tests/test_latex_utils.py -v --tb=short`

Expected: collection fails with `ModuleNotFoundError: No module named 'citeforge.latex_utils'`.

- [ ] **Step 3: Implement the shared adapter**

```python
from __future__ import annotations

from typing import Literal

from pylatexenc.latex2text import LatexNodes2Text
from unidecode import unidecode

MathMode = Literal["remove", "verbatim"]
_CONVERTERS = {
    mode: LatexNodes2Text(math_mode=mode, strict_latex_spaces="macros")
    for mode in ("remove", "verbatim")
}


def latex_to_ascii(text: str, *, math_mode: MathMode) -> str:
    """Decode LaTeX and transliterate the result without reading TeX inputs."""
    return unidecode(_CONVERTERS[math_mode].latex_to_text(text))
```

Use `math_mode="remove"` in `normalize_title()` and `math_mode="verbatim"` in serializer cleanup. Delete the replaced LaTeX command regexes and loops while retaining project-specific dash, apostrophe-year, title, field-order, and ampersand policies.

- [ ] **Step 4: Regenerate locks**

Run:

```bash
uv pip compile pyproject.toml --universal --python-version 3.10 --generate-hashes -o requirements.lock
uv pip compile pyproject.toml --extra dev --universal --python-version 3.10 --generate-hashes -o requirements-dev.lock
```

Expected: both locks contain `pylatexenc==2.11`.

- [ ] **Step 5: Verify focused behavior and corpus byte stability**

Run:

```bash
pytest tests/test_latex_utils.py tests/test_bibtex_serialize.py tests/test_core.py tests/test_text_utils.py -v --tb=short
git diff --exit-code -- output data/input.csv data/a2i2.csv
```

Expected: all tests pass and protected data files are unchanged. If existing golden bytes change outside the four newly asserted malformed LaTeX cases, revert this task and record the exact divergence as the retention reason.

- [ ] **Step 6: Commit the accepted result**

```bash
git add pyproject.toml requirements.lock requirements-dev.lock citeforge/latex_utils.py citeforge/bibtex_utils.py citeforge/text_utils.py tests/test_latex_utils.py tests/test_bibtex_serialize.py tests/test_core.py
git commit -m "refactor: centralize LaTeX decoding"
git log --oneline -1
```

If the corpus gate rejects the library, commit only the focused characterization tests and later audit record with message `test: characterize retained LaTeX normalization`.

---

### Task 3: Enforce suite-wide offline testing and remove dead helpers

**Files:**
- Modify: `pyproject.toml`
- Modify: `requirements-dev.lock`
- Modify: `tests/conftest.py`
- Modify: `tests/test_config.py` for the temporary socket-enforcement probe, then restore it
- Delete: `tests/fixtures.py`
- Modify: `tests/fakes.py`
- Modify: `tests/test_api_adapter.py`
- Modify: `tests/test_pipeline_e2e.py`
- Modify: `tests/test_apis.py`
- Modify: `tests/test_integration.py`
- Modify: `tests/factories.py`
- Modify: `.github/workflows/live-tests.yml`

**Interfaces:**
- Produces: session fixture `api_keys() -> dict[str, Any]`
- Produces: required pytest runs with sockets disabled and live pytest runs with sockets enabled

- [ ] **Step 1: Add global socket enforcement and observe a deliberate failure**

Add `pytest-socket>=0.8.0` to the dev dependencies and add `--disable-socket` to pytest `addopts`. Temporarily add this test.

```python
def test_required_suite_blocks_network() -> None:
    import socket

    socket.create_connection(("example.com", 80), timeout=0.1)
```

Run: `pytest tests/test_config.py::test_required_suite_blocks_network -v --tb=short`

Expected: fail with `SocketBlockedError`. Delete the deliberate probe immediately after observing the failure.

- [ ] **Step 2: Move API keys to one session fixture**

```python
@pytest.fixture(scope="session")
def api_keys() -> dict[str, Any]:
    return {
        "serpapi": io_utils.read_serpapi_api_key(API_CONFIGS["serpapi"]["key_file"]),
        "serply": io_utils.read_serply_api_key(API_CONFIGS["serply"]["key_file"]),
        "semantic": io_utils.read_semantic_api_key(API_CONFIGS["semantic_scholar"]["key_file"]),
        "openreview": io_utils.read_openreview_credentials(API_CONFIGS["openreview"]["key_file"]),
        "gemini": io_utils.read_gemini_api_key(API_CONFIGS["gemini"]["key_file"]),
    }
```

Import `io_utils`, `API_CONFIGS`, `Any`, and `pytest` in `tests/conftest.py`. Delete module fixtures and `tests/fixtures.py`.

- [ ] **Step 3: Remove superseded socket and dead helper code**

Delete `_NoNetworkSocket`, `install_block_network`, `fake_http_json`, and `scripted_statuses` from `tests/fakes.py`. Remove imports and calls from adapter and E2E tests. Delete the seven zero-reference factories identified by `rg`, then rerun `vulture tests --min-confidence 80` to ensure no referenced helper was removed.

- [ ] **Step 4: Enable sockets only in live CI**

```yaml
- name: Run live scholarly-service tests
  run: pytest -m live --force-enable-socket
```

- [ ] **Step 5: Regenerate the development lock and verify both modes**

Run:

```bash
uv pip compile pyproject.toml --extra dev --universal --python-version 3.10 --generate-hashes -o requirements-dev.lock
pytest -m 'not live' tests/test_api_adapter.py tests/test_pipeline_e2e.py tests/test_apis.py tests/test_integration.py -v --tb=short
pytest --collect-only -m live --force-enable-socket -q
vulture tests --min-confidence 80
```

Expected: focused non-live tests pass, seven live tests collect, and removed helper names have no references.

- [ ] **Step 6: Commit test infrastructure**

```bash
git add pyproject.toml requirements-dev.lock tests .github/workflows/live-tests.yml
git commit -m "test: enforce hermetic network isolation"
git log --oneline -1
```

---

### Task 4: Repair HTTP boundary defects

**Files:**
- Modify: `citeforge/http_utils.py`
- Modify: `citeforge/clients/search_apis.py`
- Modify: `citeforge/clients/serpapi_scholar.py`
- Modify: `citeforge/clients/serply_scholar.py`
- Modify: `tests/test_http_utils.py`
- Modify: `tests/test_apis.py`
- Modify: `tests/test_scholar.py`

**Interfaces:**
- Produces: `_cookie_header(set_cookie: str) -> str | None`
- Preserves: explicit request timeouts and declared network/decode fallback behavior

- [ ] **Step 1: Write failing cookie and exception-boundary tests**

```python
def test_openreview_login_forwards_cookie_pairs_without_response_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    response = FakeResponse(200, headers={"Set-Cookie": "sid=abc; Path=/; HttpOnly; SameSite=Lax"})
    monkeypatch.setattr(search_apis, "_get_session", lambda: FakeSession(response))
    headers = search_apis.openreview_login(("user", "password"))
    assert headers is not None
    assert headers["Cookie"] == "sid=abc"


def test_serpapi_programming_error_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(serpapi_scholar, "http_fetch_bytes", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("bug")))
    with pytest.raises(AssertionError, match="bug"):
        serpapi_scholar._serpapi_get("key", "author")
```

- [ ] **Step 2: Prove failures**

Run: `pytest tests/test_apis.py -k 'cookie_pairs' tests/test_scholar.py -k 'programming_error' -v --tb=short`

Expected: cookie assertion fails and the broad handler swallows the assertion.

- [ ] **Step 3: Implement narrow boundary handling**

```python
from http.cookies import SimpleCookie


def _cookie_header(set_cookie: str) -> str | None:
    jar = SimpleCookie()
    jar.load(set_cookie)
    pairs = [f"{name}={morsel.value}" for name, morsel in jar.items()]
    return "; ".join(pairs) or None
```

Use the helper in `openreview_login()`. Remove `socket.setdefaulttimeout(60.0)` and its import. Catch `ALL_API_ERRORS` around network/decode operations in SerpAPI and Serply while allowing `AssertionError`, `AttributeError`, and unrelated programming errors to propagate.

- [ ] **Step 4: Verify HTTP behavior**

Run: `pytest tests/test_http_utils.py tests/test_apis.py tests/test_scholar.py tests/test_regression.py -v --tb=short`

Expected: all tests pass with explicit per-request timeouts unchanged.

- [ ] **Step 5: Commit boundary repairs**

```bash
git add citeforge/http_utils.py citeforge/clients/search_apis.py citeforge/clients/serpapi_scholar.py citeforge/clients/serply_scholar.py tests/test_http_utils.py tests/test_apis.py tests/test_scholar.py
git commit -m "fix: harden scholarly HTTP boundaries"
git log --oneline -1
```

---

### Task 5: Remove false timeout and skip behavior

**Files:**
- Modify: `citeforge/pipeline/scheduler.py:343-363`
- Modify: `tests/test_scheduler_retry.py`
- Modify: `tests/test_deduplication.py:86-91`

**Interfaces:**
- Preserves: aggregate `as_completed(..., timeout=author_timeout * len(records))` deadline
- Removes: unreachable per-result timeout branch

- [ ] **Step 1: Strengthen the deterministic similarity contract**

```python
sim = text_utils.title_similarity(entry_a["fields"]["title"], entry_b["fields"]["title"])
assert 0.90 < sim < 0.95
```

Run: `pytest tests/test_deduplication.py -k 'similarity' -v --tb=short`

Expected: pass under the locked RapidFuzz version, proving the fixture is deterministic rather than optional.

- [ ] **Step 2: Add a scheduler regression test**

```python
def test_completed_future_result_is_retrieved_without_fake_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    source = inspect.getsource(scheduler.run_all)
    assert "future.result(timeout=30)" not in source
    assert "as_completed(future_to_author, timeout=author_timeout * len(records))" in source
```

Run: `pytest tests/test_scheduler_retry.py::test_completed_future_result_is_retrieved_without_fake_timeout -v --tb=short`

Expected: fail because the unreachable timeout argument still exists.

- [ ] **Step 3: Remove unreachable code**

Change `future.result(timeout=30)` to `future.result()` and delete its local `except TimeoutError` branch. Keep the outer aggregate timeout handling and pending-author logging.

- [ ] **Step 4: Verify scheduler and deduplication**

Run: `pytest tests/test_scheduler_retry.py tests/test_deduplication.py tests/test_pipeline.py -v --tb=short`

Expected: all tests pass.

- [ ] **Step 5: Commit timeout and test repairs**

```bash
git add citeforge/pipeline/scheduler.py tests/test_scheduler_retry.py tests/test_deduplication.py
git commit -m "fix: make timeout and similarity gates truthful"
git log --oneline -1
```

---

### Task 6: Make packaging, hooks, locks, and CI reproducible

**Files:**
- Modify: `pyproject.toml`
- Modify: `requirements-build.in`
- Modify: `requirements-build.lock`
- Modify: `.pre-commit-config.yaml`
- Modify: `.github/workflows/tests.yml`
- Modify: `README.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Produces: isolated mypy hook pinned to mypy 2.1.0 and required import dependencies
- Produces: wheel smoke test outside the editable checkout
- Produces: lock regeneration drift gate

- [ ] **Step 1: Fix package metadata and the isolated mypy hook**

```toml
[build-system]
requires = ["setuptools>=77.0"]
build-backend = "setuptools.build_meta"

[project]
license = "AGPL-3.0-or-later"
```

```yaml
- repo: https://github.com/pre-commit/mirrors-mypy
  rev: v2.1.0
  hooks:
    - id: mypy
      args: [citeforge/, main.py]
      pass_filenames: false
      additional_dependencies:
        - rapidfuzz==3.14.5
        - Unidecode==1.4.0
```

Set `setuptools>=77.0` in `requirements-build.in`.
Pin `uv==0.11.7` in `requirements-build.in` so the existing hashed build lock also supplies the lock generator used by CI.

- [ ] **Step 2: Add lock freshness and wheel smoke steps**

```yaml
- name: Verify generated locks are current
  run: |
    uv pip compile pyproject.toml --universal --python-version 3.10 --generate-hashes -o requirements.lock
    uv pip compile pyproject.toml --extra dev --universal --python-version 3.10 --generate-hashes -o requirements-dev.lock
    uv pip compile requirements-build.in --universal --python-version 3.10 --generate-hashes -o requirements-build.lock
    git diff --exit-code -- requirements.lock requirements-dev.lock requirements-build.lock

- name: Smoke-test installed wheel
  run: |
    smoke_dir="$(mktemp -d)"
    python -m venv "$smoke_dir/venv"
    "$smoke_dir/venv/bin/pip" install --require-hashes -r requirements.lock
    "$smoke_dir/venv/bin/pip" install --no-deps /tmp/citeforge-wheel/*.whl
    cd "$smoke_dir"
    "$smoke_dir/venv/bin/python" -c "import citeforge, citeforge.cli"
    "$smoke_dir/venv/bin/citeforge" --help
```

Run the freshness step in the lint job after its existing hashed build and development lock installation, which now installs `uv==0.11.7`. Add Python `3.14` to the test matrix.

- [ ] **Step 3: Regenerate the build lock and align documentation**

Run:

```bash
uv pip compile requirements-build.in --universal --python-version 3.10 --generate-hashes -o requirements-build.lock
```

Document locked setup using the three requirements locks. Remove claims that `language: system` makes mypy lock-pinned.

- [ ] **Step 4: Verify local gates and package installation**

Run:

```bash
pre-commit run --all-files
wheel_dir="$(mktemp -d)"
smoke_dir="$(mktemp -d)"
python -m pip wheel --no-build-isolation --no-deps --wheel-dir "$wheel_dir" .
python -m venv "$smoke_dir/venv"
"$smoke_dir/venv/bin/pip" install --require-hashes -r requirements.lock
"$smoke_dir/venv/bin/pip" install --no-deps "$wheel_dir"/*.whl
(cd "$smoke_dir" && "$smoke_dir/venv/bin/python" -c "import citeforge, citeforge.cli")
(cd "$smoke_dir" && "$smoke_dir/venv/bin/citeforge" --help)
```

Expected: hooks pass, wheel installs without the source checkout, imports work, and CLI help exits zero.

- [ ] **Step 5: Commit reproducibility repairs**

```bash
git add pyproject.toml requirements-build.in requirements-build.lock .pre-commit-config.yaml .github/workflows/tests.yml README.md CLAUDE.md
git commit -m "ci: verify locks hooks and installed wheels"
git log --oneline -1
```

---

### Task 7: Write the durable audit record

**Files:**
- Create: `docs/library-substitution-audit.md`

**Interfaces:**
- Produces: repository-wide evidence ledger covering every custom responsibility and candidate library

- [ ] **Step 1: Record the complete decision matrix**

Use one row per reviewed responsibility with these exact columns.

```markdown
| Responsibility | Custom owner | Alternatives | Provenance and health | Decision | Evidence |
|---|---|---|---|---|---|
| BibTeX parsing | `citeforge/bibtex_utils.py` | bibtexparser, pybtex | release, license, Python support, advisories, dependency count | Adopt bibtexparser 1.4.4 | corpus and standards tests |
```

Include parser, serializer, LaTeX decoding, identifier normalization, text and identity policy, cache, rate limiting, HTTP and retry, every scholarly provider, models, config, CLI, logging, CSV and JSON, filesystem scanning, scheduling, pipeline orchestration, test fakes, network isolation, packaging, locks, and CI.

- [ ] **Step 2: Record removed code and retained custom boundaries**

List exact deleted symbols, dependency additions, dependency rejections, license compatibility, known-advisory checks, and the concrete contract behind every retention. Record `diskcache` rejection with GHSA-w8v5-vhqr-4h9v and the token-bucket versus leaky-bucket semantic mismatch.

- [ ] **Step 3: Add provisional verification commands without claiming results**

```markdown
## Verification commands

- `pytest -m 'not live' --cov=citeforge --cov=main --cov-fail-under=68`
- `ruff check citeforge tests main.py`
- `ruff format --check citeforge tests main.py`
- `mypy citeforge main.py`
- `pip-audit -r requirements.lock`
- `pip-audit -r requirements-dev.lock`
```

Do not write pass or fail results until Task 8 runs them.

- [ ] **Step 4: Commit the evidence structure**

```bash
git add docs/library-substitution-audit.md
git commit -m "docs: record library substitution decisions"
git log --oneline -1
```

---

### Task 8: Run complete verification and finalize evidence

**Files:**
- Modify: `docs/library-substitution-audit.md`

**Interfaces:**
- Consumes: all previous task outputs
- Produces: exact final verification results and a fully constrained repository

- [ ] **Step 1: Run static and dependency gates**

```bash
ruff check citeforge tests main.py
ruff format --check citeforge tests main.py
.venv/bin/mypy citeforge main.py
python -m compileall -q citeforge main.py
pip-audit -r requirements.lock
pip-audit -r requirements-dev.lock
uv pip check
vulture citeforge main.py --min-confidence 80
bandit -q -r citeforge main.py
semgrep scan --config auto --quiet citeforge main.py
gitleaks dir . --no-banner
```

Expected: no unclassified production finding. Test-only dummy-secret matches are recorded by file and rule without exposing values.

- [ ] **Step 2: Run the full hermetic suite**

```bash
pytest -m 'not live' --cov=citeforge --cov=main --cov-report=term-missing --cov-report=xml --cov-fail-under=68
```

Expected: all non-live tests pass, seven live tests remain deselected, sockets are disabled, and coverage is at least 68 percent.

- [ ] **Step 3: Verify corpus, package, and workflow surfaces**

```bash
git diff --exit-code origin/main -- output data/input.csv data/a2i2.csv
pytest --collect-only -m live --force-enable-socket -q
wheel_dir="$(mktemp -d)"
smoke_dir="$(mktemp -d)"
python -m pip wheel --no-build-isolation --no-deps --wheel-dir "$wheel_dir" .
python -m venv "$smoke_dir/venv"
"$smoke_dir/venv/bin/pip" install --require-hashes -r requirements.lock
"$smoke_dir/venv/bin/pip" install --no-deps "$wheel_dir"/*.whl
(cd "$smoke_dir" && "$smoke_dir/venv/bin/citeforge" --help)
```

Expected: protected files unchanged, live tests collect, and installed CLI exits zero outside the checkout.

- [ ] **Step 4: Update the audit record with exact outputs**

Write actual test counts, deselections, coverage, lint and type results, vulnerability results, package smoke result, corpus count, protected-file comparison, and any unavailable checks. A failed or unavailable check is never called a pass.

- [ ] **Step 5: Commit final evidence**

```bash
git add docs/library-substitution-audit.md
git commit -m "docs: finalize substitution verification evidence"
git log --oneline -1
```

---

### Task 9: Remove planning artifacts and perform final tree review

**Files:**
- Delete: `docs/superpowers/specs/2026-08-11-library-substitution-audit-design.md`
- Delete: `docs/superpowers/plans/2026-08-11-library-substitution-audit.md`

**Interfaces:**
- Preserves: `docs/library-substitution-audit.md` as the durable audit record
- Removes: all temporary Superpowers design and plan files

- [ ] **Step 1: Delete the requested planning directory**

```bash
git rm -r docs/superpowers
```

Validate the exact target first with `test "$(realpath docs/superpowers)" = "$(git rev-parse --show-toplevel)/docs/superpowers"`.

- [ ] **Step 2: Inspect the final diff and status**

```bash
git diff --check
git status --short
git diff --stat origin/main...HEAD
git diff origin/main...HEAD -- . ':(exclude)requirements.lock' ':(exclude)requirements-dev.lock' ':(exclude)requirements-build.lock'
```

Expected: only audited implementation, tests, locks, CI, README, CLAUDE, and `docs/library-substitution-audit.md` remain.

- [ ] **Step 3: Commit cleanup and verify advancement**

```bash
git add -A docs/superpowers
git commit -m "chore: remove implementation planning artifacts"
git log --oneline -1
git status --short --branch
```

Expected: final worktree clean and branch ahead only by the audited commits.
