# auto-worklog

A daily work log, written for you from your **Claude Code**, **Codex** and **Cursor** sessions.

| Tool | Status |
|---|---|
| Claude Code | ✅ Supported and tested |
| Codex | ✅ Supported and tested |
| Cursor | ⚠️ Supported, **not tested yet**: please run `python3 worklog.py doctor cursor` and paste the output in an issue |

```
24.11.2026
 3.5h - replatforming: Mapped Magento product attributes to Shopify metafields
 2.25h - frontend project: Fixed mobile layout of the fit widget
 0.5h - admin: Team standup
```

- Runs hidden when Claude Code, Codex or Cursor starts. Startup stays fast.
- Writes a log for each finished workday (Monday to Friday, after 19:00 by default).
- Hours are **real measured time** from your session activity. If two projects run at the same time, the time is split between them, so a day never has more hours than the clock.
- One line per task, ready for Jira, written by the AI tool you already use (`claude -p`, `codex exec` or `cursor-agent -p`). Or `--no-ai` for facts only.
- A small local web page to view and edit the logs, with a total against an 8h day.
- Works on Linux, macOS and Windows. Needs Python 3.8+. No packages.

## Install

```bash
git clone https://github.com/kr1skyyy/auto-worklogs
python3 auto-worklogs/worklog.py install      # Windows: python
```

It asks which tools you use: **Claude Code, Codex, Cursor** (pick one or more). Then it:
- adds the Claude Code plugin (startup hook + `/worklog` command + `worklog` and `worklog-api` skills),
- adds a startup hook and the skills to `~/.codex` (a backup of `hooks.json` is kept). Codex needs `hooks = true` under `[features]` in `~/.codex/config.toml`,
- adds a `sessionStart` hook to `~/.cursor/hooks.json` (a backup is kept).

Without questions: `install --tools claude,codex,cursor` (or `both`, `all`).

Claude Code only, from inside Claude Code:

```
/plugin marketplace add kr1skyyy/auto-worklogs
/plugin install auto-worklog@auto-worklog
/worklog setup
```

## Use

| What | Command |
|---|---|
| Web page (view, edit, add, delete, rebuild) | `python3 worklog.py serve` → http://127.0.0.1:8765 |
| Last 5 days | `python3 worklog.py show` |
| A week with total hours | `python3 worklog.py week [YYYY-MM-DD]` |
| Build a day now | `python3 worklog.py day today` |
| Add a manual line | `python3 worklog.py add 2026-11-24 "0.5h - admin: Team standup"` |
| Fill older days | `python3 worklog.py backfill --since 2026-11-01` |
| Remove the hooks | `python3 worklog.py uninstall` |
| Check the Cursor reader | `python3 worklog.py doctor cursor` |

In Claude Code: `/worklog`, `/worklog week`, `/worklog web`, or ask in plain words ("add 1h for the client call yesterday").

Logs are plain text files: `~/worklog/YYYY-MM-DD.md`. Edit them in any editor.

## Cursor (supported, not tested)

Cursor keeps chats in a SQLite file that is not documented and changes between versions:

| OS | Chat database |
|---|---|
| macOS | `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` |
| Windows | `%APPDATA%\Cursor\User\globalStorage\state.vscdb` |
| Linux | `~/.config/Cursor/User/globalStorage/state.vscdb` |

- The reader opens it **read-only** and understands two layouts: messages inside `composerData:<id>` (`conversation`), and one `bubbleId:<chat>:<message>` row per message (`fullConversationHeadersOnly`).
- Times come from `createdAt` / `timestamp` on each message. A message without a time is placed between the chat's `createdAt` and `lastUpdatedAt`.
- The project folder comes from `workspaceStorage/<hash>/workspace.json` + `composer.composerData`. Chats with no folder are logged under `cursor`.
- Not read yet: Cursor CLI chats (`~/.cursor/chats/*/*/store.db`, unknown format) and `~/.cursor/projects/*/agent-transcripts` (no times).
- Another place for the files: set `cursor_app_dir` and `cursor_dir` in the config.

**Help test it:** run `python3 worklog.py doctor cursor`. It prints only counts and field names, never your chat text. Paste the output in a GitHub issue.

## API

`serve` also gives a local JSON API (127.0.0.1 only). Calls that change data need the header `X-Worklog: 1`.

| Method | Path | What it does |
|---|---|---|
| GET | `/api/config` | Settings |
| GET | `/api/days` | All saved days with lines and totals |
| GET | `/api/day/YYYY-MM-DD` | One day |
| PUT | `/api/day/YYYY-MM-DD` | Replace a day's lines: `{"entries":[{"hours":2.5,"text":"project: what"}]}` |
| DELETE | `/api/day/YYYY-MM-DD` | Delete a day |
| POST | `/api/day/YYYY-MM-DD/rebuild` | Build the day again from sessions |
| GET | `/api/export?from=&to=&format=md\|csv\|json` | Export a range |

Full guide with examples: [`skills/worklog-api/SKILL.md`](skills/worklog-api/SKILL.md).

## Rules it follows

- **Your edits win.** A day you changed by hand (in a file or in the web page) is never overwritten. Only "Rebuild" / `day DATE` replace it.
- Lines added with `add` stay when a day is rebuilt.
- Work after 19:00 is added at the next startup. A deleted day does not come back by itself.
- First run fills the last 7 days. Later runs catch up from the last run (max 31 days, because Claude Code deletes sessions after 30 days by default).
- A lock file makes sure only one background run works at a time.

## Settings

`~/.auto-worklog/config.json` (`python3 worklog.py config` prints it):

| Key | Default | Meaning |
|---|---|---|
| `sources` | both | any of `["claude", "codex", "cursor"]` |
| `cursor_app_dir` | OS default | Cursor's `User` folder |
| `workdays` | `[0,1,2,3,4]` | 0 = Monday … 6 = Sunday |
| `day_end` | `"19:00"` | a day is finished after this time |
| `weekend` | `"ignore"` | or `"monday"`: add weekend work to Monday |
| `target_hours` | `8` | normal workday, used by the web page |
| `round_to_hours` | `0.25` | round each project up to this step; 0 = exact hours |
| `idle_minutes` | `15` | a longer gap between messages is a break |
| `min_minutes` | `1` | hide lines with less time |\n| `min_task_minutes` | `15` | shorter tasks of one project are joined into one "Small tasks: a; b; c" line (0 = off) |
| `summarizer` | `"auto"` | `claude`, `codex` or `none` |
| `claude_model` | `"haiku"` | model for the summary |
| `output_dir` | `~/worklog` | where logs go |
| `date_format` | `"%d.%m.%Y"` | first line of each log |
| `exclude_projects` | `[]` | project names to skip |

Background messages: `~/.auto-worklog/log.txt`.

## Privacy

Session files are read only on your computer. For the summary, only your prompts (cut to 300 characters) and the names of changed files are sent, and only to the AI tool you already use. The helper runs without saving a session, so it never shows up in your log. The web page listens on 127.0.0.1 only and refuses other hosts and other web sites.

## License

MIT
