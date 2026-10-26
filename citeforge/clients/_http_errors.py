"""Exception boundaries shared by Scholar HTTP clients."""

from __future__ import annotations

from ..exceptions import DECODE_ERRORS, HTTP_ERRORS, TIMEOUT_ERRORS

# Deliberately excludes RuntimeError, which is reserved for programming faults.
SCHOLAR_HTTP_ERRORS = HTTP_ERRORS + TIMEOUT_ERRORS + DECODE_ERRORS
