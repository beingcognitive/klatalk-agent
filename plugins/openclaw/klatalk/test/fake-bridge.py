#!/usr/bin/env python3
"""A stand-in for `klatalk bridge`: the same wire (one JSON object per
line), scripted events from FAKE_EVENTS, every command appended to
FAKE_LOG. The plugin under test runs it exactly as it runs the real core:
`python3 -c <loader> <this file> bridge …` with the file's bytes on fd 3."""
import json
import os
import sys
import threading
import time

args = sys.argv[1:]
rooms = (args[args.index("--rooms") + 1] if "--rooms" in args else "").split(",")


def out(o):
    sys.stdout.write(json.dumps(o) + "\n")
    sys.stdout.flush()


def log_cmd(o):
    if os.environ.get("FAKE_LOG"):
        with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
            f.write(json.dumps(o) + "\n")


if os.environ.get("FAKE_ARGS"):
    with open(os.environ["FAKE_ARGS"], "w", encoding="utf-8") as f:
        json.dump({"args": args, "file": __file__,
                   "env": {"KLATALK_PROFILE": os.environ.get("KLATALK_PROFILE"),
                           "KLATALK_HOME": os.environ.get("KLATALK_HOME")}}, f)

out({"ev": "hello", "version": os.environ.get("FAKE_VERSION", "1.5.0"), "profile": "p",
     "user_id": "BOT", "nickname": "Bot", "rooms": rooms})
for r in rooms:
    out({"ev": "joined", "room": r, "sealed": False,
         "last_seq": int(os.environ.get("FAKE_LAST_SEQ", "0"))})

seq = 100
roster = json.loads(os.environ.get("FAKE_ROSTER", "{}"))
fresh_calls = 0


def events():
    time.sleep(0.03)
    for ev in json.loads(os.environ.get("FAKE_EVENTS", "[]")):
        time.sleep(ev.pop("_delay", 0) / 1000)
        out(ev)
    code = os.environ.get("FAKE_EXIT_AFTER_EVENTS")
    if code:
        time.sleep(0.02)
        os._exit(int(code))


threading.Thread(target=events, daemon=True).start()

for line in sys.stdin:
    if not line.strip():
        continue
    req = json.loads(line)
    log_cmd(req)
    rid, cmd = req.get("id"), req.get("cmd")
    if cmd == "send":
        seq += 1
        out({"id": rid, "ok": True, "seq": seq})
    elif cmd == "read":
        out({"id": rid, "ok": True, "last_read_seq": req.get("seq")})
    elif cmd == "roster":
        if req.get("fresh"):
            fresh_calls += 1
            if str(fresh_calls) == os.environ.get("FAKE_ROSTER_FAIL_AT"):
                out({"id": rid, "ok": False, "kind": "transient", "why": "hiccup"})
                continue
        members = roster.get(req.get("room"), [])
        out({"id": rid, "ok": True, "name": "Bench [x]\nline", "sealed": False,
             "members": [{"user_id": u, "nick": u, "is_ai": u == "BOT"} for u in members]})
    elif cmd == "fetch":
        with open(req["out"], "wb") as f:
            f.write(b"img")
        out({"id": rid, "ok": True, "path": req["out"], "bytes": 3})
    elif cmd in ("attach", "react"):
        seq += 1
        out({"id": rid, "ok": True, "seq": seq})
    elif cmd == "leave":
        out({"ev": "stopped", "room": req.get("room"), "why": "left"})
        out({"id": rid, "ok": True, "left": req.get("room"), "sealed": False})
    else:
        out({"id": rid, "ok": False, "kind": "usage", "why": "unknown command"})
