from __future__ import annotations

import json
import time
from datetime import datetime, tzinfo
from pathlib import Path
from unittest.mock import patch

import pytest

from src.cache import _AST, ResponseCache, get_cache_hit_counts, reset_cache_hit_counts


def _freeze_cache_clock(monkeypatch: pytest.MonkeyPatch, when: datetime) -> None:
    """Freeze src.cache's wall clock to a fixed instant so expiry tests are date-independent."""
    import src.cache as cache_mod

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:  # type: ignore[override]
            return when.astimezone(tz) if tz is not None else when

    monkeypatch.setattr(cache_mod, "datetime", _FrozenDatetime)


def test_put_and_get(tmp_path: Path) -> None:
    """Test basic cache put and get operations."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("test_ns", "key1", {"value": "hello"}, ttl_days=30)
    result = cache.get("test_ns", "key1")
    assert result == {"value": "hello"}


def test_get_missing_key(tmp_path: Path) -> None:
    """Test that get returns None for missing keys."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    result = cache.get("test_ns", "nonexistent")
    assert result is None


def test_has(tmp_path: Path) -> None:
    """Test has() checks for key existence."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    assert not cache.has("test_ns", "key1")
    cache.put("test_ns", "key1", {"v": 1})
    assert cache.has("test_ns", "key1")


def test_invalidate(tmp_path: Path) -> None:
    """Test that invalidate removes a cached entry."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("test_ns", "key1", {"v": 1})
    assert cache.has("test_ns", "key1")
    cache.invalidate("test_ns", "key1")
    assert not cache.has("test_ns", "key1")


def test_invalidate_missing_key(tmp_path: Path) -> None:
    """Test that invalidating a non-existent key doesn't raise."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.invalidate("test_ns", "nonexistent")


def test_monthly_expiry(tmp_path: Path) -> None:
    """Test that stale entries return None but the file is preserved on disk."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("test_ns", "key1", {"v": 1})

    path = Path(cache._entry_path("test_ns", "key1"))
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["timestamp"] = cache._month_boundary - 1
    path.write_text(json.dumps(entry), encoding="utf-8")

    result = cache.get("test_ns", "key1")
    assert result is None
    assert path.exists(), "Stale entry file should be preserved until overwritten by put()"


def test_stale_entry_refreshed_by_put(tmp_path: Path) -> None:
    """Test that put() overwrites a stale entry with fresh data."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("test_ns", "key1", {"v": "old"})

    path = Path(cache._entry_path("test_ns", "key1"))
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["timestamp"] = cache._month_boundary - 1
    path.write_text(json.dumps(entry), encoding="utf-8")

    assert cache.get("test_ns", "key1") is None
    cache.put("test_ns", "key1", {"v": "fresh"})
    assert cache.get("test_ns", "key1") == {"v": "fresh"}


def test_namespace_isolation(tmp_path: Path) -> None:
    """Test that different namespaces are isolated."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("ns_a", "key1", {"v": "a"})
    cache.put("ns_b", "key1", {"v": "b"})

    assert cache.get("ns_a", "key1") == {"v": "a"}
    assert cache.get("ns_b", "key1") == {"v": "b"}


def test_overwrite(tmp_path: Path) -> None:
    """Test that putting the same key overwrites the value."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("test_ns", "key1", {"v": 1})
    cache.put("test_ns", "key1", {"v": 2})
    assert cache.get("test_ns", "key1") == {"v": 2}


def test_creates_namespace_directory(tmp_path: Path) -> None:
    """Test that put creates the namespace directory if needed."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    ns_dir = tmp_path / "new_ns"
    assert not ns_dir.exists()
    cache.put("new_ns", "key1", {"v": 1})
    assert ns_dir.is_dir()


def test_corrupted_cache_file(tmp_path: Path) -> None:
    """Test that corrupted cache files return None."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("test_ns", "key1", {"v": 1})
    path = Path(cache._entry_path("test_ns", "key1"))
    path.write_text("not valid json{{{", encoding="utf-8")
    assert cache.get("test_ns", "key1") is None


def test_negative_cache_ttl_expires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that safe negative cache entries expire after their Monday/month TTL."""
    # Freeze the wall clock so expiry does not depend on the real calendar date.
    _freeze_cache_clock(monkeypatch, datetime(2026, 6, 17, 12, 0, tzinfo=_AST))
    cache = ResponseCache(cache_dir=str(tmp_path))
    # Build a safe negative (3 confirmations)
    for _ in range(3):
        cache.put_negative("test_ns", "key1")

    # Entry is fresh and safe — should be served
    result = cache.get("test_ns", "key1")
    assert result is not None
    assert result["_negative"] is True
    assert result["_safe"] is True

    # Backdate to after the (frozen) month boundary with a Monday since elapsed.
    path = Path(cache._entry_path("test_ns", "key1"))
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["timestamp"] = datetime(2026, 6, 2, tzinfo=_AST).timestamp()
    path.write_text(json.dumps(entry), encoding="utf-8")

    # Now it should be expired (either monthly or Monday boundary)
    assert cache.get("test_ns", "key1") is None


def test_positive_cache_survives_past_ttl(tmp_path: Path) -> None:
    """Test that positive cache entries are NOT expired by TTL (only monthly boundary expires them)."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("test_ns", "key1", {"v": "hello"}, ttl_days=30)

    # Backdate to 31 days ago — past the ttl_days=30 window
    path = Path(cache._entry_path("test_ns", "key1"))
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["timestamp"] = time.time() - 31 * 86400
    # Keep it within the monthly boundary so only TTL could expire it
    entry["timestamp"] = max(entry["timestamp"], cache._month_boundary + 1)
    path.write_text(json.dumps(entry), encoding="utf-8")

    result = cache.get("test_ns", "key1")
    assert result == {"v": "hello"}, "Positive entries must survive past their ttl_days"


def test_cache_counter_positive_hit(tmp_path: Path) -> None:
    """Test that a positive cache hit increments the positive counter."""
    reset_cache_hit_counts()
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("test_ns", "key1", {"v": 1})
    cache.get("test_ns", "key1")
    counts = get_cache_hit_counts()
    assert counts["positive"] == 1
    assert counts["negative"] == 0


def test_cache_counter_negative_hit(tmp_path: Path) -> None:
    """Test that a safe negative cache hit increments the negative counter."""
    reset_cache_hit_counts()
    cache = ResponseCache(cache_dir=str(tmp_path))
    # Build a safe negative (3 confirmations)
    for _ in range(3):
        cache.put_negative("test_ns", "key1")
    cache.get("test_ns", "key1")
    counts = get_cache_hit_counts()
    assert counts["negative"] == 1
    assert counts["positive"] == 0


def test_cache_counter_miss(tmp_path: Path) -> None:
    """Test that a cache miss increments the miss counter."""
    reset_cache_hit_counts()
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.get("test_ns", "nonexistent")
    counts = get_cache_hit_counts()
    assert counts["miss"] == 1
    assert counts["positive"] == 0


def test_cache_counter_reset(tmp_path: Path) -> None:
    """Test that reset_cache_hit_counts zeroes all counters."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("test_ns", "key1", {"v": 1})
    cache.get("test_ns", "key1")
    reset_cache_hit_counts()
    counts = get_cache_hit_counts()
    assert counts == {"positive": 0, "negative": 0, "miss": 0}


# --- Three-tier negative cache tests ---


def test_put_negative_confirmation_counting(tmp_path: Path) -> None:
    """Test that put_negative increments confirmation counter across calls."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    ns, key = "test_ns", "neg_key"

    for expected_count in range(1, 4):
        cache.put_negative(ns, key)
        path = Path(cache._entry_path(ns, key))
        entry = json.loads(path.read_text(encoding="utf-8"))
        data = entry["data"]
        assert data["_negative"] is True
        assert data["_confirmations"] == expected_count
        assert data["_safe"] == (expected_count >= 3)


def test_unconfirmed_negative_not_served(tmp_path: Path) -> None:
    """Test that get() returns None for unconfirmed negatives (< 3 confirmations)."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    ns, key = "test_ns", "neg_key"

    cache.put_negative(ns, key)  # 1 confirmation
    assert cache.get(ns, key) is None

    cache.put_negative(ns, key)  # 2 confirmations
    assert cache.get(ns, key) is None


def test_safe_negative_served(tmp_path: Path) -> None:
    """Test that get() returns the entry for safe negatives (>= 3 confirmations)."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    ns, key = "test_ns", "neg_key"

    for _ in range(3):
        cache.put_negative(ns, key)

    result = cache.get(ns, key)
    assert result is not None
    assert result["_negative"] is True
    assert result["_safe"] is True
    assert result["_confirmations"] == 3


def test_confirmation_counter_capped(tmp_path: Path) -> None:
    """Test that confirmation counter stabilizes and doesn't grow unbounded."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    ns, key = "test_ns", "neg_key"

    for _ in range(10):
        cache.put_negative(ns, key)

    path = Path(cache._entry_path(ns, key))
    entry = json.loads(path.read_text(encoding="utf-8"))
    # Counter caps at CACHE_NEGATIVE_CONFIRM_RUNS (3) then +1 = 4, stabilizes there
    assert entry["data"]["_confirmations"] == 4
    assert entry["data"]["_safe"] is True


def test_positive_overwrites_negative(tmp_path: Path) -> None:
    """Test that put() replaces a negative entry with positive data."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    ns, key = "test_ns", "neg_key"

    for _ in range(3):
        cache.put_negative(ns, key)

    result = cache.get(ns, key)
    assert result is not None
    assert result["_negative"] is True

    # Overwrite with positive data
    cache.put(ns, key, {"title": "Real Paper"})
    result = cache.get(ns, key)
    assert result == {"title": "Real Paper"}


def test_safe_negative_monday_expiry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that a safe negative expires after the next Monday boundary."""
    # Freeze the wall clock to a fixed Wednesday mid-month so the Monday-boundary
    # math is date-independent (the real calendar must not decide the outcome).
    _freeze_cache_clock(monkeypatch, datetime(2026, 6, 17, 12, 0, tzinfo=_AST))
    cache = ResponseCache(cache_dir=str(tmp_path))
    ns, key = "test_ns", "neg_key"

    for _ in range(3):
        cache.put_negative(ns, key)

    # Backdate to after the (frozen) month boundary with a Monday since elapsed,
    # isolating the Monday branch (timestamp stays above the month boundary).
    path = Path(cache._entry_path(ns, key))
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["timestamp"] = datetime(2026, 6, 2, tzinfo=_AST).timestamp()
    path.write_text(json.dumps(entry), encoding="utf-8")

    assert cache.get(ns, key) is None


def test_safe_negative_month_boundary_expiry(tmp_path: Path) -> None:
    """Test that a safe negative before the monthly boundary is expired."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    ns, key = "test_ns", "neg_key"

    for _ in range(3):
        cache.put_negative(ns, key)

    # Backdate to before the monthly boundary
    path = Path(cache._entry_path(ns, key))
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["timestamp"] = cache._month_boundary - 1
    path.write_text(json.dumps(entry), encoding="utf-8")

    assert cache.get(ns, key) is None


def test_counter_unconfirmed_increments_miss(tmp_path: Path) -> None:
    """Test that unconfirmed negative increments miss counter, not negative counter."""
    reset_cache_hit_counts()
    cache = ResponseCache(cache_dir=str(tmp_path))
    ns, key = "test_ns", "neg_key"

    cache.put_negative(ns, key)  # 1 confirmation — unconfirmed
    cache.get(ns, key)  # Should be a miss

    counts = get_cache_hit_counts()
    assert counts["miss"] == 1
    assert counts["negative"] == 0


def test_error_preserves_count(tmp_path: Path) -> None:
    """Test that skipping put_negative preserves existing count on disk."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    ns, key = "test_ns", "neg_key"

    cache.put_negative(ns, key)
    cache.put_negative(ns, key)

    path = Path(cache._entry_path(ns, key))
    entry = json.loads(path.read_text(encoding="utf-8"))
    assert entry["data"]["_confirmations"] == 2

    # Simulate error iteration — no put_negative call → count unchanged
    entry2 = json.loads(path.read_text(encoding="utf-8"))
    assert entry2["data"]["_confirmations"] == 2

    # Next successful empty call increments from 2 to 3
    cache.put_negative(ns, key)
    entry3 = json.loads(path.read_text(encoding="utf-8"))
    assert entry3["data"]["_confirmations"] == 3
    assert entry3["data"]["_safe"] is True


def test_safe_negative_expired_created_on_monday() -> None:
    """Entry created on Monday expires next Monday (7 days), not same day."""
    # March 2, 2026 is a Monday
    created_ts = datetime(2026, 3, 2, 12, 0, 0, tzinfo=_AST).timestamp()

    # March 8 (Saturday) — NOT expired (next Monday is March 9)
    with patch("src.cache.datetime") as mock_dt:
        mock_dt.fromtimestamp = datetime.fromtimestamp
        mock_dt.now.return_value = datetime(2026, 3, 8, 23, 59, 0, tzinfo=_AST)
        assert not ResponseCache._safe_negative_expired(created_ts)

    # March 9 (Monday) — expired
    with patch("src.cache.datetime") as mock_dt:
        mock_dt.fromtimestamp = datetime.fromtimestamp
        mock_dt.now.return_value = datetime(2026, 3, 9, 0, 0, 0, tzinfo=_AST)
        assert ResponseCache._safe_negative_expired(created_ts)


def test_safe_negative_expired_month_before_monday() -> None:
    """When 1st of month comes before next Monday, expire at 1st."""
    # March 31, 2026 is a Tuesday — next Monday = April 6, but April 1 comes first
    created_ts = datetime(2026, 3, 31, 12, 0, 0, tzinfo=_AST).timestamp()

    # March 31 end-of-day — NOT expired
    with patch("src.cache.datetime") as mock_dt:
        mock_dt.fromtimestamp = datetime.fromtimestamp
        mock_dt.now.return_value = datetime(2026, 3, 31, 23, 59, 0, tzinfo=_AST)
        assert not ResponseCache._safe_negative_expired(created_ts)

    # April 1 — expired (month boundary before Monday)
    with patch("src.cache.datetime") as mock_dt:
        mock_dt.fromtimestamp = datetime.fromtimestamp
        mock_dt.now.return_value = datetime(2026, 4, 1, 0, 0, 0, tzinfo=_AST)
        assert ResponseCache._safe_negative_expired(created_ts)


def test_safe_negative_expired_december() -> None:
    """December entry: next 1st = January 1 of next year."""
    # December 28, 2026 is a Monday — next Monday = Jan 4, 2027; Jan 1 comes first
    created_ts = datetime(2026, 12, 28, 12, 0, 0, tzinfo=_AST).timestamp()

    # December 31 — NOT expired
    with patch("src.cache.datetime") as mock_dt:
        mock_dt.fromtimestamp = datetime.fromtimestamp
        mock_dt.now.return_value = datetime(2026, 12, 31, 23, 59, 0, tzinfo=_AST)
        assert not ResponseCache._safe_negative_expired(created_ts)

    # January 1, 2027 — expired
    with patch("src.cache.datetime") as mock_dt:
        mock_dt.fromtimestamp = datetime.fromtimestamp
        mock_dt.now.return_value = datetime(2027, 1, 1, 0, 0, 0, tzinfo=_AST)
        assert ResponseCache._safe_negative_expired(created_ts)


def test_ttl_days_not_stored(tmp_path: Path) -> None:
    """ttl_days must not be written into cache entries (they are never read back)."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("ns", "k", {"v": 1}, ttl_days=99)
    entry = json.loads(Path(cache._entry_path("ns", "k")).read_text(encoding="utf-8"))
    assert "ttl_days" not in entry
    assert entry["data"] == {"v": 1}
    # The positive entry is still served (freshness = monthly boundary, not ttl).
    assert cache.get("ns", "k") == {"v": 1}


def test_month_boundary_recomputed_not_frozen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_month_boundary must recompute per access, not freeze at construction."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    _freeze_cache_clock(monkeypatch, datetime(2026, 3, 15, tzinfo=_AST))
    march_boundary = cache._month_boundary
    _freeze_cache_clock(monkeypatch, datetime(2026, 4, 15, tzinfo=_AST))
    april_boundary = cache._month_boundary
    assert april_boundary > march_boundary
    assert april_boundary == datetime(2026, 4, 1, tzinfo=_AST).timestamp()
