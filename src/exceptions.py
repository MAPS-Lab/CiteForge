from __future__ import annotations

import csv
import json
import socket
import urllib.error
import xml.etree.ElementTree as ElementTree
import requests

__all__ = [
    "HTTP_ERRORS",
    "TIMEOUT_ERRORS",
    "NETWORK_ERRORS",
    "DECODE_ERRORS",
    "PARSE_ERRORS",
    "ALL_FETCH_ERRORS",
    "ALL_API_ERRORS",
    "FILE_IO_ERRORS",
    "NUMERIC_ERRORS",
    "JSON_ERRORS",
    "FILE_READ_ERRORS",
    "API_WITH_OS_ERRORS",
    "FULL_OPERATION_ERRORS",
    "XML_PARSE_ERRORS",
    "CSV_ERRORS",
    "FIELD_ACCESS_ERRORS",
    "FILE_WRITE_ERRORS",
]

# errors raised by urllib when an HTTP request fails or a URL cannot be reached
HTTP_ERRORS = (urllib.error.HTTPError, urllib.error.URLError, requests.exceptions.RequestException)

# errors that signal an operation has taken too long and hit a timeout at the OS or socket level
TIMEOUT_ERRORS = (TimeoutError, socket.timeout)

# umbrella group for network-related failures, combining HTTP issues, timeouts,
# and unexpected runtime errors
NETWORK_ERRORS = HTTP_ERRORS + TIMEOUT_ERRORS + (RuntimeError,)

# errors that occur when converting response bytes into text using a specific encoding
DECODE_ERRORS = (UnicodeDecodeError, UnicodeError)

# errors raised while interpreting structured data such as JSON, XML, or response fields
PARSE_ERRORS = (ValueError, TypeError, KeyError)

# combined set of errors that may happen while fetching data from remote services
# and turning responses into usable structures
ALL_FETCH_ERRORS = NETWORK_ERRORS + DECODE_ERRORS + PARSE_ERRORS

# common API-facing errors focused on connectivity and decoding, usually raised before any parsing logic runs
ALL_API_ERRORS = NETWORK_ERRORS + DECODE_ERRORS

# file system operation errors when reading configuration files, API keys, or data files
# Note: FileNotFoundError is a subclass of OSError, so both are included for clarity
FILE_IO_ERRORS = (FileNotFoundError, OSError)

# numeric conversion and arithmetic errors raised during timestamp, ID, or count parsing
NUMERIC_ERRORS = (TypeError, ValueError, OverflowError)

# JSON parsing errors when loading config files or processing JSON API responses
JSON_ERRORS = (json.JSONDecodeError, ValueError, TypeError)

# combined file read errors including I/O failures, encoding issues, and malformed data
FILE_READ_ERRORS = FILE_IO_ERRORS + DECODE_ERRORS + PARSE_ERRORS

# API errors plus OS-level failures like permission issues or disk full
API_WITH_OS_ERRORS = ALL_API_ERRORS + (OSError,)

# comprehensive error set for end-to-end operations: fetch from API, parse response, and handle OS errors
FULL_OPERATION_ERRORS = ALL_API_ERRORS + PARSE_ERRORS + (OSError, ValueError)

# XML parsing errors when processing DBLP, arXiv, or other XML-based API responses
XML_PARSE_ERRORS = (ElementTree.ParseError, ValueError, TypeError)

# CSV file operation errors when reading or writing author data and summary files
CSV_ERRORS = (csv.Error, OSError, UnicodeDecodeError)

# field access and attribute lookup errors when extracting data from API responses or dictionaries
# commonly occurs when navigating nested structures with missing or mistyped fields
FIELD_ACCESS_ERRORS = (TypeError, ValueError, KeyError, AttributeError)

# file write operation errors including permissions, disk full, and encoding issues
FILE_WRITE_ERRORS = (OSError, TypeError, UnicodeEncodeError)
