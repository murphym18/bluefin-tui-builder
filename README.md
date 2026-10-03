# Bluefin build job editor

A curses editor for `bluefin-build.yml`. This version edits configuration only.
It does not build images or push them to a registry.

From the parent `ublue-os` directory, using its existing direnv environment:

```sh
python -m pip install -r bluefin-build-editor/requirements.txt
python bluefin-build-editor/editor.py
```

You can also run `just build-editor`, which uses the existing
`.direnv/python-3.14` environment directly. The configuration is always loaded
from and saved to the **current working directory**, regardless of where the
program itself lives. To share this directory separately, install
`requirements.txt` in a Python 3.10+ environment and run `python editor.py`.
Curses is included with typical Linux and macOS Python installations.

Use Up/Down or Tab/Shift-Tab to select a field or the save button.
Text fields support typing, Left/Right, Home/End, Backspace, Delete, and Ctrl-U
to clear. In selection fields, Left/Right changes the highlighted option;
Space toggles it. A number toggles and highlights that option immediately.
Enter or Space on the save button writes the file and leaves the editor open.
Esc or Ctrl-Q exits; repeat it to discard unsaved edits when prompted.
The pad scrolls to keep the active field or option visible on small terminals.

All options start selected. Sets are serialized as ordered YAML lists:

```yaml
git_rev: HEAD
tag: ''
base_name:
- bluefin
- bluefin-dx
image_flavor:
- main
- nvidia
stream_name:
- stable
- stable-daily
- latest
- beta
registry: cr.q3w.co/bluefin
```

Missing properties in an existing mapping use defaults. Empty selection lists
are allowed, as is an empty tag. Git rev and registry must be nonempty.
Malformed YAML, unknown properties, invalid values, and duplicate selections
produce an error before opening the editor and leave the file intact.
Save replaces the file atomically. Comments and custom YAML formatting are
not retained. The value `nvidia` is the editor's configuration value; a future
builder will need to map it to the appropriate build flavor.

Run the tests from `ublue-os`:

```sh
.direnv/python-3.14/bin/python -m unittest discover -s bluefin-build-editor/tests
```
