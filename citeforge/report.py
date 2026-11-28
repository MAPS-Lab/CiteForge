import json
import os
import platform
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from citeforge.model import Claim, Decision, Event, Verdict, Work
from citeforge.publish import publishable
from citeforge.state import StateRow, atomic_write

WEBSITE_DIR = "Spadon (bfdGsGUAAAAJ)"
MARKDOWN_MAX = 60_000
SHOW = 5
VOLATILE = ("timing",)


@dataclass(frozen=True, slots=True)
class RunData:
    min_year: int
    stages: Mapping[str, float]
    sources: Mapping[str, Mapping[str, int | float]]
    down: Mapping[str, str]
    statuses: Mapping[str, str]
    listings: Mapping[str, Mapping[str, int]]
    fetch: Mapping[str, Mapping[str, str]]
    decisions: Mapping[str, Mapping[str, Decision]]
    changes: Mapping[str, Mapping[str, list]]
    works: Mapping[str, Work]
    state: Mapping[str, StateRow]
    held: Sequence[tuple[str, Claim]]
    events: Sequence[Event]
    orcid_claims: Mapping[str, Sequence[Claim]]
    registry_failed: frozenset[str]
    exit_code: int


def _toml_line(section: str, member: str, work_id: str) -> str:
    return f'[[{section}]]\nmember = {json.dumps(member, ensure_ascii=False)}\nid = "{work_id}"'


def _coverage(data: RunData) -> dict[str, Any]:
    owner = {a: k for k, w in sorted(data.works.items()) for a in w.aliases}
    held = {(m, i) for m, c in data.held for i in c.ids}
    dropped = {(e.member, e.id): e.kind for e in data.events if e.kind in ("doi_mismatch", "doi_unresolved")}
    items = []
    claimed = emitted = 0
    for member, claims in sorted(data.orcid_claims.items()):
        for c in sorted(claims, key=lambda c: sorted(c.ids)):
            claimed += 1
            key = next((owner[i] for i in sorted(c.ids) if i in owner), None)
            if key is not None and publishable(data.decisions.get(member, {}).get(key), data.works.get(key)):
                emitted += 1
                continue
            d = data.decisions.get(member, {}).get(key or "")
            work = data.works.get(key or "")
            if not c.ids:
                reason = "no_identifier"
            elif work is not None and work.excluded:
                reason = work.excluded
            elif d is not None:
                reason = f"{d.verdict.value}:{d.rule}"
            elif any((member, i) in held for i in c.ids):
                reason = "doi_review"
            elif kinds := sorted({dropped[(member, i)] for i in c.ids if (member, i) in dropped}):
                reason = kinds[0]
            else:
                reason = "unmatched"
            items.append({"member": member, "id": key or min(c.ids, default=""), "title": c.title, "reason": reason})
    unexplained = sum(1 for i in items if i["reason"] == "unmatched")
    return {"claimed": claimed, "emitted": emitted, "unexplained": unexplained, "items": items}


def build(data: RunData) -> dict[str, Any]:
    members: dict[str, Any] = {}
    review = []
    for member, status in sorted(data.statuses.items()):
        mine = data.decisions.get(member, {})
        accepted = Counter(d.rule for k, d in mine.items() if publishable(d, data.works.get(k)))
        changes = data.changes.get(member, {})
        members[member] = {
            "status": status,
            "listings": dict(sorted(data.listings.get(member, {}).items())),
            "fetch": dict(sorted(data.fetch.get(member, {}).items())),
            "accepted_by_rule": dict(sorted(accepted.items())),
            "added": [list(x) for x in changes.get("added", [])],
            "removed": [list(x) for x in changes.get("removed", [])],
            "renamed": [list(x) for x in changes.get("renamed", [])],
        }
        for key, d in sorted(mine.items()):
            if d.verdict is Verdict.REVIEW and data.works[key].excluded is None:
                review.append(
                    {
                        "member": member,
                        "id": key,
                        "title": data.works[key].primary.title,
                        "rule": d.rule,
                        "evidence": sorted(d.evidence),
                        "accept": _toml_line("accept", member, key),
                        "reject": _toml_line("reject", member, key),
                    }
                )
    by_kind = Counter(e.kind for e in data.events)

    def events_of(kind: str) -> list[dict[str, str | None]]:
        return [{"member": e.member, "id": e.id, "detail": e.detail} for e in data.events if e.kind == kind]

    return {
        "run": {
            "commit": os.environ.get("GITHUB_SHA"),
            "python": platform.python_version(),
            "min_year": data.min_year,
            "exit_code": data.exit_code,
            "registry_failed": sorted(data.registry_failed),
        },
        "timing": dict(data.stages),
        "sources": {h: {**dict(v), "down": data.down.get(h)} for h, v in sorted(data.sources.items())},
        "members": members,
        "review": review,
        "excluded": dict(sorted(Counter(r.work.excluded for r in data.state.values() if r.work.excluded).items())),
        "links": {
            "doi_detached": events_of("doi_mismatch"),
            "doi_dead": events_of("doi_unresolved"),
            "fuzzy": events_of("fuzzy_link"),
            "proposals": events_of("link_proposal"),
        },
        "coverage": _coverage(data),
        "events": {"counts": dict(sorted(by_kind.items())), "items": [list(_event(e)) for e in data.events]},
    }


def _event(e: Event) -> tuple[str, str | None, str | None, str]:
    return e.kind, e.member, e.id, e.detail


def _list(title: str, items: Sequence[str], limit: int | None = SHOW) -> list[str]:
    if not items:
        return []
    shown = items if limit is None else items[:limit]
    lines = [f"### {title} ({len(items)})", "", *(f"- {i}" for i in shown)]
    if len(items) > len(shown):
        lines.append(f"- and {len(items) - len(shown)} more")
    return [*lines, ""]


def markdown(report: Mapping[str, Any]) -> str:
    members = report["members"]
    total_added = sum(len(m["added"]) for m in members.values())
    total_removed = sum(len(m["removed"]) for m in members.values())
    lines = [
        "## CiteForge refresh",
        "",
        f"Exit code {report['run']['exit_code']}, window from {report['run']['min_year']}, "
        f"{total_added} files added and {total_removed} removed across {len(members)} members.",
        "",
    ]
    down = [f"{h}: {v['down']}" for h, v in report["sources"].items() if v.get("down")]
    lines += _list("Hosts down", down, None)
    lines += _list("Registry lookups failed", report["run"]["registry_failed"])
    partial = [f"{m}: {v['status']}" for m, v in members.items() if v["status"] != "complete"]
    lines += _list("Members not complete", partial, None)
    site = members.get(WEBSITE_DIR)
    if site:
        changes = [f"added {k} as {stem}" for k, stem in site["added"]]
        changes += [f"removed {k} ({reason})" for k, reason in site["removed"]]
        changes += [f"renamed {k} from {a} to {b}" for k, a, b in site["renamed"]]
        lines += _list(f"Changes in {WEBSITE_DIR}", changes, None)
    for name in ("added", "removed", "renamed"):
        items = [f"{m}: {' '.join(map(str, x))}" for m, v in members.items() if m != WEBSITE_DIR for x in v[name]]
        lines += _list(name.capitalize(), items)
    lines += _list("Review", [f"{r['member']}: {r['id']} ({r['rule']}) {r['title']}" for r in report["review"]])
    lines += _list("Excluded", [f"{k}: {v}" for k, v in report["excluded"].items()])
    for name, items in report["links"].items():
        lines += _list(name.replace("_", " ").capitalize(), [f"{i['id']} {i['detail']}" for i in items])
    cov = report["coverage"]
    lines += [f"Coverage: {cov['emitted']} of {cov['claimed']} ORCID-claimed works emitted, "
              f"{cov['unexplained']} unexplained.", ""]  # fmt: skip
    lines += _list("Not emitted", [f"{i['member']}: {i['id'] or i['title']} ({i['reason']})" for i in cov["items"]])
    text = "\n".join(lines)
    if len(text) > MARKDOWN_MAX:
        text = text[: MARKDOWN_MAX - 40].rsplit("\n", 1)[0] + "\n\nThe report was cut at 60000 characters.\n"
    return text


def write(report: Mapping[str, Any], run_dir: Path, state_dir: Path) -> None:
    stable = {k: v for k, v in report.items() if k not in VOLATILE}
    atomic_write(state_dir / "report.json", json.dumps(stable, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    atomic_write(run_dir / "report.json", json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    atomic_write(run_dir / "report.md", markdown(report))
