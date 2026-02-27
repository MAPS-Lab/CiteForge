from __future__ import annotations

import csv
import json
import urllib.error
import xml.etree.ElementTree as ElementTree

import requests

__all__ = [
    "ALL_API_ERRORS",
    "ALL_FETCH_ERRORS",
    "API_WITH_OS_ERRORS",
    "CSV_ERRORS",
    "DECODE_ERRORS",
    "FIELD_ACCESS_ERRORS",
    "FILE_IO_ERRORS",
    "FILE_READ_ERRORS",
    "FILE_WRITE_ERRORS",
    "FULL_OPERATION_ERRORS",
    "HTTP_ERRORS",
    "JSON_ERRORS",
    "NETWORK_ERRORS",
    "NUMERIC_ERRORS",
    "PARSE_ERRORS",
    "TIMEOUT_ERRORS",
    "XML_PARSE_ERRORS",
]

# HTTP/URL request failures
HTTP_ERRORS = (urllib.error.HTTPError, urllib.error.URLError, requests.exceptions.RequestException)

# socket/OS-level timeouts
TIMEOUT_ERRORS = (TimeoutError,)

# network: HTTP + timeout + runtime
NETWORK_ERRORS = HTTP_ERRORS + TIMEOUT_ERRORS + (RuntimeError,)

# text encoding/decoding
DECODE_ERRORS = (UnicodeDecodeError, UnicodeError)

# structured data parsing
PARSE_ERRORS = (ValueError, TypeError, KeyError)

# fetch + decode + parse
ALL_FETCH_ERRORS = NETWORK_ERRORS + DECODE_ERRORS + PARSE_ERRORS

# network + decode (pre-parse)
ALL_API_ERRORS = NETWORK_ERRORS + DECODE_ERRORS

# filesystem I/O
FILE_IO_ERRORS = (FileNotFoundError, OSError)

# numeric conversion/arithmetic
NUMERIC_ERRORS = (TypeError, ValueError, OverflowError)

# JSON deserialization
JSON_ERRORS = (json.JSONDecodeError, ValueError, TypeError)

# file I/O + decode + parse
FILE_READ_ERRORS = FILE_IO_ERRORS + DECODE_ERRORS + PARSE_ERRORS

# API + OS errors
API_WITH_OS_ERRORS = (*ALL_API_ERRORS, OSError)

# API + parse + OS combined
FULL_OPERATION_ERRORS = ALL_API_ERRORS + PARSE_ERRORS + (OSError,)

# XML deserialization
XML_PARSE_ERRORS = (ElementTree.ParseError, ValueError, TypeError)

# CSV I/O
CSV_ERRORS = (csv.Error, OSError, UnicodeDecodeError)

# dict/attr field access
FIELD_ACCESS_ERRORS = (TypeError, ValueError, KeyError, AttributeError)

# file write failures
FILE_WRITE_ERRORS = (OSError, TypeError, UnicodeEncodeError)
