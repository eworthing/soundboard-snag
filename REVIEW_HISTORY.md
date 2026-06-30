--- Loop 1 (UTC 2026-06-30T13:35:13Z) ---

<!-- loop_cap: 10 -->

### Discovery (first loop only)
- Source roots: `soundboard-snag.py`, `debug_track_dates.py`
- Test command: `python3 -m py_compile soundboard-snag.py`
- Build command: `python3 -m py_compile soundboard-snag.py`
- ADRs found: none
- Domain terms (CONTEXT.md): none
- Selected lens: Generic (Python) + Security (always-included)
- Provider: claude_code. **Loop body runs inline in the main agent (Opus 4.8)** for analysis quality; the Step-3 implementation reviewer and any HALT_SUCCESS challenger are spawned as **independent subagents**, so both independence-critical gates are preserved. `spawn_isolation: inline` is recorded honestly as a deliberate choice.
- Branch: work is committed on `contest-refactor` (base `765b17b`), leaving `main` untouched.

### Loop Counter
Loop 1 of 10 (cap)

### System Flag
[STATE: CONTINUE]

---

## Contest Verdict
**Functionally solid, but structurally compromised.**

The tool works correctly and the code is honest — no fake architecture, no misleading abstractions — but its central data carrier is an anonymous 11-field positional tuple, its search engine is a single ~720-line function that fuses transport, parsing, filtering, date inference and terminal rendering, and there are zero automated tests. Ownership is clean and the program is trivially concurrency-safe (synchronous), so the weaknesses are structural (domain modeling, decomposition, regression resistance), not correctness or safety.

## Scorecard (1-10)
Format: `[Score] | [Delta] | [Proof]`.

- Architecture quality: **5.5** | SAME | search_boards fuses transport (787,855) + parse (860-903) + filter (1046-1100) + dates (962-1044) + render (1105-1141,1268-1334)
- State management and runtime ownership: **7.5** | SAME | single-threaded; SoundboardSnag attrs immutable post-`__init__` (290-293); one writer per search_boards local
- Domain modeling: **4.0** | SAME | no domain types pre-refactor — anonymous 11-tuple (1145), bare 2-tuples, stringly-typed `approx_source`
- Data flow and dependency design: **6.0** | SAME | positional 11-tuple = implicit unenforced contract across 3 read sites; main re-filters (1525) duplicating 1181
- Framework / platform best practices: **7.0** | SAME | idiomatic stdlib; `_sanitize_filename` (383-441) genuinely defensive; HTTPS; no secrets
- Concurrency and runtime safety: **9.5** | SAME | no async/threading — synchronous CLI, no shared-mutable hazard. *Accepted residual:* `time.sleep` pacing (595,1156,1176) is intentional etiquette, not a determinism hazard; permanent design carve-out.
- Code simplicity and clarity: **4.5** | SAME | ~720-line function (612-1334) ~6 levels deep; render duplicated (1121-1136≈1286-1301); dead wrapper (223-226)
- Test strategy and regression resistance: **2.5** | SAME | zero tests; branchy helpers untested; suffix-flip mutation at 118-121 uncaught
- Overall implementation credibility: **5.5** | SAME | honest code, but 11-tuple + 720-line engine + no tests undercut confidence in source-order reading

## Authority Map
- **Single-board download pipeline** — Owner: `SoundboardSnag` instance · Writers: `__init__` only · Readers: `snag`/`_snag_sound`/`_board_url` · Persistence: filesystem · Async: none · Verdict: **Single and clear**
- **Search aggregation state** — Owner: `search_boards` body · Writers: the single-threaded loop · Readers: render block + `main` · Persistence: none · Async: none · Verdict: **Single and clear** (concern is *concentration*, not ambiguity → see F2)
- **Last-Modified cache** — Owner: `last_modified_cache` closure dict · Writers: `fetch_last_modified_cached` · Verdict: **Single and clear**

## Strengths That Matter
- `_sanitize_filename` (383-441) is genuinely defensive against untrusted header/title input: HTML-entity decode, UUID strip, path-traversal neutralization (`/`,`\` → `-`), control-char removal, Windows reserved-name rewrite.
- Honest synchronous design — zero shared-concurrency hazards because there is no concurrency; `REQUEST_DELAY` pacing is deliberate server etiquette.
- Zero third-party dependencies held to genuinely (stdlib-only imports, 60-72), matching the project's stated constraint.

## Findings

### Finding F1: search_boards returns an anonymous 11-field positional tuple
**Why it matters** — The search engine's primary record forces every caller to know index 8 = views_int, 9 = approx_updated; reordering breaks all callers silently (a hazard CLAUDE.md itself warns about).
**What is wrong** — Bare 11-tuples read by position (`r[1]`, `x[8]`, `x[9]`, `results[0][1]`) and by an 11-name destructure in two render loops; field meaning lives only in a docstring.
**Evidence** — `soundboard-snag.py:1145,1181,1186,1189,1272,1533`.
**Architectural test failed** — Shallow module. **Dependency category** — `in-process`.
**Leverage impact** — Callers learn the positional layout of 11 fields. **Locality impact** — A reorder forces coordinated edits with no compiler/test to catch a miss.
**Why this weakens submission** — Anonymous positional tuples as the central domain record is the textbook weak-domain-model smell.
**Severity** — Serious deduction. **ADR conflicts** — none.
**Minimal correction path** — Introduce a `BoardResult` `typing.NamedTuple` (3.6+; still a tuple at runtime → behavior-preserving) and convert all construction + read sites to named fields.
**Blast radius** — change `soundboard-snag.py`; avoid the download pipeline / network code / `debug_track_dates.py`.

### Finding F2: search_boards is a ~720-line function fusing transport, parsing, filtering, dates and rendering
**Why it matters** — The core scraping logic is untestable and unreusable because it is welded to network I/O and printing.
**What is wrong** — One function (612-1334) does HTTP fetch + regex extraction + filtering + date inference + sorting + all rendering inside one deeply nested loop.
**Evidence** — `soundboard-snag.py:612-1334`, parse `850-942`, dates `962-1044`, render `1105-1141`.
**Architectural test failed** — Shallow module. **Dependency category** — `in-process`.
**Severity** — Serious deduction. **ADR conflicts** — none.
**Minimal correction path** — Extract pure per-board `html -> BoardResult` parsing to a module-level function; leave transport + rendering in `search_boards`. Friction proven: no parse test can exist today. Multi-loop; no class hierarchy.
**Blast radius** — change `soundboard-snag.py`; avoid `SoundboardSnag`, `main` argparse.

### Finding F3: Zero automated tests; deterministic helpers untested; hyphenated module name blocks import
**Why it matters** — Branchy pure logic (sanitization, views/date parsing) can regress silently with no signal, and the file name obstructs even writing a test.
**What is wrong** — No test files; `_sanitize_filename`, `_parse_views_count`, `_parse_http_datetime` untested; `soundboard-snag.py` (hyphen) is not importable without `importlib`.
**Evidence** — no `test_*.py`; `soundboard-snag.py:383-441,94-123`; hyphenated filename.
**Architectural test failed** — n/a. **Severity** — Serious deduction. **ADR conflicts** — none.
**Minimal correction path** — Add `test_soundboard_snag.py` (stdlib `unittest`, load via `importlib.util.spec_from_file_location`); table-driven cases for the pure helpers.
**Blast radius** — add `test_soundboard_snag.py`; avoid production logic.

### Finding F4: Dead pass-through wrapper `_fetch_last_modified`
**Why it matters** — Unused indirection adds reading cost and a second name for one behavior.
**What is wrong** — `_fetch_last_modified` (223-226) wraps `_fetch_last_modified_detailed` and drops the diagnostic, but nothing calls it.
**Evidence** — `soundboard-snag.py:223-226`; grep: zero internal callers.
**Architectural test failed** — Deletion test. **Dependency category** — `in-process`. **Severity** — Cosmetic for contest. **ADR conflicts** — none.
**Minimal correction path** — Delete it.
**Blast radius** — change `soundboard-snag.py`; avoid the `_detailed`/cached path.

### Finding F5: Date-display and skipped-breakdown rendering duplicated near-verbatim
**Why it matters** — Two copies of the same presentation logic drift apart.
**What is wrong** — The approx-updated block and the skipped-buckets breakdown each appear twice with near-identical bodies.
**Evidence** — `soundboard-snag.py:1121-1136`≈`1286-1301`; `1204-1214`≈`1316-1326`.
**Architectural test failed** — Deletion test. **Dependency category** — `in-process`. **Severity** — Noticeable weakness. **ADR conflicts** — none.
**Minimal correction path** — When F2 separates rendering, extract the two blocks into small format helpers taking a `BoardResult` + the date-stats map. Do not extract before rendering is separated.
**Blast radius** — change `soundboard-snag.py`; avoid the search/transport loop.

## Simplification Check
- Structurally necessary: F1 `BoardResult` — Shallow-module test now passes (named interface, not positions). Behavior-preserving.
- New seam justified: no.
- Helpful simplification: F4 (delete dead wrapper), F5 (de-dup render) are subtractive follow-ups.
- Should NOT be done: split file into a package, add a parser class hierarchy, or add an HTTP port/adapter — the single-file zero-dependency design does not warrant it and network-seam friction is not proven.
- Tests after fix: BoardResult index+attr compatibility + sort-key usage smoke-verified this loop; dedicated helper tests land in F3 next.

## Improvement Backlog
1. **Replace the 11-field positional tuple with `BoardResult` (F1)** — structural, needed for winning. Biggest readability/robustness gain; removes the silent-reorder hazard. (domain_modeling/data_flow/simplicity/credibility +)
2. **Add a stdlib `unittest` suite for the pure helpers (F3)** — structural, needed for winning. Test strategy is the binding constraint. (test_strategy + largest lever)
3. **Extract pure per-board HTML parsing from search_boards (F2)** — structural, needed for winning. Makes the core logic testable; shrinks the god-function. (architecture/simplicity/test_strategy +)

## Deepening Candidates
- **Per-board HTML parsing** (friction proven in F2): no parsing interface exists. Move regex extraction of name/downloads/ids/desc/category/views/tags behind `_parse_board_html(html, board_name) -> BoardResult`. Dependency category `in-process`. Test surface: fixture-based parse tests, no network. First step: extract the function and call it from the loop. Do not build a parser class hierarchy.

## Builder Notes
1. **Anonymous positional tuple as a domain record** — recognize by `r[8]`/`x[9]` or long destructures with meaning in a docstring. Rule: 3+ heterogeneous fields read at >1 site → make it a NamedTuple (index-compatible, free migration).
2. **God-function fusing transport + computation + presentation** — recognize when one function owns 15+ mutable locals, hits the network, and prints, and no slice is unit-testable. Rule: pull pure parse/filter/format out as data->data functions; let the I/O function orchestrate.
3. **Pass-through wrapper with no caller** — recognize a thin delegating function that grep shows is uncalled. Rule: run the deletion test before keeping any wrapper.

## Final Judge Narrative
Place — functionally solid but structurally compromised, and the gap is entirely structural, not correctness or safety. Ownership is trustworthy and concurrency is trustworthy because there is none. The three deductions that matter: the anonymous 11-tuple record (addressed this loop), the 720-line engine fusing I/O with logic, and the absence of tests. This loop's simplification (named `BoardResult`) helped and added no ceremony. The dominant future risk is over-reaching toward a package split or HTTP seam the single-file zero-dependency design does not warrant; future work should stay subtractive and add tests, not layers.

## Loop 1 Result
Introduced a `BoardResult` `typing.NamedTuple` in `soundboard-snag.py` and converted `search_boards`' result construction plus every read site — the `has_downloads` filter, both sort keys (`views_int`, `approx_updated`), the results-render loop, the download-suggestion line, and `main`'s search-and-download loop — from positional tuple access to named-field access. `python3 -m py_compile` passes; an `importlib` smoke test confirms `BoardResult` is index-compatible (`br[1] is br.has_downloads`), attribute access works, and the sort keys still order correctly; `--help` exits 0; grep confirms no positional access of search results remains. Targeted finding **F1 is resolved**. No unintended scorecard regression observed (the change is behavior-preserving and subtractive in cognitive load).

## Loop 1 Implementation Review
Independent reviewer (Sonnet, read-only, fresh-eyes on `git diff HEAD`): **approved**. Reality passed (no positional access of search results remains), Honesty passed (`BoardResult` NamedTuple is behavior-preserving, constructed by keyword so no field transposition, no costume layer), Regression passed (no same-or-higher-severity finding introduced). 0 regressions, 0 conditions, 1 round.


--- Loop 2 (UTC 2026-06-30T13:43:20Z) ---

<!-- loop_cap: 10 -->

### Loop Counter
Loop 2 of 10 (cap)

### System Flag
[STATE: CONTINUE]

(Discovery + Authority Map are first-loop-only; unchanged — see REVIEW_HISTORY.md loop 1. Provider claude_code; loop body inline in main (Opus); reviewer + challenger spawned independently. Branch `contest-refactor`, base for this loop `afb1ab6`.)

---

## Contest Verdict
**Functionally solid, but structurally compromised.**

After loop 1 the central record is a named `BoardResult`, which lifts domain modeling and data flow. The remaining structural gaps are the ~734-line `search_boards` function that still fuses transport, parsing, filtering, dates and rendering, and (at the start of this loop) the absence of any tests. This loop adds the missing test surface for the pure helpers.

## Scorecard (1-10)
- Architecture quality: **5.5** | SAME | `search_boards` still fuses concerns (635-1369)
- State management: **7.5** | SAME | one writer per concern; immutable instance attrs
- Domain modeling: **6.0** | UP | `BoardResult` NamedTuple (89-110, commit afb1ab6) names the central record
- Data flow: **7.0** | UP | positional 11-tuple contract removed; residual: `main` re-filters (~1560)
- Framework / platform: **7.0** | SAME | idiomatic stdlib; defensive sanitization; HTTPS; no secrets
- Concurrency: **9.5** | SAME | synchronous, no shared-mutable hazard. *Accepted residual:* `time.sleep` pacing is etiquette, not a determinism hazard (permanent carve-out)
- Code simplicity: **5.0** | UP | named reads removed positional opacity; god-function (635-1369) + dup render (1159≈1336; 1249≈1361) + dead wrapper (246) remain
- Test strategy: **2.5** | SAME | zero tests at loop-2 start (fixed this loop; scored next loop)
- Overall credibility: **6.0** | UP | `BoardResult` makes output self-describing; code stays honest

## Strengths That Matter
- Loop 1's `BoardResult` is a genuine deepening — names 11 fields with zero behavioral change.
- Filename sanitization remains a real defensive strength against untrusted input.
- Synchronous design keeps the concurrency surface trivially safe.

## Findings

### Finding F1 (stable F-003): Zero automated tests — *Priority 1, fixed this loop*
**Why it matters** — Branchy pure logic can regress silently; the hyphenated file name obstructs even writing a test.
**Evidence** — no `test_*.py` at loop-2 start; `_sanitize_filename`/`_parse_views_count`; hyphenated filename. **Test failed** — n/a. **Severity** — Serious deduction.
**Minimal correction path** — `test_soundboard_snag.py` (stdlib `unittest`, `importlib` load); table-driven helper cases.

### Finding F2 (stable F-002): ~734-line `search_boards` fuses transport, parsing, filtering, dates, rendering
**Evidence** — `soundboard-snag.py:635-1369`. **Test failed** — Shallow module. **Dependency** — `in-process`. **Severity** — Serious deduction.
**Minimal correction path** — extract pure `html -> BoardResult` parse to a module-level function (now de-risked by the test suite). Multi-loop; no class hierarchy.

### Finding F3 (stable F-004): Dead pass-through wrapper `_fetch_last_modified`
**Evidence** — `soundboard-snag.py:246-249`; zero callers. **Test failed** — Deletion test. **Severity** — Cosmetic.
**Minimal correction path** — delete it.

### Finding F4 (stable F-005): Date-display and skipped-breakdown rendering duplicated
**Evidence** — `1159`≈`1336`; `1249`≈`1361`. **Test failed** — Deletion test. **Severity** — Noticeable weakness.
**Minimal correction path** — extract format helpers once F2 separates rendering.

## Simplification Check
- Structurally necessary: F3 adds tests at the real helper Interfaces — net-new coverage, not layering.
- New seam justified: no.
- Should NOT be done: end-to-end network-mock tests of `search_boards`/`_snag_sound` this loop; the pure helpers are the honest first surface.
- Tests after fix: `test_soundboard_snag.py` at the helper Interfaces; no old tests to delete (none existed).

## Improvement Backlog
1. **Add stdlib `unittest` suite for the pure helpers (F-003)** — structural, needed for winning. Binding constraint + prerequisite for safe parse extraction. (test_strategy + largest lever)
2. **Extract pure per-board HTML parsing from `search_boards` (F-002)** — structural, needed for winning. (architecture/simplicity/test_strategy +)
3. **Delete dead `_fetch_last_modified` (F-004)** — simplification, helpful. (simplicity +)

## Deepening Candidates
- **Per-board HTML parsing** (friction in F2): extract `_parse_board_html(html, board_name)`; fixture-based parse tests; first step extract + call from loop; do not build a parser class hierarchy.

## Builder Notes
1. **Hyphenated module not importable** — load by path with `importlib.util.spec_from_file_location` under a clean name; top-level only runs when `main()` is `__name__`-guarded.
2. **Characterize before refactor** — pin current behavior of the pure helpers (incl. non-obvious cases) before extracting; refactor against green.
3. **Test the pure surface, not the I/O surface** — test deterministic helpers directly; mock I/O only after extracting pure logic.

## Final Judge Narrative
Place — functionally solid and structurally improving. Loop 1's `BoardResult` lifted four dimensions with no behavioral change; this loop closes the most glaring regression-resistance gap with a 24-case characterization suite over the pure helpers, loaded past the hyphenated-filename obstacle. Ownership and concurrency remain trustworthy. Next: extract per-board parsing from the 734-line engine, now de-risked. Standing risk: over-reaching toward a package split or HTTP seam the single-file design does not warrant.

## Loop 2 Result
Added `test_soundboard_snag.py` — a stdlib `unittest` suite of 24 characterization tests over the pure network-free helpers (`_parse_views_count`, `_parse_http_datetime`, `_extract_board_slugs_from_search_html`, `_quote_path_segment`, `_extract_filename_from_headers`, `_sanitize_filename`, the date formatters), loading the hyphenated module via `importlib`. `py_compile` passes on both files; `python3 -m unittest test_soundboard_snag` runs 24 tests, all OK. Assertions pin real behavior including non-obvious cases (`CON.mp3`→`_Con.mp3`, `audio_42`→`Audio 42.mp3`, percent-escape preservation, UTC conversion). No production code touched → behavior unchanged by construction. Targeted finding **F-003 is resolved**. No scorecard regression.

## Loop 2 Implementation Review
Independent reviewer (Sonnet, read-only): **approved**. Reality passed (real 186-line suite, importlib-loaded, asserts all six named helpers + date formatters), Honesty passed (tests at the real helper Interfaces; spot-checked assertions match production behavior), Regression passed (no production file touched; no network I/O at import or in tests). 0 regressions, 0 conditions, 1 round.


--- Loop 3 (UTC 2026-06-30T13:52:07Z) ---

<!-- loop_cap: 10 -->

### Loop Counter
Loop 3 of 10 (cap)

### System Flag
[STATE: CONTINUE]

(Discovery + Authority Map first-loop-only — see REVIEW_HISTORY.md loop 1. Provider claude_code; loop inline in main (Opus); reviewer + challenger spawned independently. Branch `contest-refactor`, base for this loop `db6f0bf`.)

---

## Contest Verdict
**Good app, but not top-tier yet.**

The three structural anchors are now addressed or in hand: the central record is a named `BoardResult`, the pure helpers have a real test suite (visible this loop, lifting test_strategy and credibility), and this loop extracts the per-board parse into a testable module-level function. What keeps it short of top-tier is the still-large `search_boards` (transport + filter + date-scan + render remain fused) plus two small residuals (a dead wrapper, duplicated render blocks).

## Scorecard (1-10)
- Architecture quality: **5.5** | SAME | at loop-3 start `search_boards` still fuses concerns; parse extraction is this loop's fix (scored next loop)
- State management: **7.5** | SAME | one writer per concern; immutable instance attrs
- Domain modeling: **6.0** | SAME | `BoardResult` names the record; `sounds_info` bare 2-tuples, `approx_source` stringly-typed
- Data flow: **7.0** | SAME | named-field contract; residual: `main` re-filters (1591) vs `search_boards` filter (1247)
- Framework / platform: **7.0** | SAME | idiomatic stdlib; defensive sanitization; HTTPS; no secrets
- Concurrency: **9.5** | SAME | synchronous, no shared-mutable hazard. *Accepted residual:* `time.sleep` pacing is etiquette (permanent carve-out)
- Code simplicity: **5.0** | SAME | ~734-line function, dup render (1190≈1367; 1280≈1392), dead wrapper (319) remain at loop start
- Test strategy: **6.5** | UP | `test_soundboard_snag.py` (commit db6f0bf) — 24 real assertions at the pure-helper Interfaces. Residual: `search_boards` parse/network paths untested (addressed this loop)
- Overall credibility: **6.5** | UP | a real suite now backs the helper claims; `BoardResult` self-describing; code honest

## Strengths That Matter
- The loop-2 suite exercises the branchy logic (sanitization, compact-views parsing) at the real Interfaces — not glue or snapshots.
- `BoardResult` keeps the search output self-describing at no runtime cost.
- Synchronous design + defensive sanitization remain real strengths.

## Findings

### Finding F1 (stable F-002): ~734-line `search_boards` fuses transport, parsing, filtering, dates, rendering — *Priority 1, fixed this loop*
**Evidence** — `soundboard-snag.py:708-1402`; inline parse block `883-943`. **Test failed** — Shallow module. **Dependency** — `in-process`. **Severity** — Serious deduction.
**Minimal correction path** — extract pure `html -> ParsedBoard` parse to `_parse_board_html`; add fixture tests. De-risked by the helper suite.

### Finding F2 (stable F-004): Dead pass-through wrapper `_fetch_last_modified`
**Evidence** — `soundboard-snag.py:319-322`; zero callers. **Test failed** — Deletion test. **Severity** — Cosmetic.
**Minimal correction path** — delete it.

### Finding F3 (stable F-005): Date-display and skipped-breakdown rendering duplicated
**Evidence** — `1175-1190`≈`1352-1367`; `1270-1280`≈`1382-1392`. **Test failed** — Deletion test. **Severity** — Noticeable weakness.
**Minimal correction path** — extract format helpers taking a `BoardResult` + date-stats map.

## Simplification Check
- Structurally necessary: F2 extraction passes Shallow-module test — parse is now a Module (html in, ParsedBoard out) with Depth, reached directly by tests.
- New seam justified: no (in-process pure function).
- Should NOT be done: parser class hierarchy / strategy / port; extracting the network-bound date-scan this loop.
- Tests after fix: `ParseBoardHtmlTests` (full/play-only/dedup/empty) at the new `_parse_board_html` Interface; no old tests become shallow.

## Improvement Backlog
1. **Extract pure per-board HTML parsing from `search_boards` (F-002)** — structural, needed for winning. (architecture/simplicity/test_strategy +)
2. **Delete dead `_fetch_last_modified` (F-004)** — simplification, helpful. (simplicity +)
3. **De-duplicate date-display + skipped-breakdown render blocks (F-005)** — simplification, helpful. (simplicity +)

## Deepening Candidates
- **`_parse_board_html`** (friction in F2): extracted this loop; fixture-tested; do not add a parser class hierarchy.

## Builder Notes
1. **Pure computation fused inside an I/O loop** — move the pure block to a module-level function (raw in, named record out); the loop binds locals from it.
2. **Return a named record from a parser** — give the extracted parser a NamedTuple result so call sites + tests read fields by name.
3. **Test the extracted Interface with a synthetic fixture** — assert each field on a small representative input plus an empty-input case.

## Final Judge Narrative
Place — a good app, not yet top-tier. Named `BoardResult`, a real helper suite (lifting test_strategy + credibility this loop), and this loop's extraction of the per-board parse into a fixture-tested `_parse_board_html`. `search_boards` still owns transport, filtering, the network date-scan and rendering, so architecture/simplicity climb next loop. Ownership and concurrency trustworthy. Remaining backlog is small and subtractive; standing risk is over-decomposition the single-file design does not need.

## Loop 3 Result
Extracted the pure per-board HTML parsing out of `search_boards` into a module-level `_parse_board_html(board_html) -> ParsedBoard` (NamedTuple); `search_boards` now calls it and binds locals from the result. Added `ParseBoardHtmlTests` (4 fixture tests: full board, play-only, download-id de-dup, empty HTML). `py_compile` passes; `python3 -m unittest test_soundboard_snag` runs 28 tests, all OK; `--help` exits 0. The extraction reuses identical regexes/dedup/unescape logic (behavior-preserving); grep confirms parse regexes no longer appear in the `search_boards` body, which shrank ~734→~694 lines. Targeted finding **F-002 is resolved**. No scorecard regression.

## Loop 3 Implementation Review
Independent reviewer (Sonnet, read-only): **approved**. Reality passed (`_parse_board_html`/`ParsedBoard` exist; all parse regexes gone from `search_boards`, only render-label prints remain), Honesty passed (line-by-line vs `HEAD`: every regex/dedup/unescape identical → behavior-preserving; no costume layer/port; 4 tests assert real fields at the new Interface), Regression passed (all 10 locals rebound; `boards_with_downloads_total`/`status`/`preview_count` preserved). 0 regressions, 0 conditions, 1 round.


--- Loop 4 (UTC 2026-06-30T13:57:13Z) ---

<!-- loop_cap: 10 -->

### Loop Counter
Loop 4 of 10 (cap)

### System Flag
[STATE: CONTINUE]

(Discovery + Authority Map first-loop-only — see REVIEW_HISTORY.md loop 1. Provider claude_code; loop inline in main (Opus); reviewer + challenger spawned independently. Branch `contest-refactor`, base for this loop `0d964bb`.)

---

## Contest Verdict
**Good app, but not top-tier yet.**

Loop 3's parse extraction (now visible) lifts architecture, simplicity, domain modeling, test strategy and credibility. This loop clears the dead wrapper. The remaining gap to top-tier is that `search_boards` still fuses filtering, the network date-scan and all terminal rendering in ~690 lines, with two duplicated render blocks.

## Scorecard (1-10)
- Architecture quality: **6.5** | UP | `_parse_board_html` is a separate tested Module (commit 0d964bb); residual: filter/date-scan/render still fused (702-1396)
- State management: **7.5** | SAME | one writer per concern; immutable instance attrs
- Domain modeling: **6.5** | UP | `ParsedBoard` NamedTuple added (0d964bb); residual: `sounds_info` bare 2-tuples, `approx_source` stringly-typed
- Data flow: **7.0** | SAME | named-field contracts; residual: `main` re-filters (~1585)
- Framework / platform: **7.0** | SAME | idiomatic stdlib; defensive sanitization; HTTPS
- Concurrency: **9.5** | SAME | synchronous, no shared-mutable hazard. *Accepted residual:* `time.sleep` pacing (permanent carve-out)
- Code simplicity: **6.0** | UP | `search_boards` shrank ~734→~694 (0d964bb); residual: dead wrapper (fixed this loop) + dup render (1184≈1361; 1274≈1386)
- Test strategy: **7.0** | UP | `ParseBoardHtmlTests` added (0d964bb); 28 tests; residual: filter/date-scan/render paths untested
- Overall credibility: **7.0** | UP | parse now test-backed; two named domain records; honest code

## Strengths That Matter
- Two extraction loops (`BoardResult`, `_parse_board_html`) each behavior-preserving and independently reviewed — honest refactor history.
- Pure helpers and per-board parse both fixture-tested at their real Interfaces.
- Synchronous design + defensive sanitization remain real strengths.

## Findings

### Finding F1 (stable F-004): Dead pass-through wrapper `_fetch_last_modified` — *Priority 1, fixed this loop*
**Evidence** — `soundboard-snag.py:319-322`; zero callers (incl. `debug_track_dates.py`). **Test failed** — Deletion test. **Severity** — Cosmetic.
**Minimal correction path** — delete it (zero-risk subtractive win first, Meta-Rule 5).

### Finding F2 (stable F-006, new): `search_boards` still fuses filtering, network date-scan and all rendering (~690 lines)
**Evidence** — `soundboard-snag.py:702-1396`; inline filter `1095-1150`; inline results render `1332-1390`. **Test failed** — Shallow module. **Dependency** — `in-process`. **Severity** — Serious deduction.
**Minimal correction path** — over next loops, extract a pure filter evaluator + render/format helpers, each fixture-tested; do not extract the network date-scan; no class hierarchy.

### Finding F3 (stable F-005): Date-display and skipped-breakdown rendering duplicated
**Evidence** — `1169-1184`≈`1346-1361`; `1264-1274`≈`1376-1386`. **Test failed** — Deletion test. **Severity** — Noticeable weakness.
**Minimal correction path** — fold into the F-006 render helpers.

## Simplification Check
- Structurally necessary: F-004 passes the Deletion test (zero callers → complexity vanishes).
- New seam justified: no.
- Should NOT be done: touch `_fetch_last_modified_detailed`/cache (the live path).
- Tests after fix: none needed; 28-test suite stays green as regression guard.

## Improvement Backlog
1. **Delete dead `_fetch_last_modified` (F-004)** — simplification, helpful. Zero-risk subtractive win. (simplicity +)
2. **Extract pure filter evaluation + render/format helpers from `search_boards` (F-006)** — structural, needed for winning. Largest remaining lever. (architecture/simplicity/test_strategy +)
3. **Fold duplicated date-display + skipped-breakdown render blocks (F-005)** — simplification, helpful; folded into F-006. (simplicity +)

## Deepening Candidates
- **Filter evaluation + result rendering** (friction in F-006): extract pure `_evaluate_filters(...)` → (meets, reasons) and `BoardResult`→str render helpers; fixture-tested; do not extract the network date-scan; no renderer class hierarchy.

## Builder Notes
1. **Clear the certain subtractive win first** — take the dead-code deletion in its own commit before the larger refactor; it shrinks the surface to reason about.
2. **Deletion test before removing any wrapper** — grep every caller across the whole repo (incl. standalone scripts) first.
3. **Name the next structural target precisely** — re-derive the residual into a concrete finding with file:line (which sub-blocks are still fused), not a vague "it's big".

## Final Judge Narrative
Place — a good app, climbing steadily. Loop 3's parse extraction shows up across five dimensions; this loop takes the certain subtractive win. The honest residual is named precisely: `search_boards` still fuses filter evaluation and two render sections in ~690 lines, with duplicated render blocks. Ownership and concurrency trustworthy. Next: extract a pure filter evaluator and render helpers while resisting any renderer class hierarchy the single-file design does not need.

## Loop 4 Result
Deleted the dead pass-through wrapper `_fetch_last_modified` (4 lines + a surrounding blank line); the live path uses `_fetch_last_modified_detailed` and the `fetch_last_modified_cached` closure. `py_compile` passes; `python3 -m unittest test_soundboard_snag` runs 28 tests, all OK; `--help` exits 0; grep confirms the non-detailed wrapper no longer appears and `debug_track_dates.py` never referenced it; net −6 lines. Targeted finding **F-004 is resolved**. No scorecard regression.

## Loop 4 Implementation Review
Independent reviewer (Sonnet, read-only): **approved**. Reality passed (wrapper gone; zero references to the non-detailed name in either file), Honesty passed (purely subtractive; `_fetch_last_modified_detailed` + cache closure untouched), Regression passed (no dangling reference; live path intact). 0 regressions, 0 conditions, 1 round.


--- Loop 5 (UTC 2026-06-30T14:04:18Z) ---

<!-- loop_cap: 10 -->

### Loop Counter
Loop 5 of 10 (cap)

### System Flag
[STATE: CONTINUE]

(Discovery + Authority Map first-loop-only — see REVIEW_HISTORY.md loop 1. Provider claude_code; loop inline in main (Opus); reviewer + challenger spawned independently. Branch `contest-refactor`, base for this loop `bd81479`.)

---

## Contest Verdict
**Good app, but not top-tier yet.**

`search_boards` continues to decompose: this loop extracts the pure filter decision into a tested `_evaluate_filters`, leaving the terminal rendering as the last big inlined block. The filter half of F-006 is resolved; the render half (which also subsumes the duplicated render blocks of F-005) is the next and final structural lever.

## Scorecard (1-10)
- Architecture quality: **6.5** | SAME | filter extraction is this loop's fix (scored next loop); render still fused
- State management: **7.5** | SAME | one writer per concern
- Domain modeling: **6.5** | SAME | `BoardResult` + `ParsedBoard`; `sounds_info` deliberately a plain (id,title) 2-tuple (guardrail: don't force types)
- Data flow: **7.0** | SAME | named-field contracts; residual: `main` re-filters (~1590)
- Framework / platform: **7.0** | SAME | idiomatic stdlib; defensive sanitization; HTTPS
- Concurrency: **9.5** | SAME | synchronous, no shared-mutable hazard. *Accepted residual:* `time.sleep` pacing (permanent carve-out)
- Code simplicity: **6.5** | UP | dead wrapper removed (commit bd81479); residual: ~678-line function still inlines date-scan + two render sections; dup render (1191≈1368; 1281≈1393)
- Test strategy: **7.0** | SAME | 28 tests at loop start (6 filter tests added this loop, scored next); rendering untested
- Overall credibility: **7.0** | SAME | two named records, parse test-backed; honest code

## Strengths That Matter
- Three behavior-preserving, independently-reviewed extractions so far (`BoardResult`, `_parse_board_html`, the filter evaluator) — a consistent, honest cadence.
- The filter decision is now a pure function with full branch coverage incl. the date-only-when-basics-pass rule.
- Synchronous design + defensive sanitization remain real strengths.

## Findings

### Finding F1 (stable F-006): `search_boards` fuses filtering + date-scan + rendering — *Priority 1, filter half resolved, render half carried forward*
**Evidence** — `soundboard-snag.py:725-1403`; inline results render `1339-1395` (remaining). **Test failed** — Shallow module. **Dependency** — `in-process`. **Severity** — Serious deduction.
**Minimal correction path** — this loop: pure `_evaluate_filters` (skipped_buckets attribution kept at call site); next loop: render/format helpers (also folds F-005). No class hierarchy.

### Finding F2 (stable F-005): Date-display and skipped-breakdown rendering duplicated
**Evidence** — `1176-1191`≈`1353-1368`; `1271-1281`≈`1383-1393`. **Test failed** — Deletion test. **Severity** — Noticeable weakness.
**Minimal correction path** — fold into the F-006 render helpers next loop.

## Simplification Check
- Structurally necessary: `_evaluate_filters` passes Shallow-module test — small Interface (fields+thresholds in, (meets, failures) out), real branching behind it, pure decision separated from the side-effecting `skipped_buckets` attribution.
- New seam justified: no.
- Should NOT be done: move `skipped_buckets` counters into the pure function; extract the network date-scan; add a class hierarchy.
- Tests after fix: `EvaluateFiltersTests` (6 branch tests) at the new `_evaluate_filters` Interface.

## Improvement Backlog
1. **Extract filter eval (this loop) then render/format helpers from `search_boards` (F-006)** — structural, needed for winning. (architecture/simplicity/test_strategy +)
2. **Fold duplicated date-display + skipped-breakdown render blocks (F-005)** — simplification, helpful; folded into F-006 render extraction next loop. (simplicity +)

## Deepening Candidates
- **Result rendering** (friction in F-006): extract `BoardResult`→str render helpers + a shared `_format_updated_line`; fixture-tested; first step folds F-005's duplicated date block; no renderer class hierarchy.

## Builder Notes
1. **Separate the pure decision from its side effect** — return the decision + structured failure info from a pure function; keep the mutation at the call site, driven by the returned info.
2. **Preserve a subtle ordering rule explicitly** — encode the implicit dependency (date filter only when basics pass) and pin it with a test.
3. **Chip a large finding across loops with shrinking, named evidence** — resolve one slice per loop, mark carried_forward, re-cite the narrowed residual.

## Final Judge Narrative
Place — a good app, decomposing steadily and honestly. This loop extracts the filter decision into a pure, fully-branch-tested `_evaluate_filters`, cleanly separated from the `skipped_buckets` side effect. F-006 is half done; the remaining slice is the terminal rendering (which absorbs F-005's duplicates). Ownership and concurrency stay trustworthy. Resisting over-typing a 2-tuple and keeping the side effect at the call site is exactly the anti-overengineering the rubric rewards.

## Loop 5 Result
Extracted a pure `_evaluate_filters(...)` from `search_boards`' inline filter block; it returns `(meets, failures)` with each failure a `(bucket_key, reason)` tuple. `search_boards` now calls it, builds `filter_reasons` from the failures, and keeps the `skipped_buckets` attribution (gated on `has_downloads`) at the call site. Added `EvaluateFiltersTests` (6 tests) covering every branch incl. the date-only-when-basics-pass rule. `py_compile` passes; `python3 -m unittest test_soundboard_snag` runs 34 tests, all OK; `--help` exits 0. Extraction reproduces the original branch logic + side-effect attribution exactly; grep confirms inline filter logic gone from `search_boards`; function shrank to ~678 lines. Targeted finding **F-006: filter half resolved, render half carried forward**. No scorecard regression.

## Loop 5 Implementation Review
Independent reviewer (Sonnet, read-only, scoped to the filter slice): **approved**. Reality passed (`_evaluate_filters` exists; inline filter logic gone), Honesty passed (behavior-preserving: independent views/sounds failures, date-only-when-basics-pass preserved, byte-identical reasons, side effect kept at call site), Regression passed (`meets_filters`/`filter_reasons` still correct downstream). 0 regressions, 0 conditions, 1 round.


--- Loop 6 (UTC 2026-06-30T14:11:34Z) ---

<!-- loop_cap: 10 -->

### Loop Counter
Loop 6 of 10 (cap)

### System Flag
[STATE: CONTINUE]

(Discovery + Authority Map first-loop-only — see REVIEW_HISTORY.md loop 1. Provider claude_code; loop inline in main (Opus); reviewer + challenger spawned independently. Branch `contest-refactor`, base for this loop `18fa3f8`.)

---

## Contest Verdict
**Good app, but not top-tier yet.**

The filter extraction (now visible) lifts architecture, simplicity, test strategy and credibility. This loop resolves the duplicated render blocks (F-005) with two pure, tested format helpers — the render-dedup slice of F-006. What remains of F-006 is the non-duplicated per-board detail render still inlined in `search_boards`.

## Scorecard (1-10)
- Architecture quality: **7.0** | UP | filter now a pure tested Module (`_evaluate_filters`, commit 18fa3f8); residual: per-board detail render still inlined (1350-1390)
- State management: **7.5** | SAME | one writer per concern
- Domain modeling: **6.5** | SAME | `BoardResult` + `ParsedBoard`; `sounds_info` deliberately a plain 2-tuple (don't force types)
- Data flow: **7.0** | SAME | residual: `main` re-filters (1582) though `search_boards` already returns only downloadable boards (redundant filter)
- Framework / platform: **7.0** | SAME | idiomatic stdlib; defensive sanitization; HTTPS
- Concurrency: **9.5** | SAME | synchronous, no shared-mutable hazard. *Accepted residual:* `time.sleep` pacing (permanent carve-out)
- Code simplicity: **7.0** | UP | filter extraction shrank `search_boards` to ~678 (18fa3f8); residual at loop start: two duplicated render blocks (fixed this loop)
- Test strategy: **7.5** | UP | `EvaluateFiltersTests` added (18fa3f8); 34 tests; rendering untested at loop start (fixed this loop)
- Overall credibility: **7.5** | UP | parse + filter both test-backed (18fa3f8); two named records; consistent honest extraction history

## Strengths That Matter
- Four behavior-preserving, independently-reviewed extractions (`BoardResult`, `_parse_board_html`, `_evaluate_filters`, render helpers) — sustained honest cadence.
- Pure helpers, parse, and filter decision all fixture-tested at their real Interfaces.
- Synchronous design + defensive sanitization remain real strengths.

## Findings

### Finding F1 (stable F-005): Date-display and skipped-breakdown rendering duplicated — *Priority 1, resolved this loop*
**Evidence** — `1176-1191`≈`1353-1368`; `1271-1281`≈`1383-1393` (pre-fix). **Test failed** — Deletion test. **Severity** — Noticeable weakness.
**Minimal correction path** — extract `_format_updated_line` + `_format_skipped_breakdown` (pure, no color); fold both sites; fixture-test.

### Finding F2 (stable F-006): `search_boards` still inlines the per-board detail render (~636 lines)
**Evidence** — `soundboard-snag.py:757-1393`; inline detail render `1350-1390`. **Test failed** — Shallow module. **Severity** — Noticeable weakness (down from Serious — parse/filter/dedup now extracted).
**Minimal correction path** — next loop: extract pure `_render_board_lines(board, stats, include_dates)`; do not extract progress/diagnostic prints; no renderer class.

## Simplification Check
- Structurally necessary: the two format helpers pass the Deletion test (duplicated blocks → one definition each) and make formatting fixture-testable. Pure string-returning functions; callers keep color/indent.
- New seam justified: no.
- Should NOT be done: move ANSI color into helpers; extract progress/diagnostic prints; renderer class hierarchy.
- Tests after fix: `FormatUpdatedLineTests` (3) + `FormatSkippedBreakdownTests` (2) at the new helper Interfaces.

## Improvement Backlog
1. **Extract pure render/format helpers for the duplicated blocks (F-005)** — simplification, needed for winning. Removes drift-prone duplication; render-dedup slice of F-006. (simplicity/test_strategy +)
2. **Extract the per-board detail render into a pure tested helper (F-006 residual)** — structural, helpful. Final render slice. (architecture/simplicity/test_strategy +)

## Deepening Candidates
- **Per-board detail render** (friction in F-006): extract `_render_board_lines(board, stats, include_dates)`; fixture-tested; print lines in the loop; do not extract progress/diagnostic prints; no renderer class.

## Builder Notes
1. **Extract formatting as string-returning helpers, keep presentation at the call site** — return semantic text; each caller adds its own color/indent wrapper.
2. **A fixed-order join helper beats repeated append blocks** — drive parts from a (key, label) table in a pure function returning the joined string ('' when empty).
3. **Name the small residual honestly** — once parse/filter/dedup are extracted, F-006's remaining detail render is Noticeable, not Serious; downgrade severity to match reality.

## Final Judge Narrative
Place — a good app, nearly through its structural backlog. This loop resolves the duplicated render blocks with two pure, tested format helpers, lifting simplicity and test strategy and single-sourcing the formatting. F-005 resolved; F-006 down to one low-severity slice. Ownership and concurrency trustworthy. The refactor has stayed subtractive and test-backed throughout, resisting the renderer-class temptation the rubric warns against.

## Loop 6 Result
Extracted two pure formatting helpers from `search_boards`: `_format_updated_line(approx_updated, approx_source, stats) -> str` and `_format_skipped_breakdown(skipped_buckets) -> str`. Folded the four duplicated render sites (two date-display, two skipped-breakdown) into single calls, each caller keeping its own color/indent. Added `FormatUpdatedLineTests` (3) + `FormatSkippedBreakdownTests` (2). `py_compile` passes; `python3 -m unittest test_soundboard_snag` runs 39 tests, all OK; `--help` exits 0; grep confirms `breakdown_parts` no longer appears and the date-display literals exist only inside the single helper; the date block keeps its 2-space indent at the inline site; `search_boards` shrank ~678→636 lines. Targeted finding **F-005 is resolved**; F-006 render-dedup slice done (detail-render residual carried). No scorecard regression.

## Loop 6 Implementation Review
Independent reviewer (Sonnet, read-only): **approved**. Reality passed (helpers at module level; all four duplicated blocks replaced; `breakdown_parts` gone), Honesty passed (byte-identical strings; callers keep their own color+indent; tests assert real strings), Regression passed (no behavior change; correct `board_date_stats` keys). 0 regressions, 0 conditions, 1 round.


--- Loop 7 (UTC 2026-06-30T14:19:10Z) ---

<!-- loop_cap: 10 -->

### Loop Counter
Loop 7 of 10 (cap)

### System Flag
[STATE: CONTINUE]

(Discovery + Authority Map first-loop-only — see REVIEW_HISTORY.md loop 1. Provider claude_code; loop inline in main (Opus); reviewer + challenger spawned independently. Branch `contest-refactor`, base for this loop `41ec0c7`.)

---

## Contest Verdict
**Good app, but not top-tier yet.**

All the pure logic in `search_boards` is now extracted and tested: parse, filter, formatting, and (this loop) the per-board render. `search_boards` is down to its orchestration role. The remaining ceiling is that the orchestration itself (pagination, cross-page dedup, early-stop, near-miss suggestions) and the `SoundboardSnag` download pipeline are network-coupled and untested because there is no injectable fetch seam.

## Scorecard (1-10)
- Architecture quality: **7.0** | SAME | per-board render extraction is this loop's fix; residual: no fetch seam → orchestration + pipeline untestable
- State management: **7.5** | SAME | one writer per concern
- Domain modeling: **6.5** | SAME | `BoardResult` + `ParsedBoard`; `sounds_info` deliberately a plain 2-tuple
- Data flow: **7.0** | SAME | residual: `main` re-filters (1593) though `search_boards` already filters (1299) — redundant (F-007)
- Framework / platform: **7.0** | SAME | idiomatic stdlib; defensive sanitization; HTTPS
- Concurrency: **9.5** | SAME | synchronous, no shared-mutable hazard. *Accepted residual:* `time.sleep` pacing (permanent carve-out)
- Code simplicity: **7.5** | UP | render duplication removed via two pure helpers (commit 41ec0c7); per-board render still inlined at loop start (fixed this loop)
- Test strategy: **8.0** | UP | format-helper tests added (41ec0c7); 39 tests cover parse/filter/formatting at real Interfaces; residual: network orchestration + pipeline untested (F-008)
- Overall credibility: **7.5** | SAME | parse/filter/formatting all test-backed; honest extraction history

## Strengths That Matter
- Five behavior-preserving, independently-reviewed extractions (`BoardResult`, `_parse_board_html`, `_evaluate_filters`, two format helpers, render helper) — every slice tested at its Interface.
- `search_boards` is now an orchestration function; all its pure logic is tested without the network.
- Synchronous design + defensive sanitization remain real strengths.

## Findings

### Finding F1 (stable F-006): `search_boards` still inlines the per-board detail render — *Priority 1, resolved this loop*
**Evidence** — `soundboard-snag.py:1350-1374` (pre-fix). **Test failed** — Shallow module. **Severity** — Noticeable weakness.
**Minimal correction path** — extract pure `_render_board_lines(board, stats, include_dates) -> list[str]`; loop prints the lines; fixture-test. No renderer class.

### Finding F2 (stable F-008, new): Network orchestration + download pipeline untestable (no fetch seam)
**Evidence** — `soundboard-snag.py:789-1404` (orchestration: pagination/dedup/early-stop/near-miss); `503-530` (`_fetch_page`); `682` (`snag` failure-abort). **Test failed** — Two-adapter rule. **Dependency** — `remote-owned`. **Severity** — Serious deduction.
**Minimal correction path** — introduce `_http_get(url)` default + an injectable `fetch` param on `search_boards` (and a `fetcher` on `SoundboardSnag`); tests pass an in-memory fake; assert pagination/dedup/early-stop/near-miss + the consecutive-failure abort. Two adapters (urlopen + fake) justify the seam.

### Finding F3 (stable F-007, new): `main` re-filters results that are already downloadable-only
**Evidence** — `soundboard-snag.py:1593` (redundant re-filter) vs `1299` (authoritative filter). **Test failed** — Deletion test. **Severity** — Cosmetic.
**Minimal correction path** — `downloadable_boards = results` with a comment that `search_boards` guarantees `has_downloads`.

## Simplification Check
- Structurally necessary: `_render_board_lines` passes the Shallow-module test (board+stats+flag in, list[str] out; render behind it, now tested).
- New seam justified: no (this loop); the F-008 fetch seam is a separate larger change.
- Should NOT be done: move ANSI out of the render helper; extract progress/diagnostic prints; renderer class.
- Tests after fix: `RenderBoardLinesTests` (6) asserting semantic content with ANSI stripped.

## Improvement Backlog
1. **Extract the per-board detail render into a pure tested `_render_board_lines` (F-006)** — structural, needed for winning. (architecture/simplicity/test_strategy +)
2. **Introduce an injectable fetch seam to test orchestration + pipeline (F-008)** — structural, needed for winning. Remaining test/architecture ceiling. (test_strategy/architecture/data_flow +)
3. **Remove redundant downloadable re-filter in `main` (F-007)** — simplification, helpful. (data_flow/simplicity +)

## Deepening Candidates
- **Page-fetch seam** (friction in F-008): add `_http_get(url)` + injectable `fetch` param; tests drive `search_boards` with an in-memory fetcher; assert pagination/dedup/early-stop/near-miss + failure-abort. Do not build an HTTP client class hierarchy.

## Builder Notes
1. **Render functions return lines; the caller prints** — return `list[str]`; test the lines, not stdout.
2. **Color is intrinsic to a render helper** — let it own the color; assert on ANSI-stripped output.
3. **Name the seam that unlocks the last untested surface** — a single injectable fetch function (real + fake) satisfies the two-adapter rule.

## Final Judge Narrative
Place — a good app, now with all of `search_boards`' pure logic extracted and tested. This loop extracts the per-board render into a fixture-tested helper, finishing the decomposition that began with `BoardResult`. The honest remaining ceiling is named precisely (F-008): the network orchestration and download pipeline have no injectable fetch seam, so the most behavior-rich code is untested. A single injected fetch function (real + fake) is the justified next move; the redundant re-filter in `main` (F-007) is a small subtractive cleanup.

## Loop 7 Result
Extracted the per-board detail render out of `search_boards` into a pure `_render_board_lines(board, stats, include_dates) -> list[str]`; the results loop now prints the returned lines. Added `RenderBoardLinesTests` (6 tests) asserting header/status, play-only, optional-field presence, dates-only-when-flag, sample-file listing, and empty-samples, with ANSI stripped. `py_compile` passes; `python3 -m unittest test_soundboard_snag` runs 45 tests, all OK; `--help` exits 0; a direct render comparison shows the produced lines are byte-identical to the original prints (colors, `\n`-prefixed Sample-files header, `{idx:2}` numbering preserved); `search_boards` shrank ~636→615 lines. Targeted finding **F-006 is resolved**. No scorecard regression.

## Loop 7 Implementation Review
Independent reviewer (Sonnet, read-only): **approved**. Reality passed (`_render_board_lines` exists; loop body is the one-line print; inline render gone), Honesty passed (line-by-line byte-identical: header/URL/optional-field order, dates arg chain, `\n`-prefixed sample header, `{idx:2}` numbering; ANSI kept in helper; 6 semantic tests at the new interface), Regression passed (correct `board_date_stats` key; `print("\n")` separator preserved). 0 regressions, 0 conditions, 1 round.


--- Loop 8 (UTC 2026-06-30T14:27:23Z) ---

<!-- loop_cap: 10 -->

### Loop Counter
Loop 8 of 10 (cap)

### System Flag
[STATE: CONTINUE]

(Discovery + Authority Map first-loop-only — see REVIEW_HISTORY.md loop 1. Provider claude_code; loop inline in main (Opus); reviewer + challenger spawned independently. Branch `contest-refactor`, base for this loop `33d60cd`.)

---

## Contest Verdict
**Good app, but not top-tier yet.**

With the render extracted last loop, all of `search_boards`' pure logic is tested. This loop adds the injectable fetch seam and exercises the orchestration **offline** (pagination, cross-page dedup, view-sort, play-only exclusion, min-views filter, early-stop). The search-orchestration half of F-008 is resolved; the `SoundboardSnag` download pipeline still calls `urlopen` directly and is the carried residual.

## Scorecard (1-10)
- Architecture quality: **7.5** | UP | all of `search_boards`' pure logic extracted into tested Modules (render done 33d60cd); residual: `SoundboardSnag` pipeline has no fetch seam
- State management: **7.5** | SAME | one writer per concern
- Domain modeling: **6.5** | SAME | `BoardResult` + `ParsedBoard`; `sounds_info` deliberately a plain 2-tuple
- Data flow: **7.0** | SAME | residual: `main` re-filters (1607) though `search_boards` already filters (F-007); fetch seam is this loop's fix
- Framework / platform: **7.0** | SAME | idiomatic stdlib; defensive sanitization; HTTPS
- Concurrency: **9.5** | SAME | synchronous, no shared-mutable hazard. *Accepted residual:* `time.sleep` pacing (permanent carve-out)
- Code simplicity: **8.0** | UP | per-board render extracted (33d60cd); `search_boards` is an orchestration core (~615 lines) with all pure logic in small tested helpers
- Test strategy: **8.5** | UP | `RenderBoardLinesTests` added (33d60cd); 45 tests cover all pure logic; orchestration untested at loop start (fixed this loop)
- Overall credibility: **8.0** | UP | every extracted Module is test-backed (33d60cd); each Interface confirmed by a test; honest code

## Strengths That Matter
- `search_boards`' pure logic is fully decomposed and tested; the function is now an orchestration core.
- The fetch seam follows the two-adapter rule (real `_http_get` + in-memory fake) and is behavior-preserving — production keeps the exact `urlopen` behavior.
- Synchronous design + defensive sanitization remain real strengths.

## Findings

### Finding F1 (stable F-008): Network orchestration + download pipeline untestable (no fetch seam) — *Priority 1, search half resolved, SoundboardSnag residual carried*
**Evidence** — `soundboard-snag.py:802-1418` (search orchestration); `695` (`SoundboardSnag.snag`, still direct `urlopen`). **Test failed** — Two-adapter rule. **Dependency** — `remote-owned`. **Severity** — Serious deduction.
**Minimal correction path** — this loop: `_http_get` default + injectable `fetch` on `search_boards`, route both reads; test orchestration offline. Next: same fetcher injection on `SoundboardSnag`; test the failure-abort.

### Finding F2 (stable F-007): `main` re-filters results that are already downloadable-only
**Evidence** — `soundboard-snag.py:1607` (redundant re-filter) vs `search_boards`' own has_downloads filter. **Test failed** — Deletion test. **Severity** — Cosmetic.
**Minimal correction path** — `downloadable_boards = results` with a comment that `search_boards` guarantees `has_downloads`.

## Simplification Check
- Structurally necessary: the fetch seam passes the Unified Seam Policy two-adapter rule (real `_http_get` + in-memory fake) — the only way to verify pagination/dedup/early-stop offline (friction proven: no test existed).
- New seam justified: **yes** — adapters: `_http_get` (production urlopen) + in-memory fake (tests).
- Should NOT be done: HTTP client class hierarchy / adapter registry; change the production default.
- Tests after fix: `SearchBoardsOrchestrationTests` (sort, play-only, pagination, min-views, early-stop) with `time.sleep` patched out.

## Improvement Backlog
1. **Add an injectable fetch seam and test the search orchestration offline (F-008)** — structural, needed for winning. (test_strategy/architecture/data_flow +)
2. **Give `SoundboardSnag` the same fetcher injection and test the failure-abort (F-008 residual)** — structural, helpful. (test_strategy/architecture +)
3. **Remove the redundant downloadable re-filter in `main` (F-007)** — simplification, helpful. (data_flow/simplicity +)

## Deepening Candidates
- **`SoundboardSnag` page/track fetcher** (friction in F-008 residual): add a `fetcher` param to `SoundboardSnag.__init__` defaulting to `_http_get`; route `_fetch_page` through it; test the 2-consecutive-failure abort + play-only RuntimeError. Do not abstract file writes this round.

## Builder Notes
1. **Inject a fetch function to make network orchestration testable** — default real fetcher + optional `fetch` param; tests pass an in-memory fake. Two adapters justify the seam.
2. **Keep the seam a function, not a class hierarchy** — a single injected callable suffices for one prod impl + a test fake.
3. **Patch out real delays in orchestration tests** — `mock.patch('time.sleep')` so tests run instantly without changing production timing.

## Final Judge Narrative
Place — a good app, now with its orchestration testable. The fetch seam is the right shape: a single injected function with a real default and an in-memory test fake, satisfying the two-adapter rule and adding deterministic tests for pagination, dedup, view-sort, play-only exclusion, min-views filtering and early-stop. F-008's search half resolved; the `SoundboardSnag` download pipeline is the named carried residual. The seam added no class ceremony — exactly the restraint the rubric rewards.

## Loop 8 Result
Added a module-level `_http_get(url)` default fetcher and an injectable `fetch` parameter on `search_boards`; routed the search-page and board-page reads through it (production passes `_http_get`, preserving the exact `urlopen` behavior). Added `SearchBoardsOrchestrationTests` (5 tests) driving `search_boards` with an in-memory fake fetcher and `time.sleep` patched out, asserting view-sort ordering, play-only exclusion, pagination across pages, min-views filtering, and early-stop at max_results. `py_compile` passes; `python3 -m unittest test_soundboard_snag` runs 50 tests, all OK; `--help` exits 0; `_http_get` reproduces the inline `Request`/`urlopen`/decode and raises the same `HTTPError`/`URLError` so existing handling is unchanged; grep confirms no direct `urlopen` remains in the `search_boards` body. Targeted finding **F-008: search-orchestration half resolved, SoundboardSnag pipeline carried forward**. No scorecard regression.

## Loop 8 Implementation Review
Independent reviewer (Sonnet, read-only): **approved**. Reality passed (`_http_get` + `fetch=None` default; both reads routed through `fetch`; no `urlopen` left in `search_boards` body), Honesty passed (`_http_get` reproduces the exact Request/urlopen/decode and propagates HTTPError/URLError; two real adapters — `_http_get` + behavior-faithful dict-backed fake — satisfy the two-adapter rule; no class hierarchy), Regression passed (5 orchestration tests assert real behavior; `time.sleep` patched; exception handlers unchanged). 0 regressions, 0 conditions, 1 round.

