from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Optional


# Define custom log levels for enhanced workflow visibility
STEP_LEVEL = 25  # Between INFO (20) and WARNING (30)
SUCCESS_LEVEL = 22  # Between INFO (20) and STEP (25)

# Register custom levels with the logging module
logging.addLevelName(STEP_LEVEL, "STEP")
logging.addLevelName(SUCCESS_LEVEL, "SUCCESS")


class LogSource:
    """
    Constants for data sources to ensure consistent naming and coloring.
    """
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
    """
    Constants for log categories to replace indentation with semantic tagging.
    """
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


class ColoredFormatter(logging.Formatter):
    """
    Custom formatter that adds ANSI color codes to log messages for terminal output,
    making different log levels, sources, and categories easily distinguishable.
    """

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
    LEVEL_COLORS = {
        "DEBUG": CYAN,
        "INFO": WHITE,
        "STEP": BOLD_CYAN,
        "SUCCESS": BOLD_GREEN,
        "WARNING": YELLOW,
        "ERROR": RED,
        "CRITICAL": BOLD_RED,
    }

    # Source colors (background or distinct foreground)
    SOURCE_COLORS = {
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

    # Category colors
    CATEGORY_COLORS = {
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
    }

    def __init__(self, fmt: str, use_color: bool = True):
        """
        Initialize the formatter with optional color support based on terminal capability.
        """
        super().__init__(fmt)
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        """
        Format the log record with optional color codes based on the log level, source, and category.
        """
        # Save original message to restore later
        original_msg = record.msg
        
        # Extract source and category if present
        source = getattr(record, "source", None)
        category = getattr(record, "category", None)
        
        if self.use_color:
            # Colorize level name
            if record.levelname in self.LEVEL_COLORS:
                record.levelname = f"{self.LEVEL_COLORS[record.levelname]}{record.levelname}{self.RESET}"
            
            parts = []
            
            # Add colored source tag
            if source:
                if source in self.SOURCE_COLORS:
                    source_color = self.SOURCE_COLORS[source]
                    parts.append(f"{source_color}[{source}]{self.RESET}")
                else:
                    parts.append(f"[{source}]")
            
            # Add colored category tag
            if category:
                if category in self.CATEGORY_COLORS:
                    cat_color = self.CATEGORY_COLORS[category]
                    parts.append(f"{cat_color}[{category}]{self.RESET}")
                else:
                    parts.append(f"[{category}]")
            
            # Combine parts with message
            if parts:
                record.msg = f"{' '.join(parts)} {record.msg}"

        formatted = super().format(record)
        
        # Restore original message
        record.msg = original_msg
        
        return formatted


class CategoryAdapter(logging.LoggerAdapter):
    """
    Adapter that adds category support to log messages.
    """

    def process(self, msg: str, kwargs: dict) -> tuple[str, dict]:
        """
        Pass source and category to extra dict.
        """
        extra = kwargs.get("extra", {})
        
        source = kwargs.pop("source", None)
        if source:
            extra["source"] = source
            
        category = kwargs.pop("category", None)
        if category:
            extra["category"] = category
            
        kwargs["extra"] = extra
        return msg, kwargs


class MainThreadFilter(logging.Filter):
    """
    Filter that only allows log records from the main thread.
    """
    def filter(self, record: logging.LogRecord) -> bool:
        return threading.current_thread() is threading.main_thread()


class ThreadLocalFileHandler(logging.Handler):
    """
    Handler that delegates to a thread-local file handler if one exists.
    """
    def __init__(self, thread_local_storage: threading.local):
        super().__init__()
        self._tls = thread_local_storage

    def emit(self, record: logging.LogRecord):
        handler = getattr(self._tls, "handler", None)
        if handler:
            handler.emit(record)


class Logger:
    """
    Enhanced logger using Python's standard logging module with support for
    colors, custom levels (STEP, SUCCESS), file mirroring, and categories.
    Supports thread-local log files and restricts console output to the main thread.
    """

    LOG_FORMAT = "%(asctime)s [%(levelname)-8s] %(message)s"

    def __init__(self):
        """
        Initialize the logger with console and optional file handlers.
        """
        self._logger = logging.getLogger("CiteForge")
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False

        self._logger.handlers.clear()

        # Console handler - only for main thread
        self._console_handler = logging.StreamHandler(sys.stdout)
        self._console_handler.setLevel(logging.DEBUG)
        self._console_handler.addFilter(MainThreadFilter())

        use_color = sys.stdout.isatty()
        date_format = "%Y-%m-%d %H:%M:%S"

        console_formatter = ColoredFormatter(self.LOG_FORMAT, use_color=use_color)
        console_formatter.datefmt = date_format
        self._console_handler.setFormatter(console_formatter)
        self._logger.addHandler(self._console_handler)

        # Thread-local storage for file handlers
        self._thread_local = threading.local()
        
        # Thread-local delegating handler
        self._tl_handler = ThreadLocalFileHandler(self._thread_local)
        self._logger.addHandler(self._tl_handler)

        self._adapter = CategoryAdapter(self._logger, {})

        self._add_custom_methods()

    def _add_custom_methods(self):
        """
        Add custom logging methods for STEP and SUCCESS levels.
        """
        def step_method(msg: str, *args, **kwargs):
            if self._logger.isEnabledFor(STEP_LEVEL):
                self._logger._log(STEP_LEVEL, msg, args, **kwargs)

        def success_method(msg: str, *args, **kwargs):
            if self._logger.isEnabledFor(SUCCESS_LEVEL):
                self._logger._log(SUCCESS_LEVEL, msg, args, **kwargs)

        self._logger.step = step_method
        self._logger.success = success_method

    def set_log_file(self, path: str):
        """
        Start mirroring all log messages to the specified file for the current thread.
        """
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError:
            pass

        try:
            # Close existing handler for this thread if any
            if hasattr(self._thread_local, "handler") and self._thread_local.handler:
                self._thread_local.handler.close()

            handler = logging.FileHandler(path, mode="w", encoding="utf-8")
            handler.setLevel(logging.DEBUG)

            date_format = "%Y-%m-%d %H:%M:%S"
            file_formatter = logging.Formatter(self.LOG_FORMAT, datefmt=date_format)
            handler.setFormatter(file_formatter)

            self._thread_local.handler = handler
            self._thread_local.log_file_path = path
        except OSError as e:
            self._thread_local.handler = None
            self._thread_local.log_file_path = None
            self._logger.error(f"Failed to open log file {path}: {e}")

    def close(self):
        """
        Stop logging to file for the current thread.
        """
        if hasattr(self._thread_local, "handler") and self._thread_local.handler:
            self._thread_local.handler.close()
            self._thread_local.handler = None
            self._thread_local.log_file_path = None

    def step(self, msg: str, source: Optional[str] = None, category: Optional[str] = None):
        """
        Log a top-level workflow step.
        """
        self._adapter.log(STEP_LEVEL, msg, source=source, category=category)

    def substep(self, msg: str, source: Optional[str] = None, category: Optional[str] = None):
        """
        Log a nested step (deprecated, maps to step with category).
        """
        self._adapter.log(STEP_LEVEL, msg, source=source, category=category)

    def info(self, msg: str, *, source: Optional[str] = None, category: Optional[str] = None):
        """
        Log informational messages.
        """
        self._adapter.info(msg, source=source, category=category)

    def warn(self, msg: str, *, source: Optional[str] = None, category: Optional[str] = None):
        """
        Log warnings.
        """
        self._adapter.warning(msg, source=source, category=category)

    def error(self, msg: str, *, source: Optional[str] = None, category: Optional[str] = None):
        """
        Log errors.
        """
        self._adapter.error(msg, source=source, category=category)

    def success(self, msg: str, *, source: Optional[str] = None, category: Optional[str] = None):
        """
        Log successful operations.
        """
        self._adapter.log(SUCCESS_LEVEL, msg, source=source, category=category)

    @property
    def log_file_path(self) -> Optional[str]:
        """
        Get the log file path for the current thread.
        """
        return getattr(self._thread_local, "log_file_path", None)


# Global logger instance
logger = Logger()
