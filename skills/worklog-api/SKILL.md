---
name: worklog-api
description: Read and change auto-worklog logs over its local HTTP API (list days, read a day, save lines, delete a day, rebuild from sessions, export Markdown/CSV/JSON). Use when a script, another tool, or the user wants to talk to the worklog server, or asks about the worklog API or endpoints.
---

# auto-worklog HTTP API

A small JSON API that comes with `worklog.py serve`. It is the same API the web page uses.

- Base URL: `http://127.0.0.1:8765` (the port is `port` in the config; `serve --port N` changes it)
- Local only: it listens on 127.0.0.1 and refuses other `Host` headers.
- Every call that **changes** data (PUT, DELETE, POST) must send the header `X-Worklog: 1`. Without it the answer is `403`.
- Dates are always `YYYY-MM-DD`. Errors come back as `{"error": "..."}` with a 4xx status.

## Start the server

Check first: `curl -s 127.0.0.1:8765/api/config`. If that fails, start it **in the background** (it runs until stopped):

```bash
python3 WORKLOG_PY serve          # Windows: python
```

WORKLOG_PY means: the file `worklog.py` two folders above this skill's base directory (`<skill base dir>/../../worklog.py`).

## Endpoints

| Method | Path | What it does |
|---|---|---|
| GET | `/api/config` | Settings: `target_hours`, `workdays`, `day_end`, `date_format`, `round_to_hours`, `sources`, `output_dir` |
| GET | `/api/days` | Every saved day, newest first: `{target, workdays, days: [Day]}` |
| GET | `/api/day/DATE` | One day (also works for a day with no log: `exists: false`) |
| PUT | `/api/day/DATE` | Replace the lines of a day. Body: `{"entries": [{"hours": 2.5, "text": "project: what was done"}]}` |
| DELETE | `/api/day/DATE` | Delete the day's log. Automatic runs will not bring it back. |
| POST | `/api/day/DATE/rebuild` | Build the day again from the Claude/Codex sessions (AI call, can take 10–60 s). **Replaces hand edits.** |
| GET | `/api/export?from=DATE&to=DATE&format=md` | Export a range. `format`: `md` (default), `csv` or `json`. Default range: this week. |

### Day object

```json
{
  "date": "2026-11-24", "label": "24.11.2026", "weekday": "Tue",
  "entries": [{"hours": 2.25, "text": "shop-migration: Built the admin dashboard with run, pause and logs"}],
  "notes": [], "total": 2.25,
  "edited": true,     // changed by hand: automatic runs leave it alone
  "exists": true
}
```

`text` is `"<project>: <what was done>"`. A text with no `project:` part is fine too.

## Examples

```bash
B=http://127.0.0.1:8765

# this week's days and totals
curl -s $B/api/days | python3 -c 'import json,sys; [print(d["date"], d["total"]) for d in json.load(sys.stdin)["days"][:7]]'

# read one day
curl -s $B/api/day/2026-11-24

# add a line to a day: read, append, write back
curl -s $B/api/day/2026-11-24 | python3 -c '
import json,sys; d=json.load(sys.stdin)
d["entries"].append({"hours": 0.5, "text": "admin: Team standup"})
print(json.dumps({"entries": d["entries"], "notes": d["notes"]}))' |
curl -s -X PUT -H "X-Worklog: 1" -H "Content-Type: application/json" --data-binary @- $B/api/day/2026-11-24

# rebuild from sessions (ask the user first if the day has hand edits)
curl -s -X POST -H "X-Worklog: 1" $B/api/day/2026-11-24/rebuild

# delete a day
curl -s -X DELETE -H "X-Worklog: 1" $B/api/day/2026-11-24

# export November as Markdown
curl -s "$B/api/export?from=2026-11-01&to=2026-11-30&format=md" -o worklog-november.md
```

## Rules

- PUT replaces **all** lines of the day. Always read the day first and send back the full list.
- Send `notes` back as you got them, or they are lost.
- A day changed with PUT counts as hand-edited, so automatic runs will not overwrite it.
- Ask the user before `rebuild` or `DELETE` on a day that has lines.
- No server needed for simple work: the CLI does the same (`show`, `week`, `add`, `export`). See the `worklog` skill.
