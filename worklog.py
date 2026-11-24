#!/usr/bin/env python3
"""auto-worklog: daily work log built from your Claude Code and Codex sessions.

Commands:
  hook                  called by the SessionStart hook; starts "catchup" in the background
  catchup               write logs for finished workdays that have no log (or have new work)
  day DATE              build (and save) the log for one date, YYYY-MM-DD, "today" or "yesterday"
  backfill --since DATE write logs for every workday from DATE until now
  show [N]              print the last N saved logs (default 5)
  week [DATE]           print the logs of one week (Mon-Sun) with the total hours
  add DATE "2h - text"  add a manual line to a day; it stays when the day is rebuilt
  path [DATE]           print the folder of the logs, or the file of one day
  serve [--port N]      open a small local web page to view and edit the logs (127.0.0.1 only)
  export [--week DATE | --from DATE --to DATE] [--format md|csv|json] [--out FILE]
                        export logs; default: this week as Markdown
  install [--tools claude,codex,cursor|all]   set up: asks which tools you use, adds the startup hooks
  uninstall             remove the startup hooks (your logs stay)
  install-codex         only add the Codex startup hook + skill (~/.codex)
  install-cursor        only add the Cursor startup hook (~/.cursor/hooks.json)   [supported, not tested]
  doctor cursor         show what the Cursor reader finds (counts and field names only, no chat text)
  uninstall-codex       remove it again
  config                print the config file path and its values

Flags: --no-ai (facts only, no AI call), --force (rewrite existing logs), --dry-run (print, do not save)
"""
import datetime as dt
import hashlib
import math
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

HOME = Path.home()
STATE_DIR = Path(os.environ.get("AUTO_WORKLOG_HOME", HOME / ".auto-worklog"))
CONFIG_FILE = STATE_DIR / "config.json"
STATE_FILE = STATE_DIR / "state.json"
LOCK_FILE = STATE_DIR / "catchup.lock"
LOG_FILE = STATE_DIR / "log.txt"
RUNNER_DIR = STATE_DIR / "runner"
SCRIPT = Path(__file__).resolve()
IS_WIN = os.name == "nt"
CHILD_ENV = "WORKLOG_CHILD"

DEFAULTS = {
    "output_dir": str(HOME / "worklog"),
    "workdays": [0, 1, 2, 3, 4],        # Monday=0 ... Sunday=6
    "day_end": "19:00",                  # a day counts as finished after this local time
    "weekend": "ignore",                 # "ignore" or "monday" (add weekend work to Monday's log)
    "date_format": "%d.%m.%Y",
    "summarizer": "auto",                # "auto", "claude", "codex" or "none"
    "claude_model": "sonnet",            # model that writes the summary
    "codex_model": "",                   # empty = your Codex default
    "idle_minutes": 15,                  # a gap longer than this between messages is a break
    "min_minutes": 1,                    # drop lines with less time than this
    "min_task_minutes": 15,              # shorter tasks of one project are joined into one "Small tasks: a; b; c" line (0 = off)
    "round_to_hours": 0.25,              # round each project UP to this step; 0 = exact measured hours
    "target_hours": 8,                   # a normal workday, shown in the web page
    "port": 8765,
    "first_run_days": 7,
    "max_backfill_days": 31,
    "claude_dir": str(HOME / ".claude"),
    "codex_dir": str(HOME / ".codex"),
    "exclude_projects": [],
    "sources": ["claude", "codex"],      # which tools to read sessions from: claude, codex, cursor (set by "install")
    "cursor_dir": str(HOME / ".cursor"),
    "cursor_app_dir": "",                # Cursor's "User" folder; empty = the normal place for this OS
}

# ---------------------------------------------------------------- utils

def log(msg):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        cfg.update(load_json(CONFIG_FILE, {}))
    else:
        save_json(CONFIG_FILE, {})  # only your changes live here; "config" prints every value
    return cfg


def parse_ts(s):
    if not s:
        return None
    try:
        if isinstance(s, (int, float)):
            t = dt.datetime.fromtimestamp(s / 1000 if s > 1e11 else s, dt.timezone.utc)
        else:
            t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=dt.timezone.utc)
        return t.astimezone()  # local time
    except (ValueError, OSError, OverflowError):
        return None


def parse_day(s):
    s = s.strip().lower()
    today = dt.date.today()
    if s == "today":
        return today
    if s == "yesterday":
        return today - dt.timedelta(days=1)
    return dt.date.fromisoformat(s)


def day_end_time(cfg):
    h, m = (int(x) for x in cfg["day_end"].split(":"))
    return dt.time(h, m)


_root_cache = {}

def project_of(cwd):
    """Name a project by its git root folder (walks up), else by the folder itself."""
    if not cwd:
        return "unknown"
    if cwd in _root_cache:
        return _root_cache[cwd]
    p = Path(cwd)
    name = None
    for q in [p, *p.parents]:
        if (q / ".git").exists():
            name = q.name
            break
        if q == HOME:
            break
    if name is None:
        name = "home" if p == HOME else p.name
    _root_cache[cwd] = name
    return name


def clean_prompt(text):
    if not isinstance(text, str):
        return None
    t = text.strip()
    if not t or t.startswith("<") or t.startswith("[Request interrupted") or t.startswith("Caveat:"):
        return None
    t = re.sub(r"\s+", " ", t)
    return t[:300]

# ---------------------------------------------------------------- readers
# Each reader yields events: dict(ts, project, session, source, prompt=None, file=None)

def iter_jsonl(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
    except OSError:
        return


def read_claude(cfg, since):
    base = Path(cfg["claude_dir"]) / "projects"
    if not base.is_dir():
        return
    cutoff = since.timestamp()
    for path in base.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        for d in iter_jsonl(path):
            if d.get("type") not in ("user", "assistant"):
                continue
            ts = parse_ts(d.get("timestamp"))
            if not ts or ts < since:
                continue
            ev = {"ts": ts, "project": project_of(d.get("cwd")), "cwd": d.get("cwd") or "",
                  "session": "c:" + str(d.get("sessionId")), "source": "claude"}
            msg = d.get("message") or {}
            content = msg.get("content")
            if d["type"] == "user" and not d.get("isSidechain") and not d.get("isMeta"):
                if isinstance(content, str):
                    ev["prompt"] = clean_prompt(content)
                elif isinstance(content, list):
                    texts = [c.get("text") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                    if texts:
                        ev["prompt"] = clean_prompt(" ".join(t for t in texts if t))
            elif d["type"] == "assistant" and isinstance(content, list):
                texts = [c.get("text") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                if texts and not d.get("isSidechain"):
                    ev["reply"] = re.sub(r"\s+", " ", " ".join(t for t in texts if t)).strip()[:500] or None
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        fp = (c.get("input") or {}).get("file_path")
                        if fp and c.get("name") in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                            ev["file"] = fp
            yield ev


def read_codex(cfg, since):
    base = Path(cfg["codex_dir"]) / "sessions"
    if not base.is_dir():
        return
    cutoff = since.timestamp()
    for path in base.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        cwd, session = "", "x:" + path.stem
        for d in iter_jsonl(path):
            p = d.get("payload") or {}
            typ = d.get("type")
            if typ == "session_meta":
                cwd = p.get("cwd") or cwd
                session = "x:" + str(p.get("id") or p.get("session_id") or path.stem)
                continue
            if typ == "turn_context" and p.get("cwd"):
                cwd = p["cwd"]
            ts = parse_ts(d.get("timestamp"))
            if not ts or ts < since:
                continue
            ev = {"ts": ts, "project": project_of(cwd), "cwd": cwd, "session": session, "source": "codex"}
            if typ == "response_item" and p.get("type") == "message" and p.get("role") == "assistant":
                texts = [c.get("text") for c in p.get("content") or [] if isinstance(c, dict) and c.get("text")]
                if texts:
                    ev["reply"] = re.sub(r"\s+", " ", " ".join(texts)).strip()[:500]
            if typ == "event_msg":
                if p.get("type") == "user_message":
                    ev["prompt"] = clean_prompt(p.get("message"))
                elif p.get("type") == "item_completed":
                    item = p.get("item") or {}
                    if item.get("type") == "UserMessage":
                        texts = [c.get("text") for c in item.get("content") or [] if isinstance(c, dict)]
                        ev["prompt"] = clean_prompt(" ".join(t for t in texts if t))
            yield ev


# ---------------------------------------------------------------- Cursor (supported, not tested)
# Cursor keeps chats in a SQLite file (state.vscdb). The layout is not documented and changes between
# versions, so this reader tries the known layouts and skips what it does not understand.

def cursor_app_dir(cfg):
    if cfg.get("cursor_app_dir"):
        return Path(os.path.expanduser(cfg["cursor_app_dir"]))
    if sys.platform == "darwin":
        return HOME / "Library" / "Application Support" / "Cursor" / "User"
    if IS_WIN:
        return Path(os.environ.get("APPDATA") or HOME / "AppData" / "Roaming") / "Cursor" / "User"
    return Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "Cursor" / "User"


def open_ro(path):
    """Open a SQLite file read-only, while Cursor may be writing to it."""
    uri = Path(path).resolve().as_uri()
    try:
        con = sqlite3.connect(uri + "?mode=ro", uri=True, timeout=5)
        con.execute("select 1 from sqlite_master limit 1")
        return con
    except sqlite3.Error:
        return sqlite3.connect(uri + "?mode=ro&immutable=1", uri=True, timeout=5)


def kv_tables(con):
    names = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    return [t for t in ("cursorDiskKV", "ItemTable") if t in names]


def kv_get(con, tables, key):
    for t in tables:
        row = con.execute(f"select value from {t} where key=?", (key,)).fetchone()
        if row and row[0] is not None:
            v = row[0]
            return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else v
    return None


def as_ts(v):
    """Cursor times are ms numbers or ISO strings."""
    if v is None or v == "":
        return None
    if isinstance(v, str) and v.strip().isdigit():
        v = int(v)
    return parse_ts(v)


def bubble_time(b):
    t = b.get("timingInfo") or {}
    for v in (b.get("createdAt"), b.get("timestamp"), b.get("serverTimestamp"),
              t.get("clientStartTime"), t.get("clientEndTime")):
        ts = as_ts(v)
        if ts:
            return ts
    return None


def cursor_workspace_map(cfg):
    """composerId -> project folder, from workspaceStorage/<hash>/{workspace.json,state.vscdb}."""
    from urllib.parse import unquote, urlparse
    out = {}
    base = cursor_app_dir(cfg) / "workspaceStorage"
    if not base.is_dir():
        return out
    for ws in base.iterdir():
        meta = load_json(ws / "workspace.json", {})
        folder = meta.get("folder") or meta.get("workspace") or ""
        if folder.startswith("file://"):
            u = urlparse(folder)
            folder = unquote(u.path)
            if IS_WIN and re.match(r"^/[A-Za-z]:", folder):
                folder = folder[1:]
        if not folder or not (ws / "state.vscdb").exists():
            continue
        try:
            con = open_ro(ws / "state.vscdb")
            raw = kv_get(con, kv_tables(con), "composer.composerData")
            con.close()
        except sqlite3.Error:
            continue
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            continue
        for c in data.get("allComposers") or []:
            if isinstance(c, dict) and c.get("composerId"):
                out[c["composerId"]] = folder
    return out


def cursor_text(b):
    t = b.get("text") or b.get("rawText") or ""
    if not t and isinstance(b.get("richText"), str):
        t = b["richText"]
    return t if isinstance(t, str) else ""


def read_cursor(cfg, since, stats=None):
    db = cursor_app_dir(cfg) / "globalStorage" / "state.vscdb"
    if not db.exists():
        return
    cutoff_ms = since.timestamp() * 1000
    try:
        con = open_ro(db)
        tables = kv_tables(con)
    except sqlite3.Error as e:
        log(f"cursor: cannot open {db}: {e}")
        return
    wmap = cursor_workspace_map(cfg)
    rows = []
    for t in tables:
        try:
            rows += con.execute(f"select key, value from {t} where key like 'composerData:%'").fetchall()
        except sqlite3.Error as e:
            log(f"cursor: query failed on {t}: {e}")
    for key, raw in rows:
        try:
            data = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw)
        except (ValueError, AttributeError):
            continue
        if not isinstance(data, dict):
            continue
        cid = data.get("composerId") or key.split(":", 1)[1]
        created = as_ts(data.get("createdAt"))
        updated = as_ts(data.get("lastUpdatedAt")) or created
        if stats is not None:
            stats["chats"] += 1
            stats["chat_keys"].update(data.keys())
            stats["chats_with_time"] += bool(created)
        if updated and updated.timestamp() * 1000 < cutoff_ms:
            continue
        # messages: old layout keeps them inline, new layout keeps only headers + one row per bubble
        bubbles = data.get("conversation")
        if not isinstance(bubbles, list) or not bubbles:
            bubbles = []
            for h in data.get("fullConversationHeadersOnly") or []:
                bid = h.get("bubbleId") if isinstance(h, dict) else None
                if not bid:
                    continue
                rawb = kv_get(con, tables, f"bubbleId:{cid}:{bid}")
                try:
                    b = json.loads(rawb) if rawb else None
                except ValueError:
                    b = None
                if isinstance(b, dict):
                    b.setdefault("type", h.get("type"))
                    bubbles.append(b)
        folder = wmap.get(cid, "")
        project = project_of(folder) if folder else "cursor"
        n = len(bubbles)
        for i, b in enumerate(bubbles):
            if not isinstance(b, dict):
                continue
            ts = bubble_time(b)
            if stats is not None:
                stats["messages"] += 1
                stats["bubble_keys"].update(b.keys())
                stats["messages_with_time"] += bool(ts)
            if not ts and created and updated and n > 1:  # no time on the message: spread over the chat
                ts = created + (updated - created) * (i / (n - 1))
            ts = ts or created
            if not ts or ts < since:
                continue
            ev = {"ts": ts, "project": project, "cwd": folder, "session": "u:" + cid, "source": "cursor"}
            text = cursor_text(b)
            if b.get("type") in (1, "user"):
                ev["prompt"] = clean_prompt(text)
            elif text:
                ev["reply"] = re.sub(r"\s+", " ", text).strip()[:500]
            if stats is not None:
                stats["recent_messages"] += 1
                stats["projects"].add(project)
            yield ev
    con.close()


def cmd_doctor(args, cfg):
    if not args or args[0] != "cursor":
        sys.exit("Use: doctor cursor")
    from collections import Counter
    app = cursor_app_dir(cfg)
    db = app / "globalStorage" / "state.vscdb"
    cdir = Path(os.path.expanduser(cfg["cursor_dir"]))
    print("auto-worklog doctor: Cursor (no chat text is printed)\n")
    print(f"Cursor app folder:   {app}  {'found' if app.exists() else 'NOT FOUND'}")
    print(f"chat database:       {db}  {'found, %.1f MB' % (db.stat().st_size / 1e6) if db.exists() else 'NOT FOUND'}")
    if db.exists():
        try:
            con = open_ro(db)
            print(f"tables:              {', '.join(kv_tables(con)) or 'none of cursorDiskKV/ItemTable'}")
            for t in kv_tables(con):
                for pat in ("composerData:%", "bubbleId:%", "agentKv%"):
                    n = con.execute(f"select count(*) from {t} where key like ?", (pat,)).fetchone()[0]
                    print(f"  {t:<12} {pat:<15} {n} rows")
            con.close()
        except sqlite3.Error as e:
            print(f"cannot read it: {e}")
    wmap = cursor_workspace_map(cfg)
    print(f"chats linked to a project folder: {len(wmap)}")
    stats = {"chats": 0, "chats_with_time": 0, "messages": 0, "messages_with_time": 0, "recent_messages": 0,
             "chat_keys": Counter(), "bubble_keys": Counter(), "projects": set()}
    since = dt.datetime.now().astimezone() - dt.timedelta(days=7)
    for _ in read_cursor(cfg, since, stats):
        pass
    print(f"\nchats:               {stats['chats']} ({stats['chats_with_time']} with a start time)")
    print(f"messages read:       {stats['messages']} ({stats['messages_with_time']} with their own time)")
    print(f"messages, last 7 days: {stats['recent_messages']} in {len(stats['projects'])} project(s)")
    print(f"chat fields:         {', '.join(k for k, _ in stats['chat_keys'].most_common(40))}")
    print(f"message fields:      {', '.join(k for k, _ in stats['bubble_keys'].most_common(40))}")
    tr = list((cdir / "projects").glob("*/agent-transcripts/**/*")) if (cdir / "projects").is_dir() else []
    chats = list((cdir / "chats").glob("*/*/store.db")) if (cdir / "chats").is_dir() else []
    print(f"\nagent transcripts:   {len([p for p in tr if p.is_file()])} files (not read yet: no times in them)")
    print(f"CLI chats (store.db): {len(chats)} (not read yet: format unknown)")
    hooks = load_json(cdir / "hooks.json", {})
    has = any("worklog.py" in (h.get("command") or "") for h in (hooks.get("hooks") or {}).get("sessionStart", []))
    print(f"startup hook:        {'installed' if has else 'not installed (run: install-cursor)'}")
    print("\nPlease paste this output into a GitHub issue so we can mark Cursor as tested.")


def collect(cfg, since):
    excl = set(cfg.get("exclude_projects") or [])
    runner = str(RUNNER_DIR)
    events = []
    sources = cfg.get("sources") or ["claude", "codex"]
    readers = [r for name, r in (("claude", read_claude), ("codex", read_codex), ("cursor", read_cursor))
               if name in sources]
    for reader in readers:
        for ev in reader(cfg, since):
            if ev["cwd"].startswith(runner) or ev["project"] in excl:
                continue
            events.append(ev)
    return events

# ---------------------------------------------------------------- time math

def work_day_of(date, cfg):
    """Which log a calendar date belongs to (None = ignored)."""
    if date.weekday() in cfg["workdays"]:
        return date
    if cfg.get("weekend") == "monday":
        d = date
        while d.weekday() not in cfg["workdays"]:
            d += dt.timedelta(days=1)
        return d
    return None


def compute_minutes(events, cfg, key="project"):
    """Split real (wall clock) active minutes between projects.
    A minute where 2 projects are active counts half for each, so the total is never more than real time."""
    idle = dt.timedelta(minutes=cfg["idle_minutes"])
    tail = dt.timedelta(minutes=2)
    by_session = defaultdict(list)
    for ev in events:
        by_session[ev["session"]].append(ev)
    slots = defaultdict(set)  # minute -> projects
    for evs in by_session.values():
        evs.sort(key=lambda e: e["ts"])
        for i, ev in enumerate(evs):
            start = ev["ts"]
            nxt = evs[i + 1]["ts"] if i + 1 < len(evs) else None
            end = nxt if nxt and nxt - start <= idle else start + tail
            m = start.replace(second=0, microsecond=0)
            while m < end:
                slots[m].add(ev.get(key))
                m += dt.timedelta(minutes=1)
    minutes = defaultdict(float)
    for projs in slots.values():
        for p in projs:
            minutes[p] += 1.0 / len(projs)
    return minutes


def fmt_hours(minutes, cfg):
    step = float(cfg.get("round_to_hours") or 0)
    h = minutes / 60
    if step:
        h = max(step, math.ceil(h / step - 1e-9) * step)
    h = max(round(h, 2), 0.01)
    return (f"{h:.2f}".rstrip("0").rstrip(".")) + "h"

# ---------------------------------------------------------------- summarize

def build_turns(events):
    """A turn = one request you gave the agent, plus all activity until your next request."""
    by_session = defaultdict(list)
    for ev in events:
        by_session[ev["session"]].append(ev)
    turns = {}
    for sid, evs in by_session.items():
        evs.sort(key=lambda e: e["ts"])
        cur, n, pending = None, 0, []
        for ev in evs:
            if ev.get("prompt"):
                n += 1
                cur = f"{sid}#{n}"
                turns[cur] = {"id": cur, "project": ev["project"], "start": ev["ts"], "prompt": ev["prompt"],
                              "reply": None, "files": set(), "source": ev["source"]}
                for p in pending:  # activity before the first request belongs to it
                    p["turn"] = cur
                pending = []
            if cur is None:
                pending.append(ev)
                continue
            ev["turn"] = cur
            t = turns[cur]
            if ev.get("reply"):
                t["reply"] = ev["reply"]
            if ev.get("file"):
                t["files"].add(Path(ev["file"]).name)
        if pending:  # a session with no request of yours (e.g. automatic runs)
            tid = f"{sid}#0"
            turns[tid] = {"id": tid, "project": pending[0]["project"], "start": pending[0]["ts"], "prompt": None,
                          "reply": None, "files": set(), "source": pending[0]["source"]}
            for p in pending:
                p["turn"] = tid
    return turns


def pick_summarizer(cfg):
    want = cfg.get("summarizer", "auto")
    if want == "none":
        return None
    sources = cfg.get("sources") or ["claude", "codex"]
    order = [s for s in ("claude", "codex", "cursor") if s in sources] if want == "auto" else [want]
    for name in order:
        exe = "cursor-agent" if name == "cursor" else name
        if shutil.which(exe):
            return name
    return None


def run_ai(prompt, cfg):
    tool = pick_summarizer(cfg)
    if not tool:
        return None
    RUNNER_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, **{CHILD_ENV: "1"})
    kw = {"cwd": str(RUNNER_DIR), "env": env, "capture_output": True, "text": True,
          "encoding": "utf-8", "errors": "replace", "timeout": 300}
    if IS_WIN:
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    exe = shutil.which("cursor-agent" if tool == "cursor" else tool)
    try:
        if tool == "claude":
            cmd = [exe, "-p", "--no-session-persistence", "--output-format", "text",
                   "--settings", json.dumps({"disableAllHooks": True})]
            if cfg.get("claude_model"):
                cmd += ["--model", cfg["claude_model"]]
            r = subprocess.run(cmd, input=prompt, **kw)
            out = r.stdout
        elif tool == "cursor":  # not tested
            r = subprocess.run([exe, "-p", "--output-format", "text", prompt], **kw)
            out = r.stdout
        else:
            out_file = RUNNER_DIR / "last.txt"
            cmd = [exe, "exec", "--ephemeral", "--skip-git-repo-check", "-s", "read-only",
                   "-C", str(RUNNER_DIR), "-o", str(out_file), "-"]
            if cfg.get("codex_model"):
                cmd[2:2] = ["-m", cfg["codex_model"]]
            r = subprocess.run(cmd, input=prompt, **kw)
            out = out_file.read_text(encoding="utf-8") if out_file.exists() else r.stdout
        if r.returncode != 0:
            log(f"{tool} exit {r.returncode}: {(r.stderr or r.stdout)[-400:]}")
        return out
    except (OSError, subprocess.SubprocessError) as e:
        log(f"{tool} failed: {e}")
        return None


TASK_PROMPT = """You turn one day of a developer's AI coding sessions into a timesheet.
Each line below is one request ("turn") the developer gave an AI agent:
id | start time | active minutes | project folder | P: the request | R: the agent's last answer

Group the turns into TASKS. These lines will be logged in Jira, so one task = one piece of work that could be its own Jira ticket or worklog entry (one feature, one bug fix, one investigation, one review, one plan, one migration step, one setup).
Rules:
- Every turn id goes into exactly one task.
- Join turns that are the same piece of work: follow-ups ("ok", "continue", "yes", "build it"), screenshots, retries, and fixes of the thing just built.
- Keep different pieces of work in different tasks when each has 15 active minutes or more.
- A task must have at least 15 active minutes (add up the minutes of its turns). Put the SHORT pieces of one project together into ONE task whose text names each piece briefly, separated by semicolons (for example "Small fixes: rich text converter; gift card test import; location setup"). Do not hide short pieces inside a bigger, unrelated task.
- If the turns mention a ticket key (like ABC-123), start that task's text with it.
- "project": the folder name. Use a better name only when the work clearly belongs to another project (for example, a tool planned from the home folder that later got its own folder).
- "text": 6 to 16 words, past tense, concrete. Name the feature, page, bug, file, or system. No filler such as "worked on", "various", "discussed", "explored". Use only facts from the lines. Do not invent.
Answer with ONLY this JSON, no other text:
{"tasks":[{"project":"...","text":"...","turns":["t1","t4"]}]}

TURNS:
"""


def turn_lines(items, plen, rlen):
    out = []
    for key, t, mins in items:
        p = (t["prompt"] or "(no request: automatic activity)")[:plen]
        line = f"{key} | {t['start']:%H:%M} | {round(mins)}m | {t['project']} | P: {p}"
        if rlen and t.get("reply"):
            line += " | R: " + t["reply"][:rlen]
        if t["files"]:
            line += " | files: " + ", ".join(sorted(t["files"])[:8])
        out.append(line)
    return "\n".join(out)


def plan_tasks(evs, cfg, use_ai=True):
    """Returns [(project, text, minutes)] for one day."""
    turns = build_turns(evs)
    tmin = compute_minutes(evs, cfg, key="turn")
    items = [(f"t{i + 1}", turns[tid], tmin.get(tid, 0)) for i, tid in
             enumerate(sorted(turns, key=lambda k: turns[k]["start"]))]
    items = [it for it in items if it[2] > 0 or it[1]["prompt"]]
    by_key = {k: (t, m) for k, t, m in items}
    tasks = []
    if use_ai and items:
        for plen, rlen in ((400, 300), (200, 150), (120, 0)):
            body = turn_lines(items, plen, rlen)
            if len(body) < 90000:
                break
        out = run_ai(TASK_PROMPT + body, cfg)
        m = re.search(r"\{.*\}", out or "", re.S)
        if m:
            try:
                for t in json.loads(m.group(0)).get("tasks", []):
                    keys = [k for k in t.get("turns", []) if k in by_key]
                    if keys:
                        tasks.append({"project": str(t.get("project") or by_key[keys[0]][0]["project"]).strip(),
                                      "text": str(t.get("text") or "").strip(), "keys": keys})
            except (ValueError, AttributeError):
                log("AI answer was not valid JSON: " + (out or "")[:300])
        else:
            log("no AI answer; using plain summary")
    used = set()
    for t in tasks:  # a turn may only count once
        t["keys"] = [k for k in t["keys"] if k not in used]
        used.update(t["keys"])
    for k, t, m in items:  # turns the AI left out go to the biggest task of their project
        if k in used:
            continue
        same = [x for x in tasks if x["project"] == t["project"]]
        if same:
            max(same, key=lambda x: sum(by_key[q][1] for q in x["keys"]))["keys"].append(k)
        else:
            first = t["prompt"] or "work"
            tasks.append({"project": t["project"], "text": first[:90] + ("..." if len(first) > 90 else ""), "keys": [k]})
    result = []
    for t in tasks:
        mins = sum(by_key[k][1] for k in t["keys"])
        if mins > 0:
            result.append([t["project"], t["text"], mins, min(by_key[k][0]["start"] for k in t["keys"])])
    # safety net: join the short tasks of a project into one line that names each of them
    small = float(cfg.get("min_task_minutes", 15))
    if small > 0:
        for proj in {r[0] for r in result}:
            shorts = [r for r in result if r[0] == proj and r[2] < small]
            if len(shorts) < 2:
                continue
            shorts.sort(key=lambda r: r[3])
            parts = [re.sub(r"^(small (fixes|tasks)|misc\w*)\s*:\s*", "", r[1], flags=re.I).rstrip(".") for r in shorts]
            joined = [proj, "Small tasks: " + "; ".join(parts), sum(r[2] for r in shorts), shorts[0][3]]
            result = [r for r in result if r not in shorts] + [joined]
    result = [r for r in result if r[2] >= float(cfg["min_minutes"])]
    per = defaultdict(float)
    for r in result:
        per[r[0]] += r[2]
    result.sort(key=lambda r: (-per[r[0]], r[0], r[3]))  # biggest project first, its tasks in time order
    return [r[:3] for r in result]


def render(day, tasks, cfg):
    out = [day.strftime(cfg["date_format"])]
    for proj, text, mins in tasks:
        out.append(f" {fmt_hours(mins, cfg)} - {proj}: {text}")
    return "\n".join(out) + "\n"

# ---------------------------------------------------------------- build days

def group_by_workday(events, cfg):
    days = defaultdict(list)
    for ev in events:
        wd = work_day_of(ev["ts"].date(), cfg)
        if wd:
            days[wd].append(ev)
    return days


def is_finished(day, cfg, now=None):
    now = now or dt.datetime.now()
    if day < now.date():
        return True
    return day == now.date() and now.time() >= day_end_time(cfg)


def out_path(day, cfg):
    return Path(os.path.expanduser(cfg["output_dir"])) / f"{day.isoformat()}.md"


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def user_edited(day, cfg, state):
    """True when the person changed the file by hand after we wrote it."""
    p = out_path(day, cfg)
    known = state.get(day.isoformat(), {}).get("sha")
    if not p.exists():
        return False
    return known is None or sha(p.read_text(encoding="utf-8")) != known  # no sha = we never wrote it


def write_day(day, text, cfg, state):
    manual = state.get(day.isoformat(), {}).get("manual", [])
    if manual:
        text = text.rstrip("\n") + "\n" + "\n".join(" " + m for m in manual) + "\n"
    p = out_path(day, cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    state.setdefault(day.isoformat(), {})["sha"] = sha(text)
    return text


def build_day(day, evs, cfg, use_ai=True):
    tasks = plan_tasks(evs, cfg, use_ai)
    if not tasks:
        return None
    return render(day, tasks, cfg)


def day_window(day, cfg):
    """Calendar range of events that belong to this workday's log."""
    start = day
    if cfg.get("weekend") == "monday":
        while (start - dt.timedelta(days=1)).weekday() not in cfg["workdays"]:
            start -= dt.timedelta(days=1)
    return dt.datetime.combine(start, dt.time.min).astimezone()


def process(since_date, cfg, force=False, use_ai=True, dry_run=False, only=None):
    since = dt.datetime.combine(since_date, dt.time.min).astimezone()
    if cfg.get("weekend") == "monday":
        since -= dt.timedelta(days=3)
    state = load_json(STATE_FILE, {})
    days = group_by_workday(collect(cfg, since), cfg)
    done = []
    for day in sorted(days):
        if day < since_date or (only and day != only):
            continue
        if not only and not is_finished(day, cfg):
            continue
        evs = days[day]
        last = max(e["ts"] for e in evs).isoformat()
        known = state.get(day.isoformat(), {})
        if not force and out_path(day, cfg).exists() and known.get("last_event", "") >= last:
            continue
        if not force and known.get("deleted"):
            continue
        if not force and user_edited(day, cfg, state):
            log(f"skip {day}: file was edited by hand (use --force to rebuild)")
            continue
        text = build_day(day, evs, cfg, use_ai)
        if text is None:
            continue
        if not dry_run:
            text = write_day(day, text, cfg, state)
            state[day.isoformat()].update(last_event=last, written_at=dt.datetime.now().isoformat())
            state[day.isoformat()].pop("deleted", None)
            save_json(STATE_FILE, state)
        done.append((day, text))
        log(f"{'previewed' if dry_run else 'wrote'} {day} ({len(evs)} events)")
    return done

# ---------------------------------------------------------------- lock + hook

def take_lock():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if LOCK_FILE.exists() and time.time() - LOCK_FILE.stat().st_mtime > 1800:
            LOCK_FILE.unlink()  # stale lock from a crashed run
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except OSError:
        return False


def cmd_hook(json_out=False):
    """Return at once; run catchup detached so startup stays fast."""
    if json_out:  # Cursor rejects a hook that prints nothing
        print("{}", flush=True)
    if os.environ.get(CHILD_ENV):
        return
    try:
        sys.stdin.read()  # hook input, not used
    except Exception:
        pass
    env = dict(os.environ, **{CHILD_ENV: "1"})
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
          "env": env, "close_fds": True}
    if IS_WIN:
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen([sys.executable, str(SCRIPT), "catchup"], **kw)


def cmd_catchup(args, cfg):
    if not take_lock():
        return
    try:
        state = load_json(STATE_FILE, {})
        today = dt.date.today()
        last_run = state.get("_last_run")
        if last_run:
            since = dt.date.fromisoformat(last_run[:10]) - dt.timedelta(days=2)
        else:
            since = today - dt.timedelta(days=cfg["first_run_days"])
        since = max(since, today - dt.timedelta(days=cfg["max_backfill_days"]))
        done = process(since, cfg, force="--force" in args, use_ai="--no-ai" not in args,
                       dry_run="--dry-run" in args)
        if "--dry-run" not in args:
            state = load_json(STATE_FILE, {})
            state["_last_run"] = dt.datetime.now().isoformat()
            save_json(STATE_FILE, state)
        if sys.stdout and sys.stdout.isatty():
            for _, text in done:
                print(text)
            if not done:
                print("Nothing to do: all finished workdays have a log.")
    except Exception as e:  # never break the user's session
        log(f"catchup error: {e!r}")
        raise
    finally:
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass

# ---------------------------------------------------------------- codex install

def codex_hook_command():
    return f'"{sys.executable}" "{SCRIPT}" hook'


def cmd_install_codex(cfg, remove=False):
    path = Path(cfg["codex_dir"]) / "hooks.json"
    data = load_json(path, {})
    hooks = data.setdefault("hooks", {})
    starts = hooks.setdefault("SessionStart", [])
    starts[:] = [g for g in starts
                 if not any("worklog.py" in (h.get("command") or "") for h in g.get("hooks", []))]
    if not remove:
        starts.append({"hooks": [{"type": "command", "command": codex_hook_command(), "timeout": 10}]})
    if path.exists():
        shutil.copy(path, path.with_suffix(".json.bak"))
    save_json(path, data)
    skill_dir = Path(cfg["codex_dir"]) / "skills" / "auto-worklog"
    if remove:
        shutil.rmtree(skill_dir, ignore_errors=True)
        shutil.rmtree(skill_dir.parent / "auto-worklog-api", ignore_errors=True)
    else:
        for name, target in (("worklog", skill_dir), ("worklog-api", skill_dir.parent / "auto-worklog-api")):
            tpl = (SCRIPT.parent / "skills" / name / "SKILL.template.md").read_text(encoding="utf-8")
            target.mkdir(parents=True, exist_ok=True)
            (target / "SKILL.md").write_text(tpl.replace("__WORKLOG_PY__", f"`{SCRIPT}`"), encoding="utf-8")
            print(f"Added the {name} skill to {target}")
    print(("Removed auto-worklog from " if remove else "Added auto-worklog to ") + str(path))
    if not remove:
        print("Make sure ~/.codex/config.toml has hooks turned on:  [features]  hooks = true")


# ---------------------------------------------------------------- web page

ENTRY_RE = re.compile(r"^\s*([\d.,]+)\s*h\s*-\s*(.*)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_log(text):
    entries, notes = [], []
    for line in text.splitlines()[1:]:
        m = ENTRY_RE.match(line)
        if m:
            entries.append({"hours": float(m.group(1).replace(",", ".")), "text": m.group(2).strip()})
        elif line.strip():
            notes.append(line.strip())
    return entries, notes


def fmt_h(h):
    return f"{round(float(h), 2):.2f}".rstrip("0").rstrip(".") + "h"


def render_entries(day, entries, notes, cfg):
    lines = [day.strftime(cfg["date_format"])]
    lines += [f" {fmt_h(e['hours'])} - {e['text'].strip()}" for e in entries if str(e.get("text", "")).strip()]
    lines += [" " + n for n in notes]
    return "\n".join(lines) + "\n"


def day_info(day, cfg, state):
    p = out_path(day, cfg)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    entries, notes = parse_log(text) if text else ([], [])
    return {"date": day.isoformat(), "label": day.strftime(cfg["date_format"]), "weekday": day.strftime("%a"),
            "entries": entries, "notes": notes, "total": round(sum(e["hours"] for e in entries), 2),
            "edited": user_edited(day, cfg, state), "exists": p.exists()}


def split_entry(text):
    proj, sep, desc = text.partition(":")
    if sep and proj.strip() and " " not in proj.strip():
        return proj.strip(), desc.strip()
    return "", text.strip()


def week_bounds(day):
    monday = day - dt.timedelta(days=day.weekday())
    return monday, monday + dt.timedelta(days=6)


def export_logs(frm, to, fmt, cfg):
    state = load_json(STATE_FILE, {})
    days = []
    d = frm
    while d <= to:
        info = day_info(d, cfg, state)
        if info["exists"]:
            days.append((d, info))
        d += dt.timedelta(days=1)
    df = cfg["date_format"]
    rows = []
    for d, info in days:
        for e in info["entries"]:
            proj, desc = split_entry(e["text"])
            rows.append({"date": d.isoformat(), "hours": e["hours"], "project": proj, "description": desc})
    if fmt == "json":
        return json.dumps({"from": frm.isoformat(), "to": to.isoformat(), "entries": rows}, indent=2, ensure_ascii=False)
    if fmt == "csv":
        import csv
        import io
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=["date", "hours", "project", "description"])
        w.writeheader()
        w.writerows(rows)
        return buf.getvalue()
    total = sum(r["hours"] for r in rows)
    target = float(cfg.get("target_hours", 8))
    workdays = sum(1 for i in range((to - frm).days + 1) if (frm + dt.timedelta(days=i)).weekday() in cfg["workdays"])
    out = [f"# Work log: {frm.strftime(df)} – {to.strftime(df)}", "",
           f"**{fmt_h(total)}** logged on {len(days)} day{'s' if len(days) != 1 else ''} (target {fmt_h(target * workdays)})", ""]
    weeks = defaultdict(list)
    for d, info in days:
        weeks[week_bounds(d)[0]].append((d, info))
    for monday in sorted(weeks):
        wk = weeks[monday]
        if len(weeks) > 1:
            wt = sum(i["total"] for _, i in wk)
            out += [f"## Week {monday.isocalendar()[1]} ({monday.strftime(df)} – {(monday + dt.timedelta(days=6)).strftime(df)}): {fmt_h(wt)}", ""]
        for d, info in wk:
            out += [f"{'###' if len(weeks) > 1 else '##'} {d.strftime('%A')} {d.strftime(df)}: {fmt_h(info['total'])}", ""]
            for e in info["entries"]:
                proj, desc = split_entry(e["text"])
                out.append(f"- **{fmt_h(e['hours'])}** " + (f"{proj}: {desc}" if proj else desc))
            out.append("")
    per = defaultdict(float)
    for r in rows:
        per[r["project"] or "other"] += r["hours"]
    if per:
        out += ["## Hours per project", "", "| Project | Hours |", "|---|---:|"]
        out += [f"| {p} | {fmt_h(h)} |" for p, h in sorted(per.items(), key=lambda kv: -kv[1])]
        out += [f"| **Total** | **{fmt_h(total)}** |", ""]
    return "\n".join(out)


def export_range(args, cfg):
    if "--from" in args:
        frm = parse_day(args[args.index("--from") + 1])
        to = parse_day(args[args.index("--to") + 1]) if "--to" in args else dt.date.today()
    else:
        ref = parse_day(args[args.index("--week") + 1]) if "--week" in args else dt.date.today()
        frm, to = week_bounds(ref)
    fmt = args[args.index("--format") + 1] if "--format" in args else "md"
    return frm, to, fmt


def cmd_serve(args, cfg):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    port = int(args[args.index("--port") + 1]) if "--port" in args else int(cfg.get("port", 8765))
    page = (SCRIPT.parent / "web" / "index.html").read_bytes()
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def guard(self, write=False):
            # only this machine; the custom header blocks other web sites from writing (CSRF)
            if self.headers.get("Host") not in allowed_hosts:
                self.send(403, {"error": "bad host"})
                return False
            if write and self.headers.get("X-Worklog") != "1":
                self.send(403, {"error": "missing header"})
                return False
            return True

        def day_from_path(self):
            parts = self.path.split("?")[0].strip("/").split("/")
            if len(parts) >= 3 and parts[:2] == ["api", "day"] and DATE_RE.match(parts[2]):
                return dt.date.fromisoformat(parts[2]), parts[3:]
            return None, None

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n < 1_000_000 else {}

        def do_GET(self):
            if not self.guard():
                return
            path = self.path.split("?")[0]
            cfg_now = load_config()
            if path in ("/", "/index.html"):
                return self.send(200, page, "text/html")
            if path == "/api/days":
                state = load_json(STATE_FILE, {})
                folder = Path(os.path.expanduser(cfg_now["output_dir"]))
                days = sorted((dt.date.fromisoformat(f.stem) for f in folder.glob("*.md") if DATE_RE.match(f.stem)),
                              reverse=True)
                return self.send(200, {"target": cfg_now.get("target_hours", 8),
                                       "workdays": cfg_now["workdays"],
                                       "days": [day_info(d, cfg_now, state) for d in days]})
            if path == "/api/export":
                from urllib.parse import parse_qs, urlparse
                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                try:
                    frm = dt.date.fromisoformat(q["from"]) if "from" in q else week_bounds(dt.date.today())[0]
                    to = dt.date.fromisoformat(q["to"]) if "to" in q else frm + dt.timedelta(days=6)
                except ValueError:
                    return self.send(400, {"error": "from/to must be YYYY-MM-DD"})
                fmt = q.get("format", "md")
                if fmt not in ("md", "csv", "json"):
                    return self.send(400, {"error": "format must be md, csv or json"})
                ctype = {"md": "text/markdown", "csv": "text/csv", "json": "application/json"}[fmt]
                return self.send(200, export_logs(frm, to, fmt, cfg_now).encode("utf-8"), ctype)
            if path == "/api/config":
                return self.send(200, {k: cfg_now[k] for k in ("target_hours", "workdays", "day_end", "date_format",
                                                               "round_to_hours", "sources", "output_dir")})
            day, rest = self.day_from_path()
            if day:
                return self.send(200, day_info(day, cfg_now, load_json(STATE_FILE, {})))
            self.send(404, {"error": "not found"})

        def do_PUT(self):
            if not self.guard(write=True):
                return
            day, rest = self.day_from_path()
            if not day:
                return self.send(404, {"error": "not found"})
            cfg_now = load_config()
            b = self.body()
            entries = [{"hours": float(e.get("hours") or 0), "text": str(e.get("text") or "")}
                       for e in b.get("entries", [])]
            p = out_path(day, cfg_now)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(render_entries(day, entries, b.get("notes", []), cfg_now), encoding="utf-8")
            # sha is not updated, so the file now counts as hand-edited and is protected
            self.send(200, day_info(day, cfg_now, load_json(STATE_FILE, {})))

        def do_DELETE(self):
            if not self.guard(write=True):
                return
            day, rest = self.day_from_path()
            if not day:
                return self.send(404, {"error": "not found"})
            cfg_now = load_config()
            p = out_path(day, cfg_now)
            if p.exists():
                p.unlink()
            state = load_json(STATE_FILE, {})
            state.setdefault(day.isoformat(), {})["deleted"] = True
            save_json(STATE_FILE, state)
            self.send(200, {"ok": True})

        def do_POST(self):
            if not self.guard(write=True):
                return
            day, rest = self.day_from_path()
            if day and rest == ["rebuild"]:
                cfg_now = load_config()
                done = process(day, cfg_now, force=True, use_ai="--no-ai" not in args, only=day)
                if not done:
                    return self.send(404, {"error": "No sessions found for this day."})
                return self.send(200, day_info(day, cfg_now, load_json(STATE_FILE, {})))
            self.send(404, {"error": "not found"})

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"auto-worklog: http://127.0.0.1:{port}  (Ctrl+C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_install_cursor(cfg, remove=False):
    path = Path(os.path.expanduser(cfg["cursor_dir"])) / "hooks.json"
    data = load_json(path, {})
    data.setdefault("version", 1)
    hooks = data.setdefault("hooks", {})
    starts = hooks.setdefault("sessionStart", [])
    starts[:] = [h for h in starts if "worklog.py" not in (h.get("command") or "")]
    if not remove:
        starts.append({"command": f'"{sys.executable}" "{SCRIPT}" hook --json'})
    if path.exists():
        shutil.copy(path, path.with_suffix(".json.bak"))
    save_json(path, data)
    print(("Removed auto-worklog from " if remove else "Added auto-worklog to ") + str(path)
          + ("" if remove else "  (Cursor support is not tested yet: run 'doctor cursor')"))


def ask_tools():
    cfg = load_config()
    has = {"claude": bool(shutil.which("claude")), "codex": bool(shutil.which("codex")),
           "cursor": cursor_app_dir(cfg).exists() or Path(os.path.expanduser(cfg["cursor_dir"])).exists()}
    names = [("claude", "Claude Code"), ("codex", "Codex"), ("cursor", "Cursor (supported, not tested)")]
    print("Which tools do you use? Pick one or more, for example: 1,2")
    for i, (key, label) in enumerate(names, 1):
        print(f"  {i}) {label}" + ("" if has[key] else "   (not found on this computer)"))
    default = ",".join(str(i) for i, (k, _) in enumerate(names, 1) if has[k]) or "1"
    a = input(f"Your choice [{default}]: ").strip() or default
    picked = [names[int(x) - 1][0] for x in re.findall(r"\d", a) if 1 <= int(x) <= len(names)]
    return picked or ask_tools()


def run_quiet(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)


def cmd_install(args, cfg):
    if "--tools" in args:
        v = args[args.index("--tools") + 1].lower()
        tools = {"both": ["claude", "codex"], "all": ["claude", "codex", "cursor"]}.get(v) or \
            [t for t in v.split(",") if t in ("claude", "codex", "cursor")]
    else:
        tools = ask_tools()
    if not tools:
        sys.exit("Use: install --tools claude,codex,cursor | both | all")
    saved = load_json(CONFIG_FILE, {})
    saved["sources"] = tools
    save_json(CONFIG_FILE, saved)
    print(f"Reading sessions from: {', '.join(tools)}  (saved in {CONFIG_FILE})")
    in_claude_plugins = ".claude" in SCRIPT.parts and "plugins" in SCRIPT.parts
    if "claude" in tools and not in_claude_plugins and "--no-plugin" not in args:
        exe = shutil.which("claude")
        if not exe:
            print("Claude Code not found: skipped the Claude plugin.")
        else:
            ok1, out1 = run_quiet([exe, "plugin", "marketplace", "add", str(SCRIPT.parent)])
            ok2, out2 = run_quiet([exe, "plugin", "install", "auto-worklog@auto-worklog"])
            print("Claude plugin installed." if ok2 else "Claude plugin install failed:\n" + out1 + "\n" + out2)
    if "codex" in tools:
        cmd_install_codex(cfg)
    elif (Path(cfg["codex_dir"]) / "hooks.json").exists():
        cmd_install_codex(cfg, remove=True)
    if "cursor" in tools:
        cmd_install_cursor(cfg)
    elif (Path(os.path.expanduser(cfg["cursor_dir"])) / "hooks.json").exists():
        cmd_install_cursor(cfg, remove=True)
    print("Done. Logs go to " + os.path.expanduser(saved.get("output_dir", DEFAULTS["output_dir"])))
    print("Open the web page with:  python3 " + str(SCRIPT) + " serve")


def cmd_uninstall(cfg):
    exe = shutil.which("claude")
    if exe:
        ok, out = run_quiet([exe, "plugin", "uninstall", "auto-worklog@auto-worklog"])
        print("Claude plugin removed." if ok else "Claude plugin: " + out[-200:])
    cmd_install_codex(cfg, remove=True)
    if (Path(os.path.expanduser(cfg["cursor_dir"])) / "hooks.json").exists():
        cmd_install_cursor(cfg, remove=True)
    print("Your logs and settings stay in place.")


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, args = argv[0], argv[1:]
    if cmd == "hook":
        return cmd_hook(json_out="--json" in argv)
    cfg = load_config()
    use_ai = "--no-ai" not in args
    dry = "--dry-run" in args
    pos = [a for a in args if not a.startswith("--")]
    if cmd == "catchup":
        cmd_catchup(args, cfg)
    elif cmd in ("day", "today"):
        day = dt.date.today() if cmd == "today" else parse_day(pos[0] if pos else "today")
        done = process(day, cfg, force=True, use_ai=use_ai, dry_run=dry, only=day)
        print(done[0][1] if done else f"No work found for {day}.")
    elif cmd == "backfill":
        if "--since" not in args:
            sys.exit("Use: backfill --since YYYY-MM-DD")
        since = parse_day(args[args.index("--since") + 1])
        for _, text in process(since, cfg, force="--force" in args, use_ai=use_ai, dry_run=dry):
            print(text)
    elif cmd == "show":
        n = int(pos[0]) if pos else 5
        files = sorted(Path(os.path.expanduser(cfg["output_dir"])).glob("*.md"))[-n:]
        print("\n".join(f.read_text(encoding="utf-8") for f in files) or "No logs yet.")
    elif cmd == "week":
        day = parse_day(pos[0]) if pos else dt.date.today()
        monday = day - dt.timedelta(days=day.weekday())
        total = 0.0
        for i in range(7):
            p = out_path(monday + dt.timedelta(days=i), cfg)
            if p.exists():
                text = p.read_text(encoding="utf-8")
                print(text)
                total += sum(float(h) for h in re.findall(r"^\s*([\d.]+)h\b", text, re.M))
        print(f"Week of {monday.strftime(cfg['date_format'])}: {total:g}h total")
    elif cmd == "add":
        if len(pos) < 2:
            sys.exit('Use: add YYYY-MM-DD "2h - what you did"')
        day, line = parse_day(pos[0]), " ".join(pos[1:]).strip()
        state = load_json(STATE_FILE, {})
        entry = state.setdefault(day.isoformat(), {})
        entry.setdefault("manual", []).append(line)
        p = out_path(day, cfg)
        if p.exists() and not user_edited(day, cfg, {day.isoformat(): {"sha": entry.get("sha")}}):
            text = p.read_text(encoding="utf-8").rstrip("\n") + "\n " + line + "\n"
        elif p.exists():  # hand-edited file: just append, keep it protected
            text = p.read_text(encoding="utf-8").rstrip("\n") + "\n " + line + "\n"
            p.write_text(text, encoding="utf-8")
            save_json(STATE_FILE, state)
            print(text)
            return
        else:
            text = day.strftime(cfg["date_format"]) + "\n " + line + "\n"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        entry["sha"] = sha(text)
        save_json(STATE_FILE, state)
        print(text)
    elif cmd == "export":
        frm, to, fmt = export_range(args, cfg)
        text = export_logs(frm, to, fmt, cfg)
        if "--out" in args:
            out = Path(args[args.index("--out") + 1]).expanduser()
            out.write_text(text, encoding="utf-8")
            print(f"Saved {out}")
        else:
            print(text)
    elif cmd == "serve":
        cmd_serve(args, cfg)
    elif cmd == "path":
        print(out_path(parse_day(pos[0]), cfg) if pos else os.path.expanduser(cfg["output_dir"]))
    elif cmd == "install":
        cmd_install(args, cfg)
    elif cmd == "uninstall":
        cmd_uninstall(cfg)
    elif cmd == "install-codex":
        cmd_install_codex(cfg)
    elif cmd == "install-cursor":
        cmd_install_cursor(cfg)
    elif cmd == "uninstall-cursor":
        cmd_install_cursor(cfg, remove=True)
    elif cmd == "doctor":
        cmd_doctor(pos, cfg)
    elif cmd == "uninstall-codex":
        cmd_install_codex(cfg, remove=True)
    elif cmd == "config":
        print(CONFIG_FILE)
        print(json.dumps(cfg, indent=2))
    else:
        sys.exit(f"Unknown command: {cmd}\n{__doc__}")


if __name__ == "__main__":
    main(sys.argv[1:])
