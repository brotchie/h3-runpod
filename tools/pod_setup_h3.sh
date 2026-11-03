#!/usr/bin/env bash
# Set up H3 Singularity on a RunPod pod from the pinned versions and start ComfyUI. Safe to re-run; it restarts the server.
# `tools/runpod.py up` runs this. It expects pod_lock.json, pod_requirements.txt and queue_service.py in /workspace/lock,
# and optionally queue.db there, which carries the take numbering over from the previous pod.
# H3 needs PyTorch cu130: below that, ComfyUI silently drops its fast kernels and steps run 2.6x slower.
# The thread limit matters because pods report every host CPU but have a much smaller cgroup quota;
# torch then throttles itself (25 s vs 3.4 s to save a 5 s clip).
set -euo pipefail
W=/workspace; V=$W/venv; C=$W/ComfyUI

L=$W/lock
COMMIT=$(python3 -c "import json; print(json.load(open('$L/pod_lock.json'))['comfyui'])")
INDEX=$(python3 -c "import json; print(json.load(open('$L/pod_lock.json'))['torch_index'])")
if [ ! -d "$C" ]; then
  git init -q "$C"
  git -C "$C" remote add origin https://github.com/comfyanonymous/ComfyUI.git
fi
git -C "$C" fetch -q --depth 1 origin "$COMMIT"
git -C "$C" checkout -q FETCH_HEAD
[ -x "$V/bin/python" ] || python3 -m venv "$V"
"$V/bin/pip" install -q -r "$L/pod_requirements.txt" --extra-index-url "$INDEX"

mkdir -p "$W/queue"
cp "$L/queue_service.py" "$W/queue/"
[ -f "$L/queue.db" ] && [ ! -f "$W/queue/queue.db" ] && cp "$L/queue.db" "$W/queue/queue.db"

"$V/bin/python" - <<'EOF'
import json, os, shutil
from huggingface_hub import hf_hub_download
M = "/workspace/ComfyUI/models/"
for m in json.load(open("/workspace/lock/pod_lock.json"))["models"]:
    dest = M + m["folder"] + "/" + os.path.basename(m["path"])
    if not os.path.exists(dest):
        os.makedirs(M + m["folder"], exist_ok=True)
        shutil.move(hf_hub_download(m["repo"], m["path"], revision=m["revision"], local_dir="/workspace/dl"), dest)
    print("ok", os.path.basename(m["path"]), flush=True)
EOF

pkill -f 'comfy_loop[.]sh' || true
pkill -f 'queue_loop[.]sh' || true
pkill -f '[m]ain.py --listen' || true
pkill -f '[q]ueue_service.py' || true
sleep 3

# Both restart themselves if they die, so a ComfyUI crash costs one job attempt rather than the whole queue.
cat > "$W/comfy_loop.sh" <<EOF
#!/usr/bin/env bash
cd "$C"
while true; do
  OMP_NUM_THREADS=6 MKL_NUM_THREADS=6 PYTHONUNBUFFERED=1 "$V/bin/python" main.py --listen 127.0.0.1 --port 8188 >> "$W/comfy.log" 2>&1
  echo "=== ComfyUI exited \$(date)" >> "$W/comfy.log"
  sleep 5
done
EOF
cat > "$W/queue_loop.sh" <<EOF
#!/usr/bin/env bash
while true; do
  PYTHONUNBUFFERED=1 python3 "$W/queue/queue_service.py" --db "$W/queue/queue.db" >> "$W/queue/service.log" 2>&1
  echo "=== queue service exited \$(date)" >> "$W/queue/service.log"
  sleep 5
done
EOF
: > "$W/comfy.log"
setsid nohup bash "$W/comfy_loop.sh" > /dev/null 2>&1 < /dev/null &
for _ in $(seq 60); do curl -sf -m 2 127.0.0.1:8188/system_stats > /dev/null && break; sleep 2; done
curl -sf -m 2 127.0.0.1:8188/system_stats > /dev/null || { echo "ComfyUI did not start; see $W/comfy.log"; exit 1; }
if ! grep -q "backend cuda: {'available': True, 'disabled': False" "$W/comfy.log"; then
  echo "ComfyUI is up but its CUDA kernels are off; check the PyTorch build"; exit 1
fi
setsid nohup bash "$W/queue_loop.sh" > /dev/null 2>&1 < /dev/null &
for _ in $(seq 15); do curl -sf -m 2 127.0.0.1:8189/health > /dev/null && break; sleep 1; done
curl -sf -m 2 127.0.0.1:8189/health > /dev/null || { echo "queue service did not start; see $W/queue/service.log"; exit 1; }
echo "ready: $("$V/bin/python" -c 'import torch; print(torch.__version__)'), CUDA kernels on, 6 threads, queue service up"
