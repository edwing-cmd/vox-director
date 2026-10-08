#!/usr/bin/env python3
"""
Provider abstraction — the pluggable media backend the pipeline stages talk to.

Backends: "atlas_cloud" (default, REST) and "higgsfield" (agent relay via the
Higgsfield MCP connector — see higgsfield_queue.py). Stages call a Provider
(submit_image/video/audio, remove_bg, get_status, upload, download) instead of a
concrete client, so adding a backend is: subclass Provider + one registry entry.
Pick a backend per project with beats.json `{"provider": "atlas_cloud"}` (default).

The layer is a thin in-process wrapper — zero extra network hops, so it does NOT
slow the pipeline; the only cost is the API latency, which is unchanged.
"""
import os
import subprocess
import sys
import time
from abc import ABC, abstractmethod

import atlas_cloud
import higgsfield_queue as hq


class ProviderError(RuntimeError):
    pass


class Provider(ABC):
    """The surface the stages need. get_status normalizes every backend's polling
    response to {status: pending|completed|failed, output: <url|None>, error}."""
    name = "base"

    @abstractmethod
    def submit_image(self, model, prompt, **params): ...
    @abstractmethod
    def submit_video(self, model, prompt, **params): ...
    @abstractmethod
    def submit_audio(self, model, **params): ...
    @abstractmethod
    def remove_bg(self, model, image_url, **params): ...
    @abstractmethod
    def get_status(self, job_id): ...
    @abstractmethod
    def upload(self, path): ...
    @abstractmethod
    def download(self, url, dest): ...


class AtlasCloudProvider(Provider):
    """Wraps the atlas_cloud client — identical behavior to calling it directly."""
    name = "atlas_cloud"

    def submit_image(self, model, prompt, **params):
        return atlas_cloud.submit_image(model, prompt, **params)

    def submit_video(self, model, prompt, **params):
        return atlas_cloud.submit_video(model, prompt, **params)

    def submit_audio(self, model, **params):
        return atlas_cloud.submit_media(model, **params)

    def remove_bg(self, model, image_url, **params):
        body = {"model": model, "image": image_url, **params}
        return atlas_cloud._post("/model/generateImage", body)["data"]["id"]

    def get_status(self, job_id):
        try:
            d = atlas_cloud._get(f"/model/prediction/{job_id}").get("data", {})
        except atlas_cloud.AtlasCloudError as e:
            return {"status": "failed", "output": None, "error": str(e)}
        st = d.get("status")
        if st in ("completed", "succeeded"):
            out = d.get("outputs") or d.get("output")
            out = out[0] if isinstance(out, list) else out
            return {"status": "completed", "output": out, "error": None}
        if st == "failed":
            return {"status": "failed", "output": None, "error": d.get("error", "")}
        return {"status": "pending", "output": None, "error": None}

    def upload(self, path):
        return atlas_cloud.upload(path)

    def download(self, url, dest):
        return atlas_cloud.download(url, dest)


class HiggsfieldProvider(Provider):
    """Agent-relay backend: queues requests for the agent's Higgsfield MCP connector.
    See higgsfield_queue.py for the hand-off protocol. Atlas model names coming from
    the stages are ignored; Higgsfield models come from beats.json "higgsfield"."""
    name = "higgsfield"
    deferred = True

    def __init__(self):
        self.s = hq.Store(hq.project_dir())
        self.new = 0

    def _enqueue(self, req):
        rid = hq.req_id(req)
        if rid in self.s.results:
            return f"hf:done:{rid}"
        if rid not in self.s.queue:
            self.s.queue[rid] = req
            self.new += 1
        return f"hf:queued:{rid}"

    def _media(self, url):
        """Map an image URL/placeholder from an earlier stage to (media_id, needs_req_id)."""
        if url.startswith("hfupload:"):
            rid = url.split(":", 1)[1]
            r = self.s.results.get(rid)
            return (r["job_id"], None) if r else (None, rid)
        job = self.s.job_for_url(url)
        if job:
            return job, None
        # an outside https URL: the agent imports it with media_import_url first
        rid = self._enqueue({"kind": "import_url", "tool": "media_import_url", "url": url})
        rid = rid.split(":")[-1]
        r = self.s.results.get(rid)
        return (r["job_id"], None) if r else (None, rid)

    def submit_image(self, model, prompt, **params):
        c = self.s.cfg
        return self._enqueue({"kind": "image", "tool": "generate_image_batch",
                              "params": {"model": c["image_model"], "prompt": prompt,
                                         "aspect_ratio": params.get("aspect_ratio", "16:9"),
                                         "resolution": c["image_resolution"]}})

    def submit_video(self, model, prompt, **params):
        c = self.s.cfg
        media, needs = self._media(params.get("image", ""))
        p = {"model": c["video_model"], "prompt": prompt,
             "duration": int(params.get("duration", 5)),
             "aspect_ratio": params.get("aspect_ratio") or params.get("ratio") or "16:9"}
        if c["video_model"].startswith("kling"):
            p.update(mode=c["video_mode"], sound="off")
        else:
            p["generate_audio"] = False
        if media:
            p["medias"] = [{"value": media, "role": "start_image"}]
        req = {"kind": "video", "tool": "generate_video_batch", "params": p}
        if needs:
            req["needs"] = needs     # resolve that upload/import first, then use its id as start_image
        return self._enqueue(req)

    def submit_audio(self, model, **params):
        if "music" in model:
            if os.environ.get("ATLASCLOUD_API_KEY"):
                return "atlas:" + atlas_cloud.submit_media(model, **params)
            return "hf:skip:music"
        c = self.s.cfg
        p = {"model": c["tts_model"], "prompt": params.get("text", "")}
        if c.get("voice_id"):
            p.update(voice_type=c.get("voice_type", "preset"), voice_id=c["voice_id"])
        if params.get("references"):
            print("[higgsfield] voice cloning isn't wired up yet — using the configured voice")
        return self._enqueue({"kind": "tts", "tool": "generate_audio_batch", "params": p})

    def remove_bg(self, model, image_url, **params):
        media, needs = self._media(image_url)
        req = {"kind": "remove_bg", "tool": "remove_background",
               "params": {"media_id": media, "media_type": "image"}}
        if needs:
            req["needs"] = needs
        return self._enqueue(req)

    def get_status(self, job_id):
        if job_id.startswith("atlas:"):
            return AtlasCloudProvider().get_status(job_id[6:])
        _, state, rid = job_id.split(":", 2)
        if state == "skip":
            return {"status": "failed", "output": None,
                    "error": "Higgsfield has no music model - put a track at audio/bgm.mp3 "
                             "or set ATLASCLOUD_API_KEY"}
        r = self.s.results.get(rid)
        if r:
            return {"status": "completed", "output": r["url"], "error": None}
        return {"status": "queued", "output": None, "error": None}

    def upload(self, path):
        rid = self._enqueue({"kind": "upload", "tool": "media_upload",
                             "path": os.path.abspath(path)}).split(":")[-1]
        return f"hfupload:{rid}"

    def download(self, url, dest):
        subprocess.run(["/usr/bin/curl", "-sL", "--retry", "3", "-o", dest, url], check=True)
        if not os.path.exists(dest) or os.path.getsize(dest) == 0:
            raise ProviderError(f"download produced empty file: {url}")
        return dest

    def flush(self):
        """Persist the queue and stop the stage so the agent can run the jobs."""
        self.s.save_queue()
        here = os.path.dirname(os.path.abspath(__file__))
        print(f"\n[higgsfield] {self.new} new request(s) queued in {self.s.qpath}")
        print(f"[higgsfield] next: python3 {here}/higgsfield_queue.py show {self.s.proj}"
              " -> submit via Higgsfield MCP -> record each with 'done' -> re-run this stage")
        sys.exit(hq.QUEUED_EXIT)


_REGISTRY = {"atlas_cloud": AtlasCloudProvider, "higgsfield": HiggsfieldProvider}


def get_provider(name=None):
    """Return a Provider instance by name (default 'atlas_cloud')."""
    name = (name or "atlas_cloud").lower()
    if name not in _REGISTRY:
        raise ProviderError(f"unknown provider '{name}'; available: {list(_REGISTRY)}")
    return _REGISTRY[name]()


def run_jobs(prov, specs, *, poll_s=3, stall_s=90, max_retries=2, deadline_s=900):
    """Submit + poll a batch of jobs, resubmitting any that FAIL or STALL.

    specs: dict of key -> submit() callable returning a job id. A job that fails,
    or stays pending past `stall_s`, is resubmitted (fresh id) up to `max_retries`
    times — this is what stops one stuck prediction from wasting the whole deadline.
    Returns key -> output URL (or None). Prints progress like the old loops did.
    """
    st = {}
    for key, submit in specs.items():
        st[key] = {"pid": submit(), "t": time.time(), "tries": 0}
        print(f"[{key}] submitted {st[key]['pid']}")

    if getattr(prov, "deferred", False):
        if any(prov.get_status(s["pid"])["status"] == "queued" for s in st.values()):
            prov.flush()        # exits; the agent runs the jobs, then re-runs the stage
        poll_s = 0              # everything already resolved — no need to wait
    done = {}
    deadline = time.time() + deadline_s
    while len(done) < len(specs) and time.time() < deadline:
        time.sleep(poll_s)
        now = time.time()
        for key, submit in specs.items():
            if key in done:
                continue
            s = st[key]
            r = prov.get_status(s["pid"])
            status = r["status"]
            if status == "completed":
                done[key] = r["output"]
                print(f"[{key}] done")
            elif status == "failed" or (status == "pending" and now - s["t"] > stall_s):
                if s["tries"] < max_retries:
                    s["tries"] += 1
                    s["pid"] = submit()
                    s["t"] = time.time()
                    why = "failed" if status == "failed" else f"stalled>{int(stall_s)}s"
                    print(f"[{key}] {why} -> resubmit #{s['tries']} ({s['pid']})")
                elif status == "failed":
                    done[key] = None
                    print(f"[{key}] FAILED: {(r.get('error') or '')[:120]}")
                # stalled + out of retries: keep waiting until the deadline
    for key in specs:
        done.setdefault(key, None)
    return done
