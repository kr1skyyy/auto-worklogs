---
name: worklog
description: View, change, add, remove, rebuild or export the daily work log that auto-worklog makes from Claude Code, Codex and Cursor sessions. Use when the user asks about their work log, timesheet, hours, what they did on a day or week, or wants to change hours, add, edit or delete an entry, rebuild a day, or export for Jira.
---

# auto-worklog

One log file per workday (default folder `~/worklog/`, file `YYYY-MM-DD.md`):

```
24.11.2026
 2.25h - shop-migration: Built the admin dashboard with run, pause and logs
 1.5h - mobile-app: Planned the next onboarding steps
```

Use only these CLI commands. They need no server. WORKLOG_PY means: __WORKLOG_PY__
Run it with `python3` (on Windows: `python`). Dates: `YYYY-MM-DD`, `today` or `yesterday`.

## View

| Goal | Command |
|---|---|
| One day, numbered lines | `python3 WORKLOG_PY view 2026-11-24` (`--json` for data) |
| Last N days | `python3 WORKLOG_PY show 5` |
| A week + total | `python3 WORKLOG_PY week [DATE]` |

## Change

Always run `view DATE` first to get the line numbers.

| Goal | Command |
|---|---|
| Change hours of line N | `python3 WORKLOG_PY set DATE N --hours 2.5` |
| Change text of line N | `python3 WORKLOG_PY set DATE N --text "project: what was done"` |
| Add a line | `python3 WORKLOG_PY add DATE "1h - project: what was done"` |
| Remove line N | `python3 WORKLOG_PY rm DATE N` |
| Make the day add up to 8h (or X) | `python3 WORKLOG_PY fit DATE [X]` |
| Delete the whole day | `python3 WORKLOG_PY delete DATE` |
| Build the day again from sessions | `python3 WORKLOG_PY rebuild DATE` (AI call, 10–60 s) |
| Build a missing day / fill a range | `python3 WORKLOG_PY day DATE` / `backfill --since DATE` |

Every change command prints the day again: show that to the user.

To move time between lines, use two `set` commands (lower one, raise the other).

## Rules

- A day changed with these commands counts as **edited by you**: the automatic run never overwrites it.
- `rebuild` and `day` **replace** the user's changes, and `delete` removes the day. Ask the user before you run them on a day that has lines.
- Lines added with `add` are kept when a day is rebuilt.
- Text format: `project: what was done`, 6 to 16 words, past tense. Keep the user's words when they give them.

## Export (for Jira and reports)

`python3 WORKLOG_PY export --week DATE` or `export --from DATE --to DATE`; `--format md|csv|json`; `--out file.md` to save.

## Web page for the user

If the user wants to see the logs in a browser: run `python3 WORKLOG_PY serve` in the background (it runs until stopped) and give them http://127.0.0.1:8765. Do not use the page yourself: use the commands above.

## Setup and settings

| Goal | Command |
|---|---|
| Choose tools | `python3 WORKLOG_PY install --tools claude,codex,cursor` (or `both`, `all`) |
| Check the Cursor reader (supported, not tested) | `python3 WORKLOG_PY doctor cursor` |
| Settings | `python3 WORKLOG_PY config` (file: `~/.auto-worklog/config.json`, holds only the user's changes) |

Keys: `workdays` (0=Mon..6=Sun), `day_end` ("19:00"), `weekend` ("ignore" or "monday"), `target_hours` (8), `round_to_hours` (0.25), `min_task_minutes` (15), `idle_minutes`, `output_dir`, `date_format`, `summarizer` (auto|claude|codex|cursor|none), `claude_model`, `sources` (claude, codex, cursor), `exclude_projects`. Background messages: `~/.auto-worklog/log.txt`.
