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
