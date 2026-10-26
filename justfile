# Pre-PR gate: the unit selection required CI runs, without the live scholarly-API tests.
test-pr:
    .venv/bin/pytest -m 'not live' -q
