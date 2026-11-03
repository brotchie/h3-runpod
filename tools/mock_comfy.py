"""Stand-in for ComfyUI's HTTP API, for testing the queue service without a GPU.

Runs prompts one at a time with a fake delay. Tests steer it through attributes on
MockComfy, or POST /_mock {"delay":..., "fail_next":..., "reject_next":...} from outside.
"""
import json
import re
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockComfy:
    def __init__(self, port=0, delay=0.1):
        self.delay, self.fail_next, self.reject_next = delay, 0, 0
        self.pending, self.running, self.history, self.executed = [], None, {}, []
        self.interrupted = set()
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.server = ThreadingHTTPServer(("127.0.0.1", port), self.handler())
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.alive = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        threading.Thread(target=self.execute_loop, daemon=True).start()

    def stop(self):
        self.alive = False
        self.wake.set()
        self.server.shutdown()
        self.server.server_close()

    def forget_everything(self):
        """What a ComfyUI restart looks like from outside: queue and history are empty."""
        with self.lock:
            self.pending.clear()
            self.history.clear()
            if self.running:
                self.interrupted.add(self.running[0])
                self.running = None

    def execute_loop(self):
        while self.alive:
            with self.lock:
                self.running = self.pending.pop(0) if self.pending else None
                current = self.running
            if not current:
                self.wake.wait(0.02)
                self.wake.clear()
                continue
            pid, graph = current
            start = time.time()
            self.executed.append(pid)
            while time.time() - start < self.delay and pid not in self.interrupted:
                time.sleep(0.005)
            with self.lock:
                if self.running is None or self.running[0] != pid:
                    continue  # forgotten by a simulated restart
                self.running = None
                self.history[pid] = self.finish(pid, graph, start)

    def finish(self, pid, graph, start):
        def message(kind, **data):
            return [kind, {"prompt_id": pid, **data}]
        begun = message("execution_start", timestamp=start * 1000)
        if pid in self.interrupted:
            return {"prompt": [0, pid, graph], "outputs": {}, "status": {"status_str": "error", "completed": False,
                    "messages": [begun, message("execution_interrupted")]}}
        if self.fail_next > 0:
            self.fail_next -= 1
            return {"prompt": [0, pid, graph], "outputs": {}, "status": {"status_str": "error", "completed": False,
                    "messages": [begun, message("execution_error", node_type="SamplerCustomAdvanced",
                                                exception_message="CUDA out of memory")]}}
        name = f"{pid[:8]}_00001_.mp4"
        return {"prompt": [0, pid, graph],
                "outputs": {"save": {"images": [{"filename": name, "subfolder": "h3", "type": "output"}], "animated": [True]}},
                "status": {"status_str": "success", "completed": True,
                           "messages": [begun, message("execution_success", timestamp=time.time() * 1000)]}}

    def handler(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def reply(self, obj, code=200, raw=None):
                body = raw if raw is not None else json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                m = re.fullmatch(r"/history/([\w-]+)", url.path)
                if url.path == "/system_stats":
                    self.reply({"system": {"comfyui_version": "mock"}, "devices": []})
                elif url.path == "/queue":
                    with mock.lock:
                        running = [[0, mock.running[0], mock.running[1]]] if mock.running else []
                        pending = [[i + 1, p, g] for i, (p, g) in enumerate(mock.pending)]
                    self.reply({"queue_running": running, "queue_pending": pending})
                elif m:
                    with mock.lock:
                        entry = mock.history.get(m[1])
                    self.reply({m[1]: entry} if entry else {})
                elif url.path == "/view":
                    self.reply(None, raw=b"fake video " + url.query.encode())
                else:
                    self.reply({"error": "not found"}, 404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/upload/image":
                    name = re.search(rb'filename="([^"]+)"', body)[1].decode()
                    return self.reply({"name": name})
                data = json.loads(body or b"{}")
                if self.path == "/prompt":
                    with mock.lock:
                        if mock.reject_next > 0:
                            mock.reject_next -= 1
                            return self.reply({"error": "invalid prompt", "node_errors": {}}, 400)
                        pid = data.get("prompt_id") or str(uuid.uuid4())
                        if not re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", pid):
                            return self.reply({"error": {"type": "invalid_prompt_id"}, "node_errors": {}}, 400)
                        mock.pending.append((pid, data["prompt"]))
                    mock.wake.set()
                    self.reply({"prompt_id": pid, "number": 0})
                elif self.path == "/interrupt":
                    mock.interrupted.add(data.get("prompt_id"))
                    self.reply({})
                elif self.path == "/queue":
                    with mock.lock:
                        mock.pending[:] = [(p, g) for p, g in mock.pending if p not in data.get("delete", [])]
                    self.reply({})
                elif self.path == "/_mock":
                    for k, v in data.items():
                        setattr(mock, k, v)
                    self.reply({})
                else:
                    self.reply({"error": "not found"}, 404)
        return Handler


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8188
    MockComfy(port, delay=float(sys.argv[2]) if len(sys.argv) > 2 else 2.0)
    print(f"mock ComfyUI on {port}", flush=True)
    threading.Event().wait()
