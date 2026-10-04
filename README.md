# Bluefin build job editor

A curses editor and build runner for `bluefin-build.yml`.

For remote HTTP submission, persistent job logs, and Pushover completion
notifications, see [the personal build server guide](SERVER.md).

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

Use Up/Down or Tab/Shift-Tab to select a field, the save button, or the build button.
Text fields support typing, Left/Right, Home/End, Backspace, Delete, and Ctrl-U
to clear. In selection fields, Left/Right changes the highlighted option;
Space toggles it. A number toggles and highlights that option immediately.
Enter or Space on the save button writes the file and leaves the editor open.
Esc or Ctrl-Q exits; repeat it to discard unsaved edits when prompted.
The pad scrolls to keep the active field or option visible on small terminals.

The tag field has an F1 shortcut beside its text box. While editing the tag,
press F1 to replace it with the short commit hash of the current Git rev.
It resolves the revision in the local Bluefin repository (or `BLUEFIN_REPO`),
using `git rev-parse --short --verify`. An invalid revision leaves the tag
unchanged and shows an error. You can edit or save the filled tag normally.

While editing the tag, **F2** looks up open PRs associated with the Git rev in
`ublue-os/bluefin` on GitHub. One match fills `pr-NUMBER` immediately. Multiple
matches open a scrolling chooser: Up/Down selects, Enter fills the tag, and Esc
cancels. No match or a lookup error leaves the tag unchanged. The lookup needs
internet access and a commit known to GitHub. It uses the public GitHub API;
optionally set `GH_TOKEN` or `GITHUB_TOKEN` for authenticated requests. The
editor shows a lookup message while waiting, with network timeouts. PRs that
are closed or merged are excluded.
If GitHub returns no open commit associations, the lookup also compares the
commit with open PR head hashes. This handles fork PRs omitted by the commit
association endpoint. Branch names must resolve locally, just like builds.

Enter or Space on **Build and push** validates and saves the current job, then
runs the build runner using the editor's Python environment. Build output appears
in the terminal. When it finishes or fails, press Enter to return to the editor.
Ctrl-C interrupts the build and returns you to the editor after the prompt.
Invalid jobs or failed saves leave you in the editor without starting a build.

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
not retained. The runner maps the value `nvidia` to Bluefin's `nvidia-open`
build flavor and image name suffix.

## Build and push

Save your job in the editor, then run from `ublue-os`:

```sh
just build-job --dry-run
just build-job
```

Or use the Python entry point with the existing virtual environment:

```sh
python bluefin-build-editor/build.py --dry-run
python bluefin-build-editor/build.py
```

The runner requires an existing configuration file; it does not silently build
the default matrix if the file is missing. `--config PATH` selects another file.
The dry run prints all planned builds, destination tags, and both pushes without
running any commands or creating a checkout.

The runner builds the Cartesian product of the selected bases, flavors, and
streams sequentially using `just build BASE STREAM FLAVOR`. Empty selections
are rejected for execution. All four streams, including `stable-daily`, are
passed directly to Bluefin's build recipe.

Published image names are `bluefin`, `bluefin-dx`, `bluefin-nvidia-open`, and
`bluefin-dx-nvidia-open`, as selected. Destinations use
`REGISTRY/STREAM/IMAGE:TAG`. Published tags follow these rules:

| Job tag | Selected streams | Published tag |
| --- | --- | --- |
| Empty | Any | Stream name, such as `stable` |
| `pr-4948` | One | `pr-4948` |
| `pr-4948` | Multiple | `pr-4948` in each stream's repository |

For example, Bluefin main on stable with an empty tag publishes to
`cr.q3w.co/bluefin/stable/bluefin:stable`. With a custom tag, it publishes to
`cr.q3w.co/bluefin/stable/bluefin:pr-4948`. Separate stream paths prevent one
stream from overwriting another within a job. Tag syntax and length are checked before
building. The registry comes from the job, with any trailing slash removed.

Like `scripts/bluefin-build-push`, the runner resolves the local Git revision,
clones a separate checkout, checks out the committed revision, and initializes
the pinned submodules recursively. Uncommitted source changes are excluded.
Checkouts and per-image, per-stream digest files are retained under `tmp`.
`--repo PATH` / `BLUEFIN_REPO` override the source repository;
`--build-dir PATH` / `BLUEFIN_BUILD_DIR` override the checkout parent. Defaults
are the `bluefin` and `tmp` directories beside this program's directory.

Git, Just, and Podman must be available. Log in to the registry as your current
user before executing, for example `podman login cr.q3w.co`. The runner uses
your current user's Podman storage for both builds and publication and shares
the shell helper's exclusive lock. Avoid concurrent manual builds, which do
not acquire this lock.

All builds must succeed before any remote publication. The runner captures
each image ID immediately after building and tags/pushes those IDs. Each
destination receives a first `podman push`, followed by a second push with
`--digestfile`, preserving the shell helper's layer-annotation workaround.
Failures stop the job immediately; a push failure can leave earlier images
published. Publication is not a transaction, and rerunning builds the job again.

Python callers can pass the dataclass directly:

```python
from build import build_and_push
from editor import BuildJob

job = BuildJob(tag="pr-4948", base_name=["bluefin"],
               image_flavor=["main"], stream_name=["stable"])
build_and_push(job, dry_run=True)
```

Use this from the program directory, or add that directory to your import path.

Run the tests from `ublue-os`:

```sh
.direnv/python-3.14/bin/python -m unittest discover -s bluefin-build-editor/tests
```
