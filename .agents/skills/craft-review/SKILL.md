---
name: craft-review
description: >-
  Use when reviewing a diff or branch, auditing code quality, pruning
  over-engineering, checking that comments still match the code, or harvesting
  technical debt. Runs the zero-token scanners first, audits symbol-to-comment
  sync with Grep, applies the antirez 9-kind comment filter (protects the 6
  positive kinds, deletes Trivial and Backup, validates shortcut:/FIXME:), and
  answers with a terse numbered delete-list. Extends honey-review and honey-debt.
license: MIT
---

# craft-review: numbered delete-list, comment filter, debt ledger

No preamble, no compliments. Facts come from the scripts; judgment from you.

## Phase 1: scanners first (zero tokens of judgment)
Run from the repository root (outside Claude Code the scripts live in the plugin's `bin/`):
```bash
bash "${CLAUDE_PLUGIN_ROOT}/bin/craft-debt-scan.sh" --quiet
python3 "${CLAUDE_PLUGIN_ROOT}/bin/craft-comment-sync.py" --check-diff --base <base-ref>
python3 "${CLAUDE_PLUGIN_ROOT}/bin/craft-metrics.py" --report <changed code files>
```
`<base-ref>` is `HEAD` for uncommitted work, the merge base for a branch (`git merge-base HEAD main`). Each script prints nothing when clean. Their findings go into the delete-list as they are; never re-derive counts or line numbers by reading. Kinds: debt markers (TODO, NO-TRIGGER, NO-CEILING); ORPHAN, BACKUP, TRIVIAL, VAGUE and DIAGRAM-FIELD/-ORDER/-SIZE (a `[field:size]` diagram that disagrees with its struct); RHYTHM, HELPER, DMOD, NO-DOC, NO-DESIGN, NO-TEACHER and NO-LAYOUT-ASSERT (a physical layout no assertion pins).

## Phase 2: symbol-to-comment sync
For every function, struct, field, constant, macro or parameter renamed, removed or changed in meaning by the diff:
- Grep the old name everywhere, comments included: `grep -rnw <old_name> .` (for generic names grep the qualified form, `Index.close`, `index_close`, scoped to the module directory, `grep -l` first).
- Check the declaration side too: headers, interfaces, `.pyi`, `.d.ts`, public docs, the module Design comment, coupled Checklist comments, `shortcut:` triggers.
- A stale Design, Teacher or Checklist comment is **P0 Comment Desync**: a wrong diagram is worse than none.

## Phase 3: the antirez 9-kind filter
This overrides honey-review's "redundant narration" rule for source comments.
- **Protect, never flag for deletion:** Function, Design, Why, Teacher (diagrams, math, numbered steps), Checklist, Guide.
- **Delete:** Trivial (reading it costs as much as the code: `i++; // increment i`, `// Constructor for User`); Backup (commented-out code or debug prints).
- **Validate:** Debt. Only `shortcut: <quantified ceiling>; <revisit trigger>` and `FIXME: <defect>; <trigger condition>`. A bare `TODO`, or a marker without ceiling or trigger, gets a concrete rewrite.
- **Missing positives are findings too:** a function >= 40 LOC without Guide comments, pointer math or bitfields without a Teacher diagram, a public function without a Function comment, a module without a Design comment, a bug fix without a Why comment. `craft-metrics.py` reports the first four as RHYTHM, NO-TEACHER, NO-DOC and NO-DESIGN; it sees presence only, so read what those comments say and flag the wrong or empty ones.

## Phase 4: Lever 1 cuts
Premature abstractions (interface with one implementation, factory for one product), dead code, single-caller helpers < 30 LOC without a `Why:`, shallow modules (D_mod < 15), hand-rolled stdlib, speculative parameters. Never flag input validation, error handling, auth, secrets or failure-path tests as over-engineering.

## Output: the delete-list
```text
DELETE / FIX LIST:
1. [net.c:42-68] Inline single-caller helper parseHeaderInternal (18 LOC) into parsePacket under one Guide comment. (-14 LOC)
2. [net.c:104-119] Delete Backup comment: commented-out legacy parser. (-16 LOC)
3. [net.c:130,145,162] Delete 3 Trivial comments restating assignments. (-3 LOC)
4. [sds.h:18] P0 Comment Desync: Teacher diagram names field free:1B, the struct field is alloc. Sync the diagram. (0 LOC)
5. [cache.c:210] Debt marker without trigger "TODO: optimize lookup" -> "shortcut: O(n) scan, <50us under 256 items; switch to binary search if n > 1024". (0 LOC)
Net: -33 LOC, 0 positive comments lost.
```
Order by severity: P0 desync and correctness first, then Backup, Trivial, Lever 1 cuts, debt rewrites. The last line always states the LOC delta and that no positive comment was lost.

## Large diffs
Over ~400 changed lines or ~10 files, delegate the audit to the `hive-craft-reviewer` subagent: it runs Phases 1-4 in its own context and returns columnar JSON keyed by `id`. Verify `n` equals the row count, then render the rows as the delete-list above for the user; never show the JSON itself.
