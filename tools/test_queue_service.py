"""Tests for tools/queue_service.py and the gen.py queue commands, against tools/mock_comfy.py.

  tools/.venv/bin/python -m unittest tools/test_queue_service.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import queue_service as qs  # noqa: E402
from mock_comfy import MockComfy  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


GRAPH = {"n": {"class_type": "X", "inputs": {}}}


class Base(unittest.TestCase):
    delay = 0.15

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mock = MockComfy(delay=self.delay)
        self.addCleanup(self.mock.stop)
        self.port = None
        self.start_service()
        self.addCleanup(self.stop_service)

    def start_service(self):
        self.port = self.port or free_port()
        self.server, self.worker = qs.serve(self.tmp / "q.db", self.port, f"http://127.0.0.1:{self.mock.port}", poll=0.02)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop_service(self):
        self.worker.halt.set()
        self.worker.join(5)
        self.server.shutdown()
        self.server.server_close()

    def api(self, path, payload=None, expect=200):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                self.assertEqual(r.status, expect)
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, expect, e.read())
            return None

    def submit(self, shot="s1", n=1, front=False):
        return self.api("/jobs", {"jobs": [{"shot": shot, "graph": GRAPH, "seed": i, "params": {"prompt": "x"}} for i in range(n)],
                                  "front": front})["ids"]

    def job(self, job_id):
        return self.api(f"/jobs/{job_id}")

    def wait_for(self, predicate, timeout=8):
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return
            time.sleep(0.01)
        self.fail(f"timed out; jobs: {[(j['id'], j['status'], j['error']) for j in self.api('/jobs')['jobs']]}")

    def wait_status(self, job_id, *statuses):
        self.wait_for(lambda: self.job(job_id)["status"] in statuses)

    def finish_order(self):
        done = [j for j in self.api("/jobs")["jobs"] if j["status"] == "done"]
        return [j["id"] for j in sorted(done, key=lambda j: j["started_at"])]


class Ordering(Base):
    def test_runs_in_order_one_at_a_time(self):
        ids = self.submit(n=4)
        peak = 0
        def all_done():
            nonlocal peak
            q = [j["status"] for j in self.api("/jobs")["jobs"]]
            peak = max(peak, q.count("running"), (1 if self.mock.running else 0) + len(self.mock.pending))
            return q.count("done") == 4
        self.wait_for(all_done)
        self.assertEqual(self.finish_order(), ids)
        self.assertEqual(peak, 1)
        self.assertEqual(len(self.mock.executed), 4)

    def test_takes_number_per_shot(self):
        self.api("/pause", {})
        a, b = self.submit("a", 2)
        c = self.submit("b")[0]
        d = self.submit("a")[0]
        self.assertEqual([self.job(i)["take"] for i in (a, b, c, d)], [1, 2, 1, 3])

    def test_list_shows_queue_order(self):
        self.api("/pause", {})
        ids = self.submit(n=3)
        self.assertEqual([j["id"] for j in self.api("/jobs")["jobs"]], ids)

    def test_front_insert_keeps_batch_order(self):
        self.api("/pause", {})
        a = self.submit(n=2)
        b = self.submit("urgent", 2, front=True)
        self.assertEqual([j["id"] for j in self.api("/jobs")["jobs"]], b + a)

    def test_move(self):
        self.api("/pause", {})
        a, b, c, d = self.submit(n=4)
        order = lambda: [j["id"] for j in self.api("/jobs?status=queued")["jobs"]]
        self.api(f"/jobs/{d}/move", {"to": "front"})
        self.assertEqual(order(), [d, a, b, c])
        self.api(f"/jobs/{d}/move", {"to": "back"})
        self.assertEqual(order(), [a, b, c, d])
        self.api(f"/jobs/{a}/move", {"after": c})
        self.assertEqual(order(), [b, c, a, d])
        self.api(f"/jobs/{d}/move", {"before": b})
        self.assertEqual(order(), [d, b, c, a])
        self.api("/resume", {})
        self.wait_status(a, "done")
        self.assertEqual(self.finish_order(), [d, b, c, a])

    def test_new_jobs_after_a_move_still_go_last(self):
        self.api("/pause", {})
        a, b = self.submit(n=2)
        self.api(f"/jobs/{b}/move", {"to": "front"})
        c = self.submit()[0]
        self.assertEqual([j["id"] for j in self.api("/jobs?status=queued")["jobs"]], [b, a, c])

    def test_cannot_move_a_running_job(self):
        a = self.submit()[0]
        self.wait_status(a, "running")
        self.api(f"/jobs/{a}/move", {"to": "front"}, expect=409)

    def test_bad_requests(self):
        self.api("/jobs", {"jobs": [{"shot": "s"}]}, expect=400)
        self.api("/jobs/999", expect=404)
        self.api("/jobs/999/cancel", {}, expect=404)
        a = self.submit()[0]
        self.api(f"/jobs/{a}/move", {}, expect=400 if self.job(a)["status"] == "queued" else 409)


class Control(Base):
    def test_pause_holds_queue_but_running_job_finishes(self):
        a, b = self.submit(n=2)
        self.wait_status(a, "running")
        self.api("/pause", {})
        self.wait_status(a, "done")
        time.sleep(0.15)
        self.assertEqual(self.job(b)["status"], "queued")
        self.api("/resume", {})
        self.wait_status(b, "done")

    def test_cancel_queued_never_runs(self):
        a, b = self.submit(n=2)
        self.api(f"/jobs/{b}/cancel", {})
        self.wait_status(a, "done")
        time.sleep(0.1)
        self.assertEqual(self.job(b)["status"], "cancelled")
        self.assertEqual(len(self.mock.executed), 1)

    def test_cancel_running_interrupts_and_moves_on(self):
        self.mock.delay = 5
        a, b = self.submit(n=2)
        self.wait_status(a, "running")
        self.wait_for(lambda: self.mock.running is not None)
        self.api(f"/jobs/{a}/cancel", {})
        self.mock.delay = 0.05
        self.wait_status(b, "done")
        self.assertEqual(self.job(a)["status"], "cancelled")
        self.assertIn(self.job(a)["prompt_id"], self.mock.interrupted)

    def test_cannot_cancel_finished(self):
        a = self.submit()[0]
        self.wait_status(a, "done")
        self.api(f"/jobs/{a}/cancel", {}, expect=409)

    def test_retry_requeues_at_back(self):
        self.mock.reject_next = 1
        a = self.submit()[0]
        self.wait_status(a, "failed")
        self.api("/pause", {})
        b = self.submit("other")[0]
        self.api(f"/jobs/{a}/retry", {})
        self.assertEqual([j["id"] for j in self.api("/jobs?status=queued")["jobs"]], [b, a])
        self.api("/resume", {})
        self.wait_status(a, "done")
        self.assertIsNone(self.job(a)["error"])

    def test_cannot_retry_done(self):
        a = self.submit()[0]
        self.wait_status(a, "done")
        self.api(f"/jobs/{a}/retry", {}, expect=409)


class Failures(Base):
    def test_rejected_graph_fails_at_once_and_queue_continues(self):
        self.mock.reject_next = 1
        a, b = self.submit(n=2)
        self.wait_status(b, "done")
        j = self.job(a)
        self.assertEqual(j["status"], "failed")
        self.assertEqual(j["attempts"], 1)
        self.assertIn("rejected", j["error"])

    def test_execution_error_retries_then_succeeds(self):
        self.mock.fail_next = 1
        a = self.submit()[0]
        self.wait_status(a, "done")
        j = self.job(a)
        self.assertEqual(j["attempts"], 2)
        self.assertIsNone(j["error"])
        self.assertEqual(len(self.mock.executed), 2)

    def test_execution_error_gives_up_with_the_message(self):
        self.mock.fail_next = 5
        a, b = self.submit(n=2)
        self.wait_status(a, "failed")
        j = self.job(a)
        self.assertEqual(j["attempts"], 2)
        self.assertIn("CUDA out of memory", j["error"])
        self.mock.fail_next = 0
        self.wait_status(b, "done")

    def test_retried_job_keeps_its_place(self):
        self.mock.fail_next = 1
        a, b = self.submit(n=2)
        self.wait_status(b, "done")
        self.assertEqual(self.finish_order()[-1], b)
        self.assertEqual(self.job(a)["status"], "done")

    def test_comfy_restart_mid_job_requeues_it(self):
        self.mock.delay = 5
        a = self.submit()[0]
        self.wait_for(lambda: self.mock.running is not None)
        first = self.job(a)["prompt_id"]
        self.mock.delay = 0.05
        self.mock.forget_everything()
        self.wait_status(a, "done")
        j = self.job(a)
        self.assertEqual(j["attempts"], 2)
        self.assertNotEqual(j["prompt_id"], first)

    def test_repeated_crashes_stop_after_max_attempts(self):
        self.mock.delay = 5
        a, b = self.submit(n=2)
        for _ in range(2):
            self.wait_for(lambda: self.mock.running is not None and self.job(a)["status"] == "running")
            self.mock.forget_everything()
            time.sleep(0.08)
        self.wait_status(a, "failed")
        self.assertIn("lost", self.job(a)["error"])
        self.mock.delay = 0.02
        self.mock.forget_everything()

    def test_comfy_down_waits_without_spending_attempts(self):
        comfy = qs.Comfy("http://127.0.0.1:1")
        self.stop_service()
        store = qs.Store(self.tmp / "q.db")
        a = store.add([{"shot": "s", "graph": GRAPH}])[0]
        worker = qs.Worker(store, comfy)
        for _ in range(3):
            with self.assertRaises(qs.ComfyDown):
                worker.tick()
        j = store.get(a)
        self.assertEqual((j["status"], j["attempts"]), ("queued", 0))
        self.start_service()

    def test_comfy_down_during_job_then_back_finishes(self):
        store = qs.Store(self.tmp / "other.db")
        a = store.add([{"shot": "s", "graph": GRAPH}])[0]
        qs.Worker(store, qs.Comfy(f"http://127.0.0.1:{self.mock.port}")).tick()
        self.assertEqual(store.get(a)["status"], "running")
        with self.assertRaises(qs.ComfyDown):
            qs.Worker(store, qs.Comfy("http://127.0.0.1:1")).tick()
        self.assertEqual(store.get(a)["status"], "running")
        self.assertEqual(store.get(a)["attempts"], 1)
        worker = qs.Worker(store, qs.Comfy(f"http://127.0.0.1:{self.mock.port}"))
        self.wait_for(lambda: (worker.tick(), store.get(a)["status"] == "done")[1])


class Restart(Base):
    delay = 0.6

    def test_service_restart_keeps_queue_order_and_pause(self):
        self.api("/pause", {})
        a, b, c = self.submit(n=3)
        self.api(f"/jobs/{c}/move", {"to": "front"})
        self.stop_service()
        self.start_service()
        self.assertTrue(self.api("/health")["paused"])
        self.assertEqual([j["id"] for j in self.api("/jobs?status=queued")["jobs"]], [c, a, b])

    def test_service_restart_mid_job_doesnt_run_it_twice(self):
        a, b = self.submit(n=2)
        self.wait_for(lambda: self.mock.running is not None)
        self.stop_service()
        self.start_service()
        self.wait_status(b, "done")
        j = self.job(a)
        self.assertEqual((j["status"], j["attempts"]), ("done", 1))
        self.assertEqual(len(self.mock.executed), 2)

    def test_service_down_across_the_end_of_a_job(self):
        a = self.submit()[0]
        self.wait_for(lambda: self.mock.running is not None)
        self.stop_service()
        time.sleep(0.8)  # finishes in ComfyUI while the service is away
        self.start_service()
        self.wait_status(a, "done")
        self.assertEqual(self.job(a)["attempts"], 1)
        self.assertEqual(len(self.mock.executed), 1)
        self.assertEqual(self.job(a)["outputs"][0]["type"], "output")


class Cli(Base):
    """The real gen.py commands, end to end against the mock."""
    delay = 0.1

    def setUp(self):
        super().setUp()
        self.config = self.tmp / "comfy.json"
        self.config.write_text(json.dumps({"host": "x", "ssh_port": 1, "ssh_user": "x", "key": "x", "local_port": self.mock.port,
                                           "remote_port": 1, "queue_local_port": self.port}))
        self.ref = self.tmp / "ref.png"
        self.ref.write_bytes(b"not really a png")

    def gen(self, *args, ok=True):
        r = subprocess.run([sys.executable, str(TOOLS / "gen.py"), *map(str, args)], capture_output=True, text=True, timeout=60,
                           env={**os.environ, "H3_COMFY_CONFIG": str(self.config)})
        if ok:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout + r.stderr

    def test_submit_queue_sync(self):
        out = self.gen("submit", "h3", "--shot", "s01", "-p", "a dog", "--ref", self.ref, "-n", 2, "--seed", 7, "--param", "sparse=0")
        self.assertIn("queued 2 job(s)", out)
        ids = [int(x) for x in out.split(":")[1].split(",")]
        j = self.job(ids[1])
        self.assertEqual(j["seed"], 8)
        self.assertNotIn("sparse", j["graph"])
        self.assertEqual(j["graph"]["cond"]["inputs"]["prompt"], "a dog")
        self.assertIn("ref_images.ref_image_0", j["graph"]["cond"]["inputs"])
        self.wait_status(ids[1], "done")
        renders = self.tmp / "out"
        self.assertIn("2 job(s) synced", self.gen("sync", "-o", renders))
        clip = renders / "s01" / "s01_t02_s8.mp4"
        self.assertTrue(clip.read_bytes().startswith(b"fake video"))
        sidecar = json.loads((renders / "s01" / "s01_t02_s8.json").read_text())
        self.assertEqual(sidecar["api_prompt"]["noise"]["inputs"]["noise_seed"], 8)
        self.assertEqual(len(json.loads((renders / "jobs.json").read_text())), 2)
        self.assertIn("0 job(s) synced", self.gen("sync", "-o", renders))

    def test_batch_file_and_controls(self):
        self.gen("pause")
        shots = self.tmp / "shots.json"
        shots.write_text(json.dumps([{"shot": "a", "prompt": "one"}, {"shot": "b", "prompt": "two", "n": 2, "seed": 3,
                                      "params": {"length": 22}}]))
        self.assertIn("queued 3 job(s): 1, 2, 3", self.gen("submit", "--file", shots))
        self.assertEqual(self.job(2)["graph"]["cond"]["inputs"]["length"], 22)
        self.gen("move", 3)
        self.gen("cancel", 1)
        listing = self.gen("queue")
        self.assertIn("PAUSED", listing)
        self.assertLess(listing.index("b t2"), listing.index("b t1"))
        self.assertIn("cancelled", listing)
        self.gen("retry", 1)
        self.gen("resume")
        self.assertIn("job(s) synced", self.gen("sync", "--wait", "--interval", 0.05, "-o", self.tmp / "o2"))
        self.assertEqual(sorted(p.name for p in (self.tmp / "o2").glob("*/*.mp4")), ["a_t01_s" + self.job(1)["seed"].__str__() + ".mp4", "b_t01_s3.mp4", "b_t02_s4.mp4"])

    def test_commands_wait_through_a_service_restart(self):
        a = self.submit()[0]
        self.wait_status(a, "done")
        self.stop_service()
        # an SSH tunnel with nothing behind it: accepts connections, then drops them
        tunnel = socket.socket()
        tunnel.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tunnel.bind(("127.0.0.1", self.port))
        tunnel.listen()
        tunnel.settimeout(0.05)
        def restart_later():
            end = time.time() + 2
            while time.time() < end:
                try:
                    tunnel.accept()[0].close()
                except OSError:
                    pass
            tunnel.close()
            self.start_service()
        t = threading.Thread(target=restart_later)
        t.start()
        self.assertIn("qt", self.gen("queue").replace("s1 t1", "qt"))
        t.join()

    def test_clear_errors(self):
        self.assertIn("reference not found", self.gen("submit", "h3", "--shot", "a", "-p", "x", "--ref", "/nope.png", ok=False))
        self.assertIn("doesn't take", self.gen("submit", "h3", "--shot", "a", "-p", "x", "--param", "bogus=1", ok=False))
        self.assertIn("shot names", self.gen("submit", "h3", "--shot", "../evil", "-p", "x", ok=False))
        self.assertIn("isn't queued", self.gen("move", 1, ok=False) if self.submit() and self.wait_for(lambda: self.job(1)["status"] == "done") is None else "")
        self.assertIn("no job 99", self.gen("cancel", 99, ok=False))


if __name__ == "__main__":
    unittest.main()
