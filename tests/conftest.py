from __future__ import annotations

import re


def extract_bibtex_field(bibtex_str: str, field_name: str) -> str | None:
    """Extract a field value from BibTeX output, handling nested braces."""
    pattern = rf"{field_name}\s*=\s*\{{"
    match = re.search(pattern, bibtex_str)
    if not match:
        return None
    start = match.end() - 1
    depth = 0
    for i in range(start, len(bibtex_str)):
        if bibtex_str[i] == "{":
            depth += 1
        elif bibtex_str[i] == "}":
            depth -= 1
            if depth == 0:
                return bibtex_str[start + 1 : i]
    return None
