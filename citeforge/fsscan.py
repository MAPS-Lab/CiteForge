"""Centralized, deterministic directory scans.

Single source of truth for the directory-scan shapes the pipeline relies on.
All scans iterate in sorted order so that determinism (byte-identical output
on cache-hit runs) is structural rather than duplicated at every call site.

Near-leaf module: standard library plus the BibTeX parser (for the shared
scan+parse core used by the per-article duplicate scans).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .bibtex_utils import parse_bibtex_to_dict

_FULL_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def _git() -> str:
    executable = shutil.which("git")
    if executable is None:
        raise ValueError("git is required for committed corpus scans")
    return executable


def _git_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in tuple(environment):
        if name in {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_INDEX_FILE",
            "GIT_CEILING_DIRECTORIES",
        } or name.startswith("GIT_CONFIG"):
            environment.pop(name, None)
    environment["GIT_NO_LAZY_FETCH"] = "1"
    return environment


def _object_format(repo_root: Path) -> str:
    try:
        value = subprocess.run(  # noqa: S603
            (_git(), "--no-replace-objects", "rev-parse", "--show-object-format"),
            cwd=repo_root,
            check=True,
            capture_output=True,
            env=_git_environment(),
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("committed Git object format is unreadable") from exc
    if value not in {"sha1", "sha256"}:
        raise ValueError("committed Git object format is unsupported")
    return value


def _verify_reachable_objects(repo_root: Path, commit: str) -> None:
    try:
        subprocess.run(  # noqa: S603
            (_git(), "--no-replace-objects", "fsck", "--no-dangling", "--no-reflogs", commit),
            cwd=repo_root,
            check=True,
            capture_output=True,
            env=_git_environment(),
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("committed Git objects fail integrity validation") from exc


@dataclass(frozen=True)
class GitTreeEntry:
    mode: str
    object_type: str
    object_id: str
    path: str


def read_committed_tree(repo_root: Path, commit: str, root: str) -> tuple[GitTreeEntry, ...]:
    """Read one committed Git tree without consulting working-tree bytes."""
    if not repo_root.is_dir() or not commit or not root or PurePosixPath(root).is_absolute():
        raise ValueError("invalid committed tree authority")
    if not _FULL_OBJECT_ID.fullmatch(commit):
        raise ValueError("committed tree authority requires one full commit object ID")
    try:
        resolved = subprocess.run(  # noqa: S603
            (_git(), "--no-replace-objects", "rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}"),
            cwd=repo_root,
            check=True,
            capture_output=True,
            env=_git_environment(),
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("committed Git tree is absent or unreadable") from exc
    if resolved != commit:
        raise ValueError("committed tree authority requires one full commit object ID")
    _verify_reachable_objects(repo_root, commit)
    command = ("git", "--no-replace-objects", "ls-tree", "-r", "-t", "-z", "--full-tree", commit, "--", root)
    try:
        raw = subprocess.run(  # noqa: S603
            (_git(), *command[1:]),
            cwd=repo_root,
            check=True,
            capture_output=True,
            env=_git_environment(),
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("committed Git tree is absent or unreadable") from exc
    entries: list[GitTreeEntry] = []
    seen: set[str] = set()
    root_parts = PurePosixPath(root).parts
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path_bytes = record.split(b"\t", 1)
            mode, object_type, object_id = metadata.decode("ascii").split(" ", 2)
            path = path_bytes.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("committed Git tree entry is malformed") from exc
        pure = PurePosixPath(path)
        if len(pure.parts) < len(root_parts) and root_parts[: len(pure.parts)] == pure.parts:
            continue
        if (
            pure.is_absolute()
            or not pure.parts
            or pure.parts[: len(root_parts)] != root_parts
            or any(
                part in {"", ".", ".."} or any(unicodedata.category(char) == "Cc" for char in part)
                for part in pure.parts
            )
        ):
            raise ValueError("committed Git tree path escapes its root")
        folded = path.casefold()
        if folded in seen:
            raise ValueError("committed Git tree has duplicate case-folded paths")
        seen.add(folded)
        if object_type == "tree" and mode != "040000":
            raise ValueError("committed Git tree mode is inconsistent")
        if object_type == "blob" and mode not in {"100644", "100755"}:
            raise ValueError("committed Git blob mode is unsupported")
        if object_type not in {"tree", "blob"}:
            raise ValueError("committed Git tree contains a submodule or non-blob leaf")
        entries.append(GitTreeEntry(mode, object_type, object_id, path))
    if not entries:
        raise ValueError("committed Git tree is absent")
    return tuple(sorted(entries, key=lambda item: item.path.casefold()))


def read_committed_blob(repo_root: Path, object_id: str) -> bytes:
    """Read one Git blob and fail closed on an absent or substituted object."""
    try:
        return subprocess.run(  # noqa: S603
            (_git(), "--no-replace-objects", "cat-file", "blob", object_id),
            cwd=repo_root,
            check=True,
            capture_output=True,
            env=_git_environment(),
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("committed Git blob is absent or unreadable") from exc


def read_committed_blobs(repo_root: Path, object_ids: tuple[str, ...]) -> dict[str, bytes]:
    """Read exact Git blobs through one bounded batch process."""
    if len(object_ids) != len(set(object_ids)) or any(not object_id for object_id in object_ids):
        raise ValueError("committed Git blob batch is invalid")
    try:
        process = subprocess.Popen(  # noqa: S603
            (_git(), "--no-replace-objects", "cat-file", "--batch"),
            cwd=repo_root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=_git_environment(),
        )
        stdout, _ = process.communicate("".join(f"{object_id}\n" for object_id in object_ids).encode())
    except OSError as exc:
        raise ValueError("committed Git blob batch is unreadable") from exc
    if process.returncode != 0:
        raise ValueError("committed Git blob batch failed")
    offset = 0
    result: dict[str, bytes] = {}
    object_format = _object_format(repo_root)
    for requested in object_ids:
        newline = stdout.find(b"\n", offset)
        if newline < 0:
            raise ValueError("committed Git blob batch header is truncated")
        try:
            object_id, object_type, size_text = stdout[offset:newline].decode("ascii").split()
            size = int(size_text)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("committed Git blob batch header is malformed") from exc
        if object_id != requested or object_type != "blob" or size < 0:
            raise ValueError("committed Git blob batch object is substituted")
        start = newline + 1
        end = start + size
        if end >= len(stdout) or stdout[end : end + 1] != b"\n":
            raise ValueError("committed Git blob batch body is truncated")
        body = stdout[start:end]
        hasher = hashlib.new(object_format)
        hasher.update(f"blob {len(body)}\0".encode())
        hasher.update(body)
        if hasher.hexdigest() != requested:
            raise ValueError("committed Git blob content does not match its object ID")
        result[requested] = body
        offset = end + 1
    if offset != len(stdout):
        raise ValueError("committed Git blob batch has trailing data")
    return result


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
