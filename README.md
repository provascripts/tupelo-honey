# tupelo-honey

**Standalone Claude Code plugin: no other plugin required.** It carries its own copy of the Honey levers it uses (write less code, terse chat, compact agent handoffs) and adds antirez-style source comments, deep modules and SQLite-grade verification, checked by zero-token hooks.
It coexists with Green-PT's [honey-for-devs](https://github.com/Green-PT/honey-for-devs): when both are installed, switch modes with `/tupelo`, because Honey owns `/honey`.

## Install

- One session, from a local clone: `claude --plugin-dir /path/to/tupelo-honey`
- For good: add the repository to a Claude Code plugin marketplace you own and install it from there.
- Other agents read the same rules: `AGENTS.md` (Codex, Gemini CLI, ...), `.cursor/rules/systems-craft.mdc` (Cursor), `.agents/skills/` (Agent Skills layout).

Needs Python 3.8+, Bash and git; nothing else.

## Modes

`/tupelo [lite|full|ultra|off]` changes the current session, `/tupelo default <mode>` the default for new sessions (initially `full`), `/tupelo` alone goes back to the default. `/honey` is an alias kept for compatibility.

- `full`: answer first, fragments, no filler; every code rule.
- `lite`: full sentences allowed in chat; code rules unchanged.
- `ultra`: answer-only chat; large read-only files are outlined before they are read.
- `off`: no chat compression; code rules and hooks stay on.

Chat compression never touches code comments or docs: their reader is a human.

## The nine comment kinds

All nine kinds are Salvatore Sanfilippo's (antirez), from [Writing system software: code comments](http://antirez.com/news/124) (2018): six positive kinds, **Function, Design, Why, Teacher, Checklist, Guide**, and three questionable ones, **Trivial, Debt, Backup**. tupelo-honey keeps his names, his order and his definitions; a Trivial comment, for instance, is a Guide comment that costs as much to read as the code it covers.

What tupelo-honey adds:

- **Debt with an exit condition.** antirez lists TODO, FIXME, XXX and "this is a hack" as debt comments. Here debt has two forms only, each carrying its own trigger on line 1: `shortcut: <quantified ceiling>; <revisit trigger>` for a deliberate simplification, `FIXME: <defect>; <trigger condition>` for a known defect. A bare `TODO` is always reported.
- **Observable signals for an author without taste.** For antirez, when to write a Guide comment is the author's judgment. A model has no such sense, so tupelo-honey writes it down as signals it can observe: a comment over two or more statements titles a block (Guide), the same words over one statement restate it (Trivial), long functions get a Guide comment about every 15-25 LOC where a block has a goal the code does not show, numbered steps anchor to a Teacher comment. They are signals, not quotas: the test is always a lower cognitive load for the reader.
- **Checked layout diagrams.** A Teacher diagram drawn with `[field:size]` cells (`[len:1B][alloc:1B][flags:1B][buf]`) is compared with its struct (names, order, gaps, fixed widths) and pinned with `static_assert(sizeof/offsetof)`, so the compiler checks what no script can.

## Hooks: two levels

| When | What is judged | Checks |
|---|---|---|
| After each Edit/Write | only the lines that edit wrote | debt markers, Backup, Trivial, Vague |
| At Stop | every file the session changed, Bash edits included | the checks above, plus orphaned symbols in comments and `[field:size]` diagrams vs their struct (**hard**); module metrics (**review signals**) |

- **Hard findings** are objective: they block each Stop while they exist. A finding that cannot be fixed is released after 3 attempts, and the user is told; one turn never blocks more than 3 Stops in a row.
- **Review signals** (D_mod, single-caller helpers, Guide rhythm, missing Function/Design/Teacher comments, layouts without an assertion) are shown once per session. They invite a look; the judgment stays with the model and AGENTS.md section 1.
- Cross-edit checks wait for Stop on purpose: a rename spread over three files is never interrupted after the first edit.
- Scope: at SessionStart the gate notes which files were already dirty, so uncommitted work from before the session never blocks it. Outside git there is no baseline and the gate stays silent.

Debt markers are read from real comments through a shared lexer: `message = "literal text # TODO"` is a string, not debt.

## Scripts

All in `bin/`, Python 3.8+ stdlib or Bash, silent when clean:

- `craft-debt-scan.sh [--quiet] [PATH...]`: ledger of debt markers.
- `craft-comment-sync.py --check-diff [--base REF]`: comments that drifted from the code.
- `craft-metrics.py --report [PATH...]`: module metrics as columnar JSON.
- `craft-gate.py`: the Stop gate (hook only).

## The Honey ecosystem

- **ESON.** Subagents return minified columnar JSON (`{"c":[...],"r":[[...]],"n":N}`, keyed by stable ids, with an `n` checksum): the same idea as ESON with zero dependencies. Real ESON only when you ask for it.
- **CCR.** Not reimplemented: AGENTS.md asks that outputs over 100 lines go to a file and be read by count, head, tail and grep; Honey's `honey-ccr` automates that if installed.
- **PX.** `honey-px` (image-rendered reads) is opt-in, for large read-only bulk such as vendored code or static docs. It is never used on a file the model will edit or must quote exactly: rendering text to images is lossy on exact strings, which contradicts the verification discipline this plugin is about.
- **honey-compress.** If you compress `AGENTS.md`, keep the nine kinds distinct and every code and ASCII block verbatim; a test fails when they are lost.

## Tests

`python3 tests/test-bin.py` (on Windows, from Git Bash). The randomized tests compare the scripts with oracles built by their own generators, print their seed and replay with `CRAFT_SEED=<seed>`. `tests/fixtures/` holds polyglot traps (C macros and `#ifdef`, C++ members, C# properties, Rust attributes and lifetimes, Go struct tags, Zig, ctypes, a dataclass, a TypeScript interface, markers inside literals): each must stay clean under every scanner, and the tests derive the faulty variants each one must catch.

## Known limitations

- Debt markers in files without a lexer (YAML, TOML, HTML, Dockerfile...) are found by a line match, which cannot tell a quoted `# TODO` from a comment.
- Struct fields are parsed for C, C++, C#, Rust, Zig, Go, Swift and Python ctypes. A struct with members the parser cannot read (a macro, an anonymous union, an `#if`, a base class) is checked for order and widths only.
- In a file that was already dirty when the session started, once the session edits it, its earlier uncommitted lines are judged too.

## License

MIT, see `LICENSE`. The rax and sds excerpts quoted in the skills stay under their own BSD-3-Clause license, Copyright (c) Salvatore Sanfilippo and contributors; `LICENSE` reproduces it.
