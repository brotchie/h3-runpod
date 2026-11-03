"""Drive the RunPod ComfyUI server through its HTTP API (over an SSH tunnel).

  gen.py tunnel                      ensure the local tunnel to the pod is up
  gen.py status                      server / GPU / queue summary
  gen.py models [folder]             list installed model files (e.g. loras)
  gen.py workflows                   list graph builders and their parameters
  gen.py run WORKFLOW [options]      queue N variants, wait, download PNGs + sidecar JSON
  gen.py sheet PATH... [-o out.png]  labelled contact sheet of images (or dirs)

Graphs are built in code by tools/workflows.py (h3).
Video workflows save the MP4 plus a middle-frame PNG with the same name.

Job queue on the pod (tools/queue_service.py), which keeps going without this machine:
  gen.py submit WORKFLOW --shot ID ... [--front]   add takes (or --file shots.json for many)
  gen.py queue [--shot ID] [--all]                 what's running, queued and finished
  gen.py move ID [--before ID | --after ID | --back]   default: to the front
  gen.py cancel ID... / retry ID... / pause / resume
  gen.py sync [--wait]                             download finished clips to renders/queue/SHOT/
Builder params: common flags below, or --param KEY=VALUE for any other.
--set NODE.INPUT=VALUE overrides a raw node input (node ids are readable names).

Connection details come from tools/comfy.json (host, ssh_port, ssh_user, key).
"""
import argparse
import hashlib
import json
import mimetypes
import os
import random
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

import workflows

ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path(os.environ.get("H3_COMFY_CONFIG", Path(__file__).resolve().parent / "comfy.json"))
RENDERS = ROOT / "renders"


def config():
    return json.loads(CONFIG.read_text())


def base_url():
    return f"http://127.0.0.1:{config()['local_port']}"


# ---------- tunnel ----------
def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def api_ok():
    try:
        with urllib.request.urlopen(base_url() + "/system_stats", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_tunnel(quiet=False):
    c = config()
    if api_ok():
        return
    if port_open(c["local_port"]):
        # ComfyUI restarts itself after a crash, and loading takes up to a minute or so
        for _ in range(60):
            if api_ok():
                return
            time.sleep(2)
        sys.exit(f"port {c['local_port']} is in use but ComfyUI isn't answering — stale tunnel? "
                 f"`lsof -iTCP:{c['local_port']}` to find it")
    cmd = ["ssh", "-f", "-N", "-i", str(Path(c["key"]).expanduser()),
           "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
           "-o", "ConnectTimeout=15",
           "-L", f"127.0.0.1:{c['local_port']}:127.0.0.1:{c['remote_port']}",
           "-p", str(c["ssh_port"]), f"{c['ssh_user']}@{c['host']}"]
    subprocess.run(cmd, check=True)
    for _ in range(20):
        if api_ok():
            if not quiet:
                print(f"tunnel up: {base_url()} -> {c['host']}:{c['ssh_port']}")
            return
        time.sleep(0.5)
    sys.exit("tunnel started but ComfyUI is not responding (is it running on the pod?)")


# ---------- http ----------
def get(path, raw=False):
    with urllib.request.urlopen(base_url() + path, timeout=60) as r:
        data = r.read()
    return data if raw else json.loads(data)


def post_json(path, payload):
    req = urllib.request.Request(base_url() + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"ComfyUI rejected the request ({e.code}):\n{e.read().decode()[:3000]}")


def upload_image(path: Path) -> str:
    """Upload a reference image (content-addressed name, so re-uploads are no-ops)."""
    data = path.read_bytes()
    name = f"h3_{hashlib.sha1(data).hexdigest()[:12]}{path.suffix.lower()}"
    boundary = uuid.uuid4().hex
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body = b"".join([
        f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n".encode(), data, b"\r\n",
        f'--{boundary}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'.encode(),
        f"--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(base_url() + "/upload/image", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["name"]


def coerce(v):
    for t in (int, float):
        try:
            return t(v)
        except ValueError:
            pass
    return {"true": True, "false": False}.get(v.lower(), v)


# ---------- commands ----------
def cmd_tunnel(a):
    ensure_tunnel()
    print("ok" if api_ok() else "not responding")


def cmd_status(a):
    ensure_tunnel(quiet=True)
    s = get("/system_stats")
    q = get("/queue")
    for d in s.get("devices", []):
        print(f"{d['name']}: VRAM {(d['vram_total'] - d['vram_free']) / 2**30:.1f}/{d['vram_total'] / 2**30:.0f} GB used")
    print(f"ComfyUI {s['system']['comfyui_version']} · queue: {len(q['queue_running'])} running, {len(q['queue_pending'])} pending")


def cmd_models(a):
    ensure_tunnel(quiet=True)
    folders = [a.folder] if a.folder else ["diffusion_models", "checkpoints", "text_encoders", "vae", "loras"]
    for f in folders:
        try:
            files = get(f"/models/{f}")
        except urllib.error.HTTPError:
            files = []
        print(f"{f}:")
        for x in files:
            print(f"  {x}")


def cmd_workflows(a):
    for name, (fn, defaults) in workflows.WORKFLOWS.items():
        doc = [l.strip() for l in (workflows.__doc__ or "").splitlines() if l.strip().startswith(name + " ")]
        print(f"{name:10} {doc[0][len(name):].strip() if doc else ''}")
        print(f"{'':10} {json.dumps({k: v for k, v in defaults.items() if k not in ('prompt', 'negative', 'prefix')})}")


def wait_for(prompt_ids, timeout):
    pending, done, t0 = set(prompt_ids), {}, time.time()
    last = ""
    while pending:
        for pid in list(pending):
            h = get(f"/history/{pid}")
            if pid in h:
                entry = h[pid]
                st = entry.get("status", {})
                if st.get("status_str") == "error" or (st.get("completed") is False and st.get("messages")):
                    msgs = [m for m in st.get("messages", []) if m[0] == "execution_error"]
                    if msgs:
                        err = msgs[0][1]
                        sys.exit(f"generation failed in node {err.get('node_id')} ({err.get('node_type')}): "
                                 f"{err.get('exception_message', '').strip()}")
                if entry.get("outputs"):
                    done[pid] = entry
                    pending.discard(pid)
        if pending:
            if time.time() - t0 > timeout:
                sys.exit(f"timed out after {timeout}s; still pending: {pending}")
            q = get("/queue")
            msg = f"  {len(done)}/{len(prompt_ids)} done · queue {len(q['queue_running'])} running, " \
                  f"{len(q['queue_pending'])} pending · {time.time() - t0:.0f}s"
            if msg.split("·")[0] != last:
                print(msg, flush=True)
                last = msg.split("·")[0]
            time.sleep(2)
    return done


def cmd_run(a):
    ensure_tunnel(quiet=True)
    if a.workflow not in workflows.WORKFLOWS:
        sys.exit(f"unknown workflow {a.workflow}; have {list(workflows.WORKFLOWS)}")

    refs_local, refs_remote = [], []
    for ref in a.ref or []:
        p = Path(ref).expanduser()
        if not p.exists():
            sys.exit(f"reference not found: {p}")
        refs_local.append(str(p)); refs_remote.append(upload_image(p))

    base_seed = a.seed if a.seed is not None else random.randrange(2**31)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = a.name or a.workflow
    out = Path(a.out) if a.out else RENDERS / f"{stamp}-{name}"
    out.mkdir(parents=True, exist_ok=True)

    params = {k: v for k, v in [("prompt", a.prompt), ("negative", a.negative), ("steps", a.steps),
                                ("cfg", a.cfg), ("width", a.width), ("height", a.height)] if v is not None}
    if refs_remote:
        params["refs"] = refs_remote
    for kv in a.param or []:
        k, _, v = kv.partition("=")
        params[k] = coerce(v)

    jobs = []
    client_id = uuid.uuid4().hex
    for i in range(a.n):
        seed = base_seed + i
        try:
            wf = workflows.build(a.workflow, **params, seed=seed, prefix=f"h3/{stamp}-{name}")
        except ValueError as e:
            sys.exit(str(e))
        for s_ in a.set or []:
            key, _, val = s_.partition("=")
            node, _, inp = key.partition(".")
            if node not in wf:
                sys.exit(f"--set: no node '{node}' (have {', '.join(wf)})")
            wf[node]["inputs"][inp] = coerce(val)
        resp = post_json("/prompt", {"prompt": wf, "client_id": client_id})
        record = {**params, "refs": refs_local, "set": a.set or []}
        jobs.append((resp["prompt_id"], seed, record, wf))
    print(f"queued {len(jobs)} × {a.workflow} (seeds {base_seed}..{base_seed + a.n - 1}) -> {out}")

    t0 = time.time()
    done = wait_for([j[0] for j in jobs], a.timeout)
    elapsed = time.time() - t0
    written = []
    for pid, seed, params, wf in jobs:
        imgs = [img for o in done[pid]["outputs"].values() for img in o.get("images", [])]
        for k, img in enumerate(imgs):
            q = urllib.parse.urlencode({"filename": img["filename"], "subfolder": img["subfolder"], "type": img["type"]})
            data = get(f"/view?{q}", raw=True)
            stem = f"{name}_s{seed}" + (f"_{k}" if len(imgs) > 1 else "")
            ext = Path(img["filename"]).suffix or ".png"
            (out / f"{stem}{ext}").write_bytes(data)
            if ext != ".png":
                print(f"  {out / (stem + ext)}")
                make_poster(out / f"{stem}{ext}", out / f"{stem}.png")  # sheets and the blank check read this frame
            (out / f"{stem}.json").write_text(json.dumps({
                "workflow": a.workflow, "seed": seed, "params": params, "prompt_id": pid,
                "remote": f"{img['subfolder']}/{img['filename']}", "batch_seconds": round(elapsed, 1),
                "created": datetime.now().isoformat(timespec="seconds"), "api_prompt": wf,
            }, indent=1))
            written.append(out / f"{stem}.png")
            if looks_blank(out / f"{stem}.png"):
                print(f"  WARNING: {stem}.png is blank/uniform — check server flags (e.g. sage attention) or params")
    print(f"done in {elapsed:.0f}s:")
    for w in written:
        print(f"  {w.relative_to(ROOT) if w.is_relative_to(ROOT) else w}")
    if len(written) > 1 and not a.no_sheet:
        sheet = make_sheet(written, out / "_sheet.jpg")
        print(f"  sheet: {sheet.relative_to(ROOT) if sheet.is_relative_to(ROOT) else sheet}")


def make_poster(video, png):
    """Middle frame of a video, as a PNG beside it."""
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                               capture_output=True, text=True, check=True).stdout)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(dur / 2), "-i", str(video), "-frames:v", "1", str(png)], check=True)


# ---------- job queue (tools/queue_service.py on the pod) ----------
def queue_url():
    return f"http://127.0.0.1:{config().get('queue_local_port', 8189)}"


def queue_api(path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    wait_for_queue()
    req = urllib.request.Request(queue_url() + path, data, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"queue: {json.loads(e.read() or b'{}').get('error', e.code)}")


def wait_for_queue(seconds=30):
    """The service restarts itself within seconds if it dies, so ride that out."""
    end = time.time() + seconds
    while not queue_ok():
        if time.time() > end:
            sys.exit(f"the queue service hasn't answered for {seconds}s; see /workspace/queue/service.log on the pod")
        time.sleep(1)


def queue_ok():
    try:
        with urllib.request.urlopen(queue_url() + "/health", timeout=15) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_queue_tunnel():
    c = config()
    if queue_ok():
        return
    local, remote = c.get("queue_local_port", 8189), c.get("queue_remote_port", 8189)
    if port_open(local):  # the tunnel is up, so the service is probably mid-restart
        wait_for_queue()
        return
    subprocess.run(["ssh", "-f", "-N", "-i", str(Path(c["key"]).expanduser()), "-o", "BatchMode=yes",
                    "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30", "-o", "ConnectTimeout=15",
                    "-L", f"127.0.0.1:{local}:127.0.0.1:{remote}", "-p", str(c["ssh_port"]), f"{c['ssh_user']}@{c['host']}"], check=True)
    for _ in range(20):
        if queue_ok():
            return
        time.sleep(0.5)
    sys.exit("tunnel started but the queue service is not responding (is it running on the pod?)")


def make_jobs(workflow, shot, prompt, refs, params, n, seed, sets=()):
    """Upload refs and build the graphs for n takes on consecutive seeds."""
    if not re.fullmatch(r"[\w.-]+", shot):
        sys.exit(f"shot names can use letters, digits, '.', '-' and '_': {shot!r}")
    if workflow not in workflows.WORKFLOWS:
        sys.exit(f"unknown workflow {workflow}; have {list(workflows.WORKFLOWS)}")
    local, remote = [], []
    for ref in refs or []:
        p = Path(ref).expanduser()
        p = p if p.is_absolute() or p.exists() else ROOT / p
        if not p.exists():
            sys.exit(f"reference not found: {ref}")
        local.append(str(p)); remote.append(upload_image(p))
    base_seed = seed if seed is not None else random.randrange(2**31)
    jobs = []
    params = dict(params)
    if params.get("audios"):  # H3 reference audio, uploaded like an image
        params["audios"] = [upload_image(ROOT / a) for a in params["audios"]]
    for i in range(n):
        built = {**params, "prompt": prompt}
        if remote:
            built["refs"] = remote
        try:
            graph = workflows.build(workflow, **built, seed=base_seed + i, prefix=f"h3/queue/{shot}/{shot}")
        except ValueError as e:
            sys.exit(f"{shot}: {e}")
        for s_ in sets:
            key, _, val = s_.partition("=")
            node, _, inp = key.partition(".")
            if node not in graph:
                sys.exit(f"--set: no node '{node}' (have {', '.join(graph)})")
            graph[node]["inputs"][inp] = coerce(val)
        jobs.append({"shot": shot, "workflow": workflow, "seed": base_seed + i, "graph": graph,
                     "params": {**params, "prompt": prompt, "set": list(sets)}, "refs": local})
    return jobs


def cmd_submit(a):
    ensure_tunnel(quiet=True)
    ensure_queue_tunnel()
    jobs = []
    if a.file:
        for s in json.loads(Path(a.file).read_text()):
            jobs += make_jobs(s.get("workflow", a.workflow or "h3"), s["shot"], s["prompt"], s.get("refs"),
                              s.get("params", {}), s.get("n", 1), s.get("seed"))
    else:
        if not (a.workflow and a.shot and a.prompt):
            sys.exit("submit needs WORKFLOW, --shot and -p (or --file shots.json)")
        params = {k: v for k, v in [("steps", a.steps), ("width", a.width), ("height", a.height)] if v is not None}
        for kv in a.param or []:
            k, _, v = kv.partition("=")
            params[k] = coerce(v)
        jobs = make_jobs(a.workflow, a.shot, a.prompt, a.ref, params, a.n, a.seed, a.set or [])
    ids = queue_api("/jobs", {"jobs": jobs, "front": a.front})["ids"]
    print(f"queued {len(ids)} job(s): {', '.join(map(str, ids))}")


def fmt_seconds(s):
    return f"{s // 3600:.0f}h{s % 3600 // 60:02.0f}m" if s >= 3600 else f"{s // 60:.0f}m{s % 60:02.0f}s"


def cmd_queue(a):
    ensure_queue_tunnel()
    h = queue_api("/health")
    jobs = queue_api("/jobs" + (f"?shot={urllib.parse.quote(a.shot)}" if a.shot else ""))["jobs"]
    live = [j for j in jobs if j["status"] in ("running", "queued")]
    rest = [j for j in jobs if j["status"] not in ("running", "queued")]
    times = [j["seconds"] for j in rest if j["status"] == "done" and j["seconds"]]
    print(f"{'PAUSED · ' if h['paused'] else ''}ComfyUI {'up' if h['comfy_up'] else 'DOWN'} · "
          + (" · ".join(f"{v} {k}" for k, v in sorted(h["counts"].items())) or "empty")
          + (f" · about {fmt_seconds(sum(times) / len(times) * len(live))} left" if times and live else ""))
    now = time.time()
    for j in live + (rest if a.all else rest[:5]):
        when = fmt_seconds(now - j["started_at"]) if j["status"] == "running" and j["started_at"] else (
            f"{j['seconds']:.0f}s" if j["seconds"] else "")
        text = " ".join(str((j["params"] or {}).get("prompt", "")).split())[:48]
        got = "" if j["status"] != "done" else ("" if j["synced"] else " unsynced")
        print(f"{j['id']:>5} {j['status']:<9} {j['shot'] + ' t' + str(j['take']):<24} {when:>7}{got}  {text}")
        if j["error"]:
            print(f"      {j['error'][:150]}")
    if not a.all and len(rest) > 5:
        print(f"      ... {len(rest) - 5} older (--all)")


def cmd_move(a):
    ensure_queue_tunnel()
    body = {"before": a.before} if a.before else {"after": a.after} if a.after else {"to": "back" if a.back else "front"}
    queue_api(f"/jobs/{a.id}/move", body)
    print(f"moved job {a.id}")


def cmd_job_action(a):
    ensure_queue_tunnel()
    for job_id in a.ids:
        print(f"job {job_id}: {queue_api(f'/jobs/{job_id}/{a.action}', {})['status']}")


def cmd_pause(a):
    ensure_queue_tunnel()
    print("paused" if queue_api("/" + a.action, {})["paused"] else "running")


def sync_once(out):
    """Download finished jobs not fetched yet. Returns how many were pulled."""
    pulled = 0
    jobs = queue_api("/jobs?status=done")["jobs"]
    for j in jobs:
        if j["synced"]:
            continue
        full = queue_api(f"/jobs/{j['id']}")
        folder = out / j["shot"]
        folder.mkdir(parents=True, exist_ok=True)
        stem = f"{j['shot']}_t{j['take']:02}_s{j['seed']}"
        try:
            for k, f in enumerate(full["outputs"]):
                q = urllib.parse.urlencode({"filename": f["filename"], "subfolder": f["subfolder"], "type": f["type"]})
                name = stem + (f"_{k}" if len(full["outputs"]) > 1 else "")
                ext = Path(f["filename"]).suffix or ".png"
                (folder / f"{name}{ext}.part").write_bytes(get(f"/view?{q}", raw=True))
                (folder / f"{name}{ext}.part").rename(folder / f"{name}{ext}")
                if ext != ".png":
                    try:
                        make_poster(folder / f"{name}{ext}", folder / f"{name}.png")
                    except (subprocess.CalledProcessError, ValueError, OSError):
                        print(f"  no poster for {name}{ext}")
            (folder / f"{stem}.json").write_text(json.dumps({k: full[k] for k in full if k != "graph"} | {"api_prompt": full["graph"]}, indent=1))
        except (urllib.error.URLError, OSError) as e:
            print(f"  job {j['id']} not downloaded yet: {e}")
            continue
        queue_api(f"/jobs/{j['id']}/synced", {})
        pulled += 1
        print(f"  {(folder / stem).relative_to(ROOT) if (folder / stem).is_relative_to(ROOT) else folder / stem} ({j['seconds']}s)", flush=True)
    # local copy of the queue, in case the pod's disk goes away
    out.mkdir(parents=True, exist_ok=True)
    (out / "jobs.json").write_text(json.dumps(queue_api("/jobs")["jobs"], indent=1))
    return pulled


def cmd_sync(a):
    ensure_tunnel(quiet=True)
    ensure_queue_tunnel()
    out = Path(a.out) if a.out else RENDERS / "queue"
    total = sync_once(out)
    while a.wait:
        h = queue_api("/health")["counts"]
        if not (h.get("queued") or h.get("running")):
            total += sync_once(out)
            break
        time.sleep(a.interval)
        total += sync_once(out)
    print(f"{total} job(s) synced to {out}")
    failed = queue_api("/jobs?status=failed")["jobs"]
    if failed:
        print(f"{len(failed)} failed: {', '.join(str(j['id']) for j in failed)} (see `gen.py queue`)")


def looks_blank(path):
    from PIL import Image, ImageStat
    st = ImageStat.Stat(Image.open(path).convert("L"))
    return st.stddev[0] < 2.0


def make_sheet(paths, out_path, cell_w=640, cols=None):
    from PIL import Image, ImageDraw, ImageFont
    paths = [Path(p) for p in paths]
    ims = [Image.open(p).convert("RGB") for p in paths]
    cols = cols or (2 if len(ims) <= 4 else 4)
    ar = ims[0].height / ims[0].width
    cell_h, label_h = int(cell_w * ar), 22
    rows = (len(ims) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell_w, rows * (cell_h + label_h)), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 15)
    except OSError:
        font = ImageFont.load_default()
    for i, (p, im) in enumerate(zip(paths, ims)):
        x, y = (i % cols) * cell_w, (i // cols) * (cell_h + label_h)
        sheet.paste(im.resize((cell_w, cell_h)), (x, y + label_h))
        draw.text((x + 6, y + 3), f"{i + 1}. {p.stem}", fill="black", font=font)
    sheet.save(out_path, quality=88)
    return out_path


def cmd_sheet(a):
    paths = []
    for p in map(Path, a.paths):
        paths += sorted(x for x in p.glob("*.png")) if p.is_dir() else [p]
    if not paths:
        sys.exit("no images")
    out = Path(a.out) if a.out else (Path(a.paths[0]) if Path(a.paths[0]).is_dir() else Path(a.paths[0]).parent) / "_sheet.jpg"
    print(make_sheet(paths, out, cols=a.cols))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tunnel").set_defaults(fn=cmd_tunnel)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    m = sub.add_parser("models"); m.add_argument("folder", nargs="?"); m.set_defaults(fn=cmd_models)
    sub.add_parser("workflows").set_defaults(fn=cmd_workflows)
    r = sub.add_parser("run")
    r.add_argument("workflow")
    r.add_argument("-p", "--prompt")
    r.add_argument("--negative")
    r.add_argument("--ref", action="append", help="reference image (repeat for ref1, ref2, ...)")
    r.add_argument("-n", type=int, default=1, help="number of variants (consecutive seeds)")
    r.add_argument("--seed", type=int)
    r.add_argument("--steps", type=int)
    r.add_argument("--cfg", type=float)
    r.add_argument("--width", type=int)
    r.add_argument("--height", type=int)
    r.add_argument("--param", action="append", help="builder param KEY=VALUE (e.g. length=192, turbo=1)")
    r.add_argument("--set", action="append", help="raw node override NODE.INPUT=VALUE")
    r.add_argument("--name", help="label for output files")
    r.add_argument("-o", "--out", help="output dir (default renders/<stamp>-<name>)")
    r.add_argument("--timeout", type=int, default=1800)
    r.add_argument("--no-sheet", action="store_true")
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("sheet"); s.add_argument("paths", nargs="+"); s.add_argument("-o", "--out")
    s.add_argument("--cols", type=int); s.set_defaults(fn=cmd_sheet)
    q = sub.add_parser("submit")
    q.add_argument("workflow", nargs="?")
    q.add_argument("--shot")
    q.add_argument("-p", "--prompt")
    q.add_argument("--ref", action="append")
    q.add_argument("-n", type=int, default=1)
    q.add_argument("--seed", type=int)
    q.add_argument("--steps", type=int)
    q.add_argument("--width", type=int)
    q.add_argument("--height", type=int)
    q.add_argument("--param", action="append")
    q.add_argument("--set", action="append")
    q.add_argument("--front", action="store_true", help="put ahead of everything queued")
    q.add_argument("--file", help='JSON list of {"shot", "prompt", "refs", "params", "n", "seed", "workflow"}')
    q.set_defaults(fn=cmd_submit)
    q = sub.add_parser("queue"); q.add_argument("--shot"); q.add_argument("--all", action="store_true"); q.set_defaults(fn=cmd_queue)
    q = sub.add_parser("move"); q.add_argument("id", type=int); q.add_argument("--back", action="store_true")
    q.add_argument("--before", type=int); q.add_argument("--after", type=int); q.set_defaults(fn=cmd_move)
    for action in ("cancel", "retry"):
        q = sub.add_parser(action); q.add_argument("ids", type=int, nargs="+"); q.set_defaults(fn=cmd_job_action, action=action)
    for action in ("pause", "resume"):
        sub.add_parser(action).set_defaults(fn=cmd_pause, action=action)
    q = sub.add_parser("sync"); q.add_argument("-o", "--out"); q.add_argument("--wait", action="store_true", help="keep going until the queue is empty")
    q.add_argument("--interval", type=float, default=30); q.set_defaults(fn=cmd_sync)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
