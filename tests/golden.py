import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from citeforge.bibtex import read_entry
from citeforge.model import Event
from citeforge.state import StateRow

CLASSES = frozenset(
    {"namesake", "not_namesake", "wrong_doi", "dead_doi", "dup_pair", "preprint_published", "notice", "withdrawn",
     "front_matter", "group_author", "reorder", "repeat_authors", "org_author", "diacritic", "year", "orcid_missing",
     "type", "venue", "text", "split_profile", "openalex_namesake"}
)  # fmt: skip
EXACT = frozenset(
    {"namesake", "openalex_namesake", "not_namesake", "wrong_doi", "dead_doi", "notice", "withdrawn", "front_matter",
     "group_author", "repeat_authors", "org_author", "diacritic"}
)  # fmt: skip
NEAR = {"dup_pair": 0.95, "preprint_published": 0.95, "reorder": 0.95}
OVERALL = 0.98
_VERDICTS = frozenset({"accept", "reject", "review"})


@dataclass(frozen=True, slots=True)
class Label:
    case: str
    cls: str
    member: str
    id: str
    expect: str
    rule: str | None
    origin: str


def load_labels(path: Path) -> list[Label]:
    labels = []
    seen: set[str] = set()
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        raw = json.loads(line)
        extra = set(raw) - {"case", "class", "member", "id", "expect", "rule", "origin"}
        if extra or raw["class"] not in CLASSES or raw["case"] in seen:
            raise ValueError(f"{path}:{n}: bad label {raw.get('case')!r}")
        seen.add(raw["case"])
        labels.append(Label(raw["case"], raw["class"], raw["member"], raw["id"], raw["expect"], raw.get("rule"),
                            raw["origin"]))  # fmt: skip
    return labels


def _row(state: Mapping[str, StateRow], work_id: str) -> StateRow | None:
    if work_id in state:
        return state[work_id]
    return next((r for _, r in sorted(state.items()) if work_id in r.work.aliases), None)


def matches(label: Label, state: Mapping[str, StateRow], files: Mapping[Path, str], events: Iterable[Event]) -> bool:
    row = _row(state, label.id)
    kind, _, arg = label.expect.partition(":")
    if label.expect in _VERDICTS:
        d = row.members.get(label.member) if row else None
        if label.expect == "reject" and d is None:
            return label.rule is None
        return d is not None and d.verdict.value == label.expect and (label.rule is None or d.rule == label.rule)
    if kind == "exclude":
        return row is not None and row.work.excluded in (f"kind:{arg}", f"status:{arg}", f"curated:{arg}")
    if kind == "linked":
        target = _row(state, arg)
        return target is not None and (
            row is target or label.id in target.work.linked or (row is not None and row.work.key in target.work.linked)
        )
    if label.expect in ("doi_detached", "doi_dead"):
        wanted = "doi_mismatch" if label.expect == "doi_detached" else "doi_unresolved"
        return any(e.kind == wanted and e.id == label.id and e.member in (label.member, None) for e in events)
    if row is None or not row.names.stem:
        return False
    text = files.get(Path(label.member) / f"{row.names.stem}.bib")
    if text is None:
        return False
    if kind == "field":
        name, _, value = arg.partition("=")
        return read_entry(text).get(name) == value
    if kind == "bytes_equal":
        return files.get(Path(arg) / f"{row.names.stem}.bib") == text
    raise ValueError(f"{label.case}: unknown expectation {label.expect!r}")


def score(labels: Sequence[Label], state: Mapping[str, StateRow], files: Mapping[Path, str],
          events: Sequence[Event]) -> dict[str, tuple[int, int]]:  # fmt: skip
    out: dict[str, tuple[int, int]] = {}
    for label in labels:
        hit, total = out.get(label.cls, (0, 0))
        out[label.cls] = (hit + matches(label, state, files, events), total + 1)
    return out


def failures(scores: Mapping[str, tuple[int, int]]) -> list[str]:
    problems = []
    hits = sum(h for h, _ in scores.values())
    total = sum(t for _, t in scores.values())
    if total and hits / total < OVERALL:
        problems.append(f"overall {hits}/{total} below {OVERALL:.0%}")
    for cls, (hit, n) in sorted(scores.items()):
        need = 1.0 if cls in EXACT else NEAR.get(cls, 0.0)
        if n and hit / n < need:
            problems.append(f"{cls} {hit}/{n} below {need:.0%}")
    return problems
