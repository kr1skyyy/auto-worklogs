---
description: View or change your daily work log (show, week, today, add, backfill)
argument-hint: "[show|week|today|day DATE|add DATE \"2h - text\"|backfill --since DATE]"
---

Use the `worklog` skill. The user asked: `/worklog $ARGUMENTS`

If the argument is `setup`: ask the user which tools they use (Claude Code, Codex, or both), then run `install --tools claude|codex|both --no-plugin`.
If the argument is `web` or `serve`: start `serve` in the background and give the user the link.
If no arguments were given, show the last 5 days. Then show the result as it is, with no extra comment.
