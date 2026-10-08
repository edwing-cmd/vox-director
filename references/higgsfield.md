# Higgsfield backend (agent relay)

Higgsfield is reached through the agent's **Higgsfield MCP connector**, which the Python
scripts cannot call. So with `"provider": "higgsfield"` each paid stage becomes a two-pass
hand-off between the script and you (the agent). Credits come from the user's Higgsfield
plan — check with the `balance` tool before a run.

## beats.json

```json
{
  "provider": "higgsfield",
  "higgsfield": {
    "image_model": "nano_banana_2",
    "image_resolution": "1k",
    "video_model": "kling3_0",
    "video_mode": "std",
    "tts_model": "seed_audio",
    "voice_type": "preset",
    "voice_id": "<from list_voices>"
  }
}
```

All keys under `"higgsfield"` are optional; the values above are the defaults (except
`voice_id`, which has none — call `list_voices` once, pick a narrator, and save it here).
Model IDs and their params are verified live with `models_explore` (`action: "get"`).

Good alternatives: `nano_banana_pro` (best text in images, costs more), `seedance_2_5` or
`kling3_0` with `video_mode: "pro"` for better motion. For real people or brands, check the
model's policy first. Kling has historically been the most permissive.

## The loop for every paid stage

`style_bakeoff.py`, `keyframes.py`, `clips.py`, `audio.py`, `croll_keyframes.py`,
`extract_elements.py`, `host_*` all follow this loop:

1. **Run the stage** as usual, e.g. `python3 scripts/keyframes.py out/<project>`.
   It writes requests to `out/<project>/higgsfield/queue.json` and exits with **code 10**.
2. **List pending work:** `python3 scripts/higgsfield_queue.py show out/<project>`.
   Each request has an id, a `kind`, the MCP `tool` to use and ready-made `params`.
3. **Submit it** with the Higgsfield MCP tools:
   - `image` → `generate_image_batch` (up to 12 per call; use the request id order as `index`)
   - `video` → `generate_video_batch`
   - `tts` → `generate_audio_batch`
   - `remove_bg` → `remove_background`
   - `import_url` → `media_import_url` (the returned media_id is the "job_id"; url = the source url)
   - `upload` → `media_upload` for the file name, `curl -X PUT --upload-file <path>` to the
     upload_url, then `media_confirm` (the media_id is the "job_id")
   Requests with `"needs": "<id>"` depend on an upload/import: resolve that one first, then
   add `"medias": [{"value": "<its job_id>", "role": "start_image"}]` (or put it in
   `media_id` for remove_bg) before submitting.
   Pass `params` through unchanged otherwise. Don't set `use_unlim` unless the user asks.
4. **Wait** with `jobs_wait` (≤12 jobs per call; repeat until all terminal).
5. **Record each result:**
   `python3 scripts/higgsfield_queue.py done out/<project> <req_id> <job_id> <result_url>`
6. **Re-run the same stage.** Finished requests resolve instantly (they're keyed by a
   content hash), files are downloaded and beats.json is updated as normal. If you changed
   any prompts, only the changed ones are queued again.

After a stage completes, show the user the results (`show_generation_by_ids`) at the same
approval gates the standard workflow uses. Never skip the cost gate: before a video stage,
preflight one clip with `generate_video` `get_cost: true` and tell the user the total.

## Music

Higgsfield has **no standalone music model** (its music model is only for its game
builder and must not be used here). In order of preference:

1. The user's own licensed track (Artlist, Epidemic, YouTube Audio Library, ...) saved as
   `out/<project>/audio/bgm.mp3`. `audio.py` reuses it and skips music generation.
2. If `ATLASCLOUD_API_KEY` is set, music alone goes to Atlas Cloud (~$0.11).
3. Neither: the bgm request fails with a clear message; the film assembles without music.

## Not supported on this backend (yet)

- Voice cloning (`voice.clone_ref`): falls back to the configured preset voice. A cloned
  Higgsfield voice can be used by setting `voice_type: "element"` + its `voice_id`.
- A-roll transcription (`asr_beats.py`) still calls Atlas directly.
