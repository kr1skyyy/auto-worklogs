---
name: worklog
description: View, build, add to or change the daily work log that auto-worklog makes from Claude Code, Codex and Cursor sessions. Use when the user asks about their work log, timesheet, what they did on a day or week, hours per project, or wants to add, fix, remove or rebuild a log entry.
---

# auto-worklog

Daily logs live in one folder (default `~/worklog/`), one file per workday, named `YYYY-MM-DD.md`:

```
24.11.2026
 4h - shop-migration: Mapped Magento product attributes to Shopify metafields
 1.5h - mobile-app: Planned the next onboarding steps
```

The script is `worklog.py`. WORKLOG_PY below means: __WORKLOG_PY__
Run it with `python3` (on Windows: `python`).

## Commands

| Goal | Command |
|---|---|
| Show recent days | `python3 WORKLOG_PY show 5` |
| Show a week + total hours | `python3 WORKLOG_PY week [YYYY-MM-DD]` |
| Build a log now (today, yesterday or a date) | `python3 WORKLOG_PY day today` (add `--dry-run` to only print) |
| Fill all missing days | `python3 WORKLOG_PY backfill --since YYYY-MM-DD` |
| Add a manual line | `python3 WORKLOG_PY add YYYY-MM-DD "2h - Client call about launch"` |
| File of a day / log folder | `python3 WORKLOG_PY path [YYYY-MM-DD]` |
| Web page to view and edit (127.0.0.1 only) | `python3 WORKLOG_PY serve` (runs until stopped; start it in the background) |
| Choose tools | `python3 WORKLOG_PY install --tools claude,codex,cursor` (or `both`, `all`) |
| Check the Cursor reader (supported, not tested) | `python3 WORKLOG_PY doctor cursor` |
| Settings | `python3 WORKLOG_PY config` |

## Changing a log

- To change or remove a line, edit the day's file directly (get it with `path DATE`).
- A file edited by hand is protected: the automatic run never overwrites it.
- `day DATE` rebuilds the day from the sessions and **replaces hand edits**. Ask the user before you run it on a day that already has a file.
- Lines added with `add` are kept when a day is rebuilt.
- Keep the format: first line is the date, then one line per entry: ` <hours>h - <project>: <what was done>`.

## Settings

The config file is `~/.auto-worklog/config.json` (`config` prints it). Keys: `workdays` (0=Mon..6=Sun), `day_end` (`"19:00"`), `weekend` (`"ignore"` or `"monday"`), `output_dir`, `date_format`, `summarizer` (`auto|claude|codex|none`), `claude_model`, `idle_minutes`, `min_minutes`, `min_task_minutes` (15), `round_to_hours` (0.25 = round up to quarter hours, 0 = exact), `target_hours` (8), `port`, `sources` (claude, codex, cursor), `cursor_app_dir`, `exclude_projects`.
Background run messages go to `~/.auto-worklog/log.txt`.
