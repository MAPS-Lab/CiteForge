import gzip
import re
from pathlib import Path

from tests.conftest import FIXTURES

ROOT = Path(__file__).parents[1]
# an e-mail address, a key-shaped query value, or a path into someone's home directory
LEAKS = re.compile(
    rb"[\w.+-]+@[\w-]+\.(?:[\w-]+\.)*(?:com|org|net|edu|ca|br|de|uk|io)\b|(?:api_key|apikey|token)=(?!REDACTED|YOUR_KEY)\w{8,}"
    rb"|(?<![\w.])/home/\w+|(?<![\w.])/Users/\w+"
)
TRACKED_DATA = ("data", "tests/golden")


def _bodies() -> list[tuple[Path, bytes]]:
    out = [(p, gzip.decompress(p.read_bytes())) for p in sorted(FIXTURES.rglob("*.json.gz"))]
    out += [(p, p.read_bytes()) for p in sorted(FIXTURES.rglob("*")) if p.is_file() and not p.name.endswith(".gz")]
    return out + [(p, p.read_bytes()) for d in TRACKED_DATA for p in sorted((ROOT / d).rglob("*")) if p.is_file()]


def test_fixtures_and_data_hold_no_email_key_or_home_path() -> None:
    leaks = [f"{p.relative_to(ROOT)}: {m.group(0)[:40]!r}" for p, body in _bodies() if (m := LEAKS.search(body))]
    assert leaks == []
