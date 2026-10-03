import curses
from dataclasses import asdict
import importlib.util
from pathlib import Path
import sys
import tempfile
import subprocess
import os
import unittest
from unittest.mock import Mock, patch

import yaml

sys.path.insert(0, str(Path(__file__).parents[1]))

spec = importlib.util.spec_from_file_location("editor", Path(__file__).parents[1] / "editor.py")
editor = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = editor
spec.loader.exec_module(editor)


class EditorTests(unittest.TestCase):
    def test_f2_lookup_results_and_modal_selection(self):
        view = editor.Editor(editor.BuildJob(tag="old"), Path("unused.yml"))
        view.active = 1
        view.handle(curses.KEY_F2)
        self.assertTrue(view.pr_lookup_requested)
        choices = [{"number": 4961, "title": "First"}, {"number": 4982, "title": "Second"}]
        with patch.object(view, "draw"), patch("github_prs.open_pull_requests", return_value=choices) as lookup:
            view.find_prs(Mock())
            lookup.assert_called_once_with("HEAD")
        self.assertEqual(view.job.tag, "old")
        view.handle(curses.KEY_DOWN)
        view.handle("\n")
        self.assertEqual(view.job.tag, "pr-4982")
        self.assertFalse(view.pr_choices)
        view.pr_choices = choices
        self.assertTrue(view.handle("\x1b"))
        self.assertEqual(view.job.tag, "pr-4982")
        self.assertFalse(view.pr_choices)
        for result in ([], choices[:1], ValueError("offline")):
            with patch.object(view, "draw"), patch("github_prs.open_pull_requests") as lookup:
                lookup.side_effect = result if isinstance(result, Exception) else None
                lookup.return_value = result
                view.find_prs(Mock())
        self.assertEqual(view.job.tag, "pr-4961")
        self.assertIn("Tag unchanged: offline", view.message)

    def test_f1_fills_tag_from_selected_revision_in_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            def commit():
                subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false",
                                "-c", "user.name=Test", "-c", "user.email=test@example.com",
                                "commit", "--allow-empty", "-qm", "fixture"], check=True)
            commit()
            expected = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], text=True).strip()
            commit()
            view = editor.Editor(editor.BuildJob(git_rev="HEAD~1", tag="old-tag"), repo / "job.yml")
            with patch.dict(os.environ, {"BLUEFIN_REPO": str(repo)}):
                view.handle(curses.KEY_F1)
                self.assertEqual(view.job.tag, "old-tag")
                view.handle(curses.KEY_DOWN)
                view.handle(curses.KEY_F1)
                self.assertEqual(view.job.tag, expected)
                self.assertEqual(view.cursor["tag"], len(expected))
                view.job.git_rev = "missing-revision"
                view.handle(curses.KEY_F1)
                self.assertEqual(view.job.tag, expected)
                self.assertIn("Tag unchanged", view.message)

    def test_build_button_navigation(self):
        view = editor.Editor(editor.BuildJob(), Path("unused.yml"))
        view.handle(curses.KEY_UP)
        self.assertEqual(view.active, 7)
        view.handle(" ")
        self.assertTrue(view.build_requested)
        view.handle(curses.KEY_DOWN)
        self.assertEqual(view.active, 0)

    def test_build_saves_current_job_and_restores_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bluefin-build.yml"
            view = editor.Editor(editor.BuildJob(tag="pr-4948"), path)
            screen = Mock()
            with patch.object(editor.curses, "def_prog_mode"), patch.object(editor.curses, "endwin"), \
                 patch.object(editor.curses, "reset_prog_mode") as restore, \
                 patch.object(editor.curses, "curs_set"), patch("builtins.input"), \
                 patch("builtins.print"), patch.object(editor.subprocess, "run") as run:
                for outcome in (Mock(returncode=0), Mock(returncode=1), KeyboardInterrupt()):
                    run.side_effect = outcome if isinstance(outcome, BaseException) else None
                    run.return_value = outcome
                    view.build(screen)
                    self.assertEqual(editor.load_job(path).tag, "pr-4948")
                    self.assertEqual(run.call_args.args[0][-2:], ["--config", str(path.resolve())])
                self.assertEqual(restore.call_count, 3)
                self.assertIn("interrupted", view.message)
                screen.clearok.assert_called_with(True)

    def test_invalid_job_does_not_start_build(self):
        view = editor.Editor(editor.BuildJob(base_name=[]), Path("unused.yml"))
        with patch.object(editor.subprocess, "run") as run:
            view.build(Mock())
            run.assert_not_called()
        self.assertIn("Build failed", view.message)

    def test_round_trip_and_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bluefin-build.yml"
            job = editor.load_job(path)
            self.assertEqual(job.git_rev, "HEAD")
            self.assertEqual(job.stream_name, ["stable", "stable-daily", "latest", "beta"])
            job.tag = "pr-4948"
            job.image_flavor = []
            editor.save_job(job, path)
            self.assertEqual(asdict(editor.load_job(path)), asdict(job))
            self.assertEqual(yaml.safe_load(path.read_text())["image_flavor"], [])
            path.write_text("tag: pr-1\n")
            self.assertEqual(editor.load_job(path).registry, "cr.q3w.co/bluefin")

    def test_invalid_file_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bluefin-build.yml"
            for content in ("[]", "unknown: 1", "git_rev: 123", "registry: ''",
                            "stream_name: [bad]", "base_name: [bluefin, bluefin]",
                            "tag: [oops]", "image_flavor: null", "tag: ["):
                with self.subTest(content=content):
                    path.write_text(content)
                    with self.assertRaises((ValueError, yaml.YAMLError)):
                        editor.load_job(path)
                    self.assertEqual(path.read_text(), content)

    def test_navigation_and_number_toggle(self):
        view = editor.Editor(editor.BuildJob(), Path("unused.yml"))
        for _ in range(4):
            view.handle(curses.KEY_DOWN)
        view.handle("4")
        self.assertEqual(view.choice["stream_name"], 3)
        self.assertNotIn("beta", view.job.stream_name)
        view.handle(" ")
        self.assertIn("beta", view.job.stream_name)
        view.handle(curses.KEY_RIGHT)
        self.assertEqual(view.choice["stream_name"], 0)
        view.handle("1")
        self.assertNotIn("stable", view.job.stream_name)

    def test_text_save_and_discard_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bluefin-build.yml"
            view = editor.Editor(editor.BuildJob(), path)
            view.handle(curses.KEY_DOWN)
            for char in "pr-4948":
                view.handle(char)
            view.handle(curses.KEY_LEFT)
            view.handle(curses.KEY_BACKSPACE)
            self.assertEqual(view.job.tag, "pr-498")
            self.assertTrue(view.handle("\x1b"))
            view.handle(curses.KEY_DOWN)
            view.active = 6
            view.handle("\n")
            self.assertEqual(editor.load_job(path).tag, "pr-498")
            self.assertFalse(view.handle("\x1b"))


if __name__ == "__main__":
    unittest.main()
