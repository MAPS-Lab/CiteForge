"""Centralized, deterministic directory scans.

Single source of truth for the two directory-scan shapes the pipeline relies
on. Both return sorted results so that determinism (byte-identical output on
cache-hit runs) is structural rather than duplicated at every call site.

Leaf module: depends only on the standard library.
"""

from __future__ import annotations

import os


def iter_author_bibs(author_dir: str) -> list[str]:
    """Return the ``.bib`` filenames in ``author_dir``, sorted.

    Filenames only (not full paths). ``OSError`` from :func:`os.listdir` is not
    swallowed here; callers keep whatever error handling they already had.
    """
    return sorted(f for f in os.listdir(author_dir) if f.endswith(".bib"))


def iter_output_dirs(out_dir: str) -> list[str]:
    """Return the immediate subdirectory names of ``out_dir``, sorted.

    Directory entry names only (not full paths); plain files are excluded.
    ``OSError`` from :func:`os.listdir` is not swallowed here.
    """
    return sorted(e for e in os.listdir(out_dir) if os.path.isdir(os.path.join(out_dir, e)))
