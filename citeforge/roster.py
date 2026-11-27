import csv
import re
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import parse_qsl, unquote_plus, urlsplit

from citeforge.ids import norm_openalex, norm_orcid
from citeforge.model import Member

COLUMNS = ("Name", "Scholar Link", "DBLP Link", "ORCID", "OpenAlex", "Enabled", "Exclusion Reason")
_SCHOLAR_HOSTS = frozenset({"scholar.google.com", "scholar.google.ca"})
_DBLP_HOSTS = frozenset({"dblp.org", "dblp.uni-trier.de"})
_DBLP_SUFFIXES = frozenset({"bib", "html", "nt", "rdf", "ris", "rss", "xml"})
_SCHOLAR_ID = re.compile(r"[A-Za-z0-9_-]{6,64}")
_DBLP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*")
_OPENALEX_AUTHOR = re.compile(r"A\d{4,12}")
_DIRNAME_UNSAFE = re.compile(r'[/\\:*?"<>|]+')
_CONTACT = re.compile(r"\S@\S")


class RosterError(ValueError):
    pass


def normalize_name(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).split()).casefold()


def extract_last_name(full_name: str | None) -> str:
    name = (full_name or "").strip()
    if not name:
        return "Unknown"
    if "," in name and (last := name.split(",", 1)[0].strip()):
        return last
    tokens = name.split()
    return tokens[-1] if tokens else name


def format_author_dirname(author_name: str | None, author_id: str) -> str:
    last_name = _DIRNAME_UNSAFE.sub("-", extract_last_name(author_name))
    if not last_name.strip(". "):
        last_name = ""
    sanitized_id = _DIRNAME_UNSAFE.sub("-", author_id)
    if not sanitized_id:
        return last_name if last_name and last_name != "Unknown" else "unknown"
    if last_name and last_name != "Unknown":
        return f"{last_name} ({sanitized_id})"
    return sanitized_id


def _unsafe(value: str) -> bool:
    return any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)


def _https_parts(link: str, row: int, field: str) -> tuple[str, str, str]:
    if _unsafe(link):
        raise RosterError(f"row {row}: {field} contains whitespace or control characters")
    try:
        parsed = urlsplit(link)
        port = parsed.port
    except ValueError as exc:
        raise RosterError(f"row {row}: {field} is malformed") from exc
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise RosterError(f"row {row}: {field} must be an uncredentialed HTTPS URL on the default port")
    if parsed.fragment:
        raise RosterError(f"row {row}: {field} must not contain a fragment")
    return parsed.hostname or "", parsed.path, parsed.query


def scholar_id(link: str, row: int) -> str:
    if not link:
        return ""
    host, path, query = _https_parts(link, row, "Scholar Link")
    if host not in _SCHOLAR_HOSTS or path not in {"/citations", "/citations/"}:
        raise RosterError(f"row {row}: Scholar Link is not a supported Google Scholar citation profile")
    for item in query.split("&"):
        key, _, value = item.partition("=")
        if unquote_plus(key) == "user" and ("%" in key or "%" in value):
            raise RosterError(f"row {row}: Scholar Link user key and ID must not be encoded")
    try:
        items = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise RosterError(f"row {row}: Scholar Link query is malformed") from exc
    users = [v for k, v in items if k == "user"]
    if len(users) != 1 or not _SCHOLAR_ID.fullmatch(users[0]):
        raise RosterError(f"row {row}: Scholar Link requires exactly one safe user ID")
    return users[0]


def dblp_id(link: str, row: int) -> str:
    if not link:
        return ""
    if _unsafe(link):
        raise RosterError(f"row {row}: DBLP Link contains whitespace or control characters")
    is_url = "://" in link or link.startswith("//")
    identifier = link
    if is_url:
        host, path, query = _https_parts(link, row, "DBLP Link")
        if host not in _DBLP_HOSTS or query:
            raise RosterError(f"row {row}: DBLP Link is not a supported DBLP profile URL")
        if not path.startswith("/pid/"):
            raise RosterError(f"row {row}: DBLP Link must use the /pid/ path")
        identifier = path.removeprefix("/pid/")
        stem, dot, suffix = identifier.rpartition(".")
        if dot:
            if suffix not in _DBLP_SUFFIXES:
                raise RosterError(f"row {row}: DBLP Link has an unsupported profile suffix")
            identifier = stem
    else:
        identifier = identifier.removeprefix("pid:")
        _, dot, suffix = identifier.rpartition(".")
        if dot and suffix in _DBLP_SUFFIXES:
            raise RosterError(f"row {row}: bare DBLP Link person ID must not contain a profile suffix")
    if not _DBLP_ID.fullmatch(identifier):
        raise RosterError(f"row {row}: DBLP Link does not contain a safe supported person ID")
    return identifier


def _enabled(value: str, row: int) -> bool:
    match value.strip().casefold():
        case "true":
            return True
        case "false":
            return False
        case _:
            raise RosterError(f"row {row}: Enabled must be explicitly true or false")


def _id_list(value: str, row: int, field: str) -> tuple[str, ...]:
    out: list[str] = []
    for raw in filter(None, (part.strip() for part in value.split(";"))):
        if field == "ORCID":
            norm = norm_orcid(raw)
        else:
            norm = norm_openalex(raw)
            norm = norm if norm and _OPENALEX_AUTHOR.fullmatch(norm) else None
        if norm is None:
            raise RosterError(f"row {row}: {field} value {raw!r} is not valid")
        if norm in out:
            raise RosterError(f"row {row}: {field} value {norm} is listed twice")
        out.append(norm)
    return tuple(out)


def _rows(path: Path, required: Sequence[str]) -> list[tuple[int, dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header is None:
            raise RosterError(f"{path}: file is empty")
        missing = [c for c in required if c not in header]
        if missing:
            raise RosterError(f"{path}: missing required columns: {', '.join(missing)}")
        rows = []
        for row, values in enumerate(reader, start=2):
            if len(values) != len(header):
                raise RosterError(f"row {row}: expected {len(header)} columns, found {len(values)}")
            rows.append((row, dict(zip(header, values, strict=True))))
        return rows


def load(path: Path, strict: bool = True) -> tuple[Member, ...]:
    members: list[Member] = []
    seen_identity: dict[tuple[str, str, str], int] = {}
    seen_ids: dict[tuple[str, str], int] = {}
    seen_dirs: dict[str, int] = {}
    required = COLUMNS if strict else tuple(c for c in COLUMNS if c not in ("ORCID", "OpenAlex"))
    for row, raw in _rows(path, required):
        name = raw["Name"].strip()
        normalized = normalize_name(name)
        if not normalized:
            raise RosterError(f"row {row}: Name must not be empty")
        if _CONTACT.search(name) or any(unicodedata.category(c) == "Cc" for c in name):
            raise RosterError(f"row {row}: Name must not contain contact data or control characters")
        scholar = scholar_id(raw["Scholar Link"].strip(), row)
        dblp = dblp_id(raw["DBLP Link"].strip(), row)
        orcids = _id_list(raw.get("ORCID", ""), row, "ORCID")
        openalex = _id_list(raw.get("OpenAlex", ""), row, "OpenAlex")
        enabled = _enabled(raw["Enabled"], row)
        reason = raw["Exclusion Reason"].strip()
        if strict and enabled and not (orcids or dblp):
            raise RosterError(f"row {row}: enabled author needs an ORCID or a DBLP pid")
        if not enabled and not reason:
            raise RosterError(f"row {row}: disabled author requires an Exclusion Reason")
        if enabled and reason:
            raise RosterError(f"row {row}: enabled author cannot have an Exclusion Reason")
        identity = (normalized, scholar, dblp)
        if identity in seen_identity:
            raise RosterError(f"row {row}: duplicate normalized identity first seen at row {seen_identity[identity]}")
        seen_identity[identity] = row
        keyed = [("Scholar", scholar), ("DBLP", dblp)] + [("ORCID", o) for o in orcids]
        for key in keyed + [("OpenAlex", a) for a in openalex]:
            if not key[1]:
                continue
            if key in seen_ids:
                raise RosterError(f"row {row}: duplicate {key[0]} identity {key[1]} first seen at row {seen_ids[key]}")
            seen_ids[key] = row
        if not enabled:
            continue
        dirname = format_author_dirname(name, scholar or dblp)
        if dirname.casefold() in seen_dirs:
            raise RosterError(f"row {row}: folder {dirname!r} collides with row {seen_dirs[dirname.casefold()]}")
        seen_dirs[dirname.casefold()] = row
        members.append(Member(row, name, dirname, scholar or None, dblp or None, orcids, openalex))
    return tuple(members)


def load_group(path: Path, members: Sequence[Member]) -> tuple[Member, ...]:
    by_name = {normalize_name(m.name): m for m in members}
    rows = _rows(path, ("Name",))
    found = [by_name[n] for _, raw in rows if (n := normalize_name(raw["Name"])) in by_name]
    if rows and len(found) * 2 < len(rows):
        raise RosterError(f"{path}: only {len(found)} of {len(rows)} group members match an enabled author")
    return tuple(dict.fromkeys(found))
