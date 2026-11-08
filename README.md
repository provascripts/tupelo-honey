<p align="center">
  <img src="assets/logo.jpg" alt="tupelo-honey logo: a jar of honey" width="460" />
</p>

<h1 align="center">tupelo-honey</h1>

<p align="center">
  <strong>Write less code and craft dense source</strong>
</p>

<p align="center">
  <em>A standalone Claude Code plugin for systems software craft: antirez-style comment discipline, deep modules and verification inspired by SQLite, checked by zero-token hooks.</em>
</p>

<p align="center">
  <a href="#install"><img src="https://img.shields.io/badge/plugin-Claude%20Code-f59e0b?style=flat-square" alt="Claude Code Plugin" /></a>
  <a href="#modes"><img src="https://img.shields.io/badge/modes-full%20%7C%20lite%20%7C%20ultra%20%7C%20off-d97706?style=flat-square" alt="Modes" /></a>
  <a href="#the-nine-comment-kinds"><img src="https://img.shields.io/badge/comments-antirez%209%20kinds-b45309?style=flat-square" alt="Comment Kinds" /></a>
  <a href="#requirements"><img src="https://img.shields.io/badge/requirements-Python%203.8+%20%7C%20Bash%20%7C%20Git-92400e?style=flat-square" alt="Requirements" /></a>
  <a href="#license"><img src="https://img.shields.io/badge/license-MIT-78350f?style=flat-square" alt="License MIT" /></a>
</p>

<p align="center">
  <a href="#install">Install</a> ·
  <a href="#modes">Modes</a> ·
  <a href="#the-nine-comment-kinds">Comment kinds</a> ·
  <a href="#hooks-two-levels">Hooks</a> ·
  <a href="#tests">Tests</a>
</p>

---

## Overview

**tupelo-honey** is a standalone Claude Code plugin for people who want their source code written with care. It carries its own copy of the Honey levers it uses (write less code, terse chat, compact agent handoffs) and pairs them with the principles of five systems programmers:

- **Salvatore Sanfilippo (antirez):** comments that carry what the code cannot say (`rax`, `sds`), readable architecture, no gratuitous complexity.
- **Rob Pike:** data dominates; choose and freeze the data structures before writing the procedures.
- **John Ousterhout:** deep modules with narrow interfaces and a low cognitive load.
- **D. Richard Hipp (SQLite):** verification against a reference model, randomized workloads and deterministic fault injection.
- **Daniel J. Bernstein (djb):** safe primitives; length-prefixed formats over ones that need a stateful parser.

> [!NOTE]
> **No other plugin required.** tupelo-honey coexists with Green-PT's [honey-for-devs](https://github.com/Green-PT/honey-for-devs): with both installed, use `/tupelo` (see [With honey-for-devs](#with-honey-for-devs)).

## Install

### Claude Code (plugin marketplace)

```text
/plugin marketplace add provascripts/tupelo-honey
/plugin install tupelo-honey@provascripts
```

Installed, the plugin brings its skills (each `SKILL.md` stays under 6 KB, the generated `craft-standard` aside; examples per language sit in its `references/` and are read only for the language in use), its hooks and a short directive injected at every session start (the core rules: precedence, the comment kinds, debt markers, locate before reading, aggregate in code). There is nothing to set up or copy, in any repository: the standard arrives with the `craft-standard` skill, which the directive has a session load once before its first code change, with no permission prompt. Nothing is written into your repository.

### Other agents

The hooks and the Stop gate exist only in Claude Code. Elsewhere the standard and the skills work as instructions, and the checks run at commit time.

- **Codex:** reads `AGENTS.md` at the root of a repository by itself and scans `.agents/skills`. Copy `AGENTS.md` and this repository's `.agents/` folder into yours, or `.agents/skills` under your home folder for every repository.
- **Gemini CLI:** a one-line `GEMINI.md` at the root of the repository holding `@./AGENTS.md`, or `{"context": {"fileName": ["AGENTS.md", "GEMINI.md"]}}` in `settings.json`. It scans `.agents/skills` too, as an alias of `.gemini/skills`.
- **Cursor:** copy `.cursor/rules/systems-craft.mdc` to the same path in your repository; project rules must keep the `.mdc` extension.

### The gate at commit time, in any editor

```bash
git clone https://github.com/provascripts/tupelo-honey
bash tupelo-honey/scripts/install-git-hook.sh /path/to/your/repository
```

The hook runs `craft-gate.py --staged` from that clone. Hard findings abort the commit (exit 1); review signals are printed on stderr and let it pass (exit 0). A `pre-commit` hook that is not ours is never overwritten. As a CI step, stage the whole branch against its base and run the same gate: `git reset --soft "$BASE" && python3 tupelo-honey/bin/craft-gate.py --staged`.

### Requirements

- **Python 3.8+** (standard library only)
- **Bash** and **Git**
- Nothing else: no third-party packages.

## Modes

Set how terse the chat is, for this session or as the default for new ones:

```text
/tupelo [lite|full|ultra|off]    # this session only
/tupelo default <mode>           # default for new sessions, applied now
/tupelo                          # back to the default
```

`/honey` is the same command: `/honey ultra`, `/honey default lite`. Both work right after install; the full name `/tupelo-honey:tupelo` works too.

| Mode | Chat | Code and comment rules | Good for |
| :--- | :--- | :--- | :--- |
| `full` *(default)* | Answer first, fragments, no filler | All rules, hooks on | Everyday development |
| `lite` | Full sentences allowed | All rules, hooks on | Design discussions, trade-offs, learning |
| `ultra` | Answer only; large read-only files are outlined before they are read | All rules, hooks on | Long loops and large audits |
| `off` | No chat compression | All rules, hooks on | When you want normal prose |

In every mode the model steps up to `lite` when brevity would drop correctness: a subtle bug, a trade-off, a learner who needs the explanation.

> [!IMPORTANT]
> **Chat compression never touches code comments or docs.** It applies to chat only: comments are written for a human maintainer and stay complete.

### With honey-for-devs

[honey-for-devs](https://github.com/Green-PT/honey-for-devs) has its own `/honey`. With both plugins installed, a bare `/honey` runs whichever plugin Claude Code loaded first: use `/tupelo`, or the full names `/tupelo-honey:tupelo` and `/honey:honey`.

## The Nine Comment Kinds

All nine kinds are Salvatore Sanfilippo's (antirez), from [*Writing system software: code comments*](http://antirez.com/news/124) (2018). In his words the first six are "mostly very positive forms of commenting, while the final three are somewhat questionable". tupelo-honey keeps his names, his order and his definitions; what it asks on top of them is in the right-hand column and in the rules for the three questionable kinds.

### Six Positive Kinds (write and maintain)

| # | Kind | antirez's definition | tupelo-honey adds |
| :-- | :--- | :--- | :--- |
| 1 | **Function** | Prevents the reader from reading the code in the first place. | The full contract: errors, ownership and lifecycle of resources, mutation, side effects. |
| 2 | **Design** | How and why the code uses certain algorithms, techniques, tricks and implementation. | One per module, at its entry point, with invariants, data layout and known limitations. |
| 3 | **Why** | The reason the code does something, even when what it does is crystal clear. | With a bug fix, when the reason is not visible in the code. |
| 4 | **Teacher** | Teaches the domain the code operates in (math, algorithms, data structures), not the code itself. | Numbered steps anchored by Guide comments; exact ASCII diagrams, and `[field:size]` layouts checked against their struct. |
| 5 | **Checklist** | Tells you what else to do when this code changes, or how a change must be made. | Enforce the coupling in code first (one table, an exhaustive `switch`, a failing test); otherwise comment at both ends. |
| 6 | **Guide** | Babysits the reader: titles a block so that reading it costs less. | Expected in long functions, as a signal and not a quota (below). |

### Three Questionable Kinds (avoid or control)

1. **Trivial:** for antirez, a Guide comment whose cognitive load is the same as or higher than the code it covers: `array_len++; /* Increment the length of our array. */`. tupelo-honey deletes it on sight; if the code is unclear, rename instead.
2. **Debt:** technical debt hard-coded in the source: FIXME, TODO, XXX, "This is a hack". antirez avoids them when he can, but admits it is not always possible. tupelo-honey allows two forms only, each with its trigger on line 1:
   - `shortcut: <quantified ceiling>; <revisit trigger>` for a deliberate simplification.
   - `FIXME: <defect>; <trigger condition>` for a known defect.

   A bare `TODO` is always reported.
3. **Backup:** older versions of the code left in as comments. "Source code is not for making backups," antirez writes: git is. tupelo-honey forbids them, debug prints included; debug logging behind a flag or a log level is code, not backup.

### What tupelo-honey Adds

- **Observable signals for an author without taste:** for antirez, when to write a Guide comment is the author's judgment. A model has no such sense, so tupelo-honey writes it down as signals it can observe: a comment over two or more statements titles a block (Guide), the same words over one statement restate it (Trivial), a long function that branches or nests gets a Guide comment wherever a block has a goal the code does not show, numbered steps anchor to a Teacher comment. **They are signals, not quotas:** the test is always a lower cognitive load for the reader.
- **Rules tied to complexity, not size:** RHYTHM asks only of a long function that also branches or nests (a flat handler or table reads top to bottom); NO-DOC only of the exported API and of functions that hand their caller something to release; NO-DESIGN only of modules with their own data structure, a binary layout, concurrency primitives or two public types. The thresholds are percentiles of antirez's own code (`bench/`), and each signal is a question whose answer may be "none".
- **Checked layout diagrams:** a Teacher diagram drawn with `[field:size]` cells (`[len:1B][alloc:1B][flags:1B][buf]`) is compared with its struct (names, order, gaps, fixed widths) and pinned with `static_assert(sizeof/offsetof)`, so the compiler checks what no script can.

The scripts check that comments are there and that diagrams match their structs; what a comment says is still the author's job.

## Hooks: Two Levels

The hooks cost no tokens and stay silent unless a rule is broken.

| When | What is judged | Checks |
| :--- | :--- | :--- |
| **After each Edit/Write** | Only the lines that edit wrote | Debt markers, Backup, Trivial (a comment that only restates its statement) |
| **At Stop** (each time the model finishes a turn) | Every file the session changed, Bash edits included | The checks above, plus `[field:size]` diagrams vs their struct and orphaned symbols: a name the session removed from all code but a comment anywhere in the repository still cites (**hard**); module metrics and orphaned symbols in prose documents (**review signals**) |

- **Hard findings** are the ones a script decides with few false positives (measured on the history of rax, sds and Redis, see `bench/`): they block each Stop while they exist. What needs judgment (a comment that may restate its code, a Vague one, the metrics) is a review signal. A finding that cannot be fixed is released after 3 attempts, and the user is told; one turn never blocks more than 3 Stops in a row.
- **Review signals** (pass-through helpers with one caller, Guide rhythm in long complex functions, missing Function/Design/Teacher comments, layouts without an assertion, a changed type or new exported function with no test touched) are questions, shown once per session, that never cost a turn: when nothing blocks, the model stops, you read them at once, and the model gets them with your next message (a UserPromptSubmit hook). They invite a look; the judgment stays with the model. D_mod (module depth) is only in `craft-metrics.py --report`: no count decides whether a module is shallow.
- **Cross-edit checks wait for Stop on purpose:** a rename spread over three files is never interrupted after the first edit.
- **Git-aware:** at SessionStart the gate notes which files were already dirty, so uncommitted work from before the session never blocks it. Outside git there is no baseline and the gate stays silent.

Debt markers are read from real comments through a shared lexer: in `message = "literal text # TODO"` the marker is a string, not debt.

## Scripts (`bin/`)

Python 3.8+ stdlib or Bash, silent when clean. While the plugin is enabled, Claude Code puts `bin/` on the Bash tool's PATH, so the scripts run by name (`craft-metrics.py --report src`); elsewhere, call them by path.

- `craft-debt-scan.sh [--quiet] [PATH...]`: ledger of debt markers, with malformed ones reported.
- `craft-comment-sync.py --check-diff [--base REF]`: comments that drifted from the code.
- `craft-metrics.py --report [PATH...]`: module metrics as columnar JSON, with the comment-to-code line ratio and a lint for multi-column box grids in comments.
- `craft-gate.py`: the Stop gate (hook); `--staged` judges the files staged for commit, for a git pre-commit hook (exit 1 on hard findings, review signals on stderr). Generated files (`*.gen.*`, a `Code generated ... DO NOT EDIT.` or `@generated` header, `linguist-generated` in `.gitattributes`) are not judged, but still count as code using a name. If git itself fails, the gate lets the session stop.
- `craft-mode.sh`: reads and saves the mode (used by `/tupelo` and the SessionStart hook).

## The Honey Ecosystem

- **ESON:** subagents return minified columnar JSON (`{"c":[...],"r":[[...]],"n":N}`, keyed by stable ids, with an `n` checksum): the same idea as ESON with zero dependencies. Real ESON only when you ask for it.
- **CCR:** not reimplemented. The standard (`AGENTS.md`) asks that outputs over 100 lines go to a file and be read by count, head, tail and grep; no script enforces this, and Honey's `honey-ccr` automates it if it is installed.
- **PX:** `honey-px` (image-rendered reads) is opt-in and needs Honey installed. It is for large read-only bulk such as vendored code or static docs, never for a file the model will edit or must quote exactly: rendering text to images is lossy on exact strings.
- **honey-compress:** if you compress `AGENTS.md` with it, keep the nine kinds distinct and every code and ASCII block verbatim; a test fails when they are lost.

## Tests

```bash
python3 tests/test-bin.py    # on Windows, from Git Bash
```

- **Differential tests:** the randomized tests compare the scripts with oracles built by their own generators.
- **Replayable:** every run prints its seed; replay it with `CRAFT_SEED=<seed>`.
- **Polyglot fixtures:** `tests/fixtures/` holds traps in C (macros, `#ifdef`, packed bitfields), C++, C#, Go, Rust, Zig, TypeScript and Python (ctypes, a dataclass, markers inside literals). Each must stay clean under every scanner, and the tests derive the faulty variants each one must catch.
- **Skill code compiles:** the C fault-injection harness and driver of `craft-verify` are compiled as two translation units against a small module with planted faults (a leak, a corrupted size, an allocator the harness never sees), each of which must stop the driver. It needs a C compiler (`cc`, `gcc`, `clang`, or `CRAFT_CC="zig cc"`) and is skipped otherwise.
- **Real code, opt-in:** `CRAFT_CORPUS=<dir>`, a folder with clones of `antirez/rax`, `antirez/sds`, `redis/redis` and `fastapi/full-stack-fastapi-template` (never vendored), adds the tests that run on them, such as the harness against the real rax.
- **Real history:** `scripts/replay-history.py <repo> [--commits N] [--path src/] [--plugin-root DIR]` judges a repository's last N commits as if each were one session, without a checkout; `--static` imports its tree as new files. `bench/` keeps the counts for rax, sds, redis `src/` and an application repository, per release.

## Known Limitations

- **Files without a lexer** (YAML, TOML, HTML, Dockerfile...): debt markers are found by a line match, which cannot tell a quoted `# TODO` from a comment. Prose files (Markdown, reStructuredText, plain text) are not scanned.
- **Constructs the lexer misreads:** nested block comments (Rust, Swift, Kotlin, Scala, Dart, Haskell, OCaml, F#, Julia, Nim, Lisp), C++ raw strings `R"(...)"`, Swift raw strings `#"..."#` and heredocs (shell, Ruby, Crystal, Perl, PHP). A file that contains one is reported as inexact: its hard findings become review signals that name the construct and its line, and the after-edit checks skip it. A real parser such as tree-sitter would read these, but it is not in the Python standard library; it could only be an optional backend.
- **Struct parsing** covers C, C++, C#, Rust, Zig, Go, Swift and Python ctypes. A struct with members the parser cannot read (a macro, an anonymous union, an `#if`, a base class) is checked for order and widths only.
- **Dirty files:** in a file that was already dirty when the session started, once the session edits it, its earlier uncommitted lines are judged too.
- **What stays with the model and human review:** the scripts see presence and shape, never judgment. The choice of architecture, a module's real depth, whether what a comment says is correct and whether the tests are sufficient are not decided by any script (NO-TEST only notices that none changed).

## License

MIT, see `LICENSE`. The rax and sds excerpts quoted in the skills stay under their own BSD-3-Clause license, Copyright (c) Salvatore Sanfilippo and contributors; `LICENSE` reproduces it.
