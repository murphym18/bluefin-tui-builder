import contextlib
from dataclasses import asdict
import io
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import parse_qs

import yaml
sys.path.insert(0, str(Path(__file__).parents[1]))
import test_build
import server
import submit
import notifications
import build
from editor import BuildJob, Editor


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_build.BuildTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.env["BLUEFIN_REPO"] = str(self.fixture.repo)
        env = patch.dict(server.os.environ, self.fixture.env, clear=True)
        env.start()
        self.addCleanup(env.stop)
        notification = patch.object(server, "notify", return_value="sent")
        self.notify = notification.start()
        self.addCleanup(notification.stop)
        self.store = server.JobStore(self.fixture.root / "jobs", self.fixture.repo,
                                     self.fixture.root / "builds")
        self.addCleanup(self.store.close)
        self.http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.http.store = self.store
        self.http_thread = threading.Thread(target=self.http.serve_forever)
        self.http_thread.start()
        self.addCleanup(self.stop_http)
        self.url = f"http://127.0.0.1:{self.http.server_port}"

    def stop_http(self):
        self.http.shutdown()
        self.http.server_close()
        self.http_thread.join()

    def post(self, data):
        request = Request(self.url + "/jobs", data=yaml.safe_dump(data).encode(),
                          headers={"Content-Type": "application/yaml"})
        with urlopen(request) as response:
            self.assertEqual(response.status, 202)
            result = json.load(response)
            self.assertEqual(response.headers["Location"], result["status_url"])
            return result

    def wait(self, job_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.store.get(job_id)
            if result["state"] in ("failed", "succeeded", "interrupted") and result["notification"] == "sent":
                return result
            time.sleep(.02)
        self.fail("worker did not complete")

    def test_accept_build_status_log_and_notification(self):
        self.store.start()
        job = BuildJob(base_name=["bluefin"], image_flavor=["main"], stream_name=["latest"])
        accepted = submit.submit_job(job, self.url)
        self.assertEqual(accepted["state"], "queued")
        self.assertEqual(job.git_rev, "HEAD")
        self.assertEqual(len(accepted["git_rev"]), 40)
        status = self.wait(accepted["id"])
        self.assertEqual(status["state"], "succeeded")
        with urlopen(accepted["log_url"]) as response:
            log = response.read().decode()
        self.assertIn("Published cr.q3w.co/bluefin/latest/bluefin:latest", log)
        self.assertIn("checkout retained", log)
        self.assertEqual(sum(call[:2] == ["podman", "push"] for call in self.fixture.calls()), 2)
        self.notify.assert_called_once()
        with urlopen(accepted["status_url"]) as response:
            self.assertEqual(json.load(response)["state"], "succeeded")

    def test_failure_keeps_logs_and_notifies(self):
        server.os.environ["FAIL_STREAM"] = "latest"
        self.store.start()
        accepted = self.post(asdict(BuildJob(base_name=["bluefin"], image_flavor=["main"], stream_name=["latest"])))
        result = self.wait(accepted["id"])
        self.assertEqual(result["state"], "failed")
        self.assertIn("Error:", (self.store.path(accepted["id"]) / "build.log").read_text())
        self.assertIn("failed", self.notify.call_args.args[0])

    def test_invalid_requests_and_unknown_job(self):
        for data in ({"base_name": []}, {"unknown": "oops"}, {"git_rev": "missing-rev"}, []):
            with self.assertRaises(HTTPError) as caught:
                self.post(data)
            self.assertEqual(caught.exception.code, 400)
            caught.exception.close()
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.url + "/jobs/../../anything")
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()
        self.assertEqual(self.store.list(), [])

    def test_queue_recovery_and_single_worker(self):
        first = self.post(asdict(BuildJob(base_name=["bluefin"], image_flavor=["main"], stream_name=["stable"])))
        second = self.post(asdict(BuildJob(base_name=["bluefin"], image_flavor=["main"], stream_name=["latest"])))
        self.store.close()
        recovered = server.JobStore(self.fixture.root / "jobs", self.fixture.repo, self.fixture.root / "builds")
        self.addCleanup(recovered.close)
        self.http.store = self.store = recovered
        recovered.start()
        a = self.wait(first["id"])
        b = self.wait(second["id"])
        self.assertEqual(a["state"], "succeeded")
        self.assertEqual(b["state"], "succeeded")
        self.assertLessEqual(a["finished_at"], b["started_at"])

    def test_restart_marks_unfinished_job_interrupted(self):
        accepted = self.post(asdict(BuildJob()))
        self.store.update(accepted["id"], state="running")
        self.store.close()
        recovered = server.JobStore(self.fixture.root / "jobs", self.fixture.repo, self.fixture.root / "builds")
        self.addCleanup(recovered.close)
        result = recovered.get(accepted["id"])
        self.assertEqual(result["state"], "interrupted")
        self.assertEqual(result["notification"], "sent")

    def test_graceful_shutdown_stops_process_group(self):
        job = self.store.submit(BuildJob(git_rev="a" * 40))
        def blocking_command(command, log):
            server.JobStore.command(self.store, [sys.executable, "-c", "import time; time.sleep(60)"], log)
        with patch.object(self.store, "command", side_effect=blocking_command):
            self.store.start()
            deadline = time.monotonic() + 3
            while self.store.process is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertIsNotNone(self.store.process)
            self.store.close()
        self.assertEqual(self.store.get(job["id"])["state"], "interrupted")

    def test_tui_submission_uses_current_job(self):
        view = Editor(BuildJob(tag="test"), self.fixture.root / "job.yml", server_url=self.url)
        view.active = 8
        view.handle("\n")
        self.assertTrue(view.submit_requested)
        with patch.object(view, "draw"):
            view.submit(Mock())
        self.assertIn("Queued", view.message)
        self.assertEqual(self.store.list()[0]["tag"], "test")


class NotificationTests(unittest.TestCase):
    def test_local_cli_notifies_success_and_failure_but_not_dry_run(self):
        for failure in (None, ValueError("build failed")):
            with patch.object(sys, "argv", ["build.py"]), patch.object(Path, "is_file", return_value=True), \
                 patch.object(build, "load_job", return_value=BuildJob()), \
                 patch.object(build, "build_and_push", side_effect=failure), \
                 patch.object(build, "notify", return_value="sent") as send, \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(build.main(), 1 if failure else 0)
                self.assertIn("failed" if failure else "succeeded", send.call_args.args[0])
        with patch.object(sys, "argv", ["build.py", "--dry-run"]), \
             patch.object(Path, "is_file", return_value=True), patch.object(build, "load_job", return_value=BuildJob()), \
             patch.object(build, "build_and_push"), patch.object(build, "notify") as send:
            self.assertEqual(build.main(), 0)
            send.assert_not_called()

    def test_pushover_payload_and_disabled_mode(self):
        with patch.dict(notifications.os.environ, {}, clear=True), patch.object(notifications, "urlopen") as send:
            self.assertEqual(notifications.notify("done"), "disabled")
            send.assert_not_called()
        with patch.dict(notifications.os.environ, {"PUSHOVER_APP_TOKEN": "token", "PUSHOVER_USER_KEY": "user"}), \
             patch.object(notifications, "urlopen", return_value=io.StringIO('{"status":1}')) as send:
            self.assertEqual(notifications.notify("finished", url="http://builder/jobs/123"), "sent")
            data = parse_qs(send.call_args.args[0].data.decode())
            self.assertEqual(data["token"], ["token"])
            self.assertEqual(data["message"], ["finished"])
        with patch.dict(notifications.os.environ, {"PUSHOVER_APP_TOKEN": "secret", "PUSHOVER_USER_KEY": "key"}), \
             patch.object(notifications, "urlopen", side_effect=ValueError("secret")):
            result = notifications.notify("done")
            self.assertIn("failed", result)
            self.assertNotIn("secret", result)
