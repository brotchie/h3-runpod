"""Durable job queue in front of ComfyUI. Runs on the pod; standard library only.

Holds shots in SQLite and feeds ComfyUI one graph at a time, so jobs survive restarts of
this service and of ComfyUI, and the order can change until a job starts. Binds to localhost;
reach it through the SSH tunnel with the `gen.py submit/queue/move/...` commands.

  GET  /health                     counts, paused flag, whether ComfyUI answers
  GET  /jobs[?status=&shot=]       jobs in queue order, without graphs
  GET  /jobs/ID                    one job, with its graph
  POST /jobs                       {"jobs": [{shot, workflow, graph, seed?, params?, refs?}], "front": bool}
  POST /jobs/ID/move               {"to": "front"|"back"} or {"before": ID} or {"after": ID}
  POST /jobs/ID/cancel             queued or running (interrupts ComfyUI)
  POST /jobs/ID/retry              failed or cancelled, back onto the end of the queue
  POST /jobs/ID/synced             client has the outputs
  POST /pause, /resume             stop or restart launching; a running job still finishes

A job gets `max_attempts` tries when it fails during execution or ComfyUI loses it (a crash).
A graph ComfyUI rejects outright fails at once, since it would fail the same way again.

Usage: python3 queue_service.py [--db PATH] [--port 8189] [--comfy http://127.0.0.1:8188]
"""
import argparse
import json
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, shot TEXT NOT NULL, take INTEGER NOT NULL, workflow TEXT,
  seed INTEGER, params TEXT, refs TEXT, graph TEXT NOT NULL, position INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 2,
  prompt_id TEXT, error TEXT, outputs TEXT, seconds REAL, synced INTEGER NOT NULL DEFAULT 0,
  submitted_at REAL, started_at REAL, finished_at REAL, UNIQUE(shot, take));
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
"""
JSON_FIELDS = ("params", "refs", "graph", "outputs")


class ComfyDown(Exception):
    pass


class Rejected(Exception):
    pass


class HttpError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Store:
    """SQLite access. One lock serializes the worker and the HTTP threads."""

    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    @staticmethod
    def decode(row, graph=False):
        job = dict(row)
        for f in JSON_FIELDS:
            job[f] = json.loads(job[f]) if job[f] else None
        if not graph:
            job.pop("graph")
        return job

    def get(self, job_id, graph=True):
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HttpError(404, f"no job {job_id}")
        return self.decode(row, graph)

    def list(self, status=None, shot=None):
        where, args = [], []
        if status:
            where.append("status=?"); args.append(status)
        if shot:
            where.append("shot=?"); args.append(shot)
        sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "")
        order = """ ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
                    CASE WHEN status IN ('running','queued') THEN position ELSE -finished_at END"""
        with self.lock:
            return [self.decode(r) for r in self.db.execute(sql + order, args)]

    def add(self, jobs, front=False):
        now, ids = time.time(), []
        with self.lock, self.db:
            lo, hi = self.db.execute("SELECT MIN(position), MAX(position) FROM jobs WHERE status='queued'").fetchone()
            lo, hi = lo or 1, hi or 0
            for offset, j in enumerate(jobs):
                if not j.get("shot") or not isinstance(j.get("graph"), dict):
                    raise HttpError(400, "each job needs a shot and a graph")
                take = self.db.execute("SELECT COALESCE(MAX(take), 0) + 1 FROM jobs WHERE shot=?", (j["shot"],)).fetchone()[0]
                pos = lo - len(jobs) + offset if front else hi + 1 + offset
                cur = self.db.execute(
                    """INSERT INTO jobs (shot, take, workflow, seed, params, refs, graph, position, max_attempts, submitted_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (j["shot"], take, j.get("workflow"), j.get("seed"), json.dumps(j.get("params")), json.dumps(j.get("refs")),
                     json.dumps(j["graph"]), pos, j.get("max_attempts", 2), now))
                ids.append(cur.lastrowid)
        return ids

    def update(self, job_id, **fields):
        for f in JSON_FIELDS:
            if f in fields:
                fields[f] = json.dumps(fields[f])
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.lock, self.db:
            self.db.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))

    def move(self, job_id, to=None, before=None, after=None):
        with self.lock, self.db:
            order = [r[0] for r in self.db.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY position")]
            if job_id not in order:
                raise HttpError(409, f"job {job_id} isn't queued")
            order.remove(job_id)
            anchor = before if before is not None else after
            if anchor is not None:
                if anchor not in order:
                    raise HttpError(409, f"job {anchor} isn't queued")
                order.insert(order.index(anchor) + (0 if before is not None else 1), job_id)
            elif to == "front":
                order.insert(0, job_id)
            elif to == "back":
                order.append(job_id)
            else:
                raise HttpError(400, 'give "to": "front"|"back", "before" or "after"')
            self.db.executemany("UPDATE jobs SET position=? WHERE id=?", [(i, j) for i, j in enumerate(order)])

    def next_queued(self):
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY position LIMIT 1").fetchone()
        return self.decode(row, True) if row else None

    def running(self):
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE status='running' LIMIT 1").fetchone()
        return self.decode(row, True) if row else None

    def back_position(self):
        with self.lock:
            return (self.db.execute("SELECT MAX(position) FROM jobs WHERE status='queued'").fetchone()[0] or 0) + 1

    def counts(self):
        with self.lock:
            return dict(self.db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())

    @property
    def paused(self):
        with self.lock:
            row = self.db.execute("SELECT value FROM state WHERE key='paused'").fetchone()
        return bool(row and row[0] == "1")

    @paused.setter
    def paused(self, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES ('paused', ?)", ("1" if value else "0",))


class Comfy:
    def __init__(self, url):
        self.url = url.rstrip("/")

    def call(self, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.url + path, data, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 400:
                raise Rejected(e.read().decode(errors="replace")[:2000])
            raise ComfyDown(f"HTTP {e.code}")
        except (urllib.error.URLError, OSError) as e:
            raise ComfyDown(str(e))
        return json.loads(body) if body.strip() else {}

    def up(self):
        try:
            self.call("/system_stats")
            return True
        except (ComfyDown, Rejected):
            return False


class Worker(threading.Thread):
    """Runs one job at a time. The prompt id is stored before the POST, so a crash at any
    point can be resolved afterwards from ComfyUI's history and queue without running a job twice."""

    def __init__(self, store, comfy, poll=1.0):
        super().__init__(daemon=True)
        self.store, self.comfy, self.poll = store, comfy, poll
        self.halt = threading.Event()

    def run(self):
        while not self.halt.wait(self.poll):
            try:
                self.tick()
            except ComfyDown:
                pass  # wait for it to come back; nothing about the job has changed
            except Exception as e:
                print(f"worker error: {e!r}", flush=True)

    def tick(self):
        job = self.store.running()
        if job:
            self.check(job)
        elif not self.store.paused:
            job = self.store.next_queued()
            if job:
                self.start_job(job)

    def start_job(self, job):
        self.comfy.call("/system_stats")  # don't spend an attempt while ComfyUI is down
        pid = str(uuid.uuid4())  # ComfyUI only accepts the canonical hyphenated form
        self.store.update(job["id"], status="running", prompt_id=pid, attempts=job["attempts"] + 1,
                          started_at=time.time(), error=None)
        try:
            self.comfy.call("/prompt", {"prompt": job["graph"], "prompt_id": pid, "client_id": "queue_service"})
        except Rejected as e:
            self.store.update(job["id"], status="failed", error=f"ComfyUI rejected the graph: {e}", finished_at=time.time())

    def check(self, job):
        pid = job["prompt_id"]
        entry = self.comfy.call(f"/history/{pid}").get(pid)
        if entry is None:
            q = self.comfy.call("/queue")
            if any(item[1] == pid for key in ("queue_running", "queue_pending") for item in q.get(key, [])):
                return
            entry = self.comfy.call(f"/history/{pid}").get(pid)  # it may have finished between the two calls
        if entry is None:
            self.retry_or_fail(job, "ComfyUI lost the job (it probably restarted)")
            return
        status = entry.get("status", {})
        if status.get("status_str") == "error":
            self.retry_or_fail(job, self.error_text(status))
            return
        if not status.get("completed", False) and "status" in entry:
            return
        outputs = [dict(f, node=node) for node, out in entry.get("outputs", {}).items()
                   for files in out.values() if isinstance(files, list)
                   for f in files if isinstance(f, dict) and "filename" in f]
        if not outputs:
            self.retry_or_fail(job, "ComfyUI finished without writing an output")
            return
        self.store.update(job["id"], status="done", outputs=outputs, seconds=self.run_seconds(status), finished_at=time.time())

    def retry_or_fail(self, job, error):
        if job["attempts"] < job["max_attempts"]:
            print(f"job {job['id']} attempt {job['attempts']} failed, retrying: {error}", flush=True)
            self.store.update(job["id"], status="queued", error=error, prompt_id=None)  # keeps its place in line
        else:
            self.store.update(job["id"], status="failed", error=error, finished_at=time.time())

    @staticmethod
    def error_text(status):
        for kind, msg in status.get("messages", []):
            if kind == "execution_error":
                return f"{msg.get('node_type')}: {msg.get('exception_message', '').strip()}"[:2000]
            if kind == "execution_interrupted":
                return "interrupted"
        return "ComfyUI reported an error"

    @staticmethod
    def run_seconds(status):
        msgs = {m[0]: m[1] for m in status.get("messages", [])}
        try:
            return round((msgs["execution_success"]["timestamp"] - msgs["execution_start"]["timestamp"]) / 1000, 1)
        except (KeyError, TypeError):
            return None


def cancel(store, comfy, job_id):
    job = store.get(job_id, graph=False)
    if job["status"] not in ("queued", "running"):
        raise HttpError(409, f"job {job_id} is {job['status']}")
    store.update(job_id, status="cancelled", finished_at=time.time())
    if job["status"] == "running":
        try:
            comfy.call("/queue", {"delete": [job["prompt_id"]]})
            comfy.call("/interrupt", {"prompt_id": job["prompt_id"]})
        except (ComfyDown, Rejected):
            pass  # if ComfyUI is down the job is already gone


def retry(store, job_id):
    job = store.get(job_id, graph=False)
    if job["status"] not in ("failed", "cancelled"):
        raise HttpError(409, f"job {job_id} is {job['status']}")
    store.update(job_id, status="queued", attempts=0, error=None, prompt_id=None, finished_at=None,
                 position=store.back_position())


def make_handler(store, comfy):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def reply(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def handle_safely(self, fn):
            try:
                self.reply(fn())
            except HttpError as e:
                self.reply({"error": str(e)}, e.code)
            except (ValueError, KeyError, TypeError) as e:
                self.reply({"error": f"bad request: {e}"}, 400)

        def do_GET(self):
            self.handle_safely(self.route_get)

        def do_POST(self):
            self.handle_safely(self.route_post)

        def route_get(self):
            url = urllib.parse.urlparse(self.path)
            if url.path == "/health":
                return {"ok": True, "paused": store.paused, "counts": store.counts(), "comfy_up": comfy.up()}
            if url.path == "/jobs":
                q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                return {"jobs": store.list(q.get("status"), q.get("shot"))}
            m = re.fullmatch(r"/jobs/(\d+)", url.path)
            if m:
                return store.get(int(m[1]))
            raise HttpError(404, "not found")

        def route_post(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/jobs":
                return {"ids": store.add(body["jobs"], bool(body.get("front")))}
            if self.path in ("/pause", "/resume"):
                store.paused = self.path == "/pause"
                return {"paused": store.paused}
            m = re.fullmatch(r"/jobs/(\d+)/(move|cancel|retry|synced)", self.path)
            if not m:
                raise HttpError(404, "not found")
            job_id, verb = int(m[1]), m[2]
            store.get(job_id, graph=False)
            if verb == "move":
                store.move(job_id, body.get("to"), body.get("before"), body.get("after"))
            elif verb == "cancel":
                cancel(store, comfy, job_id)
            elif verb == "retry":
                retry(store, job_id)
            else:
                store.update(job_id, synced=1)
            return store.get(job_id, graph=False)
    return Handler


def serve(db, port, comfy_url, poll=1.0):
    """Start the worker and HTTP server; returns (server, worker) for tests. Caller runs serve_forever."""
    store, comfy = Store(db), Comfy(comfy_url)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store, comfy))
    server.daemon_threads = True
    worker = Worker(store, comfy, poll)
    worker.start()
    return server, worker


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="/workspace/queue/queue.db")
    ap.add_argument("--port", type=int, default=8189)
    ap.add_argument("--comfy", default="http://127.0.0.1:8188")
    ap.add_argument("--poll", type=float, default=1.0)
    a = ap.parse_args()
    server, _ = serve(a.db, a.port, a.comfy, a.poll)
    print(f"queue service on 127.0.0.1:{a.port}, db {a.db}, ComfyUI {a.comfy}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
