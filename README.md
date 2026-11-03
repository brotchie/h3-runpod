# H3 on RunPod

Render [MiniMax H3](https://huggingface.co/MiniMaxAI/MiniMax-H3) video (picture and sound in one pass) on a rented RunPod H100, through a job queue that keeps going while your laptop sleeps.

- **One command up, one command down.** `runpod.py up` deploys an H100 and installs pinned versions of ComfyUI, PyTorch and the models in about 7 minutes. `runpod.py down` refuses while work is unfinished, saves the queue, and terminates the pod, so nothing bills between batches.
- **A durable queue.** Jobs live in SQLite on the pod. You can reorder, cancel, retry or pause them, a ComfyUI crash costs one attempt rather than the queue, and take numbers carry across pods.
- **Reproducible takes.** Every downloaded clip comes with a JSON sidecar holding its prompt, seed, params and the full ComfyUI graph.
- **Claude Code skills.** `h3-runpod` teaches Claude to drive all of this, and `h3-prompting` teaches it to write prompts in the structured form local H3 needs.

The model is the community [H3 Singularity](https://huggingface.co/WarmBloodAban/Minimax-h3_Singularity) Ref2VA int8 checkpoint with Comfy-Org's H3 text encoder and VAEs. Check those model cards' licences for your use.

## Setup

You need Python 3.10+, `ffmpeg` (for poster frames), and a RunPod account with credit.

1. Add your SSH public key in RunPod under **Settings → SSH Public Keys**. The scripts use `~/.ssh/id_ed25519` by default; to use another key, copy `tools/comfy.example.json` to `tools/comfy.json` and change `key`.
2. Create an API key in RunPod under **Settings → API Keys**, and put it in `.env` at the repo root, which git ignores:

   ```
   RUNPOD_API_KEY=your-key-here
   ```

   Or export `RUNPOD_API_KEY` in your shell.
3. `pip install pillow` for the contact sheets and blank-frame check.

## Use

```bash
python3 tools/runpod.py up
python3 tools/gen.py submit h3 --shot s01 -p "$(cat my_prompt.txt)" --ref hero.png -n 3
python3 tools/gen.py queue
python3 tools/gen.py sync --wait
python3 tools/runpod.py down
```

Clips land in `renders/queue/SHOT/`. See [the h3-runpod skill](.claude/skills/h3-runpod/SKILL.md) for every command, the H3 parameters, and the speed pitfalls, and [the h3-prompting skill](.claude/skills/h3-prompting/SKILL.md) for prompt format.

For a one-off render without the queue, `python3 tools/gen.py run h3 -p "..." -n 2` submits straight to ComfyUI and waits.

## Using it with Claude Code

Open Claude Code in this repo and the skills load automatically. To use them from another project, copy `.claude/skills/h3-runpod` and `.claude/skills/h3-prompting` into that project's `.claude/skills/` or into `~/.claude/skills/`, and point Claude at this repo's `tools/`.

## Files

| File | Runs on | Does |
|---|---|---|
| `tools/runpod.py` | your machine | Deploys, provisions, backs up and terminates pods through RunPod's GraphQL API |
| `tools/pod_setup_h3.sh` | the pod | Installs the pinned stack, downloads models, starts ComfyUI and the queue under restart loops |
| `tools/pod_lock.json`, `tools/pod_requirements.txt` | the pod | Docker image, ComfyUI commit, PyTorch index, exact pip and model versions |
| `tools/queue_service.py` | the pod | The job queue, standard library only |
| `tools/gen.py` | your machine | Tunnels, submits, monitors and downloads |
| `tools/workflows.py` | your machine | Builds the H3 ComfyUI graph in code |
| `tools/mock_comfy.py`, `tools/test_queue_service.py` | your machine | Fake ComfyUI and tests: `python3 -m unittest tools/test_queue_service.py` |

The pinned setup was last verified on an H100 SXM in November 2026. To move to a newer ComfyUI or model revision, edit `pod_lock.json`, bring a pod up, and regenerate `pod_requirements.txt` with `pip freeze` on it.
