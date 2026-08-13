"""Auxiliary enrichment services."""

from __future__ import annotations

import re

from ..config import GEMINI_BASE
from ..exceptions import ALL_API_ERRORS, FIELD_ACCESS_ERRORS
from ..http_utils import http_post_json
from ..log_utils import LogCategory, LogSource, logger

# ============ Gemini ============


def gemini_generate_short_title(full_title: str, api_key: str, max_words: int | None = None) -> str | None:
    """
    Call the Gemini API to generate a short CamelCase title for a publication,
    suitable for BibTeX keys and filenames.
    """
    from ..config import BIBTEX_KEY_MAX_WORDS

    if max_words is None:
        max_words = BIBTEX_KEY_MAX_WORDS

    if not api_key or not full_title:
        return None

    prompt = (
        f"Create a smart, concise CamelCase title (1 to {max_words} words) "
        f'for this publication: "{full_title}". '
        "Extract the most important keywords. "
        "Skip stop words (a, an, the, for, of, and, to, in, with, from, by, at). "
        f"Use exactly {max_words} words or fewer if shorter captures the essence better. "
        "IMPORTANT: Write as ONE word in CamelCase format with NO spaces between words "
        "(e.g., 'AttentionMechanism' not 'Attention Mechanism'). "
        "Return ONLY the CamelCase title with no quotes, explanation, spaces, or punctuation."
    )

    # The API key travels in the x-goog-api-key header rather than a URL query
    # parameter, so it never appears in a request URL, redirect, or log record.
    url = GEMINI_BASE
    request_headers = {"x-goog-api-key": api_key}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 50,
            "temperature": 0.3,
            "topP": 0.8,
            "topK": 20,
        },
    }

    try:
        data = http_post_json(url, payload, headers=request_headers, timeout=15.0)
    except (*ALL_API_ERRORS, ValueError) as e:
        logger.debug(f"GEMINI_FAIL | error={type(e).__name__}", category=LogCategory.CITEKEY)
        logger.warn(
            f"API call failed: {type(e).__name__}",
            category=LogCategory.ERROR,
            source=LogSource.SYSTEM,
        )
        return None

    if data is None:
        logger.debug("GEMINI_FAIL | reason=no_response", category=LogCategory.CITEKEY)
        return None

    # The decoded body is untrusted: json.loads can hand back a non-dict, a
    # candidate or part can be a non-dict, and "text" can be present-but-null
    # (where a default of "" does not apply). Unguarded, that AttributeError
    # escapes process_article past its except PARSE_ERRORS, which does not
    # include AttributeError, and aborts the whole author. FIELD_ACCESS_ERRORS
    # is the guard the other post-decode extraction sites already use.
    try:
        candidates = data.get("candidates") or []
        parts = (candidates[0].get("content", {}).get("parts") or []) if candidates else []
        raw_text = (parts[0].get("text") or "").strip() if parts else ""
    except FIELD_ACCESS_ERRORS:
        logger.debug("GEMINI_FAIL | reason=malformed_response", category=LogCategory.CITEKEY)
        return None
    short_title = re.sub(r"\s+", "", raw_text.strip("\"'"))

    if not short_title or len(short_title) > 100:
        logger.debug("GEMINI_FAIL | reason=invalid_length", category=LogCategory.CITEKEY)
        logger.warn("Returned no valid candidates in response", category=LogCategory.ERROR, source=LogSource.SYSTEM)
        return None

    word_count = sum(1 for c in short_title if c.isupper())
    if word_count > max_words:
        logger.debug("GEMINI_FAIL | reason=too_many_words", category=LogCategory.CITEKEY)
        logger.warn(
            f"Gemini returned {word_count} words (expected max {max_words}): '{short_title}'. "
            f"Falling back to default algorithm.",
            category=LogCategory.DEBUG,
            source=LogSource.SYSTEM,
        )
        return None

    logger.debug(
        f"GEMINI_SUCCESS | short={short_title} | word_count={word_count} | max_words={max_words} | valid=True",
        category=LogCategory.CITEKEY,
    )
    logger.info(
        f"Generated title: {short_title}",
        category=LogCategory.DEBUG,
        source=LogSource.SYSTEM,
    )
    return short_title
