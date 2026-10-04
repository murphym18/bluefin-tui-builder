#!/usr/bin/env python3
"""Personal HTTP build service with a persistent queue, status, and output logs."""
import argparse
from datetime import datetime, timezone
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
from urllib.parse import urlsplit
import uuid

import yaml
from build import plan_job
from editor import BuildJob, save_job
from notifications import notify


def now():
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, directory, repo, build_dir, *, fetch=False, public_url=None):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.repo, self.build_dir = Path(repo).resolve(), Path(build_dir).resolve()
        self.fetch, self.public_url = fetch, public_url
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.worker = None
        self.process = None
        self.service_lock = (self.directory / ".server.lock").open("a")
        try:
            fcntl.flock(self.service_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.service_lock.close()
            raise ValueError("another server is using this job directory") from None
        for job in self.list():
            if job["state"] == "running":
                self.update(job["id"], state="interrupted", finished_at=now(),
                            error="server stopped before recording build completion; inspect the log")
                delivery = notify(f"Job {job['id']} interrupted: server restarted before recording completion.")
                self.update(job["id"], notification=delivery)

    def path(self, job_id):
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise FileNotFoundError("unknown job")
        return self.directory / job_id

    def get(self, job_id):
        with self.lock:
            return json.loads((self.path(job_id) / "status.json").read_text())

    def list(self):
        with self.lock:
            jobs = [self.get(path.parent.name) for path in self.directory.glob("*/status.json")]
            return sorted(jobs, key=lambda job: (job["created_at"], job["id"]))

    def update(self, job_id, **changes):
        with self.lock:
            path = self.path(job_id) / "status.json"
            data = json.loads(path.read_text()) if path.exists() else {}
            data.update(changes)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, indent=2) + "\n")
            temporary.replace(path)
            return data

    def submit(self, job):
        plan_job(job)
        # Raw API callers must also pin revisions, so queued HEAD cannot drift.
        resolved = job.git_rev if re.fullmatch(r"[0-9a-fA-F]{40,64}", job.git_rev) else subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "--verify", "--end-of-options", f"{job.git_rev}^{{commit}}"],
            check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        job.git_rev = resolved
        job_id = uuid.uuid4().hex
        with self.lock:
            path = self.path(job_id)
            path.mkdir()
            save_job(job, path / "bluefin-build.yml")
            (path / "build.log").touch()
            return self.update(job_id, id=job_id, state="queued", created_at=now(),
                               git_rev=job.git_rev, tag=job.tag, notification="pending",
                               status_url=f"/jobs/{job_id}", log_url=f"/jobs/{job_id}/log")

    def start(self):
        self.worker = threading.Thread(target=self.work, name="build-worker")
        self.worker.start()

    def command(self, command, log):
        with self.lock:
            if self.stop.is_set():
                raise InterruptedError("server stopping")
            self.process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                            start_new_session=True)
            process = self.process
        try:
            code = process.wait()
            if code:
                raise subprocess.CalledProcessError(code, command)
        finally:
            with self.lock:
                self.process = None

    def execute(self, job):
        job_id = job["id"]
        path = self.path(job_id)
        self.update(job_id, state="running", started_at=now())
        state, error = "succeeded", None
        with (path / "build.log").open("ab", buffering=0) as log:
            try:
                if self.fetch:
                    self.command(["git", "-C", str(self.repo), "fetch", "--all", "--prune"], log)
                self.command([sys.executable, "-u", str(Path(__file__).with_name("build.py").resolve()),
                              "--config", str(path / "bluefin-build.yml"), "--repo", str(self.repo),
                              "--build-dir", str(self.build_dir), "--no-notify"], log)
            except Exception as failure:
                state = "interrupted" if self.stop.is_set() else "failed"
                error = str(failure)
                log.write(f"\nJob {state}: {error}\n".encode())
        self.update(job_id, state=state, finished_at=now(), error=error)
        url = self.public_url.rstrip("/") + job["status_url"] if self.public_url else None
        delivery = notify(f"Job {job_id} {state}.\nRevision: {job['git_rev']}\nTag: {job['tag'] or 'stream names'}\n"
                          f"Log: {job['log_url']}", url=url)
        self.update(job_id, notification=delivery)

    def work(self):
        while not self.stop.is_set():
            queued = [job for job in self.list() if job["state"] == "queued"]
            if queued:
                self.execute(queued[0])
            else:
                self.stop.wait(0.25)

    def close(self):
        self.stop.set()
        with self.lock:
            process = self.process
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        if process is not None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if self.worker is not None:
            self.worker.join()
        self.service_lock.close()


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if status == 202:
            self.send_header("Location", data["status_url"])
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if urlsplit(self.path).path != "/jobs":
            return self.respond(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 65536:
                return self.respond(413, {"error": "YAML body must be between 1 and 65536 bytes"})
            self.connection.settimeout(15)
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("incomplete request body")
            data = yaml.safe_load(body.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("configuration must be a YAML mapping")
            job = BuildJob(**data)
            accepted = self.server.store.submit(job)
        except (ValueError, TypeError, yaml.YAMLError, subprocess.SubprocessError) as error:
            detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
            return self.respond(400, {"error": detail})
        except OSError:
            return self.respond(500, {"error": "could not persist or validate job; check server output"})
        self.respond(202, accepted)

    def do_GET(self):
        parts = urlsplit(self.path).path.strip("/").split("/")
        if parts == ["jobs"]:
            return self.respond(200, self.server.store.list())
        if len(parts) not in (2, 3) or parts[0] != "jobs" or (len(parts) == 3 and parts[2] != "log"):
            return self.respond(404, {"error": "not found"})
        try:
            job = self.server.store.get(parts[1])
            if len(parts) == 2:
                return self.respond(200, job)
            with (self.server.store.path(parts[1]) / "build.log").open("rb") as log:
                remaining = os.fstat(log.fileno()).st_size
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(remaining))
                self.end_headers()
                while remaining:
                    chunk = log.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except FileNotFoundError:
            self.respond(404, {"error": "unknown job"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parent.parent
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--repo", type=Path, default=Path(os.environ.get("BLUEFIN_REPO", root / "bluefin")))
    parser.add_argument("--build-dir", type=Path, default=Path(os.environ.get("BLUEFIN_BUILD_DIR", root / "tmp")))
    parser.add_argument("--jobs-dir", type=Path, default=Path("build-jobs"))
    parser.add_argument("--fetch", action="store_true", help="fetch configured remotes before each build")
    parser.add_argument("--public-url", help="HTTP server URL for notification status links")
    args = parser.parse_args()
    store = JobStore(args.jobs_dir, args.repo, args.build_dir, fetch=args.fetch, public_url=args.public_url)
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
        server.store = store
        store.start()
        def terminate(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, terminate)
        print(f"Listening on http://{args.host}:{args.port}; jobs saved in {store.directory}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        store.close()


if __name__ == "__main__":
    main()
