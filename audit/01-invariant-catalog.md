# CiteForge INVARIANT CATALOG — Phase 1 Deliverable (Refusal Floor)

This catalog is the authoritative behavioral contract for the CiteForge refactor campaign. Every entry below is a property the DESIGN and IMPLEMENT phases must preserve. Load-bearing invariants are ranked first within each group; violating one is a hard stop. Cross-subsystem duplicates have been consolidated into a single canonical entry with all evidence `file:line` references merged (noted as *consolidates:*).

Severity legend: **LB** = load-bearing (byte-output or data-integrity impact) · **IMP** = important · **MIN** = minor.

Overarching goal (the reason most of this exists): **running the tool twice on already-processed output must produce byte-identical BibTeX.** See `pipeline-double-run-fixpoint` (Determinism) and the DETERMINISM-CRITICAL SURFACES section.

---

## 1. Determinism

#### `pipeline-double-run-fixpoint` — LB
A second run over already-processed output must be byte-identical (global idempotency). This is the umbrella property the three-way fix + ASCII-clean tables + stable serialization all exist to guarantee.
- Evidence: helpers `main.py:235-254`; three-site application; serializer `src/bibtex_utils.py:302-390`; existing-file path gated by `SKIP_SCHOLAR_FOR_EXISTING_FILES` `src/config.py:61`.
- Verify: golden double-run integration test; run N and N+1 outputs byte-identical over a representative corpus.

#### `determinism-author-sort-key` — LB
Author records are sorted by the exact composite key `(-existing_paper_count, name.lower(), scholar_id or dblp or "")` before processing — stable, tie-broken, input-order-independent.
- Evidence: `main.py:2947-2949`.
- Verify: unit test on the sort lambda with equal counts / out-of-order names+ids; golden test asserting identical order across two calls.

#### `determinism-article-ordering` — LB
Merged publications are ordered by `sort_articles_by_year_current_first`: `(current-year-first group, -year, normalized_title, first_author_sortkey)` — a total, content-derived order independent of API return order. `max_pubs` truncation and first-writer-wins filenames depend on it.
- Evidence: `src/clients/scholar.py:169-178`.
- Verify: sort a shuffled fixture; assert output equals documented ordering and is invariant to input permutation.

#### `determinism-phase2-source-order` — LB — ⚠ CORRECTS PRIOR DIGEST
Phase 2 queries sources in the fixed order **Scholar → S2 → Crossref → OpenReview → arXiv → OpenAlex → PubMed → EuropePMC**, appending each match to `enr_list` in that order. (The prior-study digest wrongly claimed Scholar→S2→Crossref→OpenAlex→PubMed→EuropePMC→arXiv→OpenReview. The evidence order below is authoritative.)
- Evidence: `main.py:1543,1572,1601,1621,1640,1660,1687,1706`.
- Verify: golden-master `.bib` with all sources mocked to distinct filler-only fields; assert the `SEARCH_START` log sequence equals `[S2,Crossref,OpenReview,arXiv,OpenAlex,PubMed,EuropePMC]`.

#### `determinism-enr-list-order` — LB *(consolidates: determinism-enr-list-accumulation, enricher-iteration-order-deterministic)*
`enr_list` is a single list accumulated across P1→P2→P2.5→P3, never reset/sorted/deduped mid-pipeline; `merge_with_policy` iterates it in insertion order and, with strict-less-than trust, the earlier of equal-rank sources wins. The merged dict is therefore a deterministic function of `(primary, ordered enrichers)`.
- Evidence: `main.py:1486` (init once), append sites `src/doi_utils.py:196,199`, `main.py:703,1552`; consumed `main.py:2026`; iteration `src/merge_utils.py:277-445` (strict `<` at :432).
- Verify: assert `enr_list` identity stable and length only grows P1→P4; shuffling equal-rank enrichers must not change merged output.

#### `determinism-doi-candidate-order` — LB *(consolidates: determinism-doi-candidate-sort-published-first, correctness-doi-candidate-published-first-nohttp, determinism-doi-candidate-set-dedup)*
Phase-3 DOI candidates are set-deduped on normalized DOI, then **stable partition-sorted published-first** (`key = 1 if is_secondary_doi else 0`), validated in order, breaking on first success. DOI inference from URLs/eprints is **HTTP-free** (cache-only), keeping P3 deterministic and independent of network timing.
- Evidence: `main.py:1945` (set-dedup), `:1949` (partition sort), `:1994-1996` (first-match break), `:1901-1943` (cache-only inference); `src/id_utils.py:44-46`.
- Verify: `[preprint, published]` → published validated first; assert no live `http_get_text` when a cached/URL-inferred DOI is available; run twice under differing `PYTHONHASHSEED`, assert identical `.bib`.

#### `determinism-reconcile-rewrite-only-when-phantoms` — LB
`reconcile_summary_csv` rewrites the CSV (and refreshes `_SUMMARY_KNOWN_PATHS`) **only when `removed>0`**; when all files exist it returns 0 without rewriting.
- Evidence: `src/io_utils.py:415-439`.
- Verify: `test_no_phantoms_no_rewrite` (mtime unchanged, removed==0); `test_phantom_entries_removed` (removed==1, phantom gone).

#### `determinism-orphan-abspath-resolution` — LB
`collect_orphan_files` and `_load_csv_titles` resolve CSV `file_path` via `os.path.abspath` before comparison/grouping, and `collect_orphan_files` returns a **sorted** list; orphans = on-disk `.bib` whose abspath ∉ abspath'd CSV set.
- Evidence: `src/io_utils.py:371-399` (`return sorted(orphans)` at :399); `main.py:2870`.
- Verify: `test_orphan_detected` / `test_no_orphans`; assert returned list is sorted.

#### `determinism-a2i2-complete-rebuild` — LB
`build_a2i2_folder` wipes every regular file in `out_dir/a2i2` before copying survivors, so the folder is a pure function of current inputs (no stale accumulation).
- Evidence: `src/io_utils.py:587-593` (wipe), `:595-615` (copy).
- Verify: `test_complete_rebuild`, `test_deterministic_output`.

#### `determinism-sorted-bib-scan` — LB *(consolidates: determinism-sorted-bib-iteration, determinism-sorted-file-scan)*
Every scan of an author dir's `.bib` files iterates in `sorted(filename)` order (baseline scan, `save_entry_to_file` duplicate scan, post-run fixup), and the duplicate scan breaks on first match — so the chosen duplicate is deterministically the sorted-first match, not FS/inode-order-dependent.
- Evidence: `main.py:817`, `src/merge_utils.py:953`, `main.py:3164,3168`; first-match breaks `merge_utils.py:1000,1011,1044,1058,1083,1099,1124,1136,1148,1168,1190`.
- Verify: two runs on a dir with 2+ matching duplicates → identical output filenames/bytes; seed `a.bib`/`z.bib` both matching → duplicate is `a.bib`.

#### `determinism-new-author-first-stable` — IMP
After count-sorting, records are re-ordered by `(_has_output(r), original_index)` (stable sort) so authors without an existing output dir run first, ties preserving count-sort index.
- Evidence: `main.py:2971-2977`.
- Verify: mix authors with/without output dirs; assert no-output precede has-output and count-sort order preserved within each group.

#### `determinism-title-similarity-pure` — IMP
`title_similarity`/`normalize_title` stay pure/deterministic: normalize (unescape, strip LaTeX/accents, lowercase, punct→space, collapse) then rapidfuzz ratio/100, returning exactly `1.0` on normalized equality. Every title-based branch boundary (0.55/0.6/0.95) depends on this.
- Evidence: `src/text_utils.py:130-155`, `:381-393`.
- Verify: `title_similarity('Deep Learning.','deep learning')==1.0`; snapshot `normalize_title` over a fixture set.

#### `determinism-pattern-iteration-order` — IMP
Fix patterns are built from ordered containers (dict/tuple/list) and applied in insertion order (`_FUSED_DICT_PATTERNS`, `_COMPOUND_SUFFIX_PATTERNS`, `_ACRONYM_CASE_PATTERNS`, `_BOOKTITLE_FIXUPS`); none may become a set/frozenset.
- Evidence: `main.py:123-130,147-150,188-232`; sources `config.py:379-808`.
- Verify: run same input under two `PYTHONHASHSEED` values → identical output; assert sources are dict/tuple/list.

#### `determinism-url-namespace-first-prefix-match` — IMP
`_classify_url` returns the namespace of the FIRST prefix (insertion order) whose substring is in the URL, else `'other'`; drives call-count tracking and rate-limiter selection.
- Evidence: `src/http_utils.py:121-143`.
- Verify: parametrized map of known hosts→namespace, unknown→`'other'`.

#### `determinism-a2i2-pick-richer-tiebreak` — IMP
Merged duplicates: more non-empty fields wins; on tie, keep lexicographically smaller source filepath (`a if a[1] <= b[1] else b`).
- Evidence: `src/io_utils.py:535-545`.
- Verify: two equal-field duplicates in different author dirs always resolve to lower-path file across runs.

#### `determinism-a2i2-write-order-collision` — IMP
Survivors written iterating `sorted(kept, key=basename)`; filename collisions resolved by appending `_2,_3,...` (counter starts at 2).
- Evidence: `src/io_utils.py:598-606`.
- Verify: `test_deterministic_output` + same-basename-different-dir fixture asserting stable `_2`.

#### `determinism-flush-rewrite-only-on-updates` — IMP
`flush_summary_csv` rewrites only if `_SUMMARY_UPDATES` non-empty; else returns immediately (append-only file untouched). Clears `_SUMMARY_UPDATES` after rewrite.
- Evidence: `src/io_utils.py:334-356`.
- Verify: flush with empty updates → mtime unchanged; with one update → exactly that row rewritten, updates cleared.

#### `determinism-cache-defensive-copy` — IMP
`get()` returns `dict(data)` (fresh shallow copy) on every hit (positive and negative), so callers cannot mutate cached state.
- Evidence: `src/cache.py:141,145`.
- Verify: mutate returned dict, re-`get()`, assert second result unaffected.

#### `determinism-utc-year-functions` — MIN
Pipeline year computations (`get_current_year`, `get_min_year`, `CONTRIBUTION_WINDOW_YEARS`) use UTC (timezone-independent window); cache expiry deliberately uses AST (UTC-4) separately.
- Evidence: `src/clients/helpers.py:139-141`; `src/config.py:46-52`; cache AST `src/cache.py:21`.
- Verify: freeze-time near Dec31/Jan1 in two timezones; assert identical UTC-based values.

---

## 2. Anti-oscillation / Three-way-fix

#### `ao-three-way-fixup-parity` — LB *(consolidates: anti-oscillation-three-way-fixup-parity, fixup-idempotence-convergence, threeway-text-booktitle-all-3-sites, fix-title-text-substep-order-shared)*
The title/venue/type correction ruleset (core reclassifications + `_fix_title_text` + `_apply_booktitle_fixups`) must be applied **identically at all three fix sites** — (A) load-time `_fixup_bib_entry`, (B) existing-file baseline fixup, (C) Phase-4 post-merge — and each must be **convergent** (`f(f(x))==f(x)`) so any entry reaches the same fixed point regardless of path. `_fix_title_text` sub-steps run in the fixed order fused-compounds → colon-space → hyphen-space → space-hyphen → acronym-case, via one shared helper (no per-site reimplementation).
- Evidence: contract `main.py:160-161`; site A `main.py:314-492,432,440`; site B `main.py:865-1309,1205,1213`; site C `main.py:2028-2401,2279,2290`; `_fixup_bib_entry` `src/merge_utils.py:314-565`; helpers `main.py:235-254`; regression `tests/test_regression.py:2825-2867`.
- Verify: idempotency test (second run byte-identical, zero rewrites); cross-path test feeding one malformed entry through each fixup → identical results; grep-assert exactly 3 call sites each for `_fix_title_text`/`_apply_booktitle_fixups`.

#### `ao-phase4-superset` — LB — ⚠ CORRECTS PRIOR DIGEST
Sites A/B/C are intentionally **not** byte-identical: all apply the shared CORE reclassification set, but Phase 4 (C) is a **superset** adding patent→misc, unpublished→misc, url-fragment-booktitle→misc, article-preprint-DOI handling, AND the **only** misc→inproceedings UPGRADE. (Prior digest wrongly claimed C omits the article/inproceedings reclassifications.) A refactor consolidating sites must preserve C's extras and must NOT add the misc-upgrade to A/B.
- Evidence: C-exclusive `main.py:2134-2144,2146-2151,2153-2162,2238-2259,2382-2401`; shared core C `main.py:2072-2236` mirroring A `main.py:324-421` and B `main.py:1028-1193`.
- Verify: reclassification-parity test per site; assert misc→inproceedings upgrade appears only in the Phase-4 path.

#### `ao-is-proc-series-guard-frontiers` — LB
The inproceedings→article reclassification (JOURNAL_ONLY_PREFIXES) must be gated by `not is_proc_series` at all three sites, because `'frontiers in artificial intelligence and applications'` is in PROCEEDINGS_SERIES_AS_JOURNAL **and** matches prefix `'frontiers in '` — without the guard the type flips every run.
- Evidence: `main.py:393-394,1162-1163,2175-2176`; tables `config.py:218-226,361-368`.
- Verify: Frontiers-in-AI entry through `_fixup_bib_entry` twice → stable `@inproceedings`.

#### `ao-is-pacm-guard` — LB
The article→inproceedings reclassification (`mu._is_conference_journal`) must be gated by `not is_pacm` (ACM_JOURNAL_PROCEEDINGS) at all three sites, because `_is_conference_journal` returns True for "Proceedings of the ACM on ..." yet PACM venues are genuine journals — else PACM type oscillates.
- Evidence: guard `main.py:417-418,1031-1032,2077-2078`; reverse rule `main.py:333-338,2098-2106`; `src/merge_utils.py:133-151`; table `config.py:230-241`.
- Verify: ACM_JOURNAL_PROCEEDINGS entry stays `@article` across two passes.

#### `ao-disjoint-reclassification-tables` — LB
Reclassification tables must remain disjoint under startswith/eq matching so no venue string is matched by two rules that reclassify it in opposite directions (article↔inproceedings). Adding a bridging prefix reintroduces oscillation.
- Evidence: `config.py:167-241,361-368`; matching `main.py:325-421`.
- Verify: table-consistency test — no forward-table string is a startswith-prefix/equal of any reverse-table string (and vice versa); guarded pair still relies on `not is_proc_series`.

#### `ao-ascii-clean-table-values` — LB
Every VALUE in a fix/correction table must already be the exact ASCII form `_normalize_to_ascii` produces (unidecode'd accents, straight quotes, `-`/`--`, `\&`), i.e. a fixpoint of `_normalize_to_ascii`. Otherwise the fix writes value X, the serializer rewrites to X', and the next run re-fixes → byte diff every run.
- Evidence: serializer `src/bibtex_utils.py:302-328,384`; pre-escaped examples `config.py:254,182-183`.
- Verify: for every table value `v`, assert `v == _normalize_to_ascii(v)`.

#### `ao-candidate-doi-disk-dedup` — LB *(consolidates: anti-oscillation-candidate-doi-disk-dedup, anti-oscillation-candidate-doi-net, anti-oscillation-two-layer-nets)*
Before final write, all DOIs seen across P2 candidates (matched **and** rejected, via `seen_dois`), P2.5 injections, P3 discovery, and the merged entry's own DOI/eprint are checked against DOIs on disk in OTHER files. On a genuine match (title_similarity ≥ `SIM_PREPRINT_TITLE_THRESHOLD`=0.55) the new file is removed and write skipped; a below-threshold match instead calls `_revert_misattributed_doi` and continues. This is the **outer** of two independent dedup nets (the other being the in-`save_entry_to_file` file scan); both must remain.
- Evidence: `main.py:1490,1531-1588,2531-2588`; file-scan net `src/merge_utils.py:980-1190`; candidate collection `main.py:657-699,1745,1752,1947,2536-2540`.
- Verify: seed published `.bib` with DOI X; process an article whose candidate set includes X under a preprint title → no new file; a duplicate detectable only by title-sim (no shared DOI) is caught by the file-scan net; misattributed low-sim DOI → reverted, entry still written.

#### `ao-skip-write-existing-better` — LB *(consolidates: anti-oscillation-skip-write-existing-better, prewrite-more-complete-guard, trust-order-prewrite-no-downgrade)*
`save_entry_to_file` keeps the existing file when it is the better version: existing published beats incoming preprint; existing-with-DOI beats incoming-without; same-class keeps existing only if it has ≥3 more populated fields; and a pre-write guard refuses to overwrite when existing has more non-empty fields, a published DOI vs incoming preprint DOI, or a specific booktitle vs generic-series — **unless** a preprint→published upgrade.
- Evidence: `src/merge_utils.py:1201-1305,1386-1416,1405-1422,1433-1451`.
- Verify: (a) existing published DOI + incoming preprint → skip; (b) existing 8 fields vs incoming 4, no upgrade → no write; (c) preprint→published upgrade with fewer fields → still writes; (d) specific vs generic booktitle → keep existing.

#### `ao-doi-published-beats-preprint-xor` — LB
For the `doi` field, a published DOI always replaces a preprint DOI and a preprint DOI never replaces a published DOI, independent of trust rank (XOR override runs before the generic trust gate). Enforced in merge and in save.
- Evidence: `src/merge_utils.py:298-316,1230-1243,1415-1416`.
- Verify: current published DOI + incoming higher-trust preprint DOI → keep published; re-merge is a fixed point.

#### `ao-journal-never-downgrade-to-preprint` — LB
The `journal` field is never replaced by a preprint-server value when the current journal is a real venue (tested against PREPRINT_SERVERS substrings).
- Evidence: `src/merge_utils.py:342-355`; `src/config.py:133-140`.
- Verify: `journal='Nature'` + incoming `journal='arXiv'` → kept Nature.

#### `ao-title-keep-longer-trust-diff` — LB *(consolidates: title-keep-longer-unless-trust-diff-threshold, title-length-keep-trust-override)*
A shorter incoming title (stripped length `< TITLE_LENGTH_KEEP_RATIO=0.7 × current`) is rejected unless the incoming source is ≥ `TRUST_DIFF_OVERRIDE_THRESHOLD=3` ranks more trusted. Both constants config-sourced.
- Evidence: `src/merge_utils.py:386-405`; `src/config.py:826,829`.
- Verify: 60-char s2 title + 20-char crossref title (diff<3) → keep long; from csl (diff≥3) → allow short.

#### `ao-eprint-removed-on-published-doi` — LB
When a non-preprint DOI coexists with an arXiv eprint, the eprint/archiveprefix/primaryclass are removed, preprint URLs rewritten to `https://doi.org/<doi>`, phantom `arXiv` journals stripped, journal backfilled from the best-ranked matching enricher (else `@article`→`@misc`).
- Evidence: `src/merge_utils.py:653-735`.
- Verify: `test_merge_doi_arxiv_handling`; OSTI-style published DOI with no journal → `@misc`.

#### `ao-p1-stash-and-pop` — LB
When the P1 baseline DOI fails validation it is stashed in `unvalidated_doi` AND popped from `bf`, so P3 can retry it while it never leaks into merged output.
- Evidence: `main.py:1512-1514`; consumed `main.py:1839`.
- Verify: baseline DOI failing CSL/BibTeX → `bf` has no `doi` after P1; `unvalidated_doi` appears among P3 candidates.

#### `ao-p3-gate` — LB
Phase 3 runs iff `(not doi_validated) OR is_secondary(baseline_doi)` — a validated preprint/data DOI still triggers P3 to attempt a published upgrade.
- Evidence: `main.py:1812-1814`.
- Verify: validated published → P3 skipped; validated arXiv/secondary → P3 runs; no validated DOI → runs.

#### `ao-p3-flag-gated-extraction` — LB
Phase 3 extracts DOIs/URLs from an API source only when that source's flag is set (candidate matched baseline); baseline eprint/url always allowed.
- Evidence: `main.py:1862-1877,1885-1899,1846-1849,1881-1883`.
- Verify: non-matching candidate (flag False) → its DOI/URL never enters candidate sets.

#### `ao-eprint-doi-injection-restored` — LB
When validating an inferred arXiv-eprint DOI, it is temporarily injected into `bf` only if `bf` had no DOI and the candidate is an eprint DOI, and is always popped back out after validation.
- Evidence: `main.py:1974-1988`.
- Verify: `bf['doi']` absent after a failed eprint-DOI validation attempt.

#### `ao-replace-keep-directional` — LB *(consolidates: dedup-replace-keep-decision-directional, trust-order-replacement-tree)*
On a confirmed duplicate: published beats preprint (keep existing published / replace existing preprint); DOI-vs-no-DOI keeps the one with the DOI; same-class both-DOI keeps incoming UNLESS existing has ≥ `new_field_count+3` non-empty fields; year-change uses the new filename; else reuse existing key. The `+3` margin and directionality are exact.
- Evidence: `src/merge_utils.py:1203-1294,1228-1292`.
- Verify: table-driven over `(existing_preprint,new_preprint,existing_doi,new_doi,field_counts)` asserting `KEEP_EXISTING|REPLACE|USE_NEW_NAME|REUSE_KEY` and whether `os.remove` fired; `existing==new+2` vs `new+3` flips the decision.

#### `ao-postrun-fixup-write-suppression` — LB
Post-run fixup rewrites a `.bib` only when `_fixup_bib_entry` reports a change AND the re-serialized content differs from the original; identical re-serialization is not written.
- Evidence: `main.py:3176-3180`.
- Verify: run post-run fixup twice over a canonical corpus; second pass writes 0 files, bytes/mtime stable.

#### `ao-429-503-excluded-from-urllib3-forcelist` — LB
The urllib3 `Retry.status_forcelist` must EXCLUDE 429 and 503, which are handled ONLY by the manual retry loop — never double-backed-off by the adapter. (`respect_retry_after_header=False` is the paired guard; see `correctness-urllib3-retry-after-disabled`.)
- Evidence: `src/http_utils.py:163-175` (exclusion :169), manual handling `:357-360`.
- Verify: `assert 429 not in _RETRY_STRATEGY.status_forcelist and 503 not in ...`.

#### `ao-preprint-pair-composite-decircularized` — IMP *(consolidates: dedup-preprint-pair-bonus-subtraction, preprint-pair-composite-decircularized, anti-oscillation-preprint-debias)*
In the different-DOI preprint/published XOR branch, the composite dedup score has the 0.10 preprint-pair bonus subtracted (`effective = score - 0.10`) before comparison to `SIM_DEDUP_COMPOSITE_THRESHOLD`=0.60, because the XOR precondition already consumed that evidence (no double-counting).
- Evidence: `src/merge_utils.py:1027-1044`; `src/config.py:811`.
- Verify: XOR pair with raw composite 0.65 / effective 0.55 must NOT match at 0.60.

#### `ao-booktitle-generic-vs-specific-directional` — IMP
A generic series booktitle is upgraded to a specific one; a specific booktitle is never replaced by a GENERIC_SERIES_NAMES value, independent of trust.
- Evidence: `src/merge_utils.py:408-427,1421-1422`; `src/config.py:346-358`.
- Verify: specific booktitle vs `'Lecture Notes in Computer Science'` → kept specific; reverse → upgraded.

#### `ao-fused-compounds-three-pass-order` — IMP
`_fix_fused_compounds` applies exactly three passes: dictionary → suffix → dictionary (the third catches entries newly exposed by the suffix pass).
- Evidence: `main.py:287-311`.
- Verify: `'Doubleedgeassisted'`→`'Double-Edge-Assisted'`; second call is a no-op.

#### `ao-deferred-baseline-no-doi` — IMP
A freshly created baseline with no DOI is not written eagerly (`path=None`); it is persisted only after Phase 4 via `save_entry_to_file`, avoiding transient files that get renamed each run.
- Evidence: `main.py:1452-1461`.
- Verify: article with no DOI + successful enrichment → exactly one file (post-P4), no intermediate stub.

#### `ao-baseline-duplicate-shortcircuit` — IMP
When the baseline save detects the article already on disk under a different name, enrichment is skipped entirely and the function returns 1 after recording the summary.
- Evidence: `main.py:1469-1484`.
- Verify: baseline save reports duplicate → enrichment phases not entered, return 1.

---

## 3. Trust-ordering

#### `to-canonical-order-strict-rank` — LB *(consolidates: trust-order-canonical-list, trust-order-strict-rank-replace, trust-ordering-single-source, trust-rank-from-list-position, trust-order-precedence)*
`TRUST_ORDER` (13 elements: `csl > doi_bibtex > datacite > pubmed > europepmc > crossref > openalex > s2 > orcid > openreview > arxiv > scholar_page > scholar_min`) is the single source-precedence authority for both `@type` selection and generic field replacement. Trust rank = list index; a populated field is replaced ONLY when the incoming rank is **strictly less** (`new_rank < cur_rank`). Equal/less-trusted never overwrite.
- Evidence: `src/config.py:66-80`; `src/merge_utils.py:241,255-266,429-445`.
- Verify: assert `TRUST_ORDER` equals frozen list; property test that the earlier-in-TRUST_ORDER source wins a contested field; equal-rank second enricher does NOT overwrite.

#### `to-unknown-source-rank-99` — LB *(consolidates: unknown-source-rank-99, trust-ordering-label-keys-match)*
Any source label not in `TRUST_ORDER` defaults to rank 99 (least trusted); it can only fill empty fields. Every `enr_list` label must be a real `TRUST_ORDER` key (a typo silently drops a source to 99 with no error).
- Evidence: `src/merge_utils.py:261,397,430-431,713`; labels `main.py:1552,1586,1614,1633,1652,1673,1700,1719`, `src/doi_utils.py:196,199`.
- Verify: enricher `source='bogus'` cannot overwrite a crossref field; assert set(labels) ⊆ set(TRUST_ORDER).

#### `to-doi-source-gate` — LB *(consolidates: trust-doi-source-gate, doi-trust-gate-registration-agencies-only)*
A surviving merged DOI is retained only if some enricher from `{csl, doi_bibtex, datacite, pubmed, europepmc, crossref}` carries a DOI whose normalized form equals it; otherwise it is stripped. (Gate skipped when `has_doi_conflict` is True.) A primary/merged DOI conflict keeps the primary unless it is a preprint→published upgrade.
- Evidence: `src/merge_utils.py:457-502`.
- Verify: DOI from s2/arxiv only → dropped; same DOI also on crossref → kept; primary published vs merged preprint → primary kept.

#### `to-empty-fill-bypasses-trust` — LB
The first `value_ok` value for an empty field is accepted unconditionally (sets `field_sources`) with no trust comparison; trust gating applies only when overwriting an already-populated field.
- Evidence: `src/merge_utils.py:288-295`.
- Verify: a field present only in the lowest-trust enricher still lands in merged output.

#### `to-type-upgrade-valid-set-rank` — LB
Entry type upgrades from an enricher only if the enricher type ∈ `{article,inproceedings,incollection,book}` AND (its rank strictly better than `best_type_src`, OR equal rank with a differing type). `best_type_src` starts at `scholar_min`.
- Evidence: `src/merge_utils.py:243,254,260-271`.
- Verify: `test_merge_with_policy` (crossref inproceedings beats s2 article); a `misc`-typed high-trust enricher does not set `etype=misc`.

#### `to-field-override-rules` — LB (umbrella)
Field-specific overrides that run **before/around** the generic trust gate, all preserved: DOI published-over-preprint (`ao-doi-published-beats-preprint-xor`); journal never→preprint (`ao-journal-never-downgrade`); title keep-longer/trust-diff (`ao-title-keep-longer-trust-diff`); booktitle generic↔specific (`ao-booktitle-generic-vs-specific`); pages leading-digit/no-dot/≤`PAGES_MAX_DIGITS` (`co-pages-validation`); author prefer-fewer-initials-then-longer (`co-author-prefer-fewer-initials`). Dropping any special-case lets a strictly-trusted source overwrite with worse data and re-triggers enrichment churn.
- Evidence: `src/merge_utils.py:298-427`; thresholds `src/config.py:826,829,340`.
- Verify: parametrized tests per field (see individual entries).

#### `co-author-prefer-fewer-initials` — IMP (trust-adjacent)
At equal author-list length, incoming is rejected if it has MORE initials-only tokens (`^[A-Z]\.$`); at equal initials, rejected if its total name text is shorter. Runs before the trust gate.
- Evidence: `src/merge_utils.py:358-383,60`.
- Verify: current `'Samuel Smith'` + higher-trust `'S. Smith'` (same count) → keep full name.

---

## 4. Dedup

#### `dd-branch-order-first-match` — LB *(consolidates: dedup-branch-order-first-match-wins, dedup-scan-branch-order-first-break, dedup-branch-order-cascade)*
`save_entry_to_file`'s duplicate scan evaluates rules in fixed precedence and **breaks on first match**: DOI-exact → DOI-version base (`.vN`) → different-DOI preprint/published XOR (distinct-arXiv-eprint exclusion; title ≥0.55; composite−0.10 ≥0.60) → external-id+title → key+title/prefix → key+author-overlap (≥0.8, sim≥0.55) → key+preprint-pair → high-title-sim ≥0.95 → truncated+authors → strong-author (sim≥0.6, overlap≥0.9) → preprint-relaxed. Reordering changes which existing file is deemed the duplicate.
- Evidence: `src/merge_utils.py:980-1190`; `src/config.py:130,205,811`.
- Verify: table-driven, one crafted pair per branch asserting the emitted `FILE_MATCH` tag equals the earliest-firing branch; an ordering test where two branches could fire asserts higher-priority wins.

#### `dd-composite-weights` — LB
`compute_dedup_score` is the additive 6-signal sum with exact weights: title 0.40, author-overlap 0.25, year 0.10(exact)/0.05(±1), venue 0.15, external_ids 0.15, preprint-XOR 0.10 (max 1.15).
- Evidence: `src/text_utils.py:511-546`.
- Verify: parametrized exact-score test (identical title+year+venue → 0.65); assert max attainable == 1.15.

#### `dd-all-candidate-dois-includes-unmatched` — LB
`all_candidate_dois` collects normalized DOIs from ALL P2 candidates (matched and rejected via `seen_dois`), plus P2.5 injections, P3 discovery, and the merged entry's own DOI/eprint — for save-time on-disk dedup. Narrowing to matched-only reopens preprint/published oscillation.
- Evidence: `main.py:1490,694-699,1745,1752,1947,2536-2540`; passed as `seen_dois` at `main.py:1588,1616,1635,1655,1674,1702,1721,1773,1800`.
- Verify: a source returns a non-matching candidate carrying DOI X → X ∈ `all_candidate_dois` after P2.

#### `dd-title-similarity-guard` — LB
A candidate DOI matching an existing on-disk file's DOI suppresses the write only when the two titles are similar (≥ `SIM_PREPRINT_TITLE_THRESHOLD`=0.55); below threshold the match is rejected as misattributed and the DOI reverted. Prevents a false API DOI from deleting an unrelated file.
- Evidence: `main.py:2564-2574`; `src/config.py:205`.
- Verify: existing DOI == candidate DOI, dissimilar titles → write proceeds + `_revert_misattributed_doi`; similar → write skipped, file removed.

#### `dd-self-match-exclusion` — LB *(consolidates: dedup-self-match-exclusion, prefer-path-excluded-from-dup-scan)*
The entry's own `prefer_path`/`prefer_doi` is excluded from every duplicate scan: `prefer_basename` skipped in the file-scan loop, `prefer_doi` removed from `check_dois`, self path skipped by abspath compare — so an entry never dedups against / deletes itself.
- Evidence: `src/merge_utils.py:978-982`; `main.py:2542-2543,2549-2550`.
- Verify: enrich an in-place file whose DOI is also in `all_candidate_dois` → file updated, not skipped/removed; a real cross-file duplicate in a different file is still detected.

#### `dd-strict-match-gate` — LB *(consolidates: correctness-strict-match-gate, dedup-strict-match-fastpath-order)*
Every enrichment entry (Scholar page, each API candidate, CSL, BibTeX) is admitted only after `bibtex_entries_match_strict` passes against the baseline. Its fast-path order/gates: exact-DOI True; same-class different-DOI False; XOR preprint/published falls through; exact arXiv eprint True / different eprint False; external_ids+title≥0.35 True; title≥0.95 requires author-overlap and no year divergence; truncated requires overlap+year; below 0.35 False; composite only when (preprint_pair | external_ids | high_author_match with ≥2 authors each).
- Evidence: gate impl `src/bibtex_utils.py:553-612,567-686`; call sites `main.py:701,1551`, `src/doi_utils.py:44,82`.
- Verify: a candidate differing in title/DOI/arXiv is rejected; parametrized tests per path (`DOI_EXACT`, `ARXIV_EXACT`, same-class-different-DOI→False, XOR→composite).

#### `dd-orphan-delete-only-confirmed-dup` — LB
An orphan `.bib` (on disk, absent from CSV) is deleted ONLY when its parsed title has similarity ≥ `SIM_MERGE_DUPLICATE_THRESHOLD`=0.95 to a CSV-tracked title in the SAME author directory; empty title or no tracked match → kept (warning logged, never deleted).
- Evidence: `main.py:3093-3109,3091-3096`; `_load_csv_titles` grouping `main.py:2870-2877`.
- Verify: orphan matching a tracked same-dir title → removed; unique/empty-title orphan → retained; orphan in author A must not be deleted based on author B's title.

#### `dd-a2i2-doi-before-title` — LB
a2i2 dedup runs DOI-based dedup (Pass 1, `doi_bases_match` fuzzy) across ALL entries first, then title-similarity dedup (Pass 2, ≥0.95) only for entries not already DOI-matched (`if idx in seen`).
- Evidence: `src/io_utils.py:547-585`.
- Verify: `test_dedup_by_title` + DOI-dup fixture; assert count==1.

#### `dd-doi-version-equivalence` — IMP
`doi_bases_match` treats DOIs differing only by trailing `.vN` as the same work; this branch fires before the different-DOI XOR logic.
- Evidence: `src/id_utils.py:50-58`; consumed `src/merge_utils.py:1003`.
- Verify: `.v1`/`.v2` of same preprint → match; mismatched bases → no match.

#### `dd-distinct-arxiv-different-papers` — IMP
Two entries with distinct non-empty arXiv eprint IDs are different papers — short-circuits preprint/published matching in file-scan and key-preprint branches and returns False in strict match.
- Evidence: `src/merge_utils.py:1020-1023,1107-1110`; `src/bibtex_utils.py:597-602`.
- Verify: eprints `2401.00001` vs `2401.00002`, similar titles, XOR DOI → not a duplicate.

#### `dd-merge-union-primary-first` — IMP
`merge_publication_lists` dedups Scholar (primary) and DBLP (secondary) independently, then appends only non-duplicate secondary items to the primary-first list using `SIM_MERGE_DUPLICATE_THRESHOLD` — Scholar takes precedence, union size/order deterministic.
- Evidence: `src/clients/scholar.py:234-265,188-192`.
- Verify: Scholar+DBLP sharing one paper → single merged entry retaining the Scholar record; primary items precede appended secondary.

#### `dd-orphan-title-scoped-to-author-dir` — IMP
Orphan duplicate comparison uses only titles tracked under that orphan's own author directory (`os.path.dirname(orphan)`), never the global set.
- Evidence: `main.py:3091-3096`; grouping `main.py:2870-2877`.
- Verify: two authors with same title; orphan under A not deleted based on B's tracked title.

#### `dd-skip-write-return-cleanup` — IMP
On `skip_write`, return `(duplicate_path, False)` and, if `prefer_path` differs from the duplicate, remove the pre-enrichment baseline file (no stub left behind).
- Evidence: `src/merge_utils.py:1298-1305`.
- Verify: file count stays 1, path reused, `was_written` False.

#### `dd-prefer-path-cleanup-blocked-when-richer` — IMP *(consolidates: prefer-path-cleanup-blocked-when-richer, data-loss-prefer-path-guard)*
When relocating an entry, the old `prefer_path` file is NOT deleted if it has more non-empty fields than the new entry, or equal fields plus a DOI; then return `(prefer_path, False)`, keeping the enriched original.
- Evidence: `src/merge_utils.py:1433-1455`.
- Verify: `prefer_path` 7 fields incl DOI, new 5 → kept, `os.remove` not called, `was_written` False.

---

## 5. Cache

#### `ca-monthly-boundary-expiry` — LB *(consolidates: cache-monthly-boundary-expiry, cache-monthly-expiry-boundary)*
Any cache entry (positive or safe-negative) whose timestamp precedes the 1st-of-current-month AST (UTC-4) boundary is stale → MISS, forcing a fresh request. The boundary is computed once at `ResponseCache` construction (see defect `ca-month-boundary-frozen`).
- Evidence: `src/cache.py:24-32,121-126`; AST `src/cache.py:21`.
- Verify: entry timestamped before the 1st → `get()` None; after → served.

#### `ca-get-branch-order` — LB
`get()` evaluates in fixed order: (1) `CACHE_ENABLED` gate, (2) file-exists, (3) JSON-load (corrupt→MISS), (4) monthly-boundary staleness, (5) negative handling — unconfirmed (`not _safe`)→MISS/force-retry, safe→`_safe_negative_expired` check, else NEG_HIT, (6) positive→POS_HIT.
- Evidence: `src/cache.py:99-145`.
- Verify: table-driven over fresh/stale/unconfirmed-neg/safe-neg-live/safe-neg-expired/positive.

#### `ca-negative-three-tier` — LB *(consolidates: cache-negative-three-tier-confirmation, cache-negative-three-tier, anti-oscillation-three-run-negative-confirmation)*
Negative entries are three-tier: transient errors never cached; unconfirmed negatives (`_confirmations < CACHE_NEGATIVE_CONFIRM_RUNS`=3) stored but NOT served (force retry); only after 3 consecutive empties is a "safe" negative served, expiring at the earlier of next Monday or 1st-of-next-month (AST). `put_negative` increments confirmations (capped then +1) and sets `_safe` at ≥3. This is the core anti-flap contract.
- Evidence: `src/cache.py:41-47,128-141,184-223`; `_safe_negative_expired` `:70-97`; `CACHE_NEGATIVE_CONFIRM_RUNS` `config.py:126`.
- Verify: `put_negative` 1–2× → `get()` None (retry); 3rd → served until Monday/month boundary.

#### `ca-safe-negative-expiry` — LB
`_safe_negative_expired` expires a safe negative at the EARLIER of next-Monday-00:00-AST or 1st-of-next-month-00:00-AST, computed from the entry's own creation timestamp (Monday-created → `+7` days, `days_to_monday = (7-weekday)%7 or 7`).
- Evidence: `src/cache.py:70-97,134-138`.
- Verify: parametrized over creation weekdays asserting `expiry == min(next_monday, next_first)`.

#### `ca-atomic-write` — LB *(consolidates: cache-atomic-write-tmp-replace-warn-noraise, concurrency-atomic-cache-write)*
`_write_entry` writes via `tempfile.mkstemp` + `os.replace` (atomic); on failure the tmp file is removed and `OSError` is caught/logged at WARN without raising — readers never see a partial JSON, and cache-write failures never propagate.
- Evidence: `src/cache.py:159-182`.
- Verify: simulate `os.replace` failure → no tmp files remain, no exception, target never partial.

#### `ca-positive-freshness-not-ttl` — LB *(consolidates: cache-positive-freshness-not-ttl, cache-ttl_days-written-not-read-vestigial)*
Positive freshness is governed **solely** by the monthly boundary — `get()` does NOT enforce per-entry `ttl_days`. `ttl_days` is written by `put`/`_write_entry` and accepted as an arg but **never read by `get()`** (vestigial). A refactor that "restores" ttl_days honoring silently overrides the monthly-refresh model for every namespace.
- Evidence: `src/cache.py:99-145,147,165`; callers `src/clients/utility_apis.py:160,275`; DOI-from-HTML ttl `main.py:1933,1936`.
- Verify: store a positive entry with `ttl_days=1` timestamped after the month boundary → still served; entry before boundary → MISS regardless of ttl.

#### `ca-confirmation-rmw-under-lock` — LB
`put_negative` does read-existing / increment / write-back of `_confirmations` entirely under the per-namespace lock; the count is monotonic and saturates (`min(existing,N)+1`).
- Evidence: `src/cache.py:195-223`; `_lock_for` `:56-58`.
- Verify: concurrent `put_negative` on one key → final count increased by number of calls (bounded by cap).

#### `ca-doi-html-negative-on-read` — IMP
Phase-3 DOI-from-HTML scraping caches both HTTP failures and empty scrapes as `{'doi':''}` (ttl_days=60) and, on read, treats an empty cached doi as a negative hit (continue to next URL) — no repeat HTTP within the window.
- Evidence: `main.py:1919-1936`.
- Verify: first run scrapes empty + caches; second run reads cache, no HTTP, moves to next URL.

#### `ca-counters-exactly-one-per-get` — IMP
Every terminating path of `get()` increments exactly one of `_CACHE_POS_HITS`/`_CACHE_NEG_HITS`/`_CACHE_MISSES` under `_CACHE_COUNTER_LOCK`.
- Evidence: `src/cache.py:100-145`; lock `:19`.
- Verify: exercise each branch once; assert exactly one counter moves per call and totals reconcile.

#### `ca-disabled-is-total-noop` — IMP
When `CACHE_ENABLED` is False, `get()` returns None (no counter change) and `put`/`put_negative` return immediately without touching disk.
- Evidence: `src/cache.py:101,148,192`; `config.py:127`.
- Verify: disabled → `get` None, no files written.

#### `ca-month-boundary-frozen` — IMP — ⚠ LATENT DEFECT
`self._month_boundary` is computed once in `__init__`, and `response_cache` is a module-level singleton created at import — the staleness boundary is frozen for the process lifetime and does not advance across a month rollover. Current behavior tests observe; see CONFLICTS.
- Evidence: `src/cache.py:54,257,24-32`.
- Verify: instantiate cache, monkeypatch clock to next month, assert `_month_boundary` unchanged unless a new `ResponseCache` is built.

---

## 6. Concurrency

#### `co-shared-state-locks` — LB *(consolidates: concurrency-shared-state-locks, csv-mutations-under-lock, cache-per-namespace-lock-and-sha256-path, concurrency-per-namespace-lock)*
All shared mutable state is lock-guarded: per-namespace cache file locks (`_lock_for` under `_meta_lock`, held for every get/put/put_negative/invalidate), a global cache-counter lock, and the summary-CSV in-memory index/updates (`_CSV_LOCK`) and API counters. Cache entry paths are `cache_dir/namespace/<sha256(key)>.json`.
- Evidence: `src/cache.py:19,52-68,103-108,155-157`; `src/io_utils.py:40,281-283,303-304,316,334,415`.
- Verify: thread-stress `put_negative` + `append_summary_to_csv` from many threads on one key → no corruption, correct final counts; assert path == `.../sha256hex.json`.

#### `co-single-writer-per-author-dir` — LB *(consolidates: single-writer-per-author-dir-no-lock, concurrency-single-writer-per-author-dir)*
`save_entry_to_file` performs unlocked read-modify-write (`listdir`+read+`os.remove`+`open('w')`) on the author dir; dedup/anti-oscillation correctness assumes **at most one writer per author dir** — parallelism is across authors (`ThreadPoolExecutor`, `MAX_WORKERS`), one `process_record` per Record. Any refactor parallelizing within an author MUST add per-dir locking.
- Evidence: `src/merge_utils.py:949-1504` (no lock); `main.py:2998-3018`; `src/config.py:833,854`.
- Verify: assert records map to unique `format_author_dirname` per submitted future; document the precondition; stress test two concurrent saves on one dir is expected-flaky (records the assumption).

#### `co-sleeps-outside-global-semaphore` — LB
All `time.sleep()` (backoff and Retry-After waits) occur OUTSIDE the `with _GLOBAL_SEMAPHORE:` block; the semaphore is held only for the actual request send/receive.
- Evidence: `src/http_utils.py:338-369` (semaphore :338-363, sleeps :366-369).
- Verify: instrument acquire/release around a forced 429; assert semaphore released before sleep.

#### `co-global-semaphore-bounds-inflight` — IMP
A single module-level `threading.Semaphore(GLOBAL_CONCURRENCY_LIMIT=16)` gates every in-flight HTTP request across all threads; each attempt acquires one permit for the send. (Default 16 > `MAX_WORKERS` 12, so it rarely binds but must still bound when `CITEFORGE_CONCURRENCY` is lowered.)
- Evidence: `src/http_utils.py:177,338`; `src/config.py:854`.
- Verify: `CITEFORGE_CONCURRENCY=2`, 8 threads → max concurrent in-flight ≤ 2.

#### `co-threadpool-worker-cap` — IMP
Author processing runs on `ThreadPoolExecutor(max_workers=MAX_WORKERS)` (default 12, `CITEFORGE_MAX_WORKERS` override); the env var is the single knob.
- Evidence: `src/config.py:833`; `main.py:2998,3001-3018`.
- Verify: executor constructed with `MAX_WORKERS`; `CITEFORGE_MAX_WORKERS=1` smoke run completes identically.

#### `co-thread-local-logging` — IMP
Each worker rebinds the logger to a per-author `author.log` via `logger.set_log_file` at `process_record` start and closes it in `finally`; the main thread logs to `output/run.log`.
- Evidence: `main.py:2707-2709,2847-2849,2898`.
- Verify: two `process_record` calls on separate threads → each author's lines only in its own log.

#### `co-result-timeouts` — IMP
Result collection uses `future.result(timeout=30)` per author and `as_completed(..., timeout=author_timeout*len(records))` with `author_timeout=1800`; timeouts are caught/logged (per-author + pipeline-level listing pending authors) rather than crashing.
- Evidence: `main.py:2996,3023,3026,3033-3052`.
- Verify: a future sleeping >30s → TimeoutError branch logs, processing continues; overall timeout == 1800×len.

#### `co-rate-limit-token-once-per-call` — IMP
`limiter.acquire()` is called exactly once per `_http_request`, before the retry loop — retries consume no additional tokens.
- Evidence: `src/http_utils.py:327-329,334`.
- Verify: force 3 manual retries → `acquire` called once.

#### `co-token-bucket-semantics` — IMP
`TokenBucketRateLimiter` refills using `time.monotonic()` (`elapsed*rate` capped at burst), deducts 1.0 per acquire, and when starved sleeps `wait=(1-tokens)/rate` + jitter up to 30% (`uniform(0, wait*0.3)`), all under a per-limiter lock.
- Evidence: `src/http_utils.py:183-211`.
- Verify: observed throughput ≈ rate with burst headroom; assert `time.monotonic` used.

#### `co-rate-limiter-registry-singleton` — IMP
`_get_rate_limiter` returns None when the namespace is absent from `RATE_LIMITS` (no throttling), else lazily creates exactly one limiter per namespace via double-checked locking.
- Evidence: `src/http_utils.py:214-233`; `src/config.py:836-850`.
- Verify: `_get_rate_limiter('other') is None`; `_get_rate_limiter('arxiv') is _get_rate_limiter('arxiv')`.

#### `co-thread-excepthook-visibility` — MIN
A custom `threading.excepthook` logs any uncaught worker-thread exception (name + type/value) before delegating to the original hook.
- Evidence: `main.py:2982-2992`.
- Verify: force an uncaught worker exception → ERROR log naming thread+exception; original hook still runs.

#### `co-session-per-thread-rotation` — MIN
`_get_session` returns a thread-local `requests.Session` with `_RETRY_STRATEGY`, rotated (closed+recreated) after `SESSION_ROTATION_THRESHOLD=50` requests; the counter increments once per attempt (retries count toward rotation).
- Evidence: `src/http_utils.py:245-260,340-341`; `src/config.py:857`.
- Verify: 51 requests on one thread → exactly one rotation.

---

## 7. Error-handling

#### `eh-per-unit-isolation` — LB *(consolidates: error-handling-per-unit-isolation, error-handling-per-source-isolation)*
Failures are isolated at each granularity: per-article exceptions (`FULL_OPERATION_ERRORS`) caught inside the article loop; per-source API exceptions (`ALL_API_ERRORS`) caught around every P2/P2.5 enrichment call — one article or one source failing never aborts the author or the run.
- Evidence: article loop `main.py:2840-2841`; per-source `main.py:1567-1568,1598-1599,1618-1619,1637-1638,1657-1658,1685-1686,1704-1705,1723-1724,1775-1779,1802-1806`.
- Verify: one API client raises → enrichment proceeds with remaining sources; one article raises → subsequent articles still process.

#### `eh-error-tuple-membership-frozen` — LB
Exception-group tuple membership is a frozen contract downstream catches depend on: `NETWORK_ERRORS = HTTP_ERRORS + TIMEOUT_ERRORS + (RuntimeError,)` **excludes** ValueError; `ALL_API_ERRORS = NETWORK_ERRORS + DECODE_ERRORS` **excludes** ValueError; `ALL_FETCH_ERRORS = NETWORK_ERRORS + DECODE_ERRORS + PARSE_ERRORS` **includes** ValueError (via `PARSE_ERRORS=(ValueError,TypeError,KeyError)`); `DECODE_ERRORS=(UnicodeDecodeError,UnicodeError)`.
- Evidence: `src/exceptions.py:31-49`.
- Verify: assert `ValueError not in ALL_API_ERRORS and ValueError not in NETWORK_ERRORS and ValueError in ALL_FETCH_ERRORS`.

#### `eh-handle-api-errors-scope` — LB
`@handle_api_errors` wraps a call in `try/except ALL_API_ERRORS`, logs DEBUG, returns `default_return`; it does NOT catch ValueError/parse errors — JSON-decode failures propagate through decorated functions.
- Evidence: `src/http_utils.py:263-278`.
- Verify: decorated fn raising ValueError propagates; raising `RequestException` → `default_return`.

#### `eh-decode-json-valueerror-with-url` — LB — ⚠ DEFECT-ADJACENT
`_decode_json_bytes` raises a plain `ValueError` (not NETWORK/DECODE) on malformed JSON, with the message embedding the **full request URL** (`{url!r}`) + 256-byte preview. This type (uncaught by `ALL_API_ERRORS`) and the URL-carrying payload are the active key-leak vector for URL-embedded secrets.
- Evidence: `src/http_utils.py:388-399`.
- Verify: on bad bytes, `isinstance(raised, ValueError)` and `url in str(raised)`.

#### `eh-gemini-key-leak` — LB — ⚠ DEFECT
The Gemini URL embeds the secret as `?key={api_key}`; a malformed-JSON `ValueError` carries that full URL, and the Gemini caller catches `(*ALL_API_ERRORS, ValueError)` and logs it at WARN — exposing the API key in warning logs. The invariant to enforce is **redaction of URL secrets before logging**.
- Evidence: `src/clients/utility_apis.py:45,88-90`; leak source `src/http_utils.py:399`.
- Verify: assert `'key='` not in captured WARN record when Gemini returns invalid JSON.

#### `eh-bibyear-fallback-lower-bound-guard` — LB
The BibTeX-year fallback removes a file only when `0 < bib_year < window_min`; `bib_year == 0` (missing/unparseable, `extract_year_from_any(fallback=0)`) never triggers deletion.
- Evidence: `main.py:3142-3151`.
- Verify: unparseable-year file retained; year=2000 (<min) removed.

#### `eh-scan-swallows-oserror` — IMP *(consolidates: file-io-errors-non-fatal, error-handling-scan-swallows-oserror)*
Every existing-file read in the dedup scans, replace-decision, collision loop, pre-write check, prefer-path cleanup, and cross-file key scan swallows `OSError` (and `UnicodeDecodeError` in main) and treats the file as absent/non-matching; a final unresolved filename collision logs a warning and returns the existing path rather than raising.
- Evidence: `src/merge_utils.py:1191-1192,1293-1294,1370-1382,1430-1431,1454-1455,1488-1489`; `main.py:2587-2588`.
- Verify: an unreadable/garbage `.bib` in the author dir → save completes without exception, new entry written.

#### `eh-orphan-parse-errors-non-fatal` — IMP
Every read/parse in the reconciliation block is defensively wrapped so one malformed `.bib` never aborts the pass: orphan title parse failure → empty title (kept), bib-year parse failure → pass, post-run fixup parse failure → pass.
- Evidence: `main.py:3084-3089,3139-3153,3172-3182`.
- Verify: plant a broken `.bib` → full block completes, broken file retained (not deleted as duplicate), other files processed.

#### `eh-candidate-loop-continues` — IMP
`_try_multiple_candidates` swallows per-candidate exceptions (logs, continues to next) and returns `(False, None)` rather than raising.
- Evidence: `main.py:683-719`.
- Verify: first candidate's `build_func` raises, second matches → returns True on second, exception logged not propagated.

#### `eh-phase-exception-types` — IMP
Phase 1 is guarded by `PARSE_ERRORS`, Phase 3 by `ALL_API_ERRORS`; each phase's except type bounds what is swallowed vs propagated.
- Evidence: `main.py:1527-1528,2008-2009`.
- Verify: P1 tolerates a parse error; P3 tolerates an API error (falls through to Phase 4).

#### `eh-scholar-retry-then-dblp-only` — IMP
Scholar fetch retries up to 3× with escalating sleep (`2.0*attempt`); persistent empty/failure → warn + continue DBLP-only (not abort); `search_metadata` status `'error'` raises `RuntimeError`.
- Evidence: `main.py:2724-2753`.
- Verify: mocked empty 3× → continues with `dblp_items`; status `'error'` → `RuntimeError`.

#### `eh-setup-exit-codes` — IMP
`main()` returns exit code 2 on unrecoverable setup failure (cannot create output dir, missing SerpAPI key, input CSV read error) and 0 on completion; missing optional keys (Serply/S2/OpenReview/Gemini) only warn and degrade.
- Evidence: `main.py:2893-2895,2903-2906,2936-2939,3231,2909-2931`.
- Verify: unreadable output dir / missing SerpAPI / bad input path → 2; happy path → 0; missing Serply → warns + continues.

#### `eh-manual-retry-loop-3-attempts` — IMP
`_http_request` loops `_MAX_RATE_LIMIT_RETRIES=3`; on 429/503 with attempts remaining it backs off (capped at `HTTP_BACKOFF_MAX=16.0`, Retry-After honored via `min(rate_wait, cap)`) and retries; on the final attempt (or any other status) it `raise_for_status()` and returns `resp.content`; `RequestException` re-raised only on the final attempt.
- Evidence: `src/http_utils.py:300,334-372,357-367`; `src/config.py:108`.
- Verify: persistent 503 → exactly 3 send attempts then raise; Retry-After='600' → observed sleep ≤ 16.0.

#### `eh-datacite-orcid-valueerror-escapes` — IMP — ⚠ DEFECT
`datacite_search_doi`/`orcid_fetch_works` guard `http_get_json` with `except NETWORK_ERRORS` and are decorated with `@handle_api_errors` (`ALL_API_ERRORS`); since neither set contains ValueError, a malformed-JSON ValueError escapes uncaught. Current behavior; a refactor must not silently mask or newly expose it without intent.
- Evidence: `src/clients/utility_apis.py:131,153-156,221,241-244`; `src/exceptions.py:49`.
- Verify: mock `http_get_json` to raise ValueError → propagates out of `datacite_search_doi`.

#### `eh-defect-post-retried-non-idempotent` — IMP — ⚠ DEFECT
POST is in urllib3 `allowed_methods` AND retried by the manual 429/503 loop, so POST bodies are re-sent on transient failures. Current behavior a refactor must consciously preserve or fix, not accidentally alter.
- Evidence: `src/http_utils.py:170,343-347`.
- Verify: `test_post_retried_on_429` asserts POST re-sent under 429.

#### `eh-a2i2-missing-csv-returns-zero` — IMP
`build_a2i2_folder` returns 0 without touching `out_dir/a2i2` when the input CSV cannot be resolved, and returns 0 if the CSV yields no names.
- Evidence: `src/io_utils.py:466-483`.
- Verify: missing csv → 0, `out_dir/a2i2` unmodified.

---

## 8. Output-format

#### `of-bibtex-field-order-stable` — LB *(consolidates: output-bibtex-field-order-stable, bibtex-field-order-stable, determinism-bibtex-field-order, serializer-field-order-and-amp-escape)*
`bibtex_from_dict` emits fields in a fixed preferred order (`title, author, year, journal, booktitle, howpublished, publisher, volume, number, pages, doi, url, eprint, archiveprefix, primaryclass`) followed by remaining fields in `sorted()` order, with 2-space indent, brace-wrapped values, comma separators, no trailing comma on the last field, `}` terminator, and one terminating newline. Byte-stable for a given field set.
- Evidence: `src/bibtex_utils.py:372-394`.
- Verify: golden test — serialize a fixed entry (keys scrambled) == expected string incl. field order, indent, trailing newline; empty-field entry → `@type{key,\n}\n`.

#### `of-ascii-escape-normalization` — LB *(consolidates: output-ascii-and-escape-normalization, bibtex-ascii-and-escaping-pipeline)*
On serialization, values pass `_normalize_to_ascii` (`html.unescape` → strip LaTeX → strip accents → curly-quote/dash/ellipsis→ASCII); titles additionally run `_sanitize_title` (trailing period trimmed unless ellipsis, duplicated post-colon suffix removed); bare `&`→`\&` in all fields EXCEPT `url` and `doi`.
- Evidence: `src/bibtex_utils.py:302-328,330-367,384-389`.
- Verify: `'AI & Society'`→`'AI \& Society'`; url with `&` unchanged; curly quote/em-dash→ASCII; serialize twice → identical.

#### `of-phase4-type-correction-order` — LB
Phase 4 applies a fixed sequential chain of `@type` reclassification and field-move rules; each observes prior rules' output, so the emitted `@type` and journal/booktitle/howpublished placement depend on this order.
- Evidence: `main.py:2031-2401`.
- Verify: golden-master over preprint article, PACM proceedings, patent, thesis DOI, bare stub — exact emitted `@type` and field placement.

#### `of-invalid-type-downgrade` — LB
By Phase 4 a missing container downgrades the type to valid BibTeX: `@article` without journal → `@misc`; `@inproceedings` without booktitle → `@misc`.
- Evidence: `main.py:2031-2036,2041-2046`.
- Verify: `@article` no journal → `misc`; `@inproceedings` no booktitle → `misc`.

#### `of-final-comma-brace-formatting` — IMP
The serializer strips the trailing comma from the last field line, appends `}`, and terminates with exactly one newline.
- Evidence: `src/bibtex_utils.py:378,391-394`.
- Verify: golden — last field no trailing comma, file ends `}\n`.

#### `of-internal-fields-stripped` — IMP *(consolidates: output-internal-fields-stripped, dedup-internal-fields-stripped ×2)*
Internal bookkeeping fields `DEDUP_INTERNAL_FIELDS` (`x_scholar_cluster_id, x_scholar_citation_id, x_s2_paper_id, x_openalex_id`) plus keywords/copyright are removed by `merge_with_policy` before serialization; `normalize_arxiv_metadata` applied.
- Evidence: `src/config.py:815-820`; `src/merge_utils.py:504-506`.
- Verify: an entry carrying `x_s2_paper_id` → absent in output but external-id dedup functioned earlier.

#### `of-filename-no-numeric-counters` — IMP *(consolidates: output-filename-no-numeric-counters, collision-loop-never-numeric-counter, output-no-numeric-filename-counters)*
Filenames come from `short_filename_for_entry`, resolving collisions by adding MORE title words (never numeric counters); identical-content collisions reuse the existing file. The collision loop breaks (reuses) on byte-identical content / equal DOIs / confirmed dedup / matching keys / title-sim ≥0.95; else logs a warning and returns the existing path with `was_written=False`.
- Evidence: `src/merge_utils.py:940-960,1307-1382`.
- Verify: two different same-key papers → distinct word-extended filenames, no `-2`; identical content → single file reused.

#### `of-citekey-fallback-chain` — IMP
Output citekey = `build_standard_citekey(...)` or existing merged key or literal `'Entry'`, in that precedence.
- Evidence: `main.py:2590-2594`.
- Verify: `build_standard_citekey` None + no key → `'Entry'`; None + existing key → keeps existing.

#### `of-csv-fieldnames-fixed-order` — IMP
The summary CSV schema/column order is the fixed `_SUMMARY_CSV_FIELDNAMES` (`file_path, trust_hits`, then source-flag columns); all writers use this order, flag fields = complement of file_path/trust_hits.
- Evidence: `src/io_utils.py:22-38,297,323,353,432`.
- Verify: import `_SUMMARY_CSV_FIELDNAMES`; pin the exact ordered list.

#### `of-a2i2-byte-fidelity-copy` — IMP
a2i2 survivors are copied as raw source bytes (read then write), NOT re-serialized through `bibtex_from_dict` — byte-identical to the (already canonicalized) author-dir source (hence a2i2 runs AFTER post-run fixup).
- Evidence: `src/io_utils.py:608-613`.
- Verify: a2i2 output byte-equal to its chosen source author-dir file.

#### `of-booktitle-fixups-ordered-idempotent` — IMP
`_apply_booktitle_fixups` runs `_VERBOSE_BOOKTITLE_RE` stripping first, then `_BOOKTITLE_FIXUPS` in declared order (e.g. `'Conference On'`→`'Conference on'` must precede truncation-completion patterns); the combined transform is idempotent.
- Evidence: `main.py:235-243,205,216-219`.
- Verify: `f(x)==f(f(x))` over a corpus including `'Conference On Innovation'`.

#### `of-text-decode-precedence-order` — IMP
`http_get_text` decodes deterministically: BOM sniff (utf-8-sig, utf-16le, utf-16be) → plain utf-8 → latin-1 `errors='replace'` (never-fail fallback); `DECODE_ERRORS` gate each attempt.
- Evidence: `src/http_utils.py:439-462`.
- Verify: parametrized over BOM'd and invalid-utf8 inputs → exact decoded string.

#### `of-baseline-json-shape` — MIN
`baseline.json` = `{"total": sum, "authors": {dir: count}}` where each count is `.bib` files in that dir, dirs iterated in `sorted` order; write failure swallowed (best-effort).
- Evidence: `main.py:3197-3208`.
- Verify: parses to `{total, authors}`, total == sum, author keys sorted; simulated write error → run still returns 0.

#### `of-badges-json-hitrate` — MIN
`badges.json` records cache positive/negative/miss/total and `hit_rate=(positive+negative)/total*100` rounded 1dp (0 when total==0), plus `last_updated=YYYY-MM`; write failure swallowed.
- Evidence: `main.py:3210-3225,3062`.
- Verify: total==0 → hit_rate==0, no exception; known counts → matches rounded formula.

---

## 9. Config-driven

#### `cd-thresholds-centralized` — LB *(consolidates: config-driven-thresholds-centralized, config-driven-thresholds, dedup-thresholds-config-sourced, dedup-threshold-constants, dedup-thresholds-config-driven, no-hardcoded-thresholds-at-fix-sites)*
All tunable thresholds/vocabularies come from `src/config.py` (similarity thresholds, `TRUST_ORDER`, cache TTLs, `MAX_WORKERS`, `MIN_YEAR`, dedup composites, rate limits, `PREPRINT_SERVERS`/`PREPRINT_DOI_PREFIXES`/`DATA_DOI_PREFIXES`/`ACM_JOURNAL_PROCEEDINGS`, `PAGES_MAX_DIGITS`, `TITLE_LENGTH_KEEP_RATIO`, `TRUST_DIFF_OVERRIDE_THRESHOLD`, `CACHE_NEGATIVE_CONFIRM_RUNS`, `SIM_DEDUP_COMPOSITE_THRESHOLD`=0.60, `SIM_DEDUP_MULTI_SIGNAL_MIN`=0.35). `main.py`/`merge_utils.py`/`io_utils.py` import them; the three fix sites reference the same imported tables so they cannot drift. No literals inlined at call sites.
- Evidence: `main.py:46-79,68-75`; `src/merge_utils.py:23-28`; `src/config.py:66,126,133,141,199,205,230,340,811-812,826,829,867-868`; `src/io_utils.py:460`.
- Verify: grep-assert no similarity/worker literals at call sites; monkeypatch a config constant and observe behavior change at both orphan-dedup and a2i2 gates; import-graph test each table defined exactly once in config.py.

#### `cd-file-ge-merge-threshold` — LB *(consolidates: dedup-threshold-file-ge-merge, config-file-ge-merge-threshold)*
`SIM_FILE_DUPLICATE_THRESHOLD` (0.95) must remain ≥ `SIM_MERGE_DUPLICATE_THRESHOLD` (0.95) — file-level dedup never laxer than merge-level.
- Evidence: `src/config.py:93,129-130`.
- Verify: static assert `SIM_FILE_DUPLICATE_THRESHOLD >= SIM_MERGE_DUPLICATE_THRESHOLD`.

#### `cd-min-year-max-pubs` — IMP *(consolidates: config-driven-min-year-and-max-pubs, min-year-config-driven-env)*
`MIN_YEAR` = `CITEFORGE_MIN_YEAR` (default 2020) with a rolling-window fallback when unset (`get_min_year`); `MAX_PUBLICATIONS_PER_AUTHOR` = `PUBLICATIONS_PER_YEAR × CONTRIBUTION_WINDOW_YEARS`. Both env/derived, never fixed literals; the year-window cleanup and a2i2 filter must use `get_min_year()`.
- Evidence: `src/config.py:32-43,52-58`; consumed `main.py:2717,2731,3117`, `src/io_utils.py:494`.
- Verify: `CITEFORGE_MIN_YEAR=2015` → `get_min_year()==2015` and `MAX_PUBLICATIONS_PER_AUTHOR` scales, both cleanup and a2i2 honor it; unset → rolling fallback.

#### `cd-inline-magic-numbers-preserved` — IMP
Several load-bearing inline literals in the dedup cascade are intentional and must be preserved (not naively routed through one shared constant): key+author-overlap gate (`overlap≥0.8 AND key_title_sim≥0.55`), strong-author gate (`sim≥0.6, ≥2 authors each, overlap≥0.9`), prefix-stub `len>20`, `high_author_match` (`overlap≥0.9, title≥0.6, ≥2 authors`), and the `+3` field-advantage margin.
- Evidence: `src/merge_utils.py:1072-1073,1090,1151-1156,1250`; `src/bibtex_utils.py:666-671`.
- Verify: golden boundary tests — `overlap=0.79 vs 0.80` flips `KEY_AUTHOR_OVERLAP`; `existing==new+2 vs new+3` flips `KEEP_EXISTING` vs `REPLACE`.

#### `cd-urllib3-retry-params` — IMP
The urllib3 `Retry` is built from config: `total=HTTP_MAX_RETRIES(2)`, `backoff_factor=HTTP_BACKOFF_INITIAL(0.25)`, `backoff_max=HTTP_BACKOFF_MAX(16.0)`, forcelist derived from `HTTP_RETRY_STATUS_CODES`.
- Evidence: `src/http_utils.py:163-175`; `src/config.py:107-110`.
- Verify: assert `_RETRY_STRATEGY.total==HTTP_MAX_RETRIES and backoff_factor==HTTP_BACKOFF_INITIAL`.

#### `cd-sim-threshold-fp-tolerance` — MIN
Candidate-acceptance similarity comparisons apply `SIM_THRESHOLD_TOLERANCE=0.01` as FP slack (e.g. `SIM_EXACT_PICK_THRESHOLD - tolerance`).
- Evidence: `src/config.py:823,90`; consumed `src/api_generics.py:320`.
- Verify: score == threshold−epsilon → acceptance stable.

---

## 10. Correctness

#### `co-return-code-contract` — LB *(consolidates: api-contract-return-counts, api-contract-return-code)*
`process_article` returns 1 exactly when a file is written/kept and 0 for every skip/failure/dedup/guard path; `process_record` sums these into the per-author count; `main()` aggregates into `total_saved`. The 1/0 int-sum contract is load-bearing for reporting.
- Evidence: `main.py:734-737,762,778,1356,1484,2485,2500,2522,2529,2586,2607,2829-2846,3027`.
- Verify: assert `process_article` returns int ∈ {0,1} across skip and write paths; `process_record` returns the sum.

#### `co-save-return-tuple` — LB
`save_entry_to_file` returns `(path, was_written)`: `was_written` False on SKIP_WRITE, prefer-path-more-complete block, and unresolved filename collision. Callers rely on `path2 != path` (rename) and `was_written` for accounting.
- Evidence: `src/merge_utils.py:934,1305,1382,1451,1504`; consumed `main.py:1463-1470,2609-2614`.
- Verify: each exit returns `(path, bool)`; SKIP_WRITE → False; fresh write → True.

#### `co-value-ok-gate-every-field` — LB
`value_ok(v) = (v is not None) and (not has_placeholder(v))` gates EVERY field on both sides: a placeholder/None incoming value is skipped; a placeholder/None current value is treated as empty (overwritable regardless of trust).
- Evidence: `src/merge_utils.py:251-252,285-295`.
- Verify: enricher `'n/a'` skipped; placeholder current value replaced by a lower-trust real value.

#### `co-doi-normalize-or-drop` — LB
Merged `doi` is normalized via `_norm_doi`; empty result → removed, else replaced with the normalized form before any downstream DOI logic.
- Evidence: `src/merge_utils.py:447-455`.
- Verify: `test_doi_normalization`; `merged['doi'] == _norm_doi` form.

#### `co-doi-conflict-primary-wins` — LB
If the primary had a DOI and the merged DOI differs, the primary DOI is restored and `has_doi_conflict` set — UNLESS a preprint→published upgrade (published kept, no conflict). `has_doi_conflict` also controls whether the trust gate runs.
- Evidence: `src/merge_utils.py:457-477`.
- Verify: primary `10.x/A`, merged `10.y/B` (both published) → restored to A; primary preprint, merged published → published kept.

#### `co-container-enforcement-by-type` — LB
Exactly one venue container per final type (`get_container_field`): article keeps `journal` (booktitle/howpublished popped); inproceedings/incollection keep `booktitle` (journal migrated/popped); all others keep `howpublished`. Last structural step before serialization.
- Evidence: `src/merge_utils.py:879-913`; `src/bibtex_build.py:27-37`.
- Verify: per-type — `@article` has journal + no booktitle/howpublished; `@inproceedings` has booktitle + no journal.

#### `co-type-revalidate-authoritative` — LB
`determine_entry_type` over `{journal,booktitle,howpublished,publisher,pages}` may reclassify to inproceedings/incollection, but an `@article` from an authoritative source (`best_type_src ∈ {csl, doi_bibtex}`) is preserved unless its DOI is secondary; `@book` never downgraded by venue content; `@misc` upgraded via venue hints.
- Evidence: `src/merge_utils.py:821-858`; `src/bibtex_build.py:176-230`.
- Verify: csl `@article` with conference-like journal stays `@article`; `@misc` with only journal → article.

#### `co-year-window-enforced-everywhere` — LB *(consolidates: correctness-year-window-enforced-everywhere, correctness-year-window-guard, a2i2-window-filter-inclusive-both-ends, filename-year-takes-precedence-over-bibyear)*
The `min_year` window is enforced at every checkpoint so no out-of-window `.bib` survives a completed run: at fetch (`scholar_windowed`/DBLP), at baseline load (out-of-window existing file removed), at final save (`0 < final_year < min_year` → rejected+removed, return 0), and in the post-run sweep. The sweep tries the **filename year first** (`_FILENAME_YEAR_RE` on `/{fname}`, always `continue`s when matched); only non-matching filenames fall through to the BibTeX-year fallback (guarded `0 < bib_year < window_min`). The a2i2 filter uses `min_year ≤ year ≤ current_year` (inclusive both ends), skipping unparseable years.
- Evidence: `main.py:2776,1438-1449,2596-2607,3116-3158,3128-3153`; `_FILENAME_YEAR_RE` `main.py:144`; `src/io_utils.py:523-530`; `get_min_year` `src/config.py:38-43`.
- Verify: `Alice2024-X.bib` with bib-year 2000 KEPT (filename wins); non-filename-year file with year 2000 (<min) removed; enrich to corrected 2019 with MIN_YEAR 2020 → rejected+deleted; a2i2 future-year and no-year fixtures excluded.

#### `co-author-attribution-filter` — LB *(consolidates: correctness-author-attribution-filter, correctness-author-contamination-guard)*
After merge, if the author field is non-empty and the target author is not in it, the entry is rejected/removed (return 0) — UNLESS the original on-disk baseline had the correct author (enrichment-corrupted attribution), in which case the original file is kept unchanged.
- Evidence: `main.py:2502-2529`.
- Verify: merged authors lack target → removed; baseline had target → original kept, return 0.

#### `co-pages-validation` — LB
Incoming `pages` accepted only if it starts with a digit, has no dot, and every hyphen/comma-separated component has ≤ `PAGES_MAX_DIGITS`=8 digits; a post-merge cleanup re-runs the rules and strips leading zeros.
- Evidence: `src/merge_utils.py:319-339,533-548`; `src/config.py:340`.
- Verify: `'2025.11.07.685935'` rejected (dot); `'139051234567'` rejected (>8); `'13905-13917'` accepted; `'007-012'`→`'7-12'`.

#### `co-process-validated-doi-append-contract` — LB
`process_validated_doi` appends `('csl',entry)+flags['doi_csl']` and `('doi_bibtex',entry)+flags['doi_bibtex']` **only for matched (non-None) entries**, and returns True iff at least one format matched. CSL is fetched first; BibTeX only if CSL did not match.
- Evidence: `src/doi_utils.py:195-209,46-48,84-89,157-165`.
- Verify: CSL matches → only `('csl',...)` appended, `flags['doi_csl']` True, `fetch_bibtex_via_doi` not called, returns True.

#### `co-gate-block-on-csv-existence` — LB
The entire post-run reconciliation block (flush, reconcile, orphan removal, year-window cleanup, post-run fixup, a2i2 build, baseline.json, badges.json) executes ONLY when `summary_csv_path` is truthy AND `os.path.exists(summary_csv_path)`; otherwise none of it runs.
- Evidence: `main.py:3070`.
- Verify: non-existent CSV → `out_dir/a2i2`, `baseline.json`, `badges.json` NOT created, no side effects.

#### `co-reconciliation-step-ordering` — LB *(consolidates: reconciliation-step-ordering, correctness-postrun-cleanup-ordering)*
Post-run steps run in fixed order: flush CSV → reconcile phantoms → collect orphans → per-orphan duplicate-gated delete → year-window `.bib` removal → post-run `_fixup_bib_entry` over ALL `.bib` → build a2i2 → baseline.json → badges.json. Later steps depend on earlier (collect_orphan_files requires reconcile first; a2i2 copies from already-cleaned+fixed dirs; the a2i2 dir is excluded from per-author sweeps).
- Evidence: `main.py:3071,3074,3079,3099,3116-3117,3160-3182,3190,3197-3208,3210-3225,3121,3166`; `src/io_utils.py:366-368`.
- Verify: integration test over a seeded dir (phantom row + orphan + out-of-window file) → final state matches ordered pipeline; a2i2 files equal post-fixup author-dir bytes; only confirmed-duplicate orphans removed; baseline totals match final `.bib` count.

#### `co-conference-journal-word-boundary` — LB *(consolidates: conference-journal-detection-word-boundary, journals-named-proceedings-word-boundary)*
`_is_conference_journal` reclassifies `@article`→`@inproceedings` when the journal looks like proceedings (contains 'proceedings'/'tagungsband', starts 'conference on', contains '@', or in `CONFERENCE_AS_JOURNAL`) but EXCLUDES real journals via **word-boundary** match against `JOURNALS_NAMED_PROCEEDINGS` (PNAS, PVLDB, Proc. IEEE, Royal Society) — prefix followed by end/space/comma/period/semicolon/colon, NOT bare substring (so "proceedings of the ieee/cvf winter conference" ≠ journal "proceedings of the ieee").
- Evidence: `src/merge_utils.py:117-151,757-765`; `src/config.py:167-171,209-214`.
- Verify: `'Proceedings of the National Academy of Sciences'` stays `@article`; `'Proceedings of the 2024 Conference on X'` → `@inproceedings`; `'Proceedings of the IEEE/CVF Winter Conference ...'` → True (conference) while `'Proceedings of the IEEE'` → False.

#### `co-doi-validation-csl-first` — IMP
DOI validation fetches CSL-JSON first and only fetches BibTeX when CSL did not match.
- Evidence: `src/doi_utils.py:157-165,148`.
- Verify: mock CSL match → `fetch_bibtex_via_doi` not called, only `('csl',...)` appended.

#### `co-summary-csv-cwd-relative-paths` — IMP *(consolidates: correctness-summary-csv-cwd-relative-paths, reconcile-uses-raw-relative-path-cwd)*
Summary CSV `file_path` entries are stored CWD-relative via `os.path.relpath`; `reconcile_summary_csv` checks `os.path.exists(fp)` on the RAW relative path, so it is correct only when CWD == project root (same CWD as when rows were written). `is_known_summary_path` dedup and phantom cleanup operate on these relative paths; `collect_orphan_files`/`_load_csv_titles` DO `os.path.abspath`.
- Evidence: `main.py:2617,1349-1355,1477-1483,3070-3114,2870-2871`; `src/io_utils.py:412-423`; contrast abspath `src/io_utils.py:376`, `main.py:2870`.
- Verify: append rows from CWD A, reconcile from CWD A → stable; `is_known_summary_path` matches on the relative form; document the CWD precondition.

#### `co-pnas-suffix-conference-guard` — IMP *(consolidates: pnas-pvldb-suffix-conference-guard, jnp-suffix-conference-guard)*
In the inproceedings→article reclassification for `JOURNALS_NAMED_PROCEEDINGS`, conversion is skipped when the booktitle suffix after the matched journal name contains 'conference', 'workshop', or 'symposium'. Present and identical at all three fix sites.
- Evidence: `main.py:342-350,1113-1124,2110-2121`.
- Verify: `test_jnp_suffix_guard_ieee_conference_stays_inproceedings`, `test_jnp_suffix_guard_pnas_becomes_article`; bare 'Proceedings of the VLDB Endowment' → article, '... Workshop on X' → stays inproceedings.

#### `co-filesystem-is-state` — IMP
When `SKIP_SCHOLAR_FOR_EXISTING_FILES` is True, an existing `.bib` whose title matches (sim ≥ `SIM_MERGE_DUPLICATE_THRESHOLD`) is loaded as the enrichment baseline and the Scholar-page fetch is skipped; scheduling (`count_existing_papers`/`_has_output`) and the summary CSV also read disk state.
- Evidence: `src/config.py:61`; `main.py:815-858,1538-1539,2852-2860,2971-2973`.
- Verify: seed a matching `.bib` → used as baseline, no Scholar citation fetch; `count_existing_papers` reflects the file.

#### `co-force-enrich-flag` — IMP
`FORCE_ENRICH` is derived once from `'--force' in sys.argv` and gates the "entry already complete → skip enrichment" shortcut; when set, complete entries are re-enriched.
- Evidence: `main.py:117,1312`.
- Verify: complete on-disk entry, `FORCE_ENRICH` False → skips (returns 1, no API calls); True → runs enrichment.

#### `co-phase25-gating` — IMP
Phase 2.5 executes only when `enr_list` is empty after Phase 2; its Tier-1 OpenAlex sub-search runs only if still empty after Crossref; it injects arXiv-id/DOI fragments to enable Phase 3 discovery.
- Evidence: `main.py:1728,1740-1745,1748-1752,1755,1782`.
- Verify: with a P2 match, P2.5 skipped; with no match + arXiv-bearing pub string → `bf['eprint']` set, arXiv DOI added to `all_candidate_dois`.

#### `co-title-is-venue-and-book-skip` — IMP
Entries whose title equals the journal or booktitle (corrupted Scholar data), and entries typed `@book` (proceedings volumes/edited books), are skipped and their file removed (return 0).
- Evidence: `main.py:2471-2485,2487-2500`.
- Verify: title==journal entry and `@book` entry each return 0 and remove any created file.

#### `co-misc-upgrade-preprint-repo-guard` — IMP
The Phase-4 misc→inproceedings upgrade is blocked when `howpublished` names a preprint server (`PREPRINT_SERVERS` + inline list) or a repository (`REPOSITORY_AS_JOURNAL`); only genuine venue howpublished values upgrade.
- Evidence: `main.py:2385-2401`; `src/config.py:133,175-187`.
- Verify: `howpublished='arXiv'` stays `@misc`; `'NeurIPS Workshop on X'` → `@inproceedings`.

#### `co-dagstuhl-doi-resolution` — IMP *(consolidates: dagstuhl-doi-resolution, dagstuhl-lipics-doi-regex)*
A DOI matching `^10.4230/(lipics|oasics).<conf>.<year>[.<paper>]$` (anchored, case-insensitive) forces `@inproceedings`, sets booktitle from `ABBREVIATED_VENUE_MAP[conf]` (else old journal), drops journal+howpublished.
- Evidence: `src/merge_utils.py:52-55,767-803,774`; `src/config.py:289-337,346-358`.
- Verify: `'10.4230/LIPIcs.ESA.2023.5'` → inproceedings, booktitle 'European Symposium on Algorithms', no journal; reject near-miss prefixes.

#### `co-venue-abbrev-expansion` — IMP
For journal/booktitle/howpublished, a value equal (case-insensitive) to an `ABBREVIATED_VENUE_MAP` key is expanded; a match in the journal field is moved to booktitle (journal popped), since all mapped venues are conferences.
- Evidence: `src/merge_utils.py:809-819`.
- Verify: `journal='SPIRE'` → booktitle 'String Processing and Information Retrieval', journal absent.

#### `co-three-casing-engines` — IMP
Title, venue, and author casing are three distinct engines that must remain separate: title ALL-CAPS via `_fix_allcaps_title` (gated at >60% uppercase), venue via `VENUE_CASE_CORRECTIONS` exact-match dict (Phase 4), author via `_fix_author_casing`.
- Evidence: `src/text_utils.py:173-200,203-227`; `config.py:251-256`, `main.py:2301-2309`; `src/merge_utils.py:166-204`.
- Verify: `_fix_allcaps_title` leaves normal mixed-case unchanged and only fires >60% caps; author/venue paths untouched by title logic.

#### `co-cross-file-key-collision-disambiguation` — IMP
Before writing, if another `.bib` in the dir holds the same citekey on a genuinely different paper (different normalized DOI), the new key gets a distinguishing suffix (first significant title word not already in the key, else `'B'`); same-paper key collisions are left intact.
- Evidence: `src/merge_utils.py:1457-1489`.
- Verify: two different-DOI papers colliding on `'Smith2024'` → second becomes `'Smith2024<Word>'` deterministically; same-DOI collision keeps key.

#### `co-doi-revert-restores-validated` — IMP
`_revert_misattributed_doi` only acts when `merged_fields['doi']==bad_doi`, replacing it with the P1-validated `doi_early` (when `doi_validated`) or else removing doi AND url; never leaves a mis-attributed candidate DOI in place.
- Evidence: `main.py:634-655`.
- Verify: bad_doi + validated fallback → doi replaced with normalized doi_early, url popped; no fallback → doi and url removed.

#### `co-year-and-fixup-skip-a2i2-dir` — IMP
Year-window cleanup and post-run fixup skip the `a2i2` entry (and non-directories), so the joint folder is never cleaned/fixed in place; a2i2 is fully rebuilt afterward.
- Evidence: `main.py:3121,3166,3190`.
- Verify: a stale file under `out_dir/a2i2` untouched by year-window/fixup passes (only `build_a2i2_folder` wipes it).

---

## CONTRADICTIONS / CONFLICTS — Defect Seams

These are the seams where fixing a known defect risks violating an invariant. Each lists the defect, the invariant(s) in tension, and the safe resolution.

### C1 — Compounding retry (429/503 double-backoff)
- **Defect**: request amplification risk if 429/503 are handled at both urllib3 and manual layers; POST retried non-idempotently.
- **Invariants in tension**: `ao-429-503-excluded-from-urllib3-forcelist`, `correctness-urllib3-retry-after-disabled` (Correctness/§10 via `eh-manual-retry-loop`), `correctness-retry-after-capped-at-backoff-max`, `co-sleeps-outside-global-semaphore`, `eh-defect-post-retried-non-idempotent`.
- **Seam**: A refactor "unifying" retry logic must NOT re-add 429/503 to `status_forcelist`, must keep `respect_retry_after_header=False`, must keep the `min(rate_wait, HTTP_BACKOFF_MAX)` cap, and must keep every `time.sleep` outside the semaphore. Any consolidation that moves sleeps inside the semaphore reintroduces slot-starvation (`co-sleeps-outside-global-semaphore`). Deciding to stop retrying POST is a behavior change (`eh-defect-post-retried-non-idempotent`) that must be conscious.

### C2 — Uncaught ValueError from `_decode_json_bytes` + Gemini API-key log leak
- **Defect**: malformed JSON raises a bare `ValueError` carrying the full URL; DataCite/ORCID let it escape; the Gemini caller catches and WARN-logs it, leaking `?key=<secret>`.
- **Invariants in tension**: `eh-error-tuple-membership-frozen`, `eh-handle-api-errors-scope`, `eh-decode-json-valueerror-with-url`, `eh-gemini-key-leak`, `eh-datacite-orcid-valueerror-escapes`.
- **Seam / DIRECT CONFLICT**: The "obvious" fix — add `ValueError` to `ALL_API_ERRORS`/`NETWORK_ERRORS` — **violates `eh-error-tuple-membership-frozen`** and silently swallows genuine decode failures across all decorated clients (changing DataCite/ORCID/Gemini result semantics to `default_return`/negative-cache). The correct fix is to **redact the URL in the `ValueError` message at `src/http_utils.py:399`** (and/or strip `key=`/tokens before logging), leaving tuple membership and propagation behavior unchanged. This resolves the key leak (`eh-gemini-key-leak`) without touching the frozen tuples.

### C3 — `ttl_days` vestigial vs monthly-boundary freshness
- **Defect**: `ttl_days` is written to every cache entry and accepted as an arg but never read by `get()`.
- **Invariants in tension**: `ca-positive-freshness-not-ttl`, `ca-monthly-boundary-expiry`, `ca-negative-three-tier`.
- **Seam / DIRECT CONFLICT**: "Restoring" ttl_days honoring in `get()` **violates `ca-positive-freshness-not-ttl`** and silently changes effective lifetimes for every namespace (DOI 90d, search 60d, gemini 365d), altering hit rates and API volume. Safe options: leave as-is (documented vestigial) OR remove the write path — but NOT wire it into expiry without an explicit, tested semantics change.

### C4 — Frozen `_month_boundary` at singleton init
- **Defect**: `_month_boundary` is computed once at import and never advances; a process spanning a month rollover keeps serving last-month entries as fresh.
- **Invariants in tension**: `ca-month-boundary-frozen` (documents current behavior), `ca-monthly-boundary-expiry` (assumes an active boundary).
- **Seam**: Fixing this (recompute per `get()`) changes month-refresh timing and will break any test that observes the frozen value (`test_month_boundary_frozen`). Because CiteForge runs are short-lived batch processes, the practical impact is low; if fixed, update `ca-month-boundary-frozen`'s verification and confirm the once-per-month refresh contract (`ca-monthly-boundary-expiry`) still holds mid-run. Do not "fix" by further hard-freezing (e.g. caching across process restarts).

### C5 — Phase-4 type-reclassification asymmetry (parity vs superset)
- **Defect / prior-digest error**: the prior study claimed Phase 4 (site C) OMITS the article/inproceedings reclassifications; reader 6 found C is a SUPERSET (it contains the core set PLUS post-enrichment-only rules).
- **Invariants in tension**: `ao-three-way-fixup-parity` (all three sites must apply the CORE rules identically) vs `ao-phase4-superset` (C legitimately has MORE rules, including the only misc→inproceedings upgrade).
- **Seam / RESOLUTION**: These are reconcilable but easy to break. The **core reclassification + text + booktitle rules must be byte-identical across A/B/C**; C's extras (patent→misc, unpublished→misc, url-booktitle→misc, article-preprint-DOI, misc→inproceedings upgrade) must remain **C-only**. A naive consolidation that makes all three sites identical will either drop C's extras or push the misc→inproceedings upgrade into A/B (where `howpublished` is still transient/pre-enrichment), promoting entries incorrectly. Any refactor here must run the reclassification-parity test AND assert the upgrade appears only in the Phase-4 path.

### C6 — OpenReview lock-free read (unguarded concurrency surface)
- **Defect**: an OpenReview client read is reported to bypass the lock discipline.
- **Invariants in tension**: `co-shared-state-locks` (all shared mutable state lock-guarded), `co-rate-limiter-registry-singleton` (double-checked locking), `co-single-writer-per-author-dir` (partitioning, not locking, protects author dirs).
- **Seam / GAP**: The 7 invariant sets contain **no OpenReview-specific invariant**, so this is an unguarded surface — the general lock discipline (`co-shared-state-locks`) is the only governing contract. OpenReview is also a Phase-2 source (`determinism-phase2-source-order` places it 4th: Scholar→S2→Crossref→**OpenReview**→...). Fixing the lock-free read must NOT (a) alter the per-namespace single-instance lock semantics of the cache, nor (b) change OpenReview's position in the Phase-2 order (which would violate `determinism-phase2-source-order` and shift `enr_list` fill). Add a targeted concurrency test for the OpenReview path as part of the fix.

### C7 — Dead `scholarly_scholar.py` (safe-delete candidate)
- **Defect**: `scholarly_scholar.py` is reported dead code.
- **Invariants in tension**: none directly — no invariant references it. Adjacent live contracts: `eh-scholar-retry-then-dblp-only`, `determinism-article-ordering` (`src/clients/scholar.py:169-178`), `determinism-phase2-source-order` (Scholar first).
- **Seam**: Removal is safe **only if** no import path reaches it. Before deleting, grep for imports/references and confirm the live Scholar path (`src/clients/scholar.py`) is untouched. This is a code-hygiene change with no invariant impact once import-freeness is verified — but verify, do not assume.

### C8 — Author-dir concurrency: locks vs partitioning (design fragility, not a live bug)
- **Tension**: `co-shared-state-locks` guards cache/CSV with explicit locks, while `co-single-writer-per-author-dir` explicitly relies on **no lock** and the ThreadPool partitioning (one worker per author). These are not contradictory today, but the asymmetry is a latent hazard.
- **Seam**: Any refactor that parallelizes within an author, or that maps two records to the same `format_author_dirname`, breaks the unstated single-writer precondition and produces TOCTOU races (duplicate/lost/deleted `.bib`). If such parallelism is introduced, a per-author-dir lock MUST be added.

### C9 — Documented cross-reader factual disagreement: Phase-2 source order
- Reader 2 (`determinism-phase2-source-order`) **explicitly corrects** the prior-study digest's claimed order. The authoritative order is **Scholar→S2→Crossref→OpenReview→arXiv→OpenAlex→PubMed→EuropePMC** (evidence `main.py:1543,1572,1601,1621,1640,1660,1687,1706`). Any design doc or test still asserting the old order (Scholar→S2→Crossref→OpenAlex→PubMed→EuropePMC→arXiv→OpenReview) is wrong and must be updated.

---

## DETERMINISM-CRITICAL SURFACES — Byte-Identity Contract

A refactor must keep the following code paths byte-for-byte stable. The master verification for all of them is a **two-run diff**: run the pipeline twice over a frozen fixture corpus from a stable CWD (project root), then `diff -r` / `git diff --exit-code` the `output/` tree — it must be empty. Supplement with `PYTHONHASHSEED` variation and freeze-time where noted.

| # | Surface | Files / lines | Governing invariants | Extra verification |
|---|---------|---------------|----------------------|--------------------|
| 1 | **BibTeX serialization** (field order, ASCII normalization, `\&` escaping excl. url/doi, trailing comma/brace/newline) | `src/bibtex_utils.py:302-394` | `of-bibtex-field-order-stable`, `of-ascii-escape-normalization`, `of-final-comma-brace-formatting`, `ao-ascii-clean-table-values` | Golden string test on a fixed entry with scrambled key order |
| 2 | **Three fixup sites + shared helpers** (must reach one fixed point) | A `main.py:314-492`; B `main.py:865-1309`; C `main.py:2028-2401`; helpers `main.py:235-254`; `_fixup_bib_entry` `src/merge_utils.py:314-565` | `ao-three-way-fixup-parity`, `ao-phase4-superset`, `ao-is-proc-series-guard-frontiers`, `ao-is-pacm-guard`, `co-pnas-suffix-conference-guard`, `co-conference-journal-word-boundary` | Feed one malformed entry through each site → identical; grep exactly 3 call sites each |
| 3 | **Fix-table iteration order** (dict/tuple/list, hash-seed independent) | `main.py:123-232`; sources `config.py:379-808` | `determinism-pattern-iteration-order`, `ao-fused-compounds-three-pass-order`, `of-booktitle-fixups-ordered-idempotent` | Run under two `PYTHONHASHSEED` values → identical output |
| 4 | **Publication ordering & union** | `src/clients/scholar.py:169-178,188-192,234-265` | `determinism-article-ordering`, `dd-merge-union-primary-first` | Shuffle-invariance test |
| 5 | **Author scheduling sort** | `main.py:2947-2949,2971-2977` | `determinism-author-sort-key`, `determinism-new-author-first-stable` | Golden order across two calls |
| 6 | **`sorted()` .bib directory scans** | `main.py:817,3164,3168`; `src/merge_utils.py:953` | `determinism-sorted-bib-scan`, `dd-branch-order-first-match` | Seed `a.bib`/`z.bib` both matching → `a.bib` chosen |
| 7 | **DOI candidate ordering** (set-dedup + stable published-first partition, cache-only inference) | `main.py:1945-1949,1901-1943,1994-1996` | `determinism-doi-candidate-order` | Two runs under differing `PYTHONHASHSEED` → identical `.bib`; assert no live HTTP |
| 8 | **Dedup scoring & normalization** | `src/text_utils.py:511-546,130-155,381-393` | `dd-composite-weights`, `determinism-title-similarity-pure` | Exact-score parametrized tests; `title_similarity` snapshot |
| 9 | **Merge iteration** (insertion-order enr_list, strict-`<` trust) | `src/merge_utils.py:277-445` | `determinism-enr-list-order`, `to-canonical-order-strict-rank` | Shuffle equal-rank enrichers → unchanged output |
| 10 | **Phase-2 source query order** | `main.py:1543-1723` | `determinism-phase2-source-order` (⚠ see C9) | Assert `SEARCH_START` log sequence |
| 11 | **a2i2 build** (wipe+rebuild, DOI-then-title dedup, richer/lower-path tiebreak, sorted write order + `_2` collision, byte-copy) | `src/io_utils.py:466-615` | `determinism-a2i2-complete-rebuild`, `dd-a2i2-doi-before-title`, `determinism-a2i2-pick-richer-tiebreak`, `determinism-a2i2-write-order-collision`, `of-a2i2-byte-fidelity-copy` | `test_complete_rebuild`, `test_deterministic_output` |
| 12 | **Post-run reconciliation block** (fixed step order, rewrite-only-on-change, relative-path reconcile) | `main.py:3070-3227`; `src/io_utils.py:334-439` | `co-gate-block-on-csv-existence`, `co-reconciliation-step-ordering`, `determinism-reconcile-rewrite-only-when-phantoms`, `determinism-flush-rewrite-only-on-updates`, `determinism-orphan-abspath-resolution`, `co-summary-csv-cwd-relative-paths` | Run reconcile from project-root CWD; assert no spurious rewrites |
| 13 | **Year-window enforcement** (filename-year-first, bib-year lower-bound guard, inclusive a2i2 filter) | `main.py:2596-2607,3116-3158`; `src/io_utils.py:523-530` | `co-year-window-enforced-everywhere`, `eh-bibyear-fallback-lower-bound-guard` | `Alice2024-X.bib` w/ bib-year 2000 kept; freeze-time near year boundary |

**Non-determinism landmines to reject in review:** any `set`/`frozenset` feeding a fix-pattern loop or an output-affecting order (#3, #7); any raw `os.listdir` over a `.bib` dir without `sorted()` (#6); numeric filename counters (`-2.bib`) instead of title-word extension (`of-filename-no-numeric-counters`); `dict`/insertion-order reliance in `bibtex_from_dict` field emission (#1); any inlined threshold literal that forks from `config.py` (`cd-thresholds-centralized`); running from a non-root CWD (#12, breaks `co-summary-csv-cwd-relative-paths`).
