---
description: Set the Honey + Systems Craft mode (lite, full, ultra, off) for this session, or with "default" for every new session.
argument-hint: "[lite|full|ultra|off] | default [lite|full|ultra|off]"
allowed-tools: Bash(bash:*)
disable-model-invocation: true
---

Mode: !`bash "${CLAUDE_PLUGIN_ROOT}/bin/craft-mode.sh" command "$ARGUMENTS"`

If the line above is missing or not of the form `mode=… scope=… default=…` or `error=…`, run `bash "${CLAUDE_PLUGIN_ROOT}/bin/craft-mode.sh" command "$ARGUMENTS"` once yourself (outside Claude Code the script is `bin/craft-mode.sh` at the plugin root).

The forms:
- `/honey` — back to the default mode in this session.
- `/honey <mode>` — this session only; the default is unchanged.
- `/honey default <mode>` — save `<mode>` as the default for new sessions and apply it now.
- `/honey default` — show the default; change nothing.

Reply with exactly one line and nothing else:
- `error=<text>` → `<text>`
- `scope=show` → `Default mode: <default>.`
- `scope=default` → `🍯 Default mode set to <mode>, now and in new sessions.` (mode `off`: `Honey default set to off, now and in new sessions.`)
- `scope=session` → `🍯 Honey + Systems Craft on (<mode>) for this session; default: <default>.` (mode `off`: `Honey mode off for this session; default: <default>.`)

Unless the line was `error=` or `scope=show`, apply `<mode>` for the rest of the session. The SessionStart hook injects the default in new sessions and after `/clear` or compaction, so a session-only mode lasts until then:
- **full** — chat under Lever 2: answer first, fragments, no wind-up, hedging or recap; never abbreviate words (`config`, not `cfg`). Code under Lever 1 and the 9 comment kinds of AGENTS.md: Function, Design, Why, Teacher, Checklist and Guide comments are written in full; Trivial and Backup comments never; debt only as `shortcut: <ceiling>; <trigger>` or `FIXME: <defect>; <trigger>`.
- **lite** — same code rules; chat may explain in full sentences (design, trade-offs, teaching).
- **ultra** — answer-only chat and loop reports. For read-only files ≥ 6000 chars, Grep an outline first, then Read with offset/limit (`honey-px` if installed); never on a file you will Edit. Code comments are unchanged: their reader is a human.
- **off** — no chat compression. Lever 1, the comment rules and the zero-token hooks stay on.

In every mode step up to `lite`, never down, when brevity would drop correctness: a subtle bug, a trade-off, a correctness argument, a learner who needs the explanation.
