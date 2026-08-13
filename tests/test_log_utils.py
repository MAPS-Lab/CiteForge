from __future__ import annotations

import threading
from pathlib import Path

from citeforge.log_utils import LogCategory, logger


def test_concurrent_threads_share_one_author_log(tmp_path: Path) -> None:
    """Two threads writing an author's log keep both sets of lines.

    A per-thread FileHandler opens mode="w", so the second thread to bind the
    same path truncated whatever the first had already written. Article-level
    parallelism puts several threads on one author's log, so the handler is
    shared and reference counted instead.
    """
    log_path = tmp_path / "Author (X1)" / "author.log"
    barrier = threading.Barrier(2)

    def worker(tag: str) -> None:
        logger.set_log_file(str(log_path))
        barrier.wait(timeout=5)
        for i in range(20):
            logger.info(f"{tag}-{i}", category=LogCategory.AUTHOR)
        logger.close()

    threads = [threading.Thread(target=worker, args=(tag,)) for tag in ("alpha", "beta")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    written = log_path.read_text(encoding="utf-8")
    assert written.count("alpha-") == 20, "first thread's lines were truncated away"
    assert written.count("beta-") == 20, "second thread's lines were lost"


def test_releasing_one_thread_leaves_the_log_open_for_the_other(tmp_path: Path) -> None:
    """close() on one thread must not shut the file another thread still holds."""
    log_path = tmp_path / "Author (X2)" / "author.log"
    opened = threading.Event()
    released = threading.Event()

    def holder() -> None:
        logger.set_log_file(str(log_path))
        opened.set()
        assert released.wait(timeout=5)
        logger.info("after-release", category=LogCategory.AUTHOR)
        logger.close()

    thread = threading.Thread(target=holder)
    thread.start()
    assert opened.wait(timeout=5)

    logger.set_log_file(str(log_path))
    logger.info("transient", category=LogCategory.AUTHOR)
    logger.close()
    released.set()

    thread.join(timeout=10)
    assert not thread.is_alive()

    written = log_path.read_text(encoding="utf-8")
    assert "transient" in written
    assert "after-release" in written, "the surviving thread lost its handler"
