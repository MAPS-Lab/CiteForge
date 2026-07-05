"""Centralized, deterministic directory scans.

Single source of truth for the directory-scan shapes the pipeline relies on.
All scans iterate in sorted order so that determinism (byte-identical output
on cache-hit runs) is structural rather than duplicated at every call site.

Near-leaf module: standard library plus the BibTeX parser (for the shared
scan+parse core used by the per-article duplicate scans).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from typing import Any

from .bibtex_utils import parse_bibtex_to_dict


def iter_author_bibs(author_dir: str) -> list[str]:
    """Return the ``.bib`` filenames in ``author_dir``, sorted.

    Filenames only (not full paths). ``OSError`` from :func:`os.listdir` is not
    swallowed here; callers keep whatever error handling they already had.
    """
    return sorted(f for f in os.listdir(author_dir) if f.endswith(".bib"))


def iter_parsed_author_bibs(
    author_dir: str,
    *,
    skip_basename: str | None = None,
    skip_path: str | None = None,
    read_errors: tuple[type[Exception], ...] = (OSError,),
    on_read_error: Callable[[str], None] | None = None,
) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield ``(filename, path, entry)`` for each parseable ``.bib`` in *author_dir*.

    Shared scan+parse core for the per-article duplicate scans (Phase 4
    candidate-DOI dedup in ``pipeline.article`` and
    ``merge_utils.save_entry_to_file``). Iterates in :func:`iter_author_bibs`
    order (sorted), so determinism is structural. Match semantics stay at the
    call sites; this helper only owns which files are opened and how read
    failures are skipped.

    ``skip_basename`` skips a file by name and ``skip_path`` by absolute-path
    identity, both before the file is opened. Exceptions in ``read_errors``
    raised while reading or parsing a file cause that file to be skipped,
    after invoking ``on_read_error`` with its filename when provided; other
    exceptions propagate. Files that parse to a falsy entry are skipped
    silently.
    """
    for filename in iter_author_bibs(author_dir):
        if skip_basename is not None and filename == skip_basename:
            continue
        path = os.path.join(author_dir, filename)
        if skip_path is not None and os.path.abspath(path) == os.path.abspath(skip_path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                entry = parse_bibtex_to_dict(fh.read())
        except read_errors:
            if on_read_error is not None:
                on_read_error(filename)
            continue
        if not entry:
            continue
        yield filename, path, entry


def iter_output_dirs(out_dir: str) -> list[str]:
    """Return the immediate subdirectory names of ``out_dir``, sorted.

    Directory entry names only (not full paths); plain files are excluded.
    ``OSError`` from :func:`os.listdir` is not swallowed here.
    """
    return sorted(e for e in os.listdir(out_dir) if os.path.isdir(os.path.join(out_dir, e)))
