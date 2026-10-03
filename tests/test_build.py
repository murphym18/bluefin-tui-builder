import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
import build
from editor import BuildJob


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.module = self.root / "module"
        self.tools = self.root / "bin"
        for path in (self.repo, self.module, self.tools):
            path.mkdir()
        self.log = self.root / "calls.jsonl"
        self.env = dict(os.environ, PATH=f"{self.tools}:{os.environ['PATH']}",
                        XDG_RUNTIME_DIR=str(self.root), GIT_ALLOW_PROTOCOL="file",
                        TEST_LOG=str(self.log))
        for repo in (self.repo, self.module):
            self.git(repo, "init", "-q")
        (self.module / "pinned.txt").write_text("pinned")
        self.commit(self.module)
        self.git(self.repo, "submodule", "add", str(self.module), "extension")
        self.commit(self.repo)
        (self.module / "pinned.txt").write_text("new tip")
        self.commit(self.module)
        (self.repo / "uncommitted.txt").write_text("must not enter checkout")
        for name in ("just", "podman"):
            path = self.tools / name
            path.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys
args = sys.argv[1:]
name = pathlib.Path(sys.argv[0]).name
assert pathlib.Path('extension/pinned.txt').read_text() == 'pinned'
assert not pathlib.Path('uncommitted.txt').exists()
with open(os.environ['TEST_LOG'], 'a') as log:
    log.write(json.dumps([name, *args]) + '\\n')
if name == 'just':
    assert os.environ['PODMAN'] == str(pathlib.Path(sys.argv[0]).with_name('podman'))
    if args[2] == os.environ.get('FAIL_STREAM'):
        sys.exit(7)
elif args[:2] == ['image', 'inspect']:
    print('id-' + args[2])
elif args[:2] == ['push', '--digestfile']:
    pathlib.Path(args[2]).write_text('sha256:test')
if name == 'podman' and args[0] == 'push' and os.environ.get('FAIL_PUSH'):
    sys.exit(9)
''')
            path.chmod(0o755)

    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], env=self.env,
                              check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, repo):
        self.git(repo, "add", ".")
        self.git(repo, "-c", "commit.gpgsign=false", "-c", "user.name=Test",
                 "-c", "user.email=test@example.com", "commit", "-qm", "fixture")

    def invoke(self, job=None, **kwargs):
        with patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stdout(io.StringIO()):
            return build.build_and_push(job or BuildJob(), repo=self.repo,
                                        build_parent=self.root / "builds", **kwargs)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_selected_matrix_and_two_pushes_use_captured_ids(self):
        job = BuildJob(tag="pr-4948", base_name=["bluefin-dx"],
                       image_flavor=["main", "nvidia"], stream_name=["stable", "stable-daily"])
        checkout = self.invoke(job)
        calls = self.calls()
        builds = [call for call in calls if call[0] == "just"]
        self.assertEqual(builds, [
            ["just", "build", "bluefin-dx", "stable", "main"],
            ["just", "build", "bluefin-dx", "stable-daily", "main"],
            ["just", "build", "bluefin-dx", "stable", "nvidia-open"],
            ["just", "build", "bluefin-dx", "stable-daily", "nvidia-open"]])
        first_tag = next(i for i, call in enumerate(calls) if call[:2] == ["podman", "tag"])
        self.assertEqual(sum(call[0] == "just" for call in calls[:first_tag]), 4)
        for image in build.plan_job(job):
            image_id = f"id-{image.source}"
            self.assertIn(["podman", "tag", image_id, image.destination], calls)
            self.assertIn(["podman", "push", image_id, f"docker://{image.destination}"], calls)
            self.assertIn(["podman", "push", "--digestfile",
                           str(checkout / f"{image.image}-{image.stream}.digest"),
                           image_id, f"docker://{image.destination}"], calls)
        self.assertEqual(sum(call[:2] == ["podman", "push"] for call in calls), 8)
        self.assertTrue(checkout.is_dir())
        self.assertTrue((self.repo / "uncommitted.txt").exists())

    def test_tag_rules_and_all_default_combinations(self):
        plan = build.plan_job(BuildJob())
        self.assertEqual(len(plan), 16)
        self.assertEqual(len({image.destination for image in plan}), 16)
        self.assertEqual(plan[0].destination, "cr.q3w.co/bluefin/stable/bluefin:stable")
        job = BuildJob(tag="pr-4948", stream_name=["latest"], registry="cr.q3w.co/bluefin/")
        self.assertTrue(all(image.destination.endswith(":pr-4948") for image in build.plan_job(job)))
        plan = build.plan_job(BuildJob(tag="pr-4948"))
        self.assertEqual({image.destination for image in plan}, {
            f"cr.q3w.co/bluefin/{stream}/{image}:pr-4948"
            for stream in ("stable", "stable-daily", "latest", "beta")
            for image in ("bluefin", "bluefin-nvidia-open", "bluefin-dx", "bluefin-dx-nvidia-open")
        })
        self.assertTrue(all(image.destination.endswith(":" + "x" * 128)
                            for image in build.plan_job(BuildJob(tag="x" * 128))))

    def test_build_failure_prevents_all_publishing(self):
        self.env["FAIL_STREAM"] = "latest"
        with self.assertRaises(subprocess.CalledProcessError):
            self.invoke(BuildJob(stream_name=["stable", "latest"]))
        self.assertFalse(any(call[:2] in (["podman", "tag"], ["podman", "push"]) for call in self.calls()))

    def test_push_failure_stops_publication(self):
        self.env["FAIL_PUSH"] = "1"
        with self.assertRaises(subprocess.CalledProcessError):
            self.invoke(BuildJob(stream_name=["latest"]))
        self.assertEqual(sum(call[:2] == ["podman", "push"] for call in self.calls()), 1)

    def test_requested_revision_is_used(self):
        selected = self.git(self.repo, "rev-parse", "HEAD")
        (self.repo / "later.txt").write_text("later commit")
        self.commit(self.repo)
        checkout = self.invoke(BuildJob(git_rev=selected, base_name=["bluefin"],
                                        image_flavor=["main"], stream_name=["latest"]))
        self.assertEqual(self.git(checkout, "rev-parse", "HEAD"), selected)
        self.assertFalse((checkout / "later.txt").exists())

    def test_invalid_jobs_and_missing_revision_do_not_build(self):
        for job in (BuildJob(base_name=[]), BuildJob(tag="bad/tag"),
                    BuildJob(registry="https://cr.q3w.co/bluefin"), BuildJob(tag="x" * 129)):
            with self.subTest(job=job), self.assertRaises(ValueError):
                self.invoke(job)
        with self.assertRaises(subprocess.CalledProcessError):
            self.invoke(BuildJob(git_rev="missing-revision"))
        self.assertFalse(self.log.exists())

    def test_submodule_failure_prevents_builds(self):
        self.git(self.repo, "config", "-f", ".gitmodules", "submodule.extension.url", str(self.root / "missing"))
        self.commit(self.repo)
        with self.assertRaises(subprocess.CalledProcessError):
            self.invoke()
        self.assertFalse(self.log.exists())

    def test_shared_lock_and_dry_run(self):
        lock_path = self.root / f"bluefin-build-push-{os.getuid()}.lock"
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "already running"):
                self.invoke()
        with patch.object(build, "run", side_effect=AssertionError("dry run executed a command")):
            self.assertIsNone(self.invoke(dry_run=True))
        self.assertFalse((self.root / "builds").exists())


if __name__ == "__main__":
    unittest.main()
