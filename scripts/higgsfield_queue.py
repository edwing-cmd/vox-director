#!/usr/bin/env python3
"""
Higgsfield backend for vox-director (agent-relay mode).

Higgsfield is reached through the agent's Higgsfield MCP connector, not a REST key,
so the scripts cannot call it directly. Instead this provider works as a hand-off:

  1. A stage script (keyframes.py, clips.py, audio.py, ...) runs with
     beats.json {"provider": "higgsfield"}. Every generation it would submit is
     written to <project>/higgsfield/queue.json and the script exits with code 10.
  2. The agent runs `python3 scripts/higgsfield_queue.py show <project>`, submits each
     pending request with the Higgsfield MCP tools (generate_*_batch + jobs_wait), and
     records each finished job with
     `python3 scripts/higgsfield_queue.py done <project> <req_id> <job_id> <result_url>`.
  3. The agent re-runs the same stage script. Requests are keyed by a hash of their
     content, so finished ones resolve instantly and the stage completes normally.

Music: Higgsfield has no standalone music model. If ATLASCLOUD_API_KEY is set, music
falls through to Atlas Cloud; otherwise put a track at <project>/audio/bgm.mp3.

Defaults (override in beats.json under "higgsfield": {...}):
  image_model  nano_banana_2        video_model  kling3_0
  tts_model    seed_audio           voice_id     (pick one via the list_voices tool)
"""
import hashlib
import json
import os
import subprocess
import sys

QUEUED_EXIT = 10
DEFAULTS = {"image_model": "nano_banana_2", "image_resolution": "1k",
            "video_model": "kling3_0", "video_mode": "std",
            "tts_model": "seed_audio", "voice_id": None}


def project_dir():
    p = os.environ.get("VOX_PROJECT")
    if p:
        return os.path.abspath(p)
    for a in sys.argv[1:]:
        if os.path.isfile(os.path.join(a, "beats.json")) or \
           os.path.isfile(os.path.join(a, "elements_spec.json")):
            return os.path.abspath(a)
    raise RuntimeError("higgsfield provider: can't locate the project dir — "
                       "pass it as the script argument or set VOX_PROJECT")


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


class Store:
    def __init__(self, proj):
        self.proj = proj
        self.dir = os.path.join(proj, "higgsfield")
        self.qpath = os.path.join(self.dir, "queue.json")
        self.rpath = os.path.join(self.dir, "results.json")
        self.queue = _load(self.qpath, {})
        self.results = _load(self.rpath, {})
        doc = _load(os.path.join(proj, "beats.json"), {})
        self.cfg = {**DEFAULTS, **doc.get("higgsfield", {})}

    def job_for_url(self, url):
        for r in self.results.values():
            if r.get("url") == url:
                return r.get("job_id")
        return None

    def save_queue(self):
        _save(self.qpath, self.queue)

    def save_results(self):
        _save(self.rpath, self.results)


def req_id(req):
    blob = json.dumps(req, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha1(blob).hexdigest()[:12]


# ------------------------------------------------------------------ CLI

def _cmd_show(proj):
    s = Store(proj)
    pending = {k: v for k, v in s.queue.items() if k not in s.results}
    blocked = {k: v for k, v in pending.items()
               if v.get("needs") and v["needs"] not in s.results}
    print(json.dumps({"project": proj, "pending": len(pending),
                      "blocked_on_upload": sorted(blocked),
                      "requests": pending}, ensure_ascii=False, indent=2))


def _cmd_done(proj, rid, job_id, url):
    s = Store(proj)
    if rid not in s.queue:
        sys.exit(f"unknown request id {rid}")
    s.results[rid] = {"job_id": job_id, "url": url}
    s.save_results()
    print(f"recorded {rid} -> {job_id}")


def _cmd_status(proj):
    s = Store(proj)
    done = sum(1 for k in s.queue if k in s.results)
    print(f"{done}/{len(s.queue)} requests resolved")


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] not in ("show", "done", "status"):
        sys.exit("usage: higgsfield_queue.py show|status <project>\n"
                 "       higgsfield_queue.py done <project> <req_id> <job_id> <url>")
    cmd, proj = sys.argv[1], os.path.abspath(sys.argv[2])
    if cmd == "show":
        _cmd_show(proj)
    elif cmd == "status":
        _cmd_status(proj)
    else:
        if len(sys.argv) != 6:
            sys.exit("usage: higgsfield_queue.py done <project> <req_id> <job_id> <url>")
        _cmd_done(proj, *sys.argv[3:6])
