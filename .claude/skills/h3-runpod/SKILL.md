---
name: h3-runpod
description: Run MiniMax H3 video generation on a rented RunPod H100 through a durable job queue. Use whenever a task needs H3 clips rendered, a pod started or stopped, takes queued, reordered, retried or downloaded, or a slow or failing H3 render diagnosed.
---

# H3 on RunPod with a queue

The pod runs ComfyUI with H3 Singularity and `tools/queue_service.py`, a small SQLite job queue in front of it. Everything is driven from this machine over SSH tunnels by `tools/runpod.py` (the pod) and `tools/gen.py` (the jobs). The queue keeps working when this machine sleeps or disconnects.

## Lifecycle

```bash
python3 tools/runpod.py up        # ~7 min: deploy an H100, install pinned versions, start ComfyUI and the queue
python3 tools/gen.py status       # GPU, VRAM, ComfyUI version
python3 tools/runpod.py down      # refuses while jobs are queued, running or not downloaded; then backs up and terminates
python3 tools/runpod.py status    # every pod on the account, with cost
```

- A pod bills by the hour (an H100 is a few dollars an hour) whether or not it renders. Bring it up only for a batch, and take it down when `gen.py sync --wait` finishes. Tell the user while a pod is running.
- Pods have no network volume. Terminating one loses everything on it, which is fine because the install is pinned in `tools/pod_lock.json` and `tools/pod_requirements.txt`, renders are downloaded to `renders/queue/`, and `down` saves the queue database to `work/pod_backup/queue.db`. The next `up` restores it so take numbers continue.
- `up` writes the new host and port into `tools/comfy.json`. Hosts change on every pod.

## Jobs

A job is one take of one shot. Takes are numbered per shot by the queue, and seeds are consecutive from `--seed` or random.

```bash
python3 tools/gen.py submit h3 --shot s01 -p "$(cat prompts/s01.txt)" --ref refs/hero.png -n 3
python3 tools/gen.py submit --file shots.json      # many shots at once
python3 tools/gen.py queue                         # running, queued, recent; with an ETA
python3 tools/gen.py move 42 --before 37           # or --after ID, --back; default is the front
python3 tools/gen.py cancel 42 / retry 42 / pause / resume
python3 tools/gen.py sync --wait                   # download until the queue is empty
```

`shots.json` is a list of `{"shot", "prompt", "refs", "params", "n", "seed", "workflow"}`. Only `shot` and `prompt` are required. Builder params go in `params`, for example `{"length": 192, "turbo": 1}`, and `"audios": ["path/to/clip.wav"]` adds reference audio.

`sync` writes `renders/queue/SHOT/SHOT_tNN_sSEED.mp4`, a middle-frame PNG, and a JSON sidecar with the prompt, params and full graph, so any take can be reproduced. It also keeps `renders/queue/jobs.json` as a copy of the whole queue.

A failed job is retried once if ComfyUI crashed mid-run. A graph ComfyUI rejects fails at once. Read the error with `gen.py queue`, and the pod logs at `/workspace/comfy.log` and `/workspace/queue/service.log`.

## H3 parameters

`python3 tools/gen.py workflows` lists defaults. The ones that matter:

| Param | Default | Notes |
|---|---|---|
| `width`, `height` | 1344x768 | Multiples of 32. Avoid 1376x768. |
| `length` | 124 | Frames on the 17k+5 grid at 24 fps: 124 ≈ 5.17 s, 192 = 8 s, 362 ≈ 15 s. |
| `steps` | 20 | Ignored with turbo, which always uses 4. |
| `sparse` | 1.3 | Block-sparse attention, about 1.6x faster. `sparse=0` for dense hero shots. |
| `turbo` | 0 | LoRA strength for the 4-step Ref2VA turbo. Good for motion drafts; colours run hot and audio is weaker. |
| `refs` | none | Up to 9 images, cited in the prompt as `<Picture 1>`, `<Picture 2>`, ... |

Write prompts with the `h3-prompting` skill. Local ComfyUI has no prompt rewriter, so the prompt must already be in H3's final structured form.

## Speed and correctness checks

- H3 needs PyTorch built for CUDA 13 (`cu130`). Below that, ComfyUI only logs a warning, turns off its fast kernels, and sparse attention silently falls back to dense: steps go from about 8 s to 21 s at 1344x768. `pod_setup_h3.sh` installs cu130 and fails if the log does not show `backend cuda: {'available': True, 'disabled': False`.
- ComfyUI runs with `OMP_NUM_THREADS=6`. Pods report every host CPU but have a quota of about 7, and the default thread count throttles torch badly (25 s instead of 3.4 s to save a 5 s clip).
- Same seed on a fresh pod gives a nearly identical clip, not a bit-identical one.
- `torch.compile` crashes on H3's kernels; the builder refuses it.

## Tests

```bash
python3 -m unittest tools/test_queue_service.py
```

They run the queue service and the `gen.py` queue commands against `tools/mock_comfy.py`, so they need no GPU.
