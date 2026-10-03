#!/usr/bin/env python3
"""Curses editor for a build job stored in the current working directory."""

import argparse
import curses
from dataclasses import asdict, dataclass, field, fields
import os
from pathlib import Path
import sys
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
        if key in ("\x1b", "\x11"):
            if asdict(self.job) == self.saved or self.confirm_quit:
                return False
            self.confirm_quit = True
            self.message = "Unsaved changes: Esc again to discard; any other key to continue."
            return True
        self.confirm_quit = False
        if key in (curses.KEY_DOWN, "\t"):
            self.active = (self.active + 1) % 7
        elif key in (curses.KEY_UP, curses.KEY_BTAB):
            self.active = (self.active - 1) % 7
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
        pad = curses.newpad(17, pad_width)
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
                pos = self.cursor[name]
                start = max(0, pos - available + 1) if active else 0
                text = value[start:start + available].ljust(available)
                pad.addstr(y, x, text, curses.A_REVERSE if active else curses.A_NORMAL)
                if active:
                    pad.chgat(y, x + pos - start, 1, curses.A_REVERSE | curses.A_BOLD | curses.A_UNDERLINE)
        pad.addstr(14, 0, "> " if self.active == 6 else "  ")
        pad.addstr(14, 16, "[ Save bluefin-build.yml ]", curses.A_REVERSE if self.active == 6 else curses.A_BOLD)
        view_height = min(17, height - 3)
        active_y = 2 + self.active * 2
        self.offset = min(self.offset, active_y)
        self.offset = max(self.offset, active_y - view_height + 1)
        self.offset = min(self.offset, 17 - view_height)
        left = max(0, selected_column - width + 2)
        screen.noutrefresh()
        pad.noutrefresh(self.offset, left, 0, 0, view_height - 1, width - 1)
        for y, text in ((height - 3, "Up/Down: field  Left/Right: move  Space/1-4: toggle"),
                        (height - 2, "Enter/Space: save button  Ctrl-U: clear text  Esc: quit"),
                        (height - 1, self.message)):
            screen.addnstr(y, 0, text, width - 1)
        screen.noutrefresh()
        curses.doupdate()

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
