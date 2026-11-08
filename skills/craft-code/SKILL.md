---
name: craft-code
description: >-
  Use when writing or substantially changing implementation code in any
  language. Favors deep cohesive functions over single-caller helpers, Guide
  comments that title blocks, Why and Checklist comments, one centralized
  cleanup path for >= 2 fallible resources, allocate-before-mutate, layout
  diagrams pinned by compile-time size/offset assertions, and a *Show/*Dump
  introspection primitive, with real raxGenericInsert and raxShow excerpts as
  models; its numbers are review signals, never quotas.
license: MIT
---

# craft-code: cohesive implementation, readable top to bottom

## Rules (LOC = non-blank, non-comment lines)
1. **Cohesive deep functions.** Keep an algorithm that shares local state (tree insertion, a state-machine step, packet decoding) in one function instead of 8 helpers passing 7 variables by reference. Keep a helper only for a benefit you can name: semantic boundary, resource ownership, removed duplication, or test seam; otherwise inline it under a Guide comment.
2. **Guide rhythm.** A long function that branches or nests: a Guide comment, a short line titling the block below it, wherever a block has a goal the code does not show; a flat sequence, `switch` or table needs none, and a comment over one statement it restates is Trivial. Long and multi-branch: a numbered step map (`1.`, `2.`, `3a.`, `3b.`) in the Teacher or Function comment, the same numbers as headers above the matching blocks.
3. **Why comments** wherever the obvious rewrite would break something: ordering, timeouts, protocol quirks, regressions. A bug fix gets one when the reason is not visible in the code ("moved before X due to bug Y; do not reorder").
4. **Checklist comments** at both ends of a coupling the compiler cannot enforce (after trying a single table, an exhaustive `switch`/`match`, `static_assert`, or a failing test).
5. **One cleanup path** for >= 2 fallible acquisitions: handles start at NULL/None, every failure jumps to one teardown block. Never duplicate multi-line teardown across branches.
6. **Allocate before you mutate.** Acquire everything a structural change needs first; if any acquisition fails, release and return while the structure is untouched.
7. **Teacher diagram before pointer math**, bitfields, packed or flexible-array layouts (craft-design for the format), a **Function comment** on the exported API and on whatever hands its caller something to release, a **Design comment** (or a pointer to it) at the top of a module with its own data structure, a binary layout, concurrency primitives or two public types.
8. **Pin every physical layout.** Draw it with `[field:size]` cells in declaration order (the Stop gate compares them with the struct) and assert its size or offsets where the compiler or the test suite runs, in the same change: the compiler then refuses a layout that drifts from the drawing. Logical diagrams (trees, `->` chains, state machines) need no assertion. Never assert `int`, `long`, `size_t` or pointer widths as constants: they change between LP64 and LLP64.
9. **Say something.** A comment of generic words ("Process the data", "Handle edge cases") is VAGUE: name the domain fact, the limit or the reason.
10. **Introspection primitive.** Non-trivial data structures ship a dumper (`raxShow` style) that prints their topology compactly, so humans, tests and agents can check invariants at a glance.

**Signals, not quotas.** 40 LOC, 58 lines without a Guide comment, 9 branches, nesting 3 (percentiles of antirez's own code) and D_mod 15 (`--report` only) point at opaque blocks and shallow modules, each as a question whose answer may be "none". The sovereign criterion is a lower cognitive load for the reader (ΔL_cog < 0) and the four helper benefits of AGENTS.md §1. Never change code only to move a number.

## Example: numbered steps, allocate before mutate (antirez/rax, `rax.c`, BSD-3-Clause, see LICENSE; verbatim, trimmed with `[...]`)
```c
        /* 2: Create the split node. Also allocate the other nodes we'll need
         *    ASAP, so that it will be simpler to handle OOM. */
        [...]
        /* OOM? Abort now that the tree is untouched. */
        [...]
        if (j == 0) {
            /* 3a: Replace the old node with the split node. */
```

## Language references
In `references/`: `layout-<lang>.md` (rule 8; c, cpp, csharp, go, python, rust, swift, zig), `cleanup-<lang>.md` (rule 5; c, cpp, go, java, python, rust, typescript, zig), `rax-excerpts-c.md` (the whole `raxGenericInsert` and `raxShow` excerpts). **Read only the file for the language in use.**

## Translation table
| Concept | C / C++ / Zig | Rust / Go | Python / TypeScript / Java |
|---|---|---|---|
| Single cleanup path | `goto err`/`goto oom`; C++ RAII; Zig `defer` + `errdefer` | `?` + `Drop`; `defer` + named `err` | `with`/`ExitStack`; one `try/finally`; try-with-resources |
| Allocate before mutate | allocate all nodes, check, then link | build the new value, then swap it in | build, validate, then assign |
| `raxShow` dumper | `xxxShow(FILE*)` | `impl fmt::Debug`; `String()` / `%+v` | `__repr__` / `dump()`; `toString()`/`inspect`; `toString()` |
| Guide comment | `/* 3a: ... */` | `// 3a: ...` | `# 3a: ...`; `// 3a: ...` |
