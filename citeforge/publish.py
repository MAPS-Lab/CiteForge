import csv
import io
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from citeforge.bibtex import FIELD_ORDER, STEM_MAX, read_entry, render
from citeforge.model import Decision, Member, Source, Verdict, Work
from citeforge.state import State, StateRow, atomic_write

GROUP_DIR = "a2i2"
SUMMARY = Path("summary.csv")
BASELINE = Path("baseline.json")
FLAGS = (
    Source.OPENALEX, Source.ORCID, Source.DBLP, Source.CROSSREF, Source.DATACITE, Source.CSL, Source.ARXIV,
    Source.CURATED,
)  # fmt: skip
_ALLOWED_FIELDS = frozenset(FIELD_ORDER) | {"ENTRYTYPE", "ID"}


class PlanError(RuntimeError):
    pass


def publishable(d: Decision | None, w: Work | None) -> bool:
    return d is not None and d.verdict is Verdict.ACCEPT and w is not None and w.excluded is None


def emitted(row: StateRow, member: str) -> bool:
    return publishable(row.members.get(member), row.work) and bool(row.names.stem)


def bib_paths(state: State, members: Sequence[Member], group: Sequence[Member]) -> dict[Path, str]:
    """Every .bib path the state emits, in member folders and the group folder, mapped to its work key."""
    out: dict[Path, str] = {}
    group_names = {m.dirname for m in group}
    for key in sorted(state):
        row = state[key]
        folders = [m.dirname for m in members if emitted(row, m.dirname)]
        if group_names.intersection(folders):
            folders.append(GROUP_DIR)
        for folder in folders:
            out[Path(folder) / f"{row.names.stem}.bib"] = key
    return out


def plan(
    state: State, members: Sequence[Member], group: Sequence[Member], output_name: str = "output"
) -> dict[Path, str]:
    files: dict[Path, str] = {}
    texts: dict[str, str] = {}
    summary: list[list[str]] = []
    counts: Counter[str] = Counter()
    for path, key in bib_paths(state, members, group).items():
        row = state[key]
        if key not in texts:
            texts[key] = render(row.work, row.names)
        files[path] = texts[key]
        folder = path.parent.as_posix()
        counts[folder] += 1
        if folder != GROUP_DIR:
            flags = [int(s in row.work.sources) for s in FLAGS]
            summary.append([f"{output_name}/{path.as_posix()}", str(sum(flags)), *map(str, flags)])
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["file_path", "trust_hits", *(s.value for s in FLAGS)])
    writer.writerows(sorted(summary))
    files[SUMMARY] = buf.getvalue()
    baseline = {"total": sum(counts.values()), "authors": dict(sorted(counts.items()))}
    files[BASELINE] = json.dumps(baseline, indent=2, sort_keys=True) + "\n"
    return files


def _balanced(value: str) -> bool:
    depth = 0
    for c in re.sub(r"\\.", "", value, flags=re.DOTALL):  # an escaped brace is text
        depth += {"{": 1, "}": -1}.get(c, 0)
        if depth < 0:
            return False
    return depth == 0


def check(files: Mapping[Path, str]) -> list[str]:
    problems: list[str] = []
    bibs = {p: t for p, t in files.items() if p.suffix == ".bib"}
    by_name: dict[str, set[str]] = {}
    seen: dict[tuple[str, str, str], Path] = {}
    folder_names: dict[tuple[str, str], Path] = {}
    counts: Counter[str] = Counter()
    for path, text in sorted(bibs.items()):
        folder = path.parent.as_posix()
        counts[folder] += 1
        by_name.setdefault(path.name, set()).add(text)
        if len(path.stem) > STEM_MAX:
            problems.append(f"{path}: name longer than {STEM_MAX}")
        lowered = (folder, path.name.casefold())
        if lowered in folder_names:
            problems.append(f"{path}: name collides with {folder_names[lowered]}")
        folder_names[lowered] = path
        if any(unicodedata.category(c) == "Cc" and c != "\n" for c in text):
            problems.append(f"{path}: control character")
        try:
            entry = read_entry(text)
        except ValueError as exc:
            problems.append(f"{path}: {exc}")
            continue
        if extra := set(entry) - _ALLOWED_FIELDS:
            problems.append(f"{path}: forbidden fields {sorted(extra)}")
        for field, value in entry.items():
            if not _balanced(value):
                problems.append(f"{path}: unbalanced braces in {field}")
        for field in ("author", "editor"):
            if field in entry and any(p.strip() in ("", "{}") for p in f" {entry[field]} ".split(" and ")):
                problems.append(f"{path}: empty name in {field}")
        for field in ("ID", "doi", "eprint"):
            if field in entry:
                key = (folder, field, entry[field].casefold())
                if key in seen:
                    problems.append(f"{path}: {field} repeats {seen[key]}")
                seen[key] = path
    for name, texts in sorted(by_name.items()):
        if len(texts) > 1:
            problems.append(f"{name}: differs between folders")
    rows = list(csv.reader(io.StringIO(files.get(SUMMARY, ""))))[1:]
    listed = sorted(r[0].split("/", 1)[1] for r in rows if r)
    expected = sorted(p.as_posix() for p in bibs if p.parent.as_posix() != GROUP_DIR)
    if listed != expected:
        problems.append("summary.csv does not match the files")
    baseline = json.loads(files.get(BASELINE, "{}") or "{}")
    if baseline.get("authors") != dict(sorted(counts.items())) or baseline.get("total") != sum(counts.values()):
        problems.append("baseline.json does not match the files")
    return problems


def on_disk(output: Path, folders: Iterable[str]) -> frozenset[Path]:
    """The .bib files under output in the given folders, skipping a symlinked folder."""
    found: set[Path] = set()
    for folder in set(folders):
        directory = output / folder
        if directory.is_dir() and not directory.is_symlink():
            found.update(p.relative_to(output) for p in directory.glob("*.bib"))
    return frozenset(found)


def write(output: Path, files: Mapping[Path, str], stale: Iterable[Path]) -> None:
    """Write the planned files and delete the stale ones; a .bib file no state row owned is never named stale."""
    for rel, text in sorted(files.items()):
        path = output / rel
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            atomic_write(path, text)
    for rel in sorted(set(stale) - files.keys()):
        directory = output / rel.parent
        if directory.is_symlink() or not directory.is_dir():
            continue
        (output / rel).unlink(missing_ok=True)
        if not any(directory.iterdir()):
            directory.rmdir()
