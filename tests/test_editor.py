import curses
from dataclasses import asdict
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

import yaml

spec = importlib.util.spec_from_file_location("editor", Path(__file__).parents[1] / "editor.py")
editor = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = editor
spec.loader.exec_module(editor)


class EditorTests(unittest.TestCase):
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
