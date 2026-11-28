import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from citeforge.bibtex import read_entry
from citeforge.text import fold

# B14: these classes must match every label, these at least 95%, and all labels together at least 98%
EXACT = frozenset({"namesake", "not_namesake", "wrong_doi"})
NEAR = {"dup_pair": 0.95, "preprint_published": 0.95}
OVERALL = 0.98
EXPECTS = re.compile(r"accept|reject|doi_detached|linked:\S+")
# a title no emitted file may carry: front matter, an editorial or a notice (B9 title patterns)
NOT_A_PAPER = re.compile(
    r"^(guest )?editorial\b|^preface\b|^foreword\b|^front matter\b|^message from\b|^welcome\b|^table of contents\b"
    r"|^conclusions?( and future works?)?$|^dear |^(corrections? to|correction:|erratum|corrigendum|retraction"
    r"|expression of concern)\b|^proceedings of\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Label:
    case: str
    cls: str
    member: str
    id: str
    expect: str
    origin: str


def load_labels(path: Path) -> list[Label]:
    labels, seen = [], set()
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        raw = json.loads(line)
        if set(raw) != {"case", "class", "member", "id", "expect", "origin"} or raw["case"] in seen:
            raise ValueError(f"{path}:{n}: bad label {raw.get('case')!r}")
        if not EXPECTS.fullmatch(raw["expect"]) or not (raw["class"] in EXACT or raw["class"] in NEAR):
            raise ValueError(f"{path}:{n}: unknown class or expectation in {raw['case']!r}")
        seen.add(raw["case"])
        labels.append(Label(raw["case"], raw["class"], raw["member"], raw["id"], raw["expect"], raw["origin"]))
    return labels


def _entries(files: Mapping[Path, str], member: str) -> list[dict[str, str]]:
    return [read_entry(text) for path, text in sorted(files.items()) if path.parent == Path(member)]


def _carrying(entries: Sequence[dict[str, str]], work_id: str) -> list[dict[str, str]]:
    kind, _, value = work_id.partition(":")
    field = "eprint" if kind == "arxiv" else "doi"
    return [e for e in entries if e.get(field, "").casefold() == value.casefold()]


def matches(label: Label, files: Mapping[Path, str]) -> bool:
    entries = _entries(files, label.member)
    emitted = _carrying(entries, label.id)
    match label.expect.partition(":"):
        case ("accept", _, _):
            return bool(emitted)
        case ("reject", _, _):
            return not emitted
        case ("doi_detached", _, _):
            family = fold(label.member.split(" (")[0])
            return all(family in fold(e.get("author", "")) for e in emitted)
        case (_, _, other):
            return not (emitted and _carrying(entries, other) and emitted != _carrying(entries, other))


def failures(labels: Sequence[Label], files: Mapping[Path, str]) -> list[str]:
    results = [(label, matches(label, files)) for label in labels]
    hits = sum(ok for _, ok in results)
    problems = [] if hits >= OVERALL * len(results) else [f"overall {hits}/{len(results)} below {OVERALL:.0%}"]
    for cls in sorted({label.cls for label in labels}):
        mine = [(label, ok) for label, ok in results if label.cls == cls]
        need = 1.0 if cls in EXACT else NEAR[cls]
        if sum(ok for _, ok in mine) < need * len(mine):
            missed = ", ".join(label.case for label, ok in mine if not ok)
            problems.append(f"{cls} {sum(ok for _, ok in mine)}/{len(mine)} below {need:.0%}: {missed}")
    return problems + [
        f"{p} is not a paper" for p, t in sorted(files.items()) if p.suffix == ".bib" and NOT_A_PAPER.search(_title(t))
    ]


def _title(text: str) -> str:
    return re.sub(r"[{}]", "", read_entry(text).get("title", ""))
