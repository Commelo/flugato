"""Past runs, one JSON line each, in ~/.local/state/flugato/history.jsonl (XDG_STATE_HOME)."""

import json
import os
import time


def path():
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "flugato", "history.jsonl")


def entry(port, verdict, code, report, neighbour=None, link=None):
    e = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "port": port, "verdict": verdict, "exit": code,
         "rows": {k: {"value": r.value, "state": r.state} for k, r in report.items()}}
    if link and link.get("speed"):
        e["speed"] = link["speed"]
    if neighbour:
        e["switch"] = neighbour.get("switch")
        e["switch_port"] = neighbour.get("port")
    return e


def append(e):
    p = path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")
    except OSError:
        return False
    return True


def recent(n=50):
    """The last n entries, newest first."""
    try:
        with open(path()) as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines[-n:]):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def when(e, now=None):
    """'14:02' today, 'yest.' yesterday, else the date."""
    now = now or time.time()
    try:
        t = time.mktime(time.strptime(e["at"], "%Y-%m-%dT%H:%M:%S"))
    except (KeyError, ValueError):
        return "?"
    day = time.strftime("%Y-%m-%d", time.localtime(t))
    if day == time.strftime("%Y-%m-%d", time.localtime(now)):
        return time.strftime("%H:%M", time.localtime(t))
    if day == time.strftime("%Y-%m-%d", time.localtime(now - 86400)):
        return "yest."
    return time.strftime("%m-%d", time.localtime(t))
