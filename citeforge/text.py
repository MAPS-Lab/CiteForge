import html
import re
import unicodedata
from collections.abc import Iterable
from functools import lru_cache

from pylatexenc.latex2text import LatexNodes2Text
from rapidfuzz import fuzz
from unidecode import unidecode

from citeforge.model import Author, NameKey
from citeforge.tables import ACRONYM_CASE_CORRECTIONS, AUTHOR_NAME_SUFFIXES, NAME_HONORIFICS, STOPWORDS

_TAG = re.compile(r"</?[A-Za-z][\w:.-]*(?:\s[^<>]*)?/?>")
_LATEX_SPECIAL = re.compile(r"(?<!\\)([%$&#_])")
_MATH_SPECIAL = re.compile(r"(?<!\\)([%&#])")
# Pandoc's rule for $...$: opens before a non-space, closes after one, not followed by a digit; $$...$$ is free.
# A lone macro such as $\sim$ may close before a digit, as in $\sim$10.
_MATH = re.compile(
    r"\$\$(?=[^$]*(?:[^\W_]|\\))([^$]+?)\$\$|(?<!\$)\$(?=[^\s$])(?=[^$]*(?:[^\W_]|\\))([^$]*?[^\s$])\$(?![\d$])"
    r"|(?<!\$)\$(\\[A-Za-z]+)\$(?!\$)"
)
_SCRIPT = re.compile(r"(?<!\\)([\^_])\s*(?:\{\s*(\\circ|[\d+\-=()ni]+)\s*\}|(\\circ(?![A-Za-z])|\d))")
_SUPERSCRIPT = str.maketrans("0123456789+-=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ")
_SUBSCRIPT = str.maketrans("0123456789+-=()ni", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₙᵢ")
_SCRIPT_LEFT = re.compile(r"(?<!\\)[\^_]")
_ESCAPED_DOLLAR = re.compile(r"(?<!\\)((?:\\\\)*)\\\$")
_MATH_ALNUM = re.compile("[\U0001d400-\U0001d7ff]")
_INVISIBLE = dict.fromkeys(map(ord, "­​‌‍⁠﻿"))
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_PARTICLES = frozenset({"de", "da", "do", "dos", "das", "van", "von", "der", "den", "du", "la", "le", "del", "di"})
_GROUP = re.compile(
    r"\b(consortium|collaboration|group|network|initiative|committee|team|study|investigators)\b", re.IGNORECASE
)
_AFFILIATION = re.compile(
    r"\b(university|universit[éàäy]|school of|department|faculty|institute|laborator(?:y|ies)|college|hospital"
    r"|agency|office|cent(?:er|re)|inc\.?|ltd\.?)(?=\W|$)",
    re.IGNORECASE,
)
_ACRONYMS = [(re.compile(rf"\b{re.escape(k)}\b"), v) for k, v in ACRONYM_CASE_CORRECTIONS.items()]
_WORD = re.compile(r"\S+")
_EMAIL = re.compile(
    r"(?:\b(?:electronic address|e-?mail(?: address)?)\s*[:.]?\s*)?[\w.%+-]+@[\w-]+(?:\.[\w-]+)+\.?", re.I
)
_EMPTY_PART = re.compile(r"\(\s*\)|\[\s*\]|(?<=[,;])\s*[,;]")
_LEADING_WORD = re.compile(r"\s*([^\W\d_]+)\.?(?![^\W\d_])\s*")
_latex = LatexNodes2Text()


def _strip_markup(s: str) -> str:
    while True:
        unescaped = s
        while (step := html.unescape(unescaped)) != unescaped:
            unescaped = step
        stripped = _TAG.sub("", unescaped)
        if stripped == s:
            return s
        s = stripped


def _script(m: re.Match[str]) -> str:
    body = m[2] or m[3]
    if body == "\\circ":
        return "°" if m[1] == "^" else ""
    return body.translate(_SUPERSCRIPT if m[1] == "^" else _SUBSCRIPT)


def _math_text(math: str) -> str:
    # plain text has no script operator, so a ^ or _ with no Unicode form is dropped
    return _MATH_SPECIAL.sub(r"\\\1", _SCRIPT_LEFT.sub("", _SCRIPT.sub(_script, math)))


def _latex_source(s: str) -> str:
    s = _ESCAPED_DOLLAR.sub(r"\1$", s)
    parts, last = [], 0
    for m in _MATH.finditer(s):
        parts += [_LATEX_SPECIAL.sub(r"\\\1", s[last : m.start()]), "$", _math_text(m[1] or m[2] or m[3]), "$"]
        last = m.end()
    return "".join(parts) + _LATEX_SPECIAL.sub(r"\\\1", s[last:])


def clean(s: str) -> str:
    out = _clean_once(s)
    # Dollars a pass leaves as text can pair into math once braces between them are gone.
    while (again := _clean_once(out)) != out:
        out = again
    return out


def _clean_once(s: str) -> str:
    s = _strip_markup(s)
    if "\\" in s or "{" in s or "}" in s or "$" in s:
        s = _latex.latex_to_text(_latex_source(s))
    s = _MATH_ALNUM.sub(lambda m: unicodedata.normalize("NFKC", m.group(0)), s)
    s = unicodedata.normalize("NFC", s).translate(_INVISIBLE)
    s = "".join(" " if unicodedata.category(c) == "Cc" else c for c in s)
    s = s.replace("{", "").replace("}", "").replace("\\", "")
    return " ".join(s.split())


def affiliation(s: str) -> str:
    """Clean affiliation text and drop the e-mail addresses registries embed in it."""
    return clean(_EMPTY_PART.sub("", _EMAIL.sub(" ", clean(s)))).strip(" ,;:")


def fold(s: str) -> str:
    return " ".join(_NON_ALNUM.sub(" ", unidecode(s).casefold()).split())


@lru_cache(maxsize=1 << 16)
def title_key(s: str) -> str:
    return fold(clean(s))


def ratio(a: str, b: str) -> float:
    return fuzz.ratio(title_key(a), title_key(b)) / 100


def token_set(a: str, b: str) -> float:
    return fuzz.token_set_ratio(title_key(a), title_key(b)) / 100


def word_prefix(a: str, b: str, min_words: int = 3) -> bool:
    wa, wb = title_key(a).split(), title_key(b).split()
    short, long = sorted((wa, wb), key=len)
    return len(short) >= min_words and long[: len(short)] == short


def family_keys(family: str) -> frozenset[str]:
    tokens = fold(family).split()
    core = [t for t in tokens if t not in _PARTICLES and t not in AUTHOR_NAME_SUFFIXES] or tokens
    keys = {"".join(tokens), "".join(core)} | {t for t in core if len(t) >= 3}
    keys.discard("")
    return frozenset(keys)


def _title_word(given: str) -> re.Match[str] | None:
    m = _LEADING_WORD.match(given)
    # an upper-case "DR" or "MS" is a pair of initials, not a title
    return m if m and given[m.end() :] and fold(m[1]) in NAME_HONORIFICS and not m[1].isupper() else None


def initials(given: str) -> str:
    while m := _title_word(given):
        given = given[m.end() :]
    return "".join(t[0] for t in fold(given).split() if t not in AUTHOR_NAME_SUFFIXES)


def name_key(given: str, family: str) -> NameKey:
    return NameKey(family_keys(family), initials(given))


def split_literal(name: str) -> tuple[str, str]:
    if "," in name:
        family, _, given = name.partition(",")
        return given.strip(), family.strip()
    parts = name.split()
    while len(parts) > 1 and fold(parts[-1]) in AUTHOR_NAME_SUFFIXES:
        parts.pop()
    return " ".join(parts[:-1]), (parts[-1] if parts else "")


def author_key(a: Author) -> NameKey | None:
    if a.family:
        return name_key(a.given, a.family)
    if a.literal and not is_group(a.literal) and not is_affiliation(a.literal):
        return name_key(*split_literal(a.literal))
    return None


def name_matches(names: Iterable[NameKey], a: Author, swapped: bool = False) -> bool:
    """Match a byline slot to the member's names; swapped also reads the given name as the family name."""
    keys = [author_key(a)]
    if swapped and a.given and a.family:
        keys.append(name_key(a.family, a.given))
    return any(
        n.families & k.families and _initials_compatible(n.initials, k.initials)
        for k in keys
        if k is not None
        for n in names
    )


def _initials_compatible(a: str, b: str) -> bool:
    return not a or not b or a.startswith(b) or b.startswith(a)


def is_group(name: str) -> bool:
    return bool(_GROUP.search(name))


def is_affiliation(name: str) -> bool:
    return bool(_AFFILIATION.search(name)) and not is_group(name)


def recase_title(s: str) -> str:
    letters = [c for c in s if c.isalpha()]
    if len(letters) < 4 or sum(c.isupper() for c in letters) < 0.8 * len(letters):
        return s

    def word(m: re.Match[str]) -> str:
        w = m.group(0)
        lower = w.lower()
        if m.start() > 0 and lower in STOPWORDS:
            return lower
        return "-".join(p[:1].upper() + p[1:] for p in lower.split("-"))

    out = _WORD.sub(word, s)
    for pattern, replacement in _ACRONYMS:
        out = pattern.sub(replacement, out)
    return out


def slug_words(title: str) -> list[str]:
    return [w[0].upper() + w[1:] for w in title_key(title).split() if w not in STOPWORDS]


def ascii_family(family: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", unidecode(family))
    return "".join(p[0].upper() + p[1:] for p in parts if p)
