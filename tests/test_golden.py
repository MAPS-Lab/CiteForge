import io
import json
from collections import Counter
from datetime import date
from pathlib import Path
from statistics import median

from citeforge import curated, roster
from citeforge.http import Http
from citeforge.run import Settings, run
from tests.conftest import FIXTURES, FakeClock, fixture_transport
from tests.golden import failures, load_labels

GOLDEN = Path(__file__).parent / "golden"
ROOT = Path(__file__).parents[1]
RECORDED_ON = date(2026, 11, 28)  # the day tests/record.py fetched tests/fixtures/golden


def test_the_golden_world_meets_the_label_and_request_thresholds(tmp_path: Path) -> None:
    clock, log = FakeClock(), io.StringIO()
    http = Http(log, {}, transport=fixture_transport(FIXTURES / "golden"), clock=clock, sleep=clock.sleep)
    paths = (GOLDEN / "world.csv", tmp_path / "output", GOLDEN / "a2i2.csv", ROOT / "data/curated.toml")
    settings = Settings(*paths, tmp_path / "state", tmp_path / "run", 2020, RECORDED_ON, "")
    members = roster.load(settings.input)
    group = roster.load_group(settings.group, members)
    result = run(settings, members, group, curated.load(settings.curated), http, {}, frozenset())
    assert failures(load_labels(GOLDEN / "labels.jsonl"), result.files) == []
    per_member = Counter(line["member"] for line in map(json.loads, log.getvalue().splitlines()) if line["member"])
    assert len(per_member) == len(members)
    assert median(per_member.values()) <= 15 and max(per_member.values()) <= 25
    assert sum(1 for _ in log.getvalue().splitlines()) <= 1000
    assert result.report["coverage"]["unexplained"] == 0
