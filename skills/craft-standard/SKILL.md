---
name: craft-standard
description: The full tupelo-honey coding standard, with the levers, where knowledge lives, the 9 comment kinds and the pre-finish checklist. Load it once per session, before the first code change.
user-invocable: false
# Generated from AGENTS.md by scripts/sync-mirrors.sh; edit AGENTS.md, not this file.
---

# AGENTS.md — Systems Craft + Honey (v5.0, language-agnostic)

Write less code, waste zero chat tokens, and craft source files with the comment mastery and verification rigor of world-class systems software: antirez ("Writing system software: code comments", `rax`, `sds`), Pike (data dominates), Ousterhout (deep modules), Hipp (SQLite verification), djb (safe primitives, don't parse). Apply these rules as a reflexive writing style; never spend reasoning tokens deciding how to comply.

## 0. Scope, Precedence & Modes
- **Scope:** any language. Examples are illustrative: translate them to the language's idiomatic comment/doc syntax.
- **Priority order:** explicit user request > this file > `honey-for-devs` > repo conventions. Repo and language conventions win on *form* (doc syntax, style guide, file layout); this file governs *what knowledge* comments and docs must carry.
- **Modes:** `/tupelo [lite|full|ultra|off]` switches the current session (alias `/honey`; if the Honey plugin is installed too, a bare `/honey` may run Honey's own skill, so prefer `/tupelo`); `/tupelo default <mode>` persists the default (initially `full`) via `bin/craft-mode.sh`, re-injected at every session start; `/tupelo` alone returns to it.
  - `full`: terse chat (Lever 2); every code rule.
  - `lite`: chat may explain in full sentences; Lever 1 and all comment rules unchanged.
  - `ultra`: answer-only chat and loop reports; dense reading for large read-only files (§6); code comments unchanged.
  - `off`: no chat compression; Lever 1, comment rules and zero-token hooks stay on.
  - **Step up to `lite`, never down,** when brevity would drop correctness: a subtle bug, a trade-off, a correctness argument, a learner who needs the explanation.
- **How this file reaches a session:** in Claude Code, with no setup in any repository, the plugin's session directive has a session load this file, as the generated `craft-standard` skill, once before its first code change. Other agents load it from the repository (README, Other agents).
- **Hard boundaries for Honey:**
  - Never apply Lever 2 ("don't narrate code") or Lever 3 (compressed payloads) to code comments or docs: their reader is a human.
  - Never abbreviate prose words or identifiers: `config`, `implementation`, `request`, `response`, `function`, `authentication` cost 1 token each, the same as `cfg`, `impl`, `req`, `res`, `fn`, `auth`. Terseness comes from dropping filler words, never from shortening words.
  - For `honey-review`, `craft-review`, `hive-reviewer` and `hive-craft-reviewer`, "redundant / narration comment" means **Trivial and Backup only** (§5). Never cut Function, Design, Why, Teacher, Checklist or Guide comments.
  - Dispatched subagents (`hive-builder`) must follow §4–§6. If syncing comments exceeds a subagent's file-count guard, the orchestrator syncs them in the same change.
  - If `honey-compress` runs on this file, keep all 9 comment kinds distinct and code/ASCII blocks verbatim. (Without Honey installed, ignore Honey references.)

## 1. Lever 1 — Less Code, Deeper Modules
- **Simplicity is hard-won:** fight complexity at its two roots, unwillingness to make design sacrifices and accumulation of design errors. Prefer dropping a marginal requirement over complicating the core architecture (record sacrifices per §4). Pull complexity downward into deep modules with narrow public interfaces: one guard in the shared primitive beats one guard per call site, so grep all callers before patching.
- **Ladder first, then design:** needs to exist (YAGNI)? → existing code → stdlib → language native → installed dependency → one line → minimum deep module; stop at the first rung that works. For non-trivial additions (new data structure, module, service, public API change, > 100 lines or ≥ 3 distinct logical phases) list 2–3 candidate designs in chat, 1 line each, plus the chosen one and why (in `lite`), then **proceed immediately**. **Stop and wait for approval** only if the choice changes a public API, a data/storage/wire format, or is irreversible (migrations, deletions).
- **Data dominates (Pike):** choose and freeze explicit data structures before procedural logic. Prefer length-prefixed, binary-safe layouts over formats that need stateful parsers (djb).
- **Measured trade-offs only:** performance tricks (custom memory layouts, caches, manual pooling, concurrency, vectorization, native extensions, bit packing) need a benchmark, profiler output, memory measurement or explicit user constraint, recorded in the Why comment or commit message. Without evidence, use the simplest data structure.
- **Deep modules (Ousterhout):** D_mod = implementation LOC / public functions (LOC = non-blank, non-comment lines); below 15 (`craft-metrics.py --report`) is a hint to look for shallow entry points to merge or hide, never a quota or a gate: the test is a lower cognitive load (ΔL_cog < 0).
- **Cohesive functions, meaningful helpers:** keep sequential steps together when they share local state and form one transformation (e.g. `raxGenericInsert`); when such a function branches and nests, segment it with Guide comments anchored to a Teacher comment, while a flat sequence or a table reads top to bottom without them. Extract helpers when they create a semantic boundary, isolate resource ownership, remove duplication or enable independent tests; a single-caller helper with none of these benefits is inlined. The clearest case is a pass-through, whose one statement hands its parameters unchanged to another function.
- **Centralized cleanup:** a function acquiring ≥ 2 fallible resources or allocations tears down in one place: `goto cleanup`/`oom` in C, `defer`/`errdefer` in Go/Zig, `?`/`Drop` in Rust, `with`/`using`, `ExitStack` or a single `try/finally` in Python/TS/Java. Allocate everything a mutation needs before touching the structure, so a failure leaves it intact.
- **Bulk is generated, never typed:** ≥ 5 similar fixtures, tables or cases → write and run a generator.
- **Dependencies:** use what the repo already has. Never add a heavy dependency without user approval.

## 2. Lever 2 — Terse Chat, Rich Source Comments
- **Chat and reports:** answer first; no wind-up, pleasantries, hedging, recap or narration of self-evident diffs. Fragments over paragraphs.
- **Carve-out:** Lever 2 never touches code comments or docs. Reject the "self-documenting code" myth: code shows *how*; comments explain what statements alone cannot, and lower the reader's cognitive load (ΔL_cog < 0).
- **Decision rule:** before writing a comment, name the exact question it answers for a future reader ("why this order?", "what happens if the write fails?"). No question → no comment. Never write comments just to fill a category.

## 3. Verification & Testing Discipline
- Every behavior change ships with a runnable test, or a 1-line explanation in chat if untestable. Bulk fixtures and cases are generated by code, never hand-typed.
- **Stateful logic** (data structures, caches, state machines, parsers): seeded randomized/property-based workloads against a trivially correct reference model (Hypothesis, fast-check, proptest, QuickCheck, `go test -fuzz`, libFuzzer). `rax-test.c` compares against a plain hash table.
- **Failure paths are code paths:** if code handles allocation, I/O, network, timeout or partial-write failures, inject those faults deterministically (fail the k-th call, k = 1..N). After each fault check the logical state against the oracle (size, lookups, order; the internal shape may change), count leaks after teardown, and assert that at least one fault fired. Test both sides of every limit (SQLite `testcase()`).
- Never claim tests pass without executing them. State unverified paths in one closing chat line.

## 4. Where Knowledge Lives (Single Source of Truth)
- **Module design**, invariants, data layout, concurrency rules, sacrifices → **one** Design comment per module at its entry point: C/C++ public header; Python module docstring or package `__init__.py`; Go `doc.go`; Rust `//!` in `lib.rs`/`mod.rs`; Java `package-info.java`; JS/TS header of the main/index file. Other files carry a pointer (`See design notes in <file>.`).
- **API contract** → the language's doc-comment form (docstring, JSDoc/TSDoc, rustdoc `///`, Javadoc, godoc, C comment above the definition), in one place only, following repo convention.
- **Local deliberate simplification** with a known ceiling → `shortcut:` at the code site. **Module-shaping sacrifice** → Design comment "Known limitations" plus a local `shortcut:` pointing to it.
- **Known defect / unhandled concrete edge case** → `FIXME:` at the code site.
- **Speculative ideas / future optimizations** → never in code, never implemented (YAGNI): one line in `TODO.md`/`DESIGN.md` if present, else mention once in chat and drop.

## 5. The 9 Comment Kinds
### Positive kinds (write and maintain)
1. **Function** — inline API reference that spares callers the body: behavior; parameters and return values (skip types already expressed by the type system); errors, exceptions and error codes; **resource ownership and lifecycle** (who frees/closes/releases memory, files, connections, locks); mutation of arguments or shared state; side effects (I/O, globals, thread-safety). 1–2 lines for trivial internal helpers, a full contract for core/public APIs. Expected on the exported API and wherever the caller inherits something to release (returned memory, an open handle, a held lock); elsewhere, when a caller would otherwise have to read the body:
   ```c
   /* Seek the greatest key in the subtree at the current node. Return 0 on
    * out of memory, otherwise 1. */
   ```
   ```python
   def load_index(path: Path) -> Index:
       """Load the on-disk index at `path`, rebuilding it if the checksum fails.

       Raises IndexCorrupt if the rebuild also fails. The returned Index owns an
       open file handle: callers must call close() or use it as a context manager.
       """
   ```
2. **Design** — module overview: problem, core algorithms and structures, data/memory layout, concurrency rules, invariants, rejected alternatives, known limitations (placed per §4). Expected in a module that holds something a reader must learn first: its own data structure, a binary layout, concurrency primitives or two or more public types. **Observable design check:** count the exceptions to the invariant and the cases of the interface (an exhaustive enumeration of algorithmic cases is not an exception: rax's insertion walks five and passes). Stop and propose simpler alternatives in chat above 3 (aim for ≤ 2), if the invariant cannot fit in one sentence, or on "except when" more than twice.
3. **Why** — why clear code must be written this exact way: ordering constraints, bug workarounds, protocol or API quirks, regressions ("moved before X due to bug Y; do not reorder"), so future cleanups or Honey simplifications cannot break it. Goes with a bug fix when the reason is not visible in the code.
4. **Teacher** — teaches the math, protocol, state machine or data-structure transformation so a generalist can maintain specialist code. Break multi-branch algorithms into numbered cases (`1.`, `2.`, `3a.`, `3b.`) and anchor each branch in the code with a Guide comment (`3a: Replace old node with split node.`). Exact ASCII diagrams for pointer/tree structures, state transitions and, where layout matters (structs, binary formats, wire protocols, `ctypes`, NumPy dtypes), memory layouts (`[hdr:32b][chars][pad][ptrs]`); mandatory before pointer arithmetic, bitfields, packed structs or flexible array members. Shapes: bracket strips, linear chains, indented trees, small branches (≤ 3 rows, as below), byte-offset tables (`0..3 header`, `4..n chars`); never multi-column box grids, their alignment breaks silently. Verify against the code: a wrong diagram is worse than none. Draw physical layouts with `[field:size]` cells in declaration order (`[len:1B][alloc:1B][flags:1B][buf: len bytes]`; `B` bytes, `b` bits; `header`, `pad`, `payload` are synthetic) so the hooks can check names, order and widths, and pin them with a compile-time or test assertion on `sizeof`/`offsetof` (or the language's form, see `craft-code`) so the compiler checks what no script can.
   ```text
   Node "ANNIBALE" -> "SCO" -> [], inserting "ANNIENTARE".
   $SPLITPOS = 4 (first mismatch: 'B' in node, 'E' in key).
                 |B| -> "ALE" -> "SCO" -> []
       "ANNI" -> |-|
                 |E| -> (insertion continues) "NTARE" -> []
   ```
5. **Checklist** — coupled edits or strict step ordering. First enforce the coupling in code (single table, exhaustive `match`/`switch`, `static_assert`, a test that fails); if impossible, place the comment at both ends:
   ```python
   # WARNING: If you add a state here, update STATE_LABELS in ui/labels.py as well.
   ```
6. **Guide** — one short line titling the block of statements below it with their common goal, or a numbered step anchored to a Teacher comment (`// 1: Save next pointer.`):
   ```c
   /* Free the query buffer. */
   sdsfree(c->querybuf);
   sdsfree(c->pending_querybuf);
   c->querybuf = NULL;
   ```
   The same line over a single statement it restates is Trivial (7). Expected where a long function branches or nests, wherever a block has a goal the code does not show (antirez averages one per ~10 LOC in his long functions); a flat sequence, `switch` or table needs none. A long multi-branch algorithm adds a numbered step map with matching step headers. Never "redundant narration".
### Negative kinds (avoid or control)
7. **Trivial** — a Guide comment whose reading cost ≥ the code it covers (antirez): one statement restated (`count += 1  # Increment count.`). Delete on sight; if 1–3 lines are unclear, rename instead. Only Trivial and Backup comments count as "redundant".
8. **Debt** — `shortcut:` and `FIXME:` only, never a bare `TODO`, `XXX` or `HACK`; ceiling and trigger on line 1, the trigger a condition (`; add an index past that`), not one word:
   ```python
   # shortcut: O(N) scan, fine under ~10k entries; add an index past that.
   ```
   ```go
   // FIXME: empty keys not handled; callers must reject them until fixed.
   ```
   A marker counts only where it opens a real comment: `craft-debt-scan.sh` reads comments through the shared lexer, never string literals, in every language of §7, and other text files (YAML, TOML, HTML...) by a line match on `#`, `//`, `--`, `/*`, `<!--`. Never invent issue IDs; delete resolved markers in the same diff.
9. **Backup** — commented-out code or debug prints: forbidden, git is the backup. Debug logging behind a flag, log level or compile-time switch is code, not backup: keep it.

## 6. Comment Maintenance & Context Hygiene
- **Preserve** every comment that remains true. Never shorten, reword or compress valid comments to save tokens.
- **Update in the same diff** every comment your edit falsifies, including **outside the edited hunk**: doc comments at the definition *and* at every declaration (header, interface, `.pyi`/`.d.ts`, public docs), the Design comment, coupled Checklist comments, `shortcut:` triggers. Find them with `grep -rnw <symbol>`; for generic names (`get`, `close`, `parse`, `run`, `index`) grep the qualified form (`Index.close`, `index_close`) scoped to the module's directory, `grep -l` first. LSP "find references" does not search comments: it never replaces the grep. **Delete** comments that have become false.
- **Locate before reading:** Grep/Glob declarations first, then Read with offset/limit; headers, Design and Function comments before bodies. `ultra`: read-only files ≥ 6000 chars via dense reading (`honey-px` if installed), never a file you will Edit or quote exactly: image reads are lossy on exact strings.
- **Aggregate in code:** counts, sorts, dedups, diffs and line measurements come from a command or script, never from reasoning.
- **Big outputs:** a command that may print > 100 lines writes to a file; read its line count, head, tail and `grep` hits, never the whole output (Honey's `honey-ccr` automates this, if installed).
- **Agent-to-agent only (Lever 3):** subagent results are minified JSON, columnar when uniform (`{"c":[...],"r":[[...]],"n":N}`; ESON only when asked), records keyed by stable `id` (never position), with an `n` count checksum verified before use. Never show these payloads to the user: render them.

## 7. Plugin Skills, Agents & Hooks
- Load on match: `craft-design` (new module, data structure, header, format, trade-off) · `craft-code` (implementation, refactoring, error cleanup, `*Show` dumpers) · `craft-review` (diff review, delete-list, comment sync, debt) · `craft-verify` (tests, differential fuzzing, fault injection).
- Subagents: `hive-craft-scout` maps modules and invariants; `hive-craft-reviewer` audits a diff. Both return Lever 3 JSON.
- Languages: comment checks cover C/C++/Objective-C/CUDA, Java, C#, Kotlin, Swift, Scala, Groovy, Dart, Solidity, PHP, Go, Rust, Zig, JS/TS, Python, shell, Ruby, Perl, R, Elixir, Crystal, Julia, Nim, PowerShell, Lua, SQL, Haskell, Lisps, OCaml, F#, Erlang, Fortran, VB; function metrics all but SQL, Haskell, Erlang, Fortran, VB, Tcl. Other files are skipped, never guessed.

### Claude Code hooks
- Zero-token hooks print only on violations. After each Edit/Write, on the lines it wrote: debt markers, Backup and Trivial comments (`craft-debt-scan.sh`, `craft-comment-sync.py`). At Stop, on every file the session changed, Bash edits included (`craft-gate.py`): those checks, `[field:size]` diagrams vs their struct and orphaned symbols (a name gone from all code that a comment anywhere in the repository still cites) are **hard**, fix them (each blocks at most 3 Stops); the `craft-metrics.py` **review signals** (pass-through helpers with one caller, Guide rhythm, missing Function/Design/Teacher comments, layouts without a size/offset assertion, a changed type or new exported function with no test touched), comments that may restate their code or say nothing specific (Vague), and orphaned symbols in prose documents are questions, shown once, that never block (when nothing else does, they arrive with the next prompt): act where one reveals a real problem, judged by §1, never to move a number; "none" is a valid answer. Beyond names, order and widths the scripts check presence, never quality: what a comment says is still yours to get right.

## 8. Pre-Finish Checklist & Formatting
- **Formatting:** follow the language's doc conventions (PEP 257, JSDoc/TSDoc, rustdoc, Javadoc, godoc starting with the symbol name). Start doc comments with a one-line summary, since tooltips and generated docs show it alone. Write plain, warm, direct English, unless the repo's comments use another language.
- **Before finishing:**
  1. Find comments referencing changed symbols (scoped grep per §6) and ensure they remain true.
  2. Verify ASCII diagrams and API contracts (errors, ownership, mutation) match the code.
  3. Ensure zero Trivial/Backup comments, zero bare `TODO`s, and valid line-1 triggers on all `shortcut:`/`FIXME:` markers.
  4. Run tests and state any unverified paths in 1 line.
