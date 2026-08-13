# Library Substitution Audit Design

## Goal

Audit every tracked CiteForge execution surface and replace custom implementations only where a mature open-source component is a stronger production fit. Preserve deterministic BibTeX output, trust policy, cache semantics, API behavior, and the Python 3.10 compatibility floor.

## Scope

The audit covers all Python packages, the CLI launcher, tests, package metadata, lock files, pre-commit configuration, and GitHub Actions workflows. Generated publication output is a compatibility corpus and is not hand-edited. Live API behavior is tested only through the existing opt-in live suite.

The implementation also repairs demonstrable adjacent defects found while tracing replacement boundaries. Each repair remains independently reviewable and tested.

## Decision method

Each custom responsibility is compared against established alternatives using correctness, functional coverage, API stability, release activity, maintainer and community health, documentation, testing, performance, security history, license compatibility, dependency footprint, Python support, and integration cost.

A dependency is adopted only when it removes meaningful maintenance surface or fixes a proven correctness gap without weakening project contracts. A small stable implementation stays custom when the candidate changes semantics, duplicates existing architecture, or costs more than it removes.

## Selected substitutions

### BibTeX parsing

Replace the parser in `citeforge/bibtex_utils.py` with `bibtexparser==1.4.4` and retain the existing `parse_bibtex_to_dict()` boundary. The adapter maps the first parsed entry from `ENTRYTYPE`, `ID`, and remaining fields into the current `{type, key, fields}` shape.

The current parser fails standard macros, concatenated values, leading comments, multiline quoted values, and multiple-entry input. `bibtexparser` parsed all 3,669 committed production BibTeX files with no failures and no semantic mismatches in entry type, key, field names, or field values. It is dual BSD or LGPLv3, compatible with AGPL-3.0-or-later, and its stable 1.4.4 release is current. The 2.x prerelease API is excluded.

Keep CiteForge's deterministic serializer. Third-party writers change field ordering, escaping, cleanup, or final bytes. Golden serializer behavior remains authoritative.

### LaTeX decoding

Characterize `pylatexenc==2.11` against the committed corpus and focused malformed-accent cases before adoption. Its `LatexNodes2Text` parser correctly handles nested commands and standard accents that the regex paths miss.

Adopt it only if a single shared adapter can preserve established non-LaTeX output and produce clearly correct text for standard LaTeX forms. Math handling, whitespace, ASCII transliteration, ampersand escaping, and deterministic serialization are explicit gates. If those gates fail, retain the current implementation and record the exact divergence in the audit report.

### Hermetic test networking

Add `pytest-socket==0.8.0` as a development dependency. Disable sockets for the required non-live suite and force-enable them only in the live workflow. Delete the local socket subclass and per-test installation calls.

This expands network isolation from two tests to the entire required suite. Keep `FakeResponse` and `FakeSession` because they directly model retries, exceptions, session closure, and rotation. General request-mocking packages do not replace those lifecycle assertions.

## Custom implementations retained

Retain the following boundaries unless implementation-time evidence contradicts the established comparison.

- Response cache. Its calendar expiry, confirmation-counted negative results, defensive copies, namespace keys, counters, and atomic JSON writes are domain semantics. `requests-cache` operates at the HTTP layer and `diskcache` has an unsafe deserialization advisory.
- Token bucket. CiteForge needs continuous refill and explicit burst capacity. `pyrate-limiter` 4.x implements leaky-bucket semantics, so replacement would change observable bursts for little code reduction.
- HTTP transport and retries. Requests plus Tenacity already centralize session reuse, source accounting, Retry-After handling, redaction, and different GET and POST retry policies.
- Scholarly API adapters. Provider SDKs duplicate transport, caching, rate limiting, scoring, and error policy while adding disproportionate dependencies.
- Identifier normalization. `idutils` preserves case and encoded separators and raises on inputs CiteForge intentionally cleans before validation.
- Identity, canonicalization, trust merge, publication-string parsing, BibTeX construction, and post-run repair. These are project policy rather than generic library responsibilities.
- CLI, models, configuration, CSV and JSON access, filesystem scanning, and scheduling. Standard-library implementations are already smaller than framework replacements.
- Logging. The per-thread author logs and category policy are project-specific and do not benefit from another logging facade.

## Adjacent repairs

Apply each repair separately with focused regression tests.

- Parse OpenReview `Set-Cookie` with the standard library and forward cookie name-value pairs only. Never copy response attributes such as `Path`, `Expires`, or `HttpOnly` into the request header.
- Remove the unreachable `future.result(timeout=30)` timeout branch after `as_completed()`. Retain and test the real aggregate timeout owned by `as_completed()`.
- Remove the process-global `socket.setdefaulttimeout()`. Every HTTP call already carries an explicit timeout, while the global mutation affects unrelated libraries.
- Narrow SerpAPI and Serply broad exception handlers to declared network and decode failures.
- Replace deterministic conditional skips in deduplication tests with explicit similarity-band assertions.
- Consolidate API-key loading into one session-scoped pytest fixture and delete duplicate fixtures.
- Delete zero-reference test helpers and factories after repository-wide reachability checks.
- Make pre-commit's mypy environment truthful and reproducible. Documentation and hook behavior must agree with the locked development environment.
- Install and smoke-test the built wheel in an isolated environment, including `citeforge --help` and package import.
- Add Python 3.14 to CI because project metadata allows it and the full suite passes locally.
- Convert the project license metadata to an SPDX expression supported by the selected setuptools floor.
- Keep `main.py` as the documented compatibility launcher. The repository instructions and README make `python3 main.py` an external contract.

## Dependency and lock policy

Retain the three hashed requirements locks because they provide minimal runtime, development, and build environments through standard pip. Do not replace them with one `uv.lock` because that adds a tool requirement to production workflows and collapses intentionally separate environments.

Regenerate affected locks from Python 3.10-compatible universal inputs. Add a freshness check so dependency declarations and generated locks cannot drift silently. Verify exact installed compatibility and audit both runtime and development dependency sets.

## Verification

The pre-change baseline is 876 non-live tests passed, 7 live tests deselected, and 74.49 percent coverage on Python 3.13. Ruff and formatting pass. The globally resolved mypy executable fails, while the locked project environment passes. This drift is part of the repair scope.

Every migration receives focused characterization tests before deletion of the superseded code. Final verification includes the following.

- Focused parser, serializer, LaTeX, HTTP, scheduler, CLI, and test-infrastructure tests
- Semantic comparison across all 3,669 committed BibTeX files
- Byte comparison of protected output and input fixtures
- Required non-live suite with suite-wide socket blocking and the coverage floor
- Live-test collection and socket-enablement validation without requiring unavailable credentials
- Ruff lint and format checks
- Mypy from the locked development environment
- Python compilation
- Wheel build, isolated installation, import, and CLI smoke test
- Lock freshness and dependency compatibility checks
- `pip-audit` over runtime and development locks
- Bandit, Semgrep, Gitleaks, Vulture, and duplication scans with findings classified rather than ignored
- Final Git diff and clean ownership check

## Audit record

Write `docs/library-substitution-audit.md` as the durable evidence record. It lists every responsibility reviewed, alternatives considered, provenance and maintenance evidence, licensing and dependency impact, adoption or retention rationale, deleted code, behavior changes, and exact final verification results.

No compatibility wrapper, parallel implementation, fallback parser, or duplicated policy remains after a successful migration. If an equivalence gate fails, the custom implementation stays and the report records the demonstrated reason.
