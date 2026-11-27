import csv
import io
import json
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from citeforge.bibtex import FIELD_ORDER, STEM_MAX, read_entry, render
from citeforge.model import Member, Source, Verdict
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


@dataclass(frozen=True, slots=True)
class Changes:
    added: tuple[str, ...]
    changed: tuple[str, ...]
    removed: tuple[str, ...]


def emitted(row: StateRow, member: str) -> bool:
    d = row.members.get(member)
    return d is not None and d.verdict is Verdict.ACCEPT and row.work.excluded is None and bool(row.names.stem)


def plan(
    state: State, members: Sequence[Member], group: Sequence[Member], output_name: str = "output"
) -> dict[Path, str]:
    files: dict[Path, str] = {}
    summary: list[list[str]] = []
    counts: Counter[str] = Counter()
    group_names = {m.dirname for m in group}
    for key in sorted(state):
        row = state[key]
        text = None
        in_group = False
        for m in members:
            if not emitted(row, m.dirname):
                continue
            text = text or render(row.work, row.names)
            path = Path(m.dirname) / f"{row.names.stem}.bib"
            files[path] = text
            counts[m.dirname] += 1
            flags = [int(s in row.work.sources) for s in FLAGS]
            summary.append([f"{output_name}/{path.as_posix()}", str(sum(flags)), *map(str, flags)])
            in_group |= m.dirname in group_names
        if in_group and text is not None:
            files[Path(GROUP_DIR) / f"{row.names.stem}.bib"] = text
            counts[GROUP_DIR] += 1
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
    i = 0
    while i < len(value):
        c = value[i]
        if c == "\\":
            i += 2
            continue
        depth += {"{": 1, "}": -1}.get(c, 0)
        if depth < 0:
            return False
        i += 1
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
        for field in ("doi", "eprint"):
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


def write(output: Path, files: Mapping[Path, str], folders: Iterable[str]) -> Changes:
    added, changed, removed = [], [], []
    for rel, text in sorted(files.items()):
        path = output / rel
        if path.exists():
            if path.read_text(encoding="utf-8") == text:
                continue
            changed.append(rel.as_posix())
        else:
            added.append(rel.as_posix())
        atomic_write(path, text)
    for folder in sorted(set(folders)):
        directory = output / folder
        if not directory.is_dir() or directory.is_symlink():
            continue
        for path in sorted(directory.glob("*.bib")):
            rel = path.relative_to(output)
            if rel not in files:
                path.unlink()
                removed.append(rel.as_posix())
        if not any(directory.iterdir()):
            directory.rmdir()
    return Changes(tuple(added), tuple(changed), tuple(removed))
