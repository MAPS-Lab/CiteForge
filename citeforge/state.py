import builtins
import json
import os
import tempfile
from collections import abc
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date
from enum import Enum
from functools import cache
from pathlib import Path
from types import NoneType, UnionType
from typing import Any, get_args, get_origin, get_type_hints

from citeforge.model import Decision, Names, Work

STATE_FILE = "works.jsonl"


@dataclass(frozen=True, slots=True)
class Searched:
    """A title search that linked nothing: its query, the review it raised if any, and the day it ran."""

    query: tuple[str, str, int | None]
    review: str | None
    on: date


@dataclass(frozen=True, slots=True)
class StateRow:
    work: Work
    names: Names
    members: Mapping[str, Decision] = field(default_factory=dict)
    missing: Mapping[str, int] = field(default_factory=dict)
    searches: Mapping[str, Searched] = field(default_factory=dict)


type State = Mapping[str, StateRow]


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, frozenset):
        return sorted((_plain(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, tuple | list):
        return [_plain(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    return value


@cache
def _hints(tp: type) -> dict[str, Any]:
    return get_type_hints(tp)


def _read(tp: Any, value: Any) -> Any:
    """Rebuild a value _plain wrote, led by its annotation: a dataclass, enum, date, collection or optional."""
    args = get_args(tp)
    if isinstance(tp, UnionType):
        return None if value is None else _read(next(a for a in args if a is not NoneType), value)
    if is_dataclass(tp):
        hints = _hints(tp)
        return tp(**{f.name: _read(hints[f.name], value[f.name]) for f in fields(tp) if f.name in value})
    if isinstance(tp, type) and issubclass(tp, Enum | date):
        return tp(value) if issubclass(tp, Enum) else date.fromisoformat(value)
    match get_origin(tp):
        case builtins.frozenset:
            return frozenset(_read(args[0], v) for v in value)
        case builtins.tuple if args[-1:] == (Ellipsis,):
            return tuple(_read(args[0], v) for v in value)
        case builtins.tuple:
            return tuple(_read(a, v) for a, v in zip(args, value, strict=True))
        case abc.Mapping | builtins.dict:
            return {k: _read(args[1], v) for k, v in value.items()}
    return value


def encode(row: StateRow) -> str:
    return json.dumps(_plain(row), sort_keys=True, ensure_ascii=False)


def decode(line: str) -> StateRow:
    return _read(StateRow, json.loads(line))


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
    finally:
        Path(tmp).unlink(missing_ok=True)


def save(directory: Path, state: State) -> None:
    lines = [encode(state[key]) for key in sorted(state)]
    atomic_write(directory / STATE_FILE, "".join(line + "\n" for line in lines))
