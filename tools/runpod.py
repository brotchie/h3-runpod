"""Minimal RunPod helper (GraphQL API). Reads RUNPOD_API_KEY from the environment or .env; never prints it.

  runpod.py up [--name N] [--disk GB]   deploy an H100, install the pinned setup, restore the queue, update comfy.json
  runpod.py backup [--pod ID]           copy the queue database (take numbering) into work/pod_backup
  runpod.py down [--pod ID]             refuse if work is unfinished, back up, then terminate the pod
  runpod.py status                      list pods with SSH endpoints

The pods have no network volume, so terminating one loses everything on it. That is fine because of what lives here:
pod_lock.json and pod_requirements.txt pin the install, renders are synced to renders/queue, and the backup restores the queue.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path(__file__).resolve().parent / "comfy.json"
EXAMPLE = Path(__file__).resolve().parent / "comfy.example.json"
LOCK = Path(__file__).resolve().parent / "pod_lock.json"
BACKUP = ROOT / "work/pod_backup"
IMAGE = json.loads(LOCK.read_text())["image"]
PREFERENCE = ["H100 SXM", "H100 NVL", "H100 PCIe"]


def load_config():
    """comfy.json once `up` has written it, else the example's defaults."""
    return json.loads((CONFIG if CONFIG.exists() else EXAMPLE).read_text())


def api(query, variables=None):
    key = os.environ.get("RUNPOD_API_KEY") or next(
        (l.split("=", 1)[1].strip() for l in ((ROOT / ".env").read_text().splitlines() if (ROOT / ".env").exists() else [])
         if l.startswith("RUNPOD_API_KEY=")), None)
    if not key:
        sys.exit("set RUNPOD_API_KEY in the environment or in .env at the repo root")
    req = urllib.request.Request("https://api.runpod.io/graphql",
                                 json.dumps({"query": query, "variables": variables or {}}).encode(),
                                 {"Content-Type": "application/json", "Authorization": "Bearer " + key,
                                  "User-Agent": "curl/8"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}") or {"errors": [{"message": f"HTTP {e.code}"}]}


def pods():
    q = """{ myself { pods { id name desiredStatus costPerHr machine { gpuDisplayName dataCenterId }
           runtime { uptimeInSeconds ports { ip isIpPublic privatePort publicPort type } } } } }"""
    return api(q)["data"]["myself"]["pods"]


def ssh_endpoint(pod):
    for p in (pod.get("runtime") or {}).get("ports") or []:
        if p["privatePort"] == 22 and p["isIpPublic"]:
            return p["ip"], p["publicPort"]
    return None


def deploy(name, disk):
    types = {t["displayName"]: t["id"] for t in api("{ gpuTypes { id displayName } }")["data"]["gpuTypes"]}
    m = """mutation($input: PodFindAndDeployOnDemandInput) {
           podFindAndDeployOnDemand(input: $input) { id costPerHr machine { gpuDisplayName } } }"""
    for gpu in PREFERENCE:
        for cloud in ("SECURE", "COMMUNITY"):
            r = api(m, {"input": {"cloudType": cloud, "gpuCount": 1, "gpuTypeId": types[gpu], "name": name,
                                  "imageName": IMAGE, "containerDiskInGb": disk, "volumeInGb": 0,
                                  "minVcpuCount": 8, "minMemoryInGb": 64, "ports": "22/tcp,8188/http",
                                  "startSsh": True, "supportPublicIp": True}})
            pod = (r.get("data") or {}).get("podFindAndDeployOnDemand")
            if pod:
                print(f"deployed {pod['id']} {gpu} {cloud} ${pod['costPerHr']}/h", flush=True)
                return pod
            print(f"{gpu} {cloud}: {r.get('errors', [{}])[0].get('message', r)[:120]}", flush=True)
    sys.exit("no H100 capacity")


def ssh_opts():
    key = Path(load_config()["key"]).expanduser()
    return ["-i", str(key), "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=accept-new"]


def run_ssh(ep, command, **kw):
    return subprocess.run(["ssh", *ssh_opts(), "-p", str(ep[1]), f"root@{ep[0]}", command], stdin=subprocess.DEVNULL, **kw)


def scp(ep, sources, dest):
    subprocess.run(["scp", "-q", *ssh_opts(), "-P", str(ep[1]), *sources, dest], stdin=subprocess.DEVNULL, check=True)


def close_tunnels():
    """A tunnel to an old pod would answer on the local ports and hide the new one."""
    subprocess.run(["pkill", "-f", "ssh .*-L 127.0.0.1:818[89]:"], check=False)


def provision(ep):
    subprocess.run(["ssh-keygen", "-R", f"[{ep[0]}]:{ep[1]}"], capture_output=True, check=False)
    for _ in range(60):  # the port opens a little before sshd takes logins
        if run_ssh(ep, "mkdir -p /workspace/lock", capture_output=True).returncode == 0:
            break
        time.sleep(5)
    else:
        sys.exit("SSH never accepted a login")
    tools = Path(__file__).resolve().parent
    sources = [str(LOCK), str(tools / "pod_requirements.txt"), str(tools / "queue_service.py")]
    if (BACKUP / "queue.db").exists():
        sources.append(str(BACKUP / "queue.db"))
    scp(ep, sources, f"root@{ep[0]}:/workspace/lock/")
    with (tools / "pod_setup_h3.sh").open() as script:
        subprocess.run(["ssh", *ssh_opts(), "-p", str(ep[1]), f"root@{ep[0]}", "bash -s"], stdin=script, check=True)


def cmd_up(a):
    pod = deploy(a.name, a.disk)
    for _ in range(120):
        cur = next((p for p in pods() if p["id"] == pod["id"]), None)
        ep = cur and ssh_endpoint(cur)
        if ep:
            print(f"ssh -p {ep[1]} root@{ep[0]}", flush=True)
            provision(ep)
            close_tunnels()
            c = load_config()
            c.update(host=ep[0], ssh_port=ep[1],
                     pod=f"{pod['id']} ({a.name}, {pod['machine']['gpuDisplayName']}, ${pod['costPerHr']}/h)")
            CONFIG.write_text(json.dumps(c, indent=2) + "\n")
            print(f"ready: {pod['id']} ${pod['costPerHr']}/h", flush=True)
            return
        time.sleep(5)
    sys.exit(f"pod {pod['id']} deployed but no public SSH endpoint after 10 min")


def target(pod_id):
    """The named pod, or the one comfy.json points at."""
    pod_id = pod_id or (load_config().get("pod") or "").split(" ")[0]
    if not pod_id:
        sys.exit("no pod in comfy.json; pass --pod ID (see `runpod.py status`)")
    pod = next((p for p in pods() if p["id"] == pod_id), None)
    if not pod:
        sys.exit(f"no pod {pod_id}")
    return pod


QUEUE_STATE = """
import json, sqlite3
db = sqlite3.connect("/workspace/queue/queue.db")
print(json.dumps({"active": db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')").fetchone()[0],
                  "unsynced": db.execute("SELECT COUNT(*) FROM jobs WHERE status = 'done' AND COALESCE(synced, 0) = 0").fetchone()[0]}))
db.execute("VACUUM INTO '/workspace/queue/backup.db'")
"""


def backup(pod):
    """Take a clean copy of the queue database. Returns the queue's unfinished work."""
    ep = ssh_endpoint(pod)
    if not ep:
        sys.exit(f"pod {pod['id']} has no SSH endpoint")
    run_ssh(ep, "rm -f /workspace/queue/backup.db", check=True)
    out = subprocess.run(["ssh", *ssh_opts(), "-p", str(ep[1]), f"root@{ep[0]}", "python3 -"], input=QUEUE_STATE, capture_output=True, text=True, check=True).stdout
    BACKUP.mkdir(parents=True, exist_ok=True)
    tmp = BACKUP / "queue.db.tmp"
    scp(ep, [f"root@{ep[0]}:/workspace/queue/backup.db"], str(tmp))
    tmp.replace(BACKUP / "queue.db")
    return json.loads(out)


def cmd_backup(a):
    print(backup(target(a.pod)), BACKUP / "queue.db")


def cmd_down(a):
    pod = target(a.pod)
    state = backup(pod)
    if state["active"] or state["unsynced"]:
        sys.exit(f"not terminating {pod['id']}: {state['active']} jobs still queued or running, {state['unsynced']} renders not downloaded")
    r = api("mutation($input: PodTerminateInput!) { podTerminate(input: $input) }", {"input": {"podId": pod["id"]}})
    if r.get("errors"):
        sys.exit(f"terminate failed: {r['errors']}")
    c = load_config()
    if (c.get("pod") or "").startswith(pod["id"]):
        close_tunnels()
    print(f"terminated {pod['id']}, queue saved to {BACKUP / 'queue.db'}")


def cmd_status(a):
    for p in pods():
        print(p["id"], p["name"], p["desiredStatus"], f"${p['costPerHr']}/h",
              (p.get("machine") or {}).get("gpuDisplayName"), ssh_endpoint(p))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("up"); u.add_argument("--name", default="h3"); u.add_argument("--disk", type=int, default=150)
    u.set_defaults(fn=cmd_up)
    b = sub.add_parser("backup"); b.add_argument("--pod"); b.set_defaults(fn=cmd_backup)
    d = sub.add_parser("down"); d.add_argument("--pod"); d.set_defaults(fn=cmd_down)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    a = ap.parse_args()
    a.fn(a)
