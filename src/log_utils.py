from __future__ import annotations

import logging
import os
import sys
from typing import Optional


# Define custom log levels for enhanced workflow visibility
STEP_LEVEL = 25  # Between INFO (20) and WARNING (30)
SUCCESS_LEVEL = 22  # Between INFO (20) and STEP (25)

# Register custom levels with the logging module
logging.addLevelName(STEP_LEVEL, "STEP")
logging.addLevelName(SUCCESS_LEVEL, "SUCCESS")


class ColoredFormatter(logging.Formatter):
    """
    Custom formatter that adds ANSI color codes to log messages for terminal output,
    making different log levels easily distinguishable at a glance.
    """

    COLORS = {
        "DEBUG": "\033[36m",      # Cyan
        "INFO": "\033[37m",       # White
        "STEP": "\033[96m",       # Bright cyan
        "SUCCESS": "\033[92m",    # Bright green
        "WARNING": "\033[93m",    # Bright yellow
        "ERROR": "\033[91m",      # Bright red
        "CRITICAL": "\033[91m",   # Bright red
    }
    RESET = "\033[0m"

    def __init__(self, fmt: str, use_color: bool = True):
        """
        Initialize the formatter with optional color support based on terminal capability.
        """
        super().__init__(fmt)
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        """
        Format the log record with optional color codes based on the log level.
        """
        formatted = super().format(record)
        if self.use_color and record.levelname in self.COLORS:
            color = self.COLORS[record.levelname]
            return f"{color}{formatted}{self.RESET}"
        return formatted


class IndentAdapter(logging.LoggerAdapter):
    """
    Adapter that adds indentation support to log messages for visual hierarchy,
    allowing nested operations to be clearly distinguished in the output.
    """

    def process(self, msg: str, kwargs: dict) -> tuple[str, dict]:
        """
        Add indentation prefix to messages based on the indent level in extra data.
        """
        # Extract indent from extra dict
        extra = kwargs.get("extra", {})
        indent = extra.pop("indent", 0)
        prefix = "    " * indent  # 4 spaces per indent level
        return f"{prefix}{msg}", kwargs


class Logger:
    """
    Enhanced logger using Python's standard logging module with support for
    colors, custom levels (STEP, SUCCESS), file mirroring, and indentation.

    This implementation is compatible with PyCharm and other IDEs that support
    standard Python logging formats, enabling syntax highlighting and log filtering.
    """

    def __init__(self):
        """
        Initialize the logger with console and optional file handlers, detecting
        terminal capabilities for color support.
        """
        self._logger = logging.getLogger("CiteForge")
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False  # Prevent duplicate logs from root logger

        # Remove any existing handlers to avoid duplicates
        self._logger.handlers.clear()

        # Console handler with color support
        self._console_handler = logging.StreamHandler(sys.stdout)
        self._console_handler.setLevel(logging.DEBUG)

        # Detect if terminal supports colors
        use_color = sys.stdout.isatty()

        # Standard format compatible with PyCharm and most log viewers
        # Format: 2025-01-13 14:23:45 [INFO    ] Processing article...
        log_format = "%(asctime)s [%(levelname)-8s] %(message)s"
        date_format = "%Y-%m-%d %H:%M:%S"

        console_formatter = ColoredFormatter(log_format, use_color=use_color)
        console_formatter.datefmt = date_format
        self._console_handler.setFormatter(console_formatter)
        self._logger.addHandler(self._console_handler)

        # File handler (added later via set_log_file)
        self._file_handler: Optional[logging.FileHandler] = None
        self.log_file_path: Optional[str] = None

        # Adapter for indentation support
        self._adapter = IndentAdapter(self._logger, {})

        # Add custom level methods to the logger
        self._add_custom_methods()

    def _add_custom_methods(self):
        """
        Add custom logging methods for STEP and SUCCESS levels to the underlying logger.
        """
        def step_method(msg: str, *args, **kwargs):
            if self._logger.isEnabledFor(STEP_LEVEL):
                self._logger._log(STEP_LEVEL, msg, args, **kwargs)

        def success_method(msg: str, *args, **kwargs):
            if self._logger.isEnabledFor(SUCCESS_LEVEL):
                self._logger._log(SUCCESS_LEVEL, msg, args, **kwargs)

        # Attach to logger instance
        self._logger.step = step_method
        self._logger.success = success_method

    def set_log_file(self, path: str):
        """
        Start mirroring all log messages to the specified file, creating parent
        directories as needed and using a plain format without colors.
        """
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError:
            pass

        try:
            # Remove existing file handler if present
            if self._file_handler:
                self._logger.removeHandler(self._file_handler)
                self._file_handler.close()

            # Create new file handler
            self._file_handler = logging.FileHandler(path, mode="w", encoding="utf-8")
            self._file_handler.setLevel(logging.DEBUG)

            # Plain format for file (no colors)
            log_format = "%(asctime)s [%(levelname)-8s] %(message)s"
            date_format = "%Y-%m-%d %H:%M:%S"
            file_formatter = logging.Formatter(log_format, datefmt=date_format)
            self._file_handler.setFormatter(file_formatter)

            self._logger.addHandler(self._file_handler)
            self.log_file_path = path
        except OSError as e:
            self._file_handler = None
            self.log_file_path = None
            # Log error to console only
            self._logger.error(f"Failed to open log file {path}: {e}")

    def close(self):
        """
        Stop logging to file by removing and closing the file handler while
        preserving console logging.
        """
        if self._file_handler:
            self._logger.removeHandler(self._file_handler)
            self._file_handler.close()
            self._file_handler = None
            self.log_file_path = None

    def step(self, msg: str):
        """
        Log a top-level workflow step (e.g., starting work on a new author),
        highlighted prominently with no indentation.
        """
        self._adapter.log(STEP_LEVEL, msg, extra={"indent": 0})

    def substep(self, msg: str):
        """
        Log a nested step under the current operation (e.g., processing an article),
        indented one level for visual hierarchy.
        """
        self._adapter.log(STEP_LEVEL, msg, extra={"indent": 1})

    def info(self, msg: str, *, indent: int = 1):
        """
        Log informational messages about normal progress and decisions, with
        optional indentation to reflect workflow structure.
        """
        self._adapter.info(msg, extra={"indent": indent})

    def warn(self, msg: str, *, indent: int = 1):
        """
        Log warnings for recoverable issues that do not stop processing
        (e.g., missing fields, skipped enrichments).
        """
        self._adapter.warning(msg, extra={"indent": indent})

    def error(self, msg: str, *, indent: int = 1):
        """
        Log errors for failures that prevent a step from completing as expected,
        routed to stderr for visibility while allowing the pipeline to continue.
        """
        # Switch error handler to stderr
        old_stream = self._console_handler.stream
        self._console_handler.setStream(sys.stderr)
        try:
            self._adapter.error(msg, extra={"indent": indent})
        finally:
            self._console_handler.setStream(old_stream)

    def success(self, msg: str, *, indent: int = 1):
        """
        Log successful operations and milestones (e.g., file saved, enrichment complete),
        highlighted in green for easy identification.
        """
        self._adapter.log(SUCCESS_LEVEL, msg, extra={"indent": indent})


# Global logger instance for use across the application
logger = Logger()
