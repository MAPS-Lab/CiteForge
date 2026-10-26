"""Colored, category-tagged logging.

A thread-aware logger with custom STEP and SUCCESS levels and category tags. It
mirrors each worker's output to a per-author log file while writing the main run
log, so a run can be followed both globally and per author.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
from collections.abc import MutableMapping
from typing import Any, ClassVar, cast

STEP_LEVEL = 25  # Between INFO (20) and WARNING (30)
SUCCESS_LEVEL = 22  # Between INFO (20) and STEP (25)

logging.addLevelName(STEP_LEVEL, "STEP")
logging.addLevelName(SUCCESS_LEVEL, "SUCCESS")


class LogSource:
    """Data-source constants for log naming and coloring."""

    SCHOLAR = "Scholar"
    DBLP = "DBLP"
    S2 = "Semantic Scholar"
    CROSSREF = "Crossref"
    OPENREVIEW = "OpenReview"
    ARXIV = "arXiv"
    OPENALEX = "OpenAlex"
    PUBMED = "PubMed"
    EUROPEPMC = "Europe PMC"
    DOI = "DOI"
    SYSTEM = "System"


class LogCategory:
    """Constants for log categories to replace indentation with semantic tagging."""

    AUTHOR = "AUTHOR"
    ARTICLE = "ARTICLE"
    FETCH = "FETCH"
    SEARCH = "SEARCH"
    MATCH = "MATCH"
    SAVE = "SAVE"
    SKIP = "SKIP"
    ERROR = "ERROR"
    DEBUG = "DEBUG"
    PLAN = "PLAN"
    # Audit categories (file-only, never console)
    AUDIT = "AUDIT"
    MERGE = "MERGE"
    CLEANUP = "CLEANUP"
    DEDUP = "DEDUP"
    CACHE = "CACHE"
    SCORE = "SCORE"
    DOI_VAL = "DOI_VAL"
    ARXIV = "ARXIV"
    PARSE = "PARSE"
    SERIAL = "SERIAL"
    CITEKEY = "CITEKEY"


class ColoredFormatter(logging.Formatter):
    """Formatter that adds ANSI color codes for levels, sources, and categories."""

    # ANSI Color Codes
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    BOLD_CYAN = "\033[1;36m"
    BOLD_GREEN = "\033[1;32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    BOLD_RED = "\033[1;31m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    GREEN = "\033[32m"
    LIGHT_MAGENTA = "\033[95m"
    LIGHT_BLUE = "\033[94m"
    LIGHT_CYAN = "\033[96m"
    DARK_GRAY = "\033[90m"
    BOLD_MAGENTA = "\033[1;35m"
    BOLD_BLUE = "\033[1;34m"
    RESET = "\033[0m"

    # Level colors
    LEVEL_COLORS: ClassVar[dict[str, str]] = {
        "DEBUG": CYAN,
        "INFO": WHITE,
        "STEP": BOLD_CYAN,
        "SUCCESS": BOLD_GREEN,
        "WARNING": YELLOW,
        "ERROR": RED,
        "CRITICAL": BOLD_RED,
    }

    # Source colors (background or distinct foreground)
    SOURCE_COLORS: ClassVar[dict[str, str]] = {
        LogSource.SCHOLAR: BLUE,
        LogSource.DBLP: CYAN,
        LogSource.S2: MAGENTA,
        LogSource.CROSSREF: YELLOW,
        LogSource.OPENREVIEW: RED,
        LogSource.ARXIV: GREEN,
        LogSource.OPENALEX: LIGHT_MAGENTA,
        LogSource.PUBMED: LIGHT_BLUE,
        LogSource.EUROPEPMC: LIGHT_CYAN,
        LogSource.DOI: DARK_GRAY,
        LogSource.SYSTEM: WHITE,
    }

    # Category colors (audit categories all use DARK_GRAY for file-only output)
    CATEGORY_COLORS: ClassVar[dict[str, str]] = {
        LogCategory.AUTHOR: BOLD_MAGENTA,
        LogCategory.ARTICLE: BOLD_BLUE,
        LogCategory.FETCH: CYAN,
        LogCategory.SEARCH: YELLOW,
        LogCategory.MATCH: BOLD_GREEN,
        LogCategory.SAVE: GREEN,
        LogCategory.SKIP: DARK_GRAY,
        LogCategory.ERROR: RED,
        LogCategory.DEBUG: DARK_GRAY,
        LogCategory.PLAN: MAGENTA,
        **dict.fromkeys(
            [
                LogCategory.AUDIT,
                LogCategory.MERGE,
                LogCategory.CLEANUP,
                LogCategory.DEDUP,
                LogCategory.CACHE,
                LogCategory.SCORE,
                LogCategory.DOI_VAL,
                LogCategory.ARXIV,
                LogCategory.PARSE,
                LogCategory.SERIAL,
                LogCategory.CITEKEY,
            ],
            DARK_GRAY,
        ),
    }

    def __init__(self, fmt: str, use_color: bool = True):
        super().__init__(fmt)
        self.use_color = use_color

    def _colored_tag(self, label: str, color_map: dict[str, str]) -> str:
        """Build a colored `[label]` tag, falling back to plain text if no color mapping exists."""
        color = color_map.get(label, "")
        return f"{color}[{label}]{self.RESET}" if color else f"[{label}]"

    def format(self, record: logging.LogRecord) -> str:
        original_msg = record.msg
        original_levelname = record.levelname
        source = getattr(record, "source", None)
        category = getattr(record, "category", None)

        if self.use_color:
            if record.levelname in self.LEVEL_COLORS:
                record.levelname = f"{self.LEVEL_COLORS[record.levelname]}{record.levelname}{self.RESET}"

            parts: list[str] = []
            if source:
                parts.append(self._colored_tag(source, self.SOURCE_COLORS))
            if category:
                parts.append(self._colored_tag(category, self.CATEGORY_COLORS))
            if parts:
                record.msg = f"{' '.join(parts)} {record.msg}"

        formatted = super().format(record)
        record.msg = original_msg
        record.levelname = original_levelname
        return formatted


class CategoryAdapter(logging.LoggerAdapter):  # type: ignore[type-arg]
    """Adapter that passes source and category through to the extra dict."""

    def process(self, msg: str, kwargs: MutableMapping[str, Any]) -> tuple[str, MutableMapping[str, Any]]:
        extra = kwargs.get("extra", {})
        for key in ("source", "category"):
            value = kwargs.pop(key, None)
            if value:
                extra[key] = value
        kwargs["extra"] = extra
        return msg, kwargs


class MainThreadFilter(logging.Filter):
    """Filter that only allows log records from the main thread."""

    def filter(self, record: logging.LogRecord) -> bool:
        return threading.current_thread() is threading.main_thread()


class ThreadLocalFileHandler(logging.Handler):
    """Handler that delegates to a thread-local file handler if one exists."""

    def __init__(self, thread_local_storage: threading.local):
        super().__init__()
        self._tls = thread_local_storage

    def emit(self, record: logging.LogRecord) -> None:
        handler = getattr(self._tls, "handler", None)
        if handler:
            handler.emit(record)


# One open file handler per log path, shared by every thread bound to it and
# reference counted. A per-thread handler cannot work once more than one thread
# writes an author's log, because FileHandler opens mode="w" and the second
# thread would truncate the first thread's output. logging.Handler.emit locks
# per record, so lines from concurrent threads interleave but never tear. Logs
# are untracked, so interleaving does not affect output determinism.
_SHARED_HANDLERS: dict[str, list[Any]] = {}
_SHARED_HANDLERS_LOCK = threading.Lock()


def _acquire_shared_handler(path: str, fmt: str, datefmt: str) -> logging.FileHandler:
    """Return the handler for *path*, opening it on first use. Raises OSError."""
    with _SHARED_HANDLERS_LOCK:
        entry = _SHARED_HANDLERS.get(path)
        if entry is None:
            handler = logging.FileHandler(path, mode="w", encoding="utf-8")
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
            _SHARED_HANDLERS[path] = [handler, 1]
            return handler
        entry[1] += 1
        return cast("logging.FileHandler", entry[0])


def _release_shared_handler(path: str | None) -> None:
    """Drop one reference to *path*'s handler, closing it when the last one goes."""
    if path is None:
        return
    with _SHARED_HANDLERS_LOCK:
        entry = _SHARED_HANDLERS.get(path)
        if entry is None:
            return
        entry[1] -= 1
        if entry[1] <= 0:
            _SHARED_HANDLERS.pop(path, None)
            cast("logging.FileHandler", entry[0]).close()


class Logger:
    """Logger with colors, custom levels (STEP/SUCCESS), thread-local files, and categories."""

    LOG_FORMAT = "%(asctime)s [%(levelname)-8s] %(message)s"
    DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

    def __init__(self) -> None:
        self._logger = logging.getLogger("CiteForge")
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False
        self._logger.handlers.clear()

        # Console handler, main thread only
        self._console_handler = logging.StreamHandler(sys.stdout)
        self._console_handler.setLevel(logging.INFO)
        self._console_handler.addFilter(MainThreadFilter())

        console_formatter = ColoredFormatter(self.LOG_FORMAT, use_color=sys.stdout.isatty())
        console_formatter.datefmt = self.DATE_FORMAT
        self._console_handler.setFormatter(console_formatter)
        self._logger.addHandler(self._console_handler)

        # Thread-local file handler delegation
        self._thread_local = threading.local()
        self._tl_handler = ThreadLocalFileHandler(self._thread_local)
        self._logger.addHandler(self._tl_handler)

        self._adapter = CategoryAdapter(self._logger, {})

    def set_log_file(self, path: str) -> None:
        """Bind this thread to *path*'s shared log handler, opening it if needed."""
        with contextlib.suppress(OSError):
            os.makedirs(os.path.dirname(path), exist_ok=True)

        self._close_thread_handler()
        try:
            self._thread_local.handler = _acquire_shared_handler(path, self.LOG_FORMAT, self.DATE_FORMAT)
            self._thread_local.log_file_path = path
        except OSError as e:
            self._thread_local.handler = None
            self._thread_local.log_file_path = None
            self._logger.error(f"Failed to open log file {path}: {e}")

    def _close_thread_handler(self) -> None:
        """Release this thread's reference to its log handler, if it holds one."""
        if getattr(self._thread_local, "handler", None):
            _release_shared_handler(getattr(self._thread_local, "log_file_path", None))
            self._thread_local.handler = None
            self._thread_local.log_file_path = None

    def close(self) -> None:
        """Stop logging to file for the current thread."""
        self._close_thread_handler()

    def step(self, msg: str, *, source: str | None = None, category: str | None = None) -> None:
        self._adapter.log(STEP_LEVEL, msg, source=source, category=category)

    def info(self, msg: str, *, source: str | None = None, category: str | None = None) -> None:
        self._adapter.info(msg, source=source, category=category)

    def warn(self, msg: str, *, source: str | None = None, category: str | None = None) -> None:
        self._adapter.warning(msg, source=source, category=category)

    def error(self, msg: str, *, source: str | None = None, category: str | None = None) -> None:
        self._adapter.error(msg, source=source, category=category)

    def success(self, msg: str, *, source: str | None = None, category: str | None = None) -> None:
        self._adapter.log(SUCCESS_LEVEL, msg, source=source, category=category)

    def debug(self, msg: str, *, source: str | None = None, category: str | None = None) -> None:
        """Debug-level audit messages (file only, never console)."""
        self._adapter.debug(msg, source=source, category=category)

    @property
    def log_file_path(self) -> str | None:
        return getattr(self._thread_local, "log_file_path", None)


# Global logger instance
logger = Logger()
