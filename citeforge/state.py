import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from enum import Enum
from pathlib import Path
from typing import Any

from citeforge.model import Author, Decision, Kind, Names, Record, Rel, Source, Status, Verdict, Work

STATE_FILE = "works.jsonl"


@dataclass(frozen=True, slots=True)
class StateRow:
    work: Work
    names: Names
    members: Mapping[str, Decision] = field(default_factory=dict)
    missing: Mapping[str, int] = field(default_factory=dict)


type State = Mapping[str, StateRow]


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, frozenset):
        return sorted((_plain(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, tuple | list):
        return [_plain(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    return value


def _author(d: Mapping[str, Any]) -> Author:
    values: dict[str, Any] = {**d, "rors": tuple(d["rors"]), "affiliations": tuple(d["affiliations"])}
    return Author(**values)


def _record(d: Mapping[str, Any]) -> Record:
    values: dict[str, Any] = {
        **d,
        "source": Source(d["source"]),
        "ids": frozenset(d["ids"]),
        "kind": Kind(d["kind"]),
        "authors": tuple(map(_author, d["authors"])),
        "editors": tuple(map(_author, d["editors"])),
        "dates": tuple(d["dates"]),
        "container": tuple(d["container"]),
        "relations": frozenset((Rel(r), t) for r, t in d["relations"]),
        "status": Status(d["status"]),
    }
    return Record(**values)


def _work(d: Mapping[str, Any]) -> Work:
    return Work(
        key=d["key"],
        primary=_record(d["primary"]),
        eprint=_record(d["eprint"]) if d["eprint"] else None,
        linked=frozenset(d["linked"]),
        kind=Kind(d["kind"]),
        status=Status(d["status"]),
        year=d["year"],
        excluded=d["excluded"],
        provenance=tuple((f, Source(s)) for f, s in d["provenance"]),
        aliases=frozenset(d["aliases"]),
        sources=frozenset(map(Source, d["sources"])),
    )


def encode(row: StateRow) -> str:
    return json.dumps(_plain(row), sort_keys=True, ensure_ascii=False)


def decode(line: str) -> StateRow:
    d = json.loads(line)
    return StateRow(
        work=_work(d["work"]),
        names=Names(**d["names"]),
        members={
            m: Decision(Verdict(x["verdict"]), x["rule"], frozenset(x["evidence"])) for m, x in d["members"].items()
        },
        missing=dict(d["missing"]),
    )


def load(directory: Path) -> dict[str, StateRow]:
    path = directory / STATE_FILE
    if not path.exists():
        return {}
    rows: dict[str, StateRow] = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = decode(line)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path}:{n}: unreadable state row: {exc}") from exc
        if row.work.key in rows:
            raise ValueError(f"{path}:{n}: work {row.work.key} repeats")
        rows[row.work.key] = row
    return rows


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save(directory: Path, state: State) -> None:
    lines = [encode(state[key]) for key in sorted(state)]
    atomic_write(directory / STATE_FILE, "".join(line + "\n" for line in lines))
