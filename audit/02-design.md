I have everything I need. Both load-bearing disputes are now verified against source:

- **C5 superset membership**: `article_patent->misc` lives at **both** Site B (main.py:1053-1057, `EXISTING_FIXUP`) **and** Site C (main.py:2134-2143, `TYPE_CORRECT`); `article_unpublished->misc` likewise at Site B (1041-1045) **and** Site C (2146-2149). The catalog's `ao-phase4-superset` claim that these are "C-only" is **factually wrong** — they are `{B,C}`. Only `misc_workshop->inproceedings` (2382-2397) is genuinely C-exclusive. This confirms the judges' correction and disqualifies Proposal 1's two-bucket model.
- **C7 dead-code**: `scholarly_scholar.py` has **zero** importers; `tests/test_scholarly_scholar.py` imports the **live** clients (`src.clients.scholar`, `serpapi_scholar`, `serply_scholar`), so the test must be **kept**. Proposal 3's plan to delete it is wrong.

Producing the final design document.

---

# CiteForge Refactor — Phase 2 Final Decomposition Design

*Deliverable of the DESIGN SYNTHESIZER. Input contract: `audit/01-invariant-catalog.md`. Output contract: this document is the authoritative plan the IMPLEMENT phase consumes, one worktree-isolated task per migration step.*

---

## 1. Winning decomposition and why

**Winner: Proposal 2 — "Minimal-risk, move-only, byte-identity-preserved at every commit; C5 single-sourced via per-site ordered recipes, deferred to a gated final step."** It is the unanimous choice of all three judge lenses (invariant-hawk 8.44, pragmatic-shipper 76/100, architecture-purist 8.61 — the top score in every scorecard). The reason is that the campaign's prime directive is second-run byte-identity, and the one seam that can silently destroy it — C5, the three-way-fix — has a domain shape that only Proposal 2 models correctly: I verified against source that the three fix sites run the CORE reclassification chain in **three different orders** and with **non-nested, per-rule membership** (Site B already contains `patent->misc` and `unpublished->misc`, which Proposal 1 wrongly fences as C-only and would therefore delete from B). Proposal 2's answer — single-source each rule **body** once, but give each site an explicit **ordered recipe** listing its own rules in that site's literal current order — is byte-identical **by construction** and does not depend on the rules commuting, whereas Proposal 1 and Proposal 3 both impose one shared order on all three sites and stake byte-identity on an (un-proven) commutation assumption. Every step before C5 is a whole-symbol relocation behind a re-export shim, so each commit is independently green under ruff+mypy+pytest and byte-identical under a two-run diff, and the single behavior-touching change lands last behind four gates. Its only real weakness is cohesion (it leaves `merge_utils.py` monolithic); we close that gap by grafting Proposal 1's cohesive-engine split as an explicitly-sequenced **Wave 2** that runs only after byte-identity is locked.

---

## 2. Grafts from the runner-up proposals (with attribution)

The final design is Proposal 2's move-only spine and per-site-recipe C5 model, hardened with the following explicit grafts:

| # | Grafted idea | Source | Why it is adopted |
|---|--------------|--------|-------------------|
| G1 | **`src/venue.py` shared low layer** for `_is_conference_journal`, `_matches_journal_named_proceedings`, `infer_howpublished_from_doi`, `_normalize_howpublished` (Dagstuhl/LIPIcs). | Proposal 3 (determinism-safety lens) — *"the sharpest coupling insight in any proposal."* | `fixup` rules call `_is_conference_journal`, which lives in `merge_utils` today. Without extracting it to a shared low module, `fixup` would import `merge` and create a cycle. This is the single most valuable graft and must land **before** any fixup rule references it. |
| G2 | **`src/fsscan.py` as the only sorted-`.bib`-scan API** (`iter_author_bibs`, `iter_output_dirs`) plus a grep-lint that fails CI on any raw `os.listdir` over a `.bib` dir. | Proposal 3 | Turns `determinism-sorted-bib-scan` (surface #6) from a convention reviewers must remember into a structural CI failure. |
| G3 | **`patterns.py` container-type assertion test** (every fix table is `list`/`tuple`/`dict`, never `set`/`frozenset`) + **`ao-ascii-clean-table-values` fixpoint test** (every table value equals its own `_normalize_to_ascii(value)`) + **golden `PREFERRED_FIELD_ORDER` serializer test**. | Proposal 3 | Makes surfaces #1 and #3 structural rather than tested-by-luck. The ascii-fixpoint test co-lands with the pattern move so a non-fixpoint value cannot cause an every-run byte diff. |
| G4 | **Per-rule `sites: frozenset[Site]` membership tag** on each `FixupRule`, plus an **AST/grep test** asserting the `misc->inproceedings` upgrade symbol is referenced only in the Phase-4 recipe. | Proposal 3 (membership model) composed with Proposal 2 (per-site ordered recipes). | Membership becomes testable and self-documenting; combined with P2's ordered recipes this yields byte-safety **and** a structural anti-flatten guarantee with **no reorder bet**. |
| G5 | **Explicit boundary doc**: `all_candidate_dois` is a dedup-only `set`; its **order comes solely from the published-first partition sort**. | Proposal 3 | Kills the "no sets anywhere" over-correction while proving surface #7. |
| G6 | **`PipelineContext` dataclass** threading `enr_list`, `all_candidate_dois`, `flags`, `doi_validated`, `unvalidated_doi` through the split phase modules. | Proposal 1 (cohesion-first lens) | The clean structural guarantee for `determinism-enr-list-order` once `process_article` is genuinely split (Wave 2). Used only when the phase split happens. |
| G7 | **Full cohesive engine layout** (`merge/policy`, `dedup/{scan,decide,score,candidate_doi}`, `save/write`) as a sequenced **Wave 2** after byte-identity is locked, plus the **golden Phase-4 fixture matrix** (preprint article / PACM / patent / thesis DOI / bare stub) as the C5 parity oracle. | Proposal 1 | Closes Proposal 2's cohesion gap without risking the byte-identity floor during the risky C5 change. |

**Two catalog corrections adopted (verified in source, mandatory for IMPLEMENT):**
- **CC1** — `ao-three-way-fixup-parity` and Determinism-Surface #2 cite `_fixup_bib_entry` at `src/merge_utils.py:314-565`. It is actually at **`main.py:314-565`** (tests import it from `main`). Treat `main.py` as authoritative.
- **CC2** — `ao-phase4-superset` lists `patent->misc` and `unpublished->misc` as "C-only." **Source disproves this:** both are at Site B (main.py:1041-1057) **and** Site C (main.py:2134-2149). The genuinely C-exclusive rules are `inproceedings_url_booktitle->misc` (2153-2162), `inproceedings_no_booktitle->misc` (2038-2046), `article-preprint-DOI` handling (2238-2259), and the **sole** `misc_workshop->inproceedings` upgrade (2382-2401). C5 membership is a **per-rule frozenset**, not a nested subset.

---

## 3. Definitive target package layout (END STATE)

Dependency direction is strictly downward and acyclic: `pipeline → {fixup, merge, dedup, save, bibtex} → {venue, fsscan, config, http_utils, cache, io_utils, exceptions, log_utils, clients}`. No back-edges. `main.py` ends as a thin entrypoint; re-export shims at old paths keep every `from main import …` / `from src.merge_utils import …` test green until the final cleanup step.

```
src/
  config.py              UNCHANGED — single home of all thresholds/tables (cd-thresholds-centralized)
  exceptions.py          UNCHANGED — frozen error tuples (eh-error-tuple-membership-frozen)
  models.py              UNCHANGED
  log_utils.py           UNCHANGED
  bibtex_build.py        UNCHANGED — determine_entry_type, get_container_field (co-container-enforcement-by-type)
  publication_parser.py / api_generics.py / doi_utils.py / id_utils.py   UNCHANGED
  text_utils.py          title_similarity/normalize_title stay pure; compute_dedup_score → dedup/score.py (Wave 2)
  http_utils.py          KEPT — C1/C2 land in place (retry loop, _decode_json_bytes redaction, semaphore)
  cache.py               KEPT — C3/C4 land in place (ResponseCache monthly-boundary + ttl_days decision)
  io_utils.py            KEPT — CSV index/flush + a2i2/orphan/reconcile helpers (consumed by pipeline/reconcile.py)
  bibtex_utils.py        KEPT — serializer; PREFERRED_FIELD_ORDER promoted to named tuple + golden test

  venue.py         NEW (Wave 1, G1) — shared LOW layer imported by BOTH fixup and merge
  fsscan.py        NEW (Wave 1, G2) — the ONLY sorted-.bib-scan API + grep-lint

  fixup/           NEW (Wave 1) — SINGLE SOURCE OF TRUTH for the three-way fix (seam C5)
    patterns.py
    text.py
    rules.py
    engine.py

  pipeline/        NEW (Wave 1: article/record/schedule/reconcile ; Wave 2: context + phase split)
    article.py
    record.py
    schedule.py
    reconcile.py
    context.py           (Wave 2, G6)
    baseline.py / phase2_enrich.py / phase3_discovery.py / phase4_save.py   (Wave 2, G7)

  merge/           NEW (Wave 2, G7)
    policy.py
  dedup/           NEW (Wave 2, G7)
    scan.py / decide.py / score.py / candidate_doi.py
  save/            NEW (Wave 2, G7)
    write.py

  clients/               UNCHANGED except: C6 OpenReview lock (search_apis.py); scholarly_scholar.py DELETED (Wave 1)
main.py                  thin entrypoint (cli/setup/dispatch) + transitional re-export shims
```

### Per-module responsibility, moved-from, invariants protected

| Module | Responsibility | Moved from (file:line) | Invariants protected |
|--------|----------------|------------------------|----------------------|
| **`src/venue.py`** (NEW) | Venue-classification predicates shared by fixup and merge; extracting them breaks the latent `fixup→merge` cycle. | `merge_utils.py:108-165` (`_is_conference_journal`, `_matches_journal_named_proceedings`, `infer_howpublished_from_doi`, `_normalize_howpublished`, Dagstuhl/LIPIcs regex :52-55,767-803) | `co-conference-journal-word-boundary`, `ao-is-pacm-guard`, `co-pnas-suffix-conference-guard`, `co-dagstuhl-doi-resolution` |
| **`src/fsscan.py`** (NEW) | Single sorted-scan API: `iter_author_bibs(dir)->sorted[str]`, `iter_output_dirs(out)->sorted[str]`. Every `.bib`-dir listing routes through it. | Replaces raw `os.listdir` at `main.py:817,2545,2858,3119,3123,3164,3168,3199,3202`; `merge_utils.py:953,1462`; `io_utils.py:383,391,510,590` | `determinism-sorted-bib-scan` (#6), `determinism-orphan-abspath-resolution`, `determinism-a2i2-write-order-collision` |
| **`src/fixup/patterns.py`** (NEW) | Every pre-compiled fix regex and repeated literal, as `list`/`tuple`/`dict` only. | `main.py:119-232` (`_FUSED_DICT_PATTERNS`, `_COMPOUND_SUFFIX_PATTERNS`, `_ACRONYM_CASE_PATTERNS`, `_BOOKTITLE_FIXUPS`, `_VERBOSE_BOOKTITLE_RE`, `_US_PATENT_RE`, garbage/venue regexes); vocab imported from `config.py:361-808` | `determinism-pattern-iteration-order` (#3), `ao-fused-compounds-three-pass-order`, `of-booktitle-fixups-ordered-idempotent`, `ao-ascii-clean-table-values`, `cd-thresholds-centralized` |
| **`src/fixup/text.py`** (NEW) | The already-single-source title/booktitle text transforms (3 call sites each today) + garbage/corruption predicates. | `main.py:235-311` (`_apply_booktitle_fixups`, `_fix_title_text`, `_is_garbage_title`, `_is_corrupted_title`, `_fix_fused_compounds`) | `ao-three-way-fixup-parity` (text/booktitle half — surface #2), `ao-fused-compounds-three-pass-order` |
| **`src/fixup/rules.py`** (NEW) | The `Site` enum, `FixupRule` dataclass, one function per correction step (**body single-sourced**), and each rule's `sites: frozenset[Site]` membership. | Rule bodies distilled from Site A `main.py:314-565`, Site B `main.py:865-1309`, Site C `main.py:2028-2401` | `ao-three-way-fixup-parity`, `ao-phase4-superset`, `ao-is-proc-series-guard-frontiers`, `ao-is-pacm-guard`, `ao-disjoint-reclassification-tables`, `co-misc-upgrade-preprint-repo-guard` |
| **`src/fixup/engine.py`** (NEW) | `SITE_A_RECIPE`/`SITE_B_RECIPE`/`SITE_C_RECIPE` (ordered tuples reproducing each site's **literal current order**) and `run_fixup(entry, recipe)->bool`. | The orchestration currently inline at each of A/B/C | `of-phase4-type-correction-order`, `ao-postrun-fixup-write-suppression`, `pipeline-double-run-fixpoint` |
| **`src/pipeline/article.py`** | The 5-phase per-article orchestrator; Sites B & C become `run_fixup(...)` calls; returns the 1/0 contract. | `main.py:721-2687` incl. helpers `_read_doi_from_file`(624-633), `_revert_misattributed_doi`(634-655), `_try_multiple_candidates`(657-720), `_entry_is_complete`(568-623) | `determinism-phase2-source-order` (#10, C9), `determinism-enr-list-order`, `determinism-doi-candidate-order` (#7), `ao-candidate-doi-disk-dedup`, `co-return-code-contract`, `co-year-window-enforced-everywhere` (save leg) |
| **`src/pipeline/record.py`** | Per-author worker: thread-local logging, Scholar-retry-then-DBLP, article loop, per-unit isolation. | `main.py:2688-2851` (`process_record`) | `co-thread-local-logging`, `eh-scholar-retry-then-dblp-only`, `eh-per-unit-isolation`, `co-single-writer-per-author-dir` (precondition doc — C8) |
| **`src/pipeline/schedule.py`** | Author scheduling: composite sort key, new-author-first stable re-order, CSV title index, ThreadPool fan-out, excepthook, result timeouts. | `main.py:2852-2884` (`count_existing_papers`, `_load_csv_titles`), sort blocks `2947-2949`/`2971-2977`, pool `2998-3018` | `determinism-author-sort-key` (#5), `determinism-new-author-first-stable`, `co-single-writer-per-author-dir` (C8), `co-threadpool-worker-cap`, `co-result-timeouts` |
| **`src/pipeline/reconcile.py`** | The whole post-run block as ONE fixed-order function, gated on CSV existence. | `main.py:3070-3227`; calls `io_utils.py:334-439,466-615` helpers | `co-gate-block-on-csv-existence`, `co-reconciliation-step-ordering` (#12), `determinism-reconcile-rewrite-only-when-phantoms`, `co-year-window-enforced-everywhere` (#13), `of-a2i2-byte-fidelity-copy`, `co-year-and-fixup-skip-a2i2-dir`, `co-summary-csv-cwd-relative-paths` |
| **`src/pipeline/context.py`** (Wave 2, G6) | `PipelineContext` dataclass carrying the enrichment accumulator across split phases. | `enr_list` init `main.py:1486`; `all_candidate_dois` `1490`; flags/`doi_validated`/`unvalidated_doi` locals in `process_article` | `determinism-enr-list-order`, `dd-all-candidate-dois-includes-unmatched`, `ao-p1-stash-and-pop` |
| **`src/merge/policy.py`** (Wave 2) | The trust engine: insertion-order iteration, strict-`<` replacement, DOI-source gate, field-override rules. | `merge_utils.py:230-925` (`merge_with_policy` + author/DOI helpers 166-229) | `to-canonical-order-strict-rank`, `determinism-enr-list-order` (#9), `to-doi-source-gate`, `to-field-override-rules`, `ao-doi-published-beats-preprint-xor`, `of-internal-fields-stripped` |
| **`src/dedup/scan.py`** / **`decide.py`** / **`score.py`** / **`candidate_doi.py`** (Wave 2) | The in-save duplicate cascade (first-match break), the directional replace/keep tree + pre-write guard + prefer-path cleanup, the additive 6-signal scorer, and the outer save-time candidate-DOI net. | `merge_utils.py:978-1200` (scan), `1201-1305`+`1386-1455` (decide/guard), `text_utils.py:511-546` (score), `main.py:634-655,657-720,2531-2588` (candidate_doi) | `dd-branch-order-first-match`, `ao-replace-keep-directional`, `ao-skip-write-existing-better`, `dd-composite-weights`, `ao-candidate-doi-disk-dedup`, `dd-self-match-exclusion`, `dd-title-similarity-guard`, `cd-inline-magic-numbers-preserved` |
| **`src/save/write.py`** (Wave 2) | `save_entry_to_file` orchestration: container enforcement, citekey fallback, word-extension collision loop, cross-file key disambiguation, unlocked per-dir RMW; returns `(path, was_written)`. | `merge_utils.py:927-978,1307-1382,1457-1504` | `co-single-writer-per-author-dir` (C8), `of-filename-no-numeric-counters`, `of-citekey-fallback-chain`, `co-cross-file-key-collision-disambiguation`, `co-save-return-tuple` |
| **`src/bibtex_utils.py`** (kept) | BibTeX serialization; `PREFERRED_FIELD_ORDER` promoted to a named module-level tuple. | in place `bibtex_utils.py:302-394` | `of-bibtex-field-order-stable` (#1), `of-ascii-escape-normalization`, `of-final-comma-brace-formatting`, `ao-ascii-clean-table-values` |
| **`src/http_utils.py`** (kept) | Retry/backoff/concurrency + JSON decode; C1 + C2 land here. | in place `http_utils.py:163-175,300-372,388-399` | `ao-429-503-excluded-from-urllib3-forcelist`, `co-sleeps-outside-global-semaphore`, `eh-error-tuple-membership-frozen`, `eh-gemini-key-leak` |
| **`src/cache.py`** (kept) | Monthly-boundary freshness + three-tier negatives; C3 + C4 land here. | in place `cache.py:24-32,99-145,147-223` | `ca-positive-freshness-not-ttl`, `ca-monthly-boundary-expiry`, `ca-month-boundary-frozen`, `ca-get-branch-order` |

---

## 4. Single-source-of-truth fixup design (seam C5)

### Problem shape (verified in source, not the catalog)

The CORE reclassification chain is textually triplicated at Site A (`main.py:314-565`), Site B (`main.py:865-1309`), Site C (`main.py:2028-2401`), and:

1. The three sites run the CORE rules in **three different orders** (A ends with conference-journal reclassification; B and C run it first).
2. Membership is **per-rule, not nested**: `article_patent->misc` and `article_unpublished->misc` are in `{B, C}` (main.py:1041-1057 / 2134-2149); `misc_workshop->inproceedings` is in `{C}` only (2382-2397); `inproceedings_repository->misc` / `inproceedings_preprint->misc` are in `{A, B, C}` (360/374, 1139/1150, 2216/2164).
3. Byte-identity today is preserved by each site's own order plus rule commutation (`ao-disjoint-reclassification-tables`) — **not** by a shared order.
4. `_fix_title_text` and `_apply_booktitle_fixups` are **already** single-source (exactly 3 call sites each): `main.py:432/1205/2279` and `440/1213/2290`. Only the reclassification chain is triplicated.

Therefore: single-source each rule **body**, but preserve each site's **order and membership** exactly. Do **not** collapse to one shared master order (that would stake byte-identity on a commutation bet neither Proposal 3 nor Proposal 1 can prove).

### Concrete API (`src/fixup/rules.py` + `src/fixup/engine.py`)

```python
# src/fixup/rules.py
from dataclasses import dataclass
from enum import Enum, auto
from collections.abc import Callable

class Site(Enum):
    A_LOAD    = auto()   # load-time _fixup_bib_entry (also the post-run sweep)
    B_EXISTING = auto()  # existing-file baseline fixup
    C_PHASE4  = auto()   # Phase-4 post-merge fixup

@dataclass(frozen=True)
class FixupRule:
    name: str
    apply: Callable[[dict], bool]   # mutates entry in place; returns True iff it changed anything
    sites: frozenset[Site]          # MEMBERSHIP (for tests/self-doc); does NOT govern order

# --- one function per correction step; each body defined EXACTLY ONCE ---
# CORE (sites = {A, B, C}); guards read tables from config / predicates from venue.py:
def _rule_procedia_to_inproceedings(e: dict) -> bool: ...
def _rule_pacm_to_article(e: dict) -> bool: ...            # not-is_pacm guard (ao-is-pacm-guard)
def _rule_jnp_to_article(e: dict) -> bool: ...             # PNAS/PVLDB suffix guard (co-pnas-suffix-conference-guard)
def _rule_journal_only_prefix_to_article(e: dict) -> bool: ...  # not-is_proc_series guard (ao-is-proc-series-guard-frontiers)
def _rule_inst_repo_to_phdthesis(e: dict) -> bool: ...
def _rule_conference_journal_to_inproceedings(e: dict) -> bool: ...  # uses venue._is_conference_journal
def _rule_inproceedings_repository_to_misc(e: dict) -> bool: ...
def _rule_inproceedings_preprint_to_misc(e: dict) -> bool: ...
# ... remaining CORE rules ...

# {B, C} — CORRECTED membership (catalog CC2):
def _rule_article_patent_to_misc(e: dict) -> bool: ...
def _rule_article_unpublished_to_misc(e: dict) -> bool: ...

# {C} only — the genuine Phase-4 superset:
def _rule_inproceedings_no_booktitle_to_misc(e: dict) -> bool: ...
def _rule_inproceedings_url_booktitle_to_misc(e: dict) -> bool: ...
def _rule_article_preprint_doi_handle(e: dict) -> bool: ...
def _rule_misc_to_inproceedings_upgrade(e: dict) -> bool: ...  # SOLE upgrade; preprint/repo guard (co-misc-upgrade-preprint-repo-guard)
```

```python
# src/fixup/engine.py
from .rules import FixupRule, Site, _rule_procedia_to_inproceedings, ...  # explicit imports

# Each recipe reproduces THAT SITE'S literal current order and membership.
SITE_A_RECIPE: tuple[FixupRule, ...] = (  # A order: ends with conference-journal
    R_procedia, R_pacm_to_article, R_jnp_to_article, ..., R_inst_repo_to_phdthesis,
    R_inproceedings_repository_to_misc, R_inproceedings_preprint_to_misc, ...,
    R_conference_journal_to_inproceedings,   # LAST at A
)
SITE_B_RECIPE: tuple[FixupRule, ...] = (  # B order: conference-journal first; INCLUDES patent/unpublished + B text extras
    R_conference_journal_to_inproceedings, ..., R_article_patent_to_misc, R_article_unpublished_to_misc, ...,
)
SITE_C_RECIPE: tuple[FixupRule, ...] = (  # C order (Phase-4): superset, upgrade LAST
    R_inproceedings_no_booktitle_to_misc, R_conference_journal_to_inproceedings, ...,
    R_article_patent_to_misc, R_article_unpublished_to_misc, R_inproceedings_url_booktitle_to_misc,
    R_article_preprint_doi_handle, ..., R_misc_to_inproceedings_upgrade,   # SOLE C-only upgrade, LAST
)

def run_fixup(entry: dict, recipe: tuple[FixupRule, ...]) -> bool:
    changed = False
    for rule in recipe:
        changed |= rule.apply(entry)
    return changed
```

Call-site collapse:
- Site A body → `run_fixup(e, SITE_A_RECIPE)`; the post-run sweep (`main.py:3176`) also uses `SITE_A_RECIPE`.
- Site B body → `run_fixup(baseline, SITE_B_RECIPE)` + the existing write-if-changed wrapper.
- Site C body → `run_fixup(merged, SITE_C_RECIPE)`.

### Why this satisfies C5

- **Parity is structural, not copy-paste**: a CORE rule is one callable object referenced by all three recipes — it cannot drift, and its guards (`not is_pacm`, `not is_proc_series`, PNAS suffix, word-boundary) exist once.
- **Superset preserved, never flattened**: `_rule_misc_to_inproceedings_upgrade` is physically absent from `SITE_A_RECIPE`/`SITE_B_RECIPE`; `sites == {C_PHASE4}`. The `{B,C}` rules appear in both B and C recipes — the CC2 correction is honored.
- **Byte-identity does not depend on commutation**: each recipe is the site's literal order, so output equals today's bytes even if two rules would fire in opposite directions under a different order. Proposal 2's stated fallback ("if the differential test finds a non-commuting pair, keep each site's order") is already the design — no redesign needed.

### Guard tests (all must pass to land C5)
1. **Membership**: `assert Site.A_LOAD not in R_misc_to_inproceedings_upgrade.sites and Site.B_EXISTING not in it`; `assert R_article_patent_to_misc.sites == {Site.B_EXISTING, Site.C_PHASE4}`; every rule in `SITE_X_RECIPE` has `Site.X in rule.sites`.
2. **AST/grep (G4)**: `_rule_misc_to_inproceedings_upgrade` symbol is referenced only in `engine.py`'s `SITE_C_RECIPE`.
3. **Differential OLD-vs-NEW**: run the pre-refactor inline A/B/C blocks vs the new recipes over a large synthetic entry matrix; assert per-site equality. This is the strongest single C5 gate — it surfaces any non-commuting pair.
4. **Golden Phase-4 fixture matrix (G7)**: preprint article / PACM / patent / thesis DOI / bare stub → exact emitted `@type` and field placement (`of-phase4-type-correction-order`).
5. **Idempotence**: `run_fixup(deepcopy(e), R); run_fixup(e, R)` yields zero further change (`pipeline-double-run-fixpoint`).
6. **Two-run byte diff** over the frozen corpus, under two `PYTHONHASHSEED` values.

> Note on logging: per-rule debug tags differ across sites (`EXISTING_FIXUP|…` vs `TYPE_CORRECT|…`). No test asserts these and `run.log` is never byte-compared (only `output/*.bib` is the contract). Rules may drop per-rule logging or accept an optional `tag` argument threaded by `run_fixup`; either is byte-neutral. State the chosen option in the C5 commit message.

---

## 5. Ordered, individually-green migration sequence

Each step is **one worktree-isolated IMPLEMENT task**. The gate after **every** step is identical and non-negotiable:

> **GATE** = `ruff check src/ tests/ main.py` **and** `mypy src/ main.py` **and** full `pytest` **and** the **two-run byte-identity check**: run the pipeline twice over the frozen fixture corpus from **project-root CWD**, then `git diff --exit-code output/` must be empty. For steps touching surfaces #3/#7 or C5, repeat under a second `PYTHONHASHSEED`. For year-window steps, add freeze-time near the year boundary.

Re-export shims at old paths (`import X as X` / `__all__` to satisfy ruff F401) keep all existing imports resolving until Step 15.

**Wave 0 — oracle**

- **Step 0 — Baseline oracle.** Capture a frozen cache-hit fixture corpus; record the canonical two-run `git diff --exit-code output/` as empty and `ruff`/`mypy`/`pytest` green. This is the regression oracle every later step diffs against. *Proof: the snapshot itself.*

**Wave 1 — move-only spine + structural guards + C5 (byte-identical by construction, C5 last)**

- **Step 1 — C7 dead-code.** Delete `src/clients/scholarly_scholar.py`. **Keep** `tests/test_scholarly_scholar.py` (verified: it imports the live `scholar`/`serpapi_scholar`/`serply_scholar`, not the dead module). Re-run the zero-import grep at delete time. *Proof: grep shows zero importers; full pytest green.* → GATE.
- **Step 2 — `src/venue.py` (G1).** Move `merge_utils.py:108-165` + Dagstuhl/LIPIcs (52-55,767-803) verbatim; re-export from `merge_utils`. *Proof: existing venue/merge tests + `co-conference-journal-word-boundary`, `co-dagstuhl-doi-resolution` tests.* → GATE.
- **Step 3 — `src/fsscan.py` (G2).** Add `iter_author_bibs`/`iter_output_dirs`; route each raw `os.listdir` site through it **one at a time**; add the grep-lint (CI fails on raw `os.listdir` over a `.bib` dir). *Proof: `determinism-sorted-bib-scan` test after each site; `a.bib`/`z.bib` both-matching → `a.bib` chosen.* → GATE.
- **Step 4 — `fixup/patterns.py` + `fixup/text.py` (G3).** Move `main.py:119-311` verbatim; re-export `_fix_title_text`, `_apply_booktitle_fixups`, `_is_garbage_title`, `_is_corrupted_title` from `main`. Add the **container-type assertion test** and the **`ao-ascii-clean-table-values` fixpoint test**. *Proof: pattern-type test + fixpoint test + `from main import _is_garbage_title` resolves.* → GATE.
- **Step 5 — Serializer lock-in (G3).** Promote `PREFERRED_FIELD_ORDER` to a named module tuple in `bibtex_utils.py`; add the **golden scrambled-key serializer string test**. No relocation. *Proof: golden string test; `of-bibtex-field-order-stable`.* → GATE.
- **Step 6 — Extract Site A.** Move `_fixup_bib_entry` (`main.py:314-565`) into `fixup/` as the seed of `rules.py`+`engine.py`, wired as `SITE_A_RECIPE` with per-rule `sites` tags (CORE = `{A,B,C}`). Re-export `_fixup_bib_entry` from `main` (post-run sweep at 3176 + tests still import it). Site A drives the post-run sweep, giving highest test coverage at lowest risk. *Proof: idempotence + `ao-postrun-fixup-write-suppression` tests.* → GATE.
- **Step 7 — `pipeline/schedule.py`.** Move `count_existing_papers`, `_load_csv_titles`, and extract `schedule_authors()` from the sort blocks (`2947-2949`/`2971-2977`). *Proof: `determinism-author-sort-key` + new-author-first golden order.* → GATE.
- **Step 8 — `pipeline/article.py`.** Whole-function relocation of `process_article` (`main.py:721-2687`) + its helpers (568-720). Sites B & C ride along **inline, unchanged** (they become recipes in Step 12). Largest move; **zero logic edits**. *Proof: `SEARCH_START` Phase-2-order test (C9), `co-return-code-contract`, two-run diff.* → GATE.
- **Step 9 — `pipeline/record.py`.** Move `process_record` (`main.py:2688-2851`). *Proof: `co-thread-local-logging`, `eh-scholar-retry-then-dblp-only`.* → GATE.
- **Step 10 — `pipeline/reconcile.py`.** Extract the inline post-run block (`main.py:3070-3227`) into `run_post_run_reconciliation(...)`; `main()` calls it from project-root CWD. *Proof: reconciliation integration test (phantom + orphan + out-of-window seed) run from project root; `co-reconciliation-step-ordering`, `co-summary-csv-cwd-relative-paths`.* → GATE.
- **Step 11 — Structural invariant lock.** Add tests: rule-membership map, "no `set`/`frozenset` feeds an ordered/output loop," and the `all_candidate_dois` dedup-only boundary doc + assertion (G5). *Proof: the tests themselves; landmine grep-lint green.* → GATE.
- **Step 12 — C5 (LAST, highest risk, four gates).** Extract the CORE + `{B,C}` + `{C}` rule bodies into `fixup/rules.py`; build `SITE_B_RECIPE` and `SITE_C_RECIPE` in each site's **literal current order** with corrected membership; rewrite Site B (`865-1309`) and Site C (`2028-2401`) as `run_fixup(...)` calls. **Gate = the four C5 guard tests (§4) + differential OLD-vs-NEW matrix + golden Phase-4 fixture + two-run diff under two `PYTHONHASHSEED`.** *This is the only behavior-touching step in Wave 1.* → GATE.

**Wave 2 — cohesive engine split (G7) + defect fixes (byte-neutral; each step reversible)**

- **Step 13 — Split `merge_utils.py`.** In sub-steps, each behind GATE: (a) `merge/policy.py` ← `230-925`; (b) `dedup/score.py` ← `text_utils.py:511-546`; (c) `dedup/scan.py` ← `978-1200`; (d) `dedup/decide.py` ← `1201-1305,1386-1455`; (e) `dedup/candidate_doi.py` ← `main.py:634-720,2531-2588`; (f) `save/write.py` ← `927-978,1307-1382,1457-1504`. Re-export from `merge_utils` shim. *Proof: `dd-*`, `to-*`, `ao-*`, `of-filename-*` table-driven tests after each sub-extraction.* → GATE.
- **Step 14 — `pipeline/context.py` + phase split (G6).** Introduce `PipelineContext`; extract `baseline.py`/`phase2_enrich.py`/`phase3_discovery.py`/`phase4_save.py` from `article.py` one at a time, each replacing a slice of `process_article` with a ctx-threaded call, two-run-green after **each** phase. `enr_list` created once, only appended-to. *Proof: `determinism-enr-list-order` shuffle-equal-rank test; two-run after each phase.* → GATE.
- **Step 15 — Defect fixes + shim removal.** Land C1/C2/C3/C4/C6 in their cohesive homes (§6), each with its seam-guard test and two-run diff; then remove the transitional re-export shims. **Final** `ruff` + `mypy` + full `pytest` + two-run byte-identity. → GATE.

---

## 6. Defect-fix placement (each mapped to a step, with seam guard)

| Defect | Lands in step | Home module:line | Seam guard (MUST hold) |
|--------|---------------|------------------|------------------------|
| **C7 — dead code** | Step 1 | delete `src/clients/scholarly_scholar.py` | Re-grep zero importers at delete time; **keep** `tests/test_scholarly_scholar.py` (it exercises the live clients). |
| **C5 — three-way-fix parity vs superset** | Step 12 | `src/fixup/` (rules.py + engine.py) | CORE bodies single-sourced; per-site ordered recipes preserve order+membership; `misc->inproceedings` upgrade `sites=={C}`; `{B,C}` for patent/unpublished (CC2). Differential + golden + membership + two-run gates. |
| **C2 — ValueError/URL key-leak** | Step 15 | `src/http_utils.py:399` (`_decode_json_bytes`) | Add `_redact_url()` to strip `key=`/tokens **before** the URL enters the `ValueError` message. Do **NOT** add `ValueError` to `NETWORK_ERRORS`/`ALL_API_ERRORS` (`eh-error-tuple-membership-frozen`). Test: `'key='` absent from the WARN record; `ValueError` still raised/propagated. |
| **C1 — retry double-backoff** | Step 15 | `src/http_utils.py:163-175,300-372` (`_http_request`, `_RETRY_STRATEGY`) | Any consolidation keeps 429/503 **out** of `status_forcelist`, `respect_retry_after_header=False`, the `min(rate_wait, HTTP_BACKOFF_MAX)` cap, and every `time.sleep` **outside** `_GLOBAL_SEMAPHORE`. Stopping POST retries is a conscious behavior change (`eh-defect-post-retried-non-idempotent`) — no-op unless explicitly chosen. |
| **C4 — frozen `_month_boundary`** | Step 15 (optional) | `src/cache.py:123` | Optionally recompute `_month_boundary()` per `get()`; update `test_month_boundary_frozen`. Low impact (short batch). Do **not** hard-freeze across restarts. |
| **C3 — vestigial `ttl_days`** | Step 15 (decision) | `src/cache.py` | Leave documented-vestigial **or** remove the write path (`put`/`_write_entry`). **Never** read it in `get()` (`ca-positive-freshness-not-ttl`). |
| **C6 — OpenReview lock-free read** | Step 15 | `src/clients/search_apis.py:~433-490` | Add lock-guarded read at the client only; do **not** change OpenReview's Phase-2 ordinal (4th, encoded in the article/phase-2 source sequence per C9) nor the per-namespace cache lock semantics. Add a targeted concurrency test. |
| **C8 — author-dir single writer** | Steps 7 & 9 (structural, no code fix) | `pipeline/schedule.py`, `pipeline/record.py`, `save/write.py` | Assert one Record → one unique `format_author_dirname` per future; keep the lock-free RMW; add **no** intra-author parallelism. A per-dir lock is added **only if** within-author parallelism is ever introduced. |

---

## 7. Risks, rollback, and determinism landmines

### Top risks and mitigations
1. **C5 order/membership drift (highest).** A wrong `sites` tag or recipe order silently mis-promotes entries (or, worst case, pushes the `misc->inproceedings` upgrade into A/B where `howpublished` is pre-enrichment — `co-misc-upgrade-preprint-repo-guard`). *Mitigation:* per-site literal-order recipes (no reorder bet); the differential OLD-vs-NEW matrix; the "upgrade only in C" AST test; the golden Phase-4 fixture; two-run under two `PYTHONHASHSEED`. C5 lands last, alone.
2. **Catalog mis-facts (CC1, CC2).** Building C5 from the catalog's "patent/unpublished are C-only" text would drop those rules from Site B and break `pipeline-double-run-fixpoint` for baseline patent/unpublished entries on the complete-entry-skip-enrichment path (where Site B is the *only* fixup). *Mitigation:* membership derived from the source grep in §Verification above; the membership test encodes `{B,C}`.
3. **`enr_list` threading (Wave 2).** Splitting `process_article` risks resetting/reordering the accumulator. *Mitigation:* `PipelineContext` created once, append-only; two-run diff after each phase.
4. **Import cycles.** `fixup` uses `_is_conference_journal`; without `venue.py` (Step 2, before Step 12) it would import `merge`. *Mitigation:* Step 2 precedes all fixup work; an import-graph test asserts no back-edges.
5. **Re-export/F401 churn.** ~30 modules of import updates. *Mitigation:* `import as`/`__all__` shims at every old path until Step 15; a per-step checklist item.
6. **CWD-relative reconcile (`co-summary-csv-cwd-relative-paths`).** Moving the post-run block must keep raw-relpath `os.path.exists` checks and run from project root. *Mitigation:* Step 10 integration test runs from project-root CWD.
7. **Toolchain mismatch.** This repo mandates **pip + mypy + ruff + pytest** (NOT uv/pyrefly). *Mitigation:* the GATE names the exact toolchain; a mismatched toolchain would produce a false green.
8. **Large single relocation (Step 8, ~1970 lines).** Risk is import wiring, not logic. *Mitigation:* pure whole-function move, zero body edits, two-run diff.

### Rollback
Every step is a single worktree-isolated commit that is byte-green in isolation (Wave 1 steps are relocations; Wave 2 steps are behind shims). **Rollback = revert that one commit**; the re-export shims mean reverting a later step never orphans an earlier one. The Step-0 oracle is the fixed comparison point for any bisection. If a GATE fails, the step does not merge — there is no partial-state to unwind.

### Determinism landmines reviewers MUST reject
- Any `set`/`frozenset` feeding a **fix-pattern loop** or an **output-affecting order** (surfaces #3, #7). *The lone legitimate set is `all_candidate_dois` — dedup-only; its order comes solely from the published-first partition sort (G5).*
- Any raw `os.listdir` over a `.bib` dir **without `sorted()`** (surface #6) — now a CI grep-lint failure via `fsscan.py`.
- **Numeric filename counters** (`-2.bib`) instead of title-word extension (`of-filename-no-numeric-counters`).
- Reliance on **`dict` insertion order** in `bibtex_from_dict` field emission (surface #1) — field order comes from the named `PREFERRED_FIELD_ORDER` tuple.
- Any **inlined threshold literal** that forks from `config.py` (`cd-thresholds-centralized`); reclassification/fix-table **values that are not `_normalize_to_ascii` fixpoints** (`ao-ascii-clean-table-values`).
- **Collapsing the three fix sites to one shared order** or making all three byte-identical — this either flattens C's superset or pushes the `misc->inproceedings` upgrade into A/B (`ao-phase4-superset`). Recipes must stay per-site.
- Running the pipeline (or reconcile) from a **non-root CWD** (surface #12, breaks `co-summary-csv-cwd-relative-paths`).
- Re-adding **429/503 to `status_forcelist`**, moving any `time.sleep` **inside** `_GLOBAL_SEMAPHORE`, or adding `ValueError` to the **frozen error tuples** (C1/C2 seams).
