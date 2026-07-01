# BibEntry domain-model design (supersedes fixup Steps 4/6/C5)

## Problem
Bibliographic entries are untyped dicts `{type,key,fields}` canonicalized (type
reclassification + title/venue normalization) at THREE open-coded sites: A =
`_fixup_bib_entry` (orphan/terminal sweep, main.py:154), B = existing-file
pre-enrichment (main.py:713-1181), C = Phase-4 post-merge (main.py:1895-2298).
This triplication is the "fixup patch" smell; the fix is to make canonicalization
intrinsic to a `BibEntry` type so a non-canonical entry is not produced.

## Crux finding (reader A)
One idempotent `canonicalize()` reproduces A/B/C EXCEPT for one irreducible input:
a single boolean `terminal` ("enrichment exhausted"). It gates exactly 3 rules
that fire on a field's ABSENCE:
- R17 article & no journal -> misc
- R18 inproceedings & no booktitle -> misc
- R19 article & preprint-DOI & no vol/pages -> misc (downgrade branch)
Pre-merge these must be LEFT ALONE (enrichment may still fill the field);
post-merge they downgrade. Every other rule (R1-R16, R20 misc->inproceedings,
R21, all text N*) is DATA-driven and folds into a fixpoint. The "C-only"
misc->inproceedings (R20) is data-driven: its precondition (a conference
howpublished) is manufactured by earlier rules in the same pass, not externally.

Ordering: the union needs ONE canonical order because (a) "zenodo" is in BOTH
REPOSITORY_AS_JOURNAL and PREPRINT_SERVERS so R16 (keep value as howpublished)
and R12 (drop journal) do NOT commute -> canonical order R16-before-R12 (B/C
already do this); (b) the title chain N18/N19/N1/N2/N9/N10 is order-sensitive.

Today A/B/C are NOT mutually byte-identical: A is a strict subset (lacks
N18/N19/R14/R15/R16); B has a DESTRUCTIVE branch (N22 title==venue -> delete
file, main.py:1106) and pipeline-only rewrites (email-from-author). So:
- canonicalize() is PURE (entry -> entry); side effects (file delete, I/O) stay
  in the pipeline, not the model. This is part of the coherence win.
- The model adopts the UNION of the pure normalization rules + the terminal flag.

## Blast radius (reader B)
Pervasive shared-mutable-dict: ~84 field reads + dozens of in-place type/fields
mutations in main.py, ~40 in merge_utils, `_fixup_bib_entry` 40+ in-place writes
returning a change-flag. A big-bang immutable value object breaks the
alias-and-mutate contract at hundreds of sites. => Adopt BibEntry INCREMENTALLY.

## Parse/serialize boundary (reader C)
parse_bibtex_to_dict is near-lossless (lowercases type + field keys, strips,
unwraps braces, preserves insertion order). bibtex_from_dict is lossy/normalizing
(PREFERRED_FIELD_ORDER then sorted extras; _normalize_to_ascii; _sanitize_title;
&-escape excl url/doi; 2-space indent; trailing-comma strip; single trailing \n).
Invariant to preserve: serialize(parse(serialize(x))) == serialize(x).
`to_bibtex()` must OWN the full serialization contract (move PREFERRED_FIELD_ORDER
+ the 3 nested helpers onto/behind it).

## Design
`src/entry.py` (new) — `class BibEntry`:
- data: `entry_type: str` (lowercased), `key: str` (case preserved),
  `fields: dict[str,str]` (ordered, lowercased keys). Dedup ids
  (x_scholar_cluster_id/...) modeled as transient, non-serialized.
- boundaries (single construct/serialize points):
  - `from_bibtex(text) -> BibEntry | None`  (wraps parse_bibtex_to_dict)
  - `from_raw(type,key,fields,*,arxiv=...) -> BibEntry` (wraps build_bibtex_entry
    assembly + get_container_field placement)
  - `to_bibtex() -> str` (owns the serialization contract)
- ONE normalization: `canonicalize(*, terminal: bool) -> bool` (in-place, returns
  changed) OR returns a new BibEntry. Applies the union of type + text rules in
  the single canonical order; `terminal` gates R17/R18/R19-misc.
- rules live as an ordered registry of small pure predicate+action functions with
  metadata (needs_terminal), in `src/entry_rules.py` (rehomes src/fixup/*). This
  is the SINGLE SOURCE OF TRUTH; the 3 sites disappear.

Call-site collapse:
- Site B (pre-merge): `entry.canonicalize(terminal=False)`
- Site C (post-merge): `entry.canonicalize(terminal=True)`
- Site A (orphan/terminal sweep): `entry.canonicalize(terminal=True)`
- B's destructive N22 (title==venue file delete) + email-from-author stay in the
  pipeline as explicit steps around canonicalize (they are I/O, not normalization).

## Byte-identity strategy
Differential test is the gate: for a large synthetic entry matrix AND the golden
output/ corpus, assert `canonicalize(terminal)` reproduces the CURRENT per-site
output (run the pre-refactor A/B/C blocks vs the new model). Plus the two-run
byte-identity check (run pipeline twice, git diff --exit-code output/). Resolve
the zenodo R16<R12 order and the title-chain order to match B/C. Because A is a
subset today, verify canonicalize(terminal=True) is a NO-OP on already-C-normalized
files (it should be); investigate any diff before landing.

## Incremental migration (each step gated: ruff+mypy+pytest+differential+two-run)
1. Introduce BibEntry with from_bibtex/to_bibtex ONLY (thin wrappers over existing
   parse/serialize); no call-site changes yet. Golden serializer + round-trip tests.
2. Move the serialization contract onto to_bibtex (PREFERRED_FIELD_ORDER + helpers);
   bibtex_from_dict delegates. Byte-identical.
3. Build entry_rules registry (union, canonical order, terminal metadata) from the
   existing A/B/C rules; add the differential OLD-vs-NEW matrix test. No call-site
   change yet (registry validated in isolation).
4. Route Site C -> canonicalize(terminal=True). Gate incl. differential + two-run.
5. Route Site B -> canonicalize(terminal=False) (keep its pipeline-only I/O steps
   separate). Gate.
6. Route Site A (orphan sweep) -> canonicalize(terminal=True). Gate.
7. Delete the 3 open-coded blocks + src/fixup/ (absorbed). Gate.
8. (Later, optional) migrate field-access sites to typed accessors; not required
   for the coherence win.

## Why this satisfies the critique
- Canonicalization is intrinsic to the type (produced canonical via from_*/
  canonicalize), not "applied" as a patch in 3 places.
- The 3 sites collapse to one method + one honest boolean; the differences that
  remain are explicit and justified (terminal), not hidden duplication.
- Pure normalization is separated from pipeline I/O.
- Single construct + single serialize boundary.
- Incremental + differential-gated => byte-identity preserved and provable.

---

# REVISION (Codex-vetted) — supersedes the sections above where they conflict

The original design was NOT byte-safe. Corrections (each verified against source):

## 1. Five stages, not three (ordered enum, not a bool)
Complete entries (~96% cache-hit) run Site B, then quick-fixups (main.py:1185-1212),
then `return 1` and NEVER reach Site C. Plus tier2 (main.py:2300) and the N22 delete
(main.py:2367); merge_with_policy also normalizes author/pages/volume. So:
`CanonicalStage` enum, ordered: LOAD_REPAIR, COMPLETE_SKIP_FINALIZE, POST_MERGE,
POST_TIER2_VALIDATE, POSTRUN_ORPHAN_REPAIR. Each rule carries the set of stages it
runs at. `canonicalize(entry, stage)`.

## 2. Preserve each stage's EXACT current rule set first; NO union routing
Do NOT route B or A to the union. Complete entries persist B's output straight to
the on-disk baseline, so any C-only rule added to B (url_booktitle->misc :2026,
misc_workshop->inproceedings :2271) changes committed bytes immediately. Site A's
narrow subset is load-bearing (cleanup for tier2-bypassed venues + undone Phase-4
corrections). Step 1 makes each site call canonicalize(stage=X) reproducing its
CURRENT rules byte-for-byte; unify differences later ONLY rule-by-rule under a
per-corpus differential diff gate.

## 3. Declarative side-effects
canonicalize is pure but returns CanonicalResult(entry, actions) where actions are
declarative (DeleteFile, SkipEntry). The pipeline executes them at the correct
stage (N22 delete depends on canonicalized title/venue; runs after tier2 in C but
before the complete-skip in B). A global "canonicalize then side-effects" pass
cannot express that ordering; declarative actions can.

## 4. Determinism fixes (fold into the registry build)
- Replace `next(x for x in FROZENSET ... startswith)` first-match (main.py:184 etc.)
  with ordered tuples matched by (priority, longest-prefix). frozenset-first-match
  is PYTHONHASHSEED-dependent.
- zenodo in BOTH REPOSITORY_AS_JOURNAL and PREPRINT_SERVERS => R16-before-R12 is a
  SEMANTIC canonical order.
- Two-run gate: compare serialized `.bib` bytes under PYTHONHASHSEED=1,2,3, gated
  PER STAGE on the actual control flow (complete-path vs incomplete-path). Do NOT
  use blanket `git diff output/` — summary.csv row order is concurrently
  nondeterministic (orthogonal issue) and would mask real .bib regressions.

## 5. Migration byte-safety
- Steps 1-2 (introduce BibEntry; bibtex_from_dict delegates to to_bibtex) MUST keep
  to_bibtex emitting the transient x_* dedup fields exactly as today. Defer
  transient/non-serialized modeling to a later separately-gated step (the strip
  lives only at merge_utils.py:412; B writes serialize without merge).
- Non-byte-neutral steps (gate hard, corpus diff): B->union, A->union,
  trailing-canonicalize-after-tier2, moving x_* strip into the serializer.
- Assert the completeness invariant that makes B safe (main.py:432): complete =>
  has_venue AND non-preprint-DOI => the absence-trio (R17/R18/R19-misc) cannot fire.
- Dead code: the complete-skip article-preprint-doi->misc at main.py:1199-1212 is
  unreachable (_entry_is_complete requires non-preprint DOI; block requires
  is_secondary_doi). Mark dead in the registry; do not port as live.

## Honest scope note
The "3 sites disappear" was oversold. Reality: 5 stages become EXPLICIT,
single-sourced stage members of one registry (a large improvement over 3
copy-pasted blocks), with side-effects declarative and one pure canonicalize core.
Unifying the per-stage rule differences into a true single rule set is a SEPARATE,
rule-by-rule, corpus-gated effort that may reveal some differences are load-bearing.
