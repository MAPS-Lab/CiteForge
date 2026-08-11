# Task 6 report

## Outcome

Repaired package metadata, build-lock reproducibility, mypy hook isolation,
Python 3.14 CI coverage, and installed-wheel smoke testing.

## Files changed

- `pyproject.toml`
- `requirements-build.in`
- `requirements-build.lock`
- `.pre-commit-config.yaml`
- `.github/workflows/tests.yml`
- `README.md`
- `CLAUDE.md`

## Implementation

- Raised the build requirement to `setuptools>=77.0` and made the SPDX license
  a PEP 639 string.
- Pinned `uv==0.11.7` in the build input and regenerated its hashed lock entry.
- Replaced the system mypy hook with `mirrors-mypy` `v2.1.0`, adding only
  `rapidfuzz==3.14.5` and `Unidecode==1.4.0` for imports used by the type gate.
- Added Python 3.14 to the test matrix.
- Added deterministic Python 3.10 universal lock regeneration in the lint job.
- Added a wheel smoke test that creates a fresh environment, installs only the
  runtime lock and built wheel, then imports the package and runs its CLI from
  outside the source checkout.
- Documented the build, runtime, and development locks in README and CLAUDE.

## Verification

Executed from `/home/spadon/Codebases/CiteForge/.worktrees/library-substitution-audit`.

```bash
uv --version
python --version
pre-commit --version
```

Passed with `uv 0.11.7`, Python 3.13.11, and pre-commit 4.6.0.

```bash
uv pip compile pyproject.toml --universal --python-version 3.10 --generate-hashes -o requirements.lock
uv pip compile pyproject.toml --extra dev --universal --python-version 3.10 --generate-hashes -o requirements-dev.lock
uv pip compile requirements-build.in --universal --python-version 3.10 --generate-hashes -o requirements-build.lock
```

Passed. The runtime and development locks had no dependency drift. The build
lock gained only the hashed `uv==0.11.7` entry.

```bash
pre-commit run --all-files
```

Passed all hooks, including the isolated mypy v2.1.0 environment. No additional
mypy hook dependencies were required.

```bash
.venv/bin/ruff check citeforge/ tests/ main.py
.venv/bin/mypy citeforge/ main.py
```

Passed. Ruff reported `All checks passed!`; mypy reported success in 37 source
files.

```bash
python - <<'PY'
from pathlib import Path
import tomllib
import yaml
for path in (Path('pyproject.toml'),):
    with path.open('rb') as file:
        tomllib.load(file)
for path in (Path('.pre-commit-config.yaml'), Path('.github/workflows/tests.yml')):
    with path.open() as file:
        yaml.safe_load(file)
print('TOML and YAML parse successfully')
PY
git diff --check
```

Passed. TOML and both YAML files parsed successfully, with no whitespace errors.

```bash
wheel_dir="$(mktemp -d)"
smoke_dir="$(mktemp -d)"
python -m pip wheel --no-build-isolation --no-deps --wheel-dir "$wheel_dir" .
python -m venv "$smoke_dir/venv"
"$smoke_dir/venv/bin/pip" install --require-hashes -r requirements.lock
"$smoke_dir/venv/bin/pip" install --no-deps "$wheel_dir"/*.whl
(cd "$smoke_dir" && "$smoke_dir/venv/bin/python" -c "import citeforge, citeforge.cli")
(cd "$smoke_dir" && "$smoke_dir/venv/bin/citeforge" --help)
```

Passed. The wheel built as `citeforge-1.0.0-py3-none-any.whl`; its import and
CLI help both passed from the fresh smoke directory.

```bash
git add pyproject.toml requirements-build.in requirements-build.lock .pre-commit-config.yaml .github/workflows/tests.yml README.md CLAUDE.md
uv pip compile pyproject.toml --universal --python-version 3.10 --generate-hashes -o requirements.lock
uv pip compile pyproject.toml --extra dev --universal --python-version 3.10 --generate-hashes -o requirements-dev.lock
uv pip compile requirements-build.in --universal --python-version 3.10 --generate-hashes -o requirements-build.lock
git diff --exit-code -- requirements.lock requirements-dev.lock requirements-build.lock
git diff --cached --check
```

Passed. Recompilation was deterministic with no unstaged lock drift and no
staged whitespace errors.

## Self-review

The lock freshness check runs after installing the pinned build lock, so CI uses
the same `uv==0.11.7` compiler recorded in source. The smoke test cannot import
the editable checkout because it changes into a fresh `mktemp` directory before
importing and invoking the installed console script. The isolated mypy hook has
only the two direct runtime imports needed for meaningful type checking.

## Concerns

None. The local wheel smoke used Python 3.13. CI now also covers Python 3.14 in
the test matrix.

## Commit

`ci: verify locks hooks and installed wheels`
