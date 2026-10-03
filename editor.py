#!/usr/bin/env python3
"""Curses editor for a build job stored in the current working directory."""

import argparse
import curses
from dataclasses import asdict, dataclass, field, fields
import os
from pathlib import Path
import sys
import subprocess
import tempfile

import yaml


OPTIONS = {
    "base_name": ("bluefin", "bluefin-dx"),
    "image_flavor": ("main", "nvidia"),
    "stream_name": ("stable", "stable-daily", "latest", "beta"),
}
LABELS = ("Git rev", "Tag", "Base name", "Image flavor", "Stream name", "Registry")


@dataclass
class BuildJob:
    git_rev: str = "HEAD"
    tag: str = ""
    base_name: list[str] = field(default_factory=lambda: list(OPTIONS["base_name"]))
    image_flavor: list[str] = field(default_factory=lambda: list(OPTIONS["image_flavor"]))
    stream_name: list[str] = field(default_factory=lambda: list(OPTIONS["stream_name"]))
    registry: str = "cr.q3w.co/bluefin"

    def validate(self):
        for name in ("git_rev", "tag", "registry"):
            value = getattr(self, name)
            if not isinstance(value, str) or any(not c.isprintable() for c in value):
                raise ValueError(f"{name} must be a single-line string")
        for name in ("git_rev", "registry"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        for name, options in OPTIONS.items():
            value = getattr(self, name)
            if not isinstance(value, list) or any(item not in options for item in value):
                raise ValueError(f"{name} must be a list containing only {', '.join(options)}")
            if len(value) != len(set(value)):
                raise ValueError(f"{name} contains duplicate values")
            # Lists encode sets while keeping a portable, predictable YAML order.
            setattr(self, name, [item for item in options if item in value])


def load_job(path):
    if not path.exists():
        return BuildJob()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("configuration must be a YAML mapping")
    unknown = set(data) - {f.name for f in fields(BuildJob)}
    if unknown:
        raise ValueError(f"unknown properties: {', '.join(map(str, unknown))}")
    job = BuildJob(**data)
    job.validate()
    return job


def save_job(job, path):
    job.validate()
    # Replace only after a complete write; a failed save leaves the old file intact.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as output:
            temporary = Path(output.name)
            yaml.safe_dump(asdict(job), output, sort_keys=False)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class Editor:
    def __init__(self, job, path):
        self.job, self.path = job, path
        self.names = [f.name for f in fields(job)]
        self.active = 0
        self.choice = {name: 0 for name in OPTIONS}
        self.cursor = {name: len(getattr(job, name)) for name in self.names if name not in OPTIONS}
        self.offset = 0
        self.message = ""
        self.saved = asdict(job)
        self.confirm_quit = False
        self.build_requested = False
        self.pr_lookup_requested = False
        self.pr_choices = []
        self.pr_index = 0

    def toggle(self, name, index):
        self.choice[name] = index
        value = OPTIONS[name][index]
        selected = getattr(self.job, name)
        if value in selected:
            selected.remove(value)
        else:
            selected.append(value)
        setattr(self.job, name, [v for v in OPTIONS[name] if v in selected])

    def handle(self, key):
        if self.pr_choices:
            if key in ("\x1b", "\x11"):
                self.pr_choices = []
                self.message = "PR selection cancelled; tag unchanged."
            elif key in (curses.KEY_DOWN, "\t"):
                self.pr_index = (self.pr_index + 1) % len(self.pr_choices)
            elif key in (curses.KEY_UP, curses.KEY_BTAB):
                self.pr_index = (self.pr_index - 1) % len(self.pr_choices)
            elif key in ("\n", "\r", curses.KEY_ENTER):
                self.use_pr(self.pr_choices[self.pr_index])
                self.pr_choices = []
            return True
        if key in ("\x1b", "\x11"):
            if asdict(self.job) == self.saved or self.confirm_quit:
                return False
            self.confirm_quit = True
            self.message = "Unsaved changes: Esc again to discard; any other key to continue."
            return True
        self.confirm_quit = False
        if self.active == 1 and key == curses.KEY_F1:
            self.fill_tag_from_revision()
        elif self.active == 1 and key == curses.KEY_F2:
            self.pr_lookup_requested = True
        elif key in (curses.KEY_DOWN, "\t"):
            self.active = (self.active + 1) % 8
        elif key in (curses.KEY_UP, curses.KEY_BTAB):
            self.active = (self.active - 1) % 8
        elif self.active == 7:
            if key in ("\n", "\r", curses.KEY_ENTER, " "):
                self.build_requested = True
        elif self.active == 6:
            if key in ("\n", "\r", curses.KEY_ENTER, " "):
                try:
                    save_job(self.job, self.path)
                    self.saved = asdict(self.job)
                    self.message = f"Saved {self.path.name}"
                except (OSError, ValueError) as error:
                    self.message = f"Save failed: {error}"
        else:
            name = self.names[self.active]
            if name in OPTIONS:
                index = self.choice[name]
                if key in (curses.KEY_LEFT, curses.KEY_RIGHT):
                    self.choice[name] = (index + (1 if key == curses.KEY_RIGHT else -1)) % len(OPTIONS[name])
                elif key == " ":
                    self.toggle(name, index)
                elif isinstance(key, str) and key in "1234" and int(key) <= len(OPTIONS[name]):
                    self.toggle(name, int(key) - 1)
            else:
                value, pos = getattr(self.job, name), self.cursor[name]
                if key == curses.KEY_LEFT:
                    pos = max(0, pos - 1)
                elif key == curses.KEY_RIGHT:
                    pos = min(len(value), pos + 1)
                elif key in (curses.KEY_HOME, "\x01"):
                    pos = 0
                elif key in (curses.KEY_END, "\x05"):
                    pos = len(value)
                elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    if pos:
                        value, pos = value[:pos - 1] + value[pos:], pos - 1
                elif key == curses.KEY_DC:
                    value = value[:pos] + value[pos + 1:]
                elif key == "\x15":
                    value, pos = "", 0
                elif isinstance(key, str) and key.isprintable():
                    value, pos = value[:pos] + key + value[pos:], pos + len(key)
                setattr(self.job, name, value)
                self.cursor[name] = pos
        return True

    def use_pr(self, pr):
        self.job.tag = f"pr-{pr['number']}"
        self.cursor["tag"] = len(self.job.tag)
        self.message = f"Selected Bluefin PR #{pr['number']}"

    def find_prs(self, screen):
        from github_prs import open_pull_requests

        self.pr_lookup_requested = False
        self.message = "Looking up open Bluefin PRs on GitHub..."
        self.draw(screen)
        try:
            matches = open_pull_requests(self.job.git_rev)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
            self.message = f"Tag unchanged: {detail}"
            return
        if not matches:
            self.message = "No associated open Bluefin PR found; tag unchanged."
        elif len(matches) == 1:
            self.use_pr(matches[0])
        else:
            self.pr_choices = matches
            self.pr_index = 0
            self.message = "Choose an open PR to use for the tag."

    def fill_tag_from_revision(self):
        repo = Path(os.environ.get("BLUEFIN_REPO") or
                    Path(__file__).resolve().parent.parent / "bluefin").resolve()
        try:
            result = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "--short", "--verify",
                 "--end-of-options", f"{self.job.git_rev}^{{commit}}"],
                check=True, capture_output=True, text=True,
            )
            tag = result.stdout.strip()
            if not tag:
                raise ValueError("Git returned an empty revision")
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
            self.message = f"Tag unchanged: {detail}"
            return
        self.job.tag = tag
        self.cursor["tag"] = len(tag)
        self.message = f"Tag filled from {self.job.git_rev}: {tag}"

    def build(self, screen):
        from build import plan_job

        self.build_requested = False
        try:
            plan_job(self.job)
            save_job(self.job, self.path)
            self.saved = asdict(self.job)
        except (OSError, ValueError) as error:
            self.message = f"Build failed: {error}"
            return
        curses.def_prog_mode()
        curses.endwin()
        try:
            print("Building and pushing the saved job...", flush=True)
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("build.py")),
                                     "--config", str(self.path.resolve())])
            self.message = ("Build and push completed." if result.returncode == 0 else
                            f"Build and push failed (exit {result.returncode}); see terminal output.")
        except OSError as error:
            self.message = f"Build failed: {error}"
        except KeyboardInterrupt:
            self.message = "Build interrupted; checkout retained."
        finally:
            print(self.message, flush=True)
            try:
                input("Press Enter to return to the editor...")
            except (EOFError, KeyboardInterrupt):
                pass
            curses.reset_prog_mode()
            screen.keypad(True)
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            screen.clearok(True)

    def draw(self, screen):
        height, width = screen.getmaxyx()
        screen.erase()
        if height < 7 or width < 24:
            try:
                screen.addnstr(0, 0, "Resize terminal (24x7 min)", max(0, width - 1))
            except curses.error:
                pass
            screen.refresh()
            return
        pad_width = max(width, 160)
        pad = curses.newpad(19, pad_width)
        dirty = " *" if asdict(self.job) != self.saved else ""
        pad.addstr(0, 0, "Bluefin build job" + dirty, curses.A_BOLD)
        selected_column = 0
        for row, name in enumerate(self.names):
            y = 2 + row * 2
            active = self.active == row
            pad.addstr(y, 0, f"{'>' if active else ' '} {LABELS[row]:12}")
            x = 16
            if name in OPTIONS:
                for index, option in enumerate(OPTIONS[name]):
                    text = f"{index + 1} [{'x' if option in getattr(self.job, name) else ' '}] {option}"
                    highlighted = active and self.choice[name] == index
                    if highlighted:
                        selected_column = x + len(text)
                    pad.addstr(y, x, text, curses.A_REVERSE if highlighted else curses.A_NORMAL)
                    x += len(text) + 3
            else:
                value = getattr(self.job, name)
                available = max(1, width - x - 2)
                hint = "[F1: short rev] [F2: PR]" if width >= 52 else "F1/F2"
                if name == "tag":
                    available = max(1, available - len(hint) - 1)
                pos = self.cursor[name]
                start = max(0, pos - available + 1) if active else 0
                text = value[start:start + available].ljust(available)
                pad.addstr(y, x, text, curses.A_REVERSE if active else curses.A_NORMAL)
                if active:
                    pad.chgat(y, x + pos - start, 1, curses.A_REVERSE | curses.A_BOLD | curses.A_UNDERLINE)
                if name == "tag":
                    pad.addstr(y, x + available + 1, hint, curses.A_BOLD if active else curses.A_NORMAL)
        pad.addstr(14, 0, "> " if self.active == 6 else "  ")
        pad.addstr(14, 16, "[ Save bluefin-build.yml ]", curses.A_REVERSE if self.active == 6 else curses.A_BOLD)
        pad.addstr(16, 0, "> " if self.active == 7 else "  ")
        pad.addstr(16, 16, "[ Build and push ]", curses.A_REVERSE if self.active == 7 else curses.A_BOLD)
        view_height = min(19, height - 3)
        active_y = 2 + self.active * 2
        self.offset = min(self.offset, active_y)
        self.offset = max(self.offset, active_y - view_height + 1)
        self.offset = min(self.offset, 19 - view_height)
        left = max(0, selected_column - width + 2)
        screen.noutrefresh()
        pad.noutrefresh(self.offset, left, 0, 0, view_height - 1, width - 1)
        for y, text in ((height - 3, "Up/Down: field  Left/Right: move  Space/1-4: toggle"),
                        (height - 2, "Enter/Space: activate button  Ctrl-U: clear  Esc: quit"),
                        (height - 1, self.message)):
            screen.addnstr(y, 0, text, width - 1)
        screen.noutrefresh()
        if self.pr_choices:
            self.draw_pr_chooser(screen)
        curses.doupdate()

    def draw_pr_chooser(self, screen):
        height, width = screen.getmaxyx()
        modal_height = min(height, len(self.pr_choices) + 5)
        modal_width = min(width, 80)
        modal = curses.newwin(modal_height, modal_width,
                              (height - modal_height) // 2, (width - modal_width) // 2)
        modal.erase()
        modal.box()
        modal.addnstr(1, 2, "Choose a pull request", modal_width - 4, curses.A_BOLD)
        visible = modal_height - 4
        first = max(0, min(self.pr_index - visible + 1, len(self.pr_choices) - visible))
        for row, pr in enumerate(self.pr_choices[first:first + visible]):
            selected = first + row == self.pr_index
            # Keep remote titles within the modal even when they contain wide glyphs.
            text = f"{'>' if selected else ' '} #{pr['number']} {pr['title']}"
            text = text.encode("ascii", "replace").decode("ascii")
            modal.addnstr(row + 2, 2, text, modal_width - 4,
                          curses.A_REVERSE if selected else curses.A_NORMAL)
        help_text = "Up/Down  Enter: use  Esc: cancel" if modal_width >= 36 else "Up/Down Enter Esc"
        modal.addnstr(modal_height - 2, 2, help_text, modal_width - 4)
        modal.noutrefresh()

    def run(self, screen):
        screen.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        while True:
            self.draw(screen)
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            if not self.handle(key):
                break
            if self.build_requested:
                self.build(screen)
            if self.pr_lookup_requested:
                self.find_prs(screen)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    path = Path.cwd() / "bluefin-build.yml"
    try:
        job = load_job(path)
        curses.wrapper(Editor(job, path).run)
    except (OSError, ValueError, yaml.YAMLError, curses.error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
