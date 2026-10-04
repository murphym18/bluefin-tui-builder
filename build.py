#!/usr/bin/env python3
"""Build and publish the image combinations selected in bluefin-build.yml."""

import argparse
from dataclasses import dataclass
import fcntl
from itertools import product
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

import yaml
from notifications import notify

from editor import BuildJob, load_job


TAG = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}\Z")
REGISTRY = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]+)?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+\Z")


@dataclass(frozen=True)
class ImageBuild:
    base: str
    flavor: str
    stream: str
    image: str
    destination: str

    @property
    def source(self):
        return f"localhost/{self.image}:{self.stream}"


def plan_job(job: BuildJob) -> list[ImageBuild]:
    job.validate()
    if any(not getattr(job, name) for name in ("base_name", "image_flavor", "stream_name")):
        raise ValueError("select at least one base name, image flavor, and stream to build")
    registry = job.registry.rstrip("/")
    if not REGISTRY.fullmatch(registry):
        raise ValueError("registry must be a registry namespace, e.g. cr.q3w.co/bluefin (no URL scheme)")
    if job.tag and not TAG.fullmatch(job.tag):
        raise ValueError("tag must contain only letters, numbers, underscores, dots, and hyphens (max 128)")
    plan = []
    for base, selected_flavor, stream in product(job.base_name, job.image_flavor, job.stream_name):
        flavor = "nvidia-open" if selected_flavor == "nvidia" else selected_flavor
        image = base if flavor == "main" else f"{base}-{flavor}"
        tag = job.tag or stream
        if not TAG.fullmatch(tag):
            raise ValueError(f"resulting tag is invalid or too long: {tag}")
        plan.append(ImageBuild(base, flavor, stream, image, f"{registry}/{stream}/{image}:{tag}"))
    return plan


def run(command, *, cwd=None, env=None, capture=False):
    print(f"+ {shlex.join(map(str, command))}", flush=True)
    result = subprocess.run(list(map(str, command)), cwd=cwd, env=env, check=True,
                            text=True, stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def preview(job, plan, repo, build_parent):
    print(f"Revision: {job.git_rev}; repository: {repo}")
    print(f"A committed checkout with pinned submodules will be retained under {build_parent}.")
    print(f"Build all {len(plan)} images before publishing:")
    for image in plan:
        print(shlex.join(["just", "build", image.base, image.stream, image.flavor]))
        print(f"  Capture ID of {image.source}")
    for image in plan:
        print(shlex.join(["podman", "tag", "<captured-id>", image.destination]))
        print(shlex.join(["podman", "push", "<captured-id>", f"docker://{image.destination}"]))
        print(shlex.join(["podman", "push", "--digestfile",
                          f"<checkout>/{image.image}-{image.stream}.digest",
                          "<captured-id>", f"docker://{image.destination}"]))


def build_and_push(job: BuildJob, *, repo=None, build_parent=None, dry_run=False):
    """Build from a committed revision, then publish each captured image ID twice.

    Return the retained checkout path, or None for a dry run.
    """
    plan = plan_job(job)
    root = Path(__file__).resolve().parent.parent
    repo = Path(repo or os.environ.get("BLUEFIN_REPO", root / "bluefin")).resolve()
    build_parent = Path(build_parent or os.environ.get("BLUEFIN_BUILD_DIR", root / "tmp")).resolve()
    if dry_run:
        preview(job, plan, repo, build_parent)
        return None
    executables = {}
    for name in ("git", "just", "podman"):
        executable = shutil.which(name)
        if executable is None:
            raise ValueError(f"missing required tool: {name}")
        executables[name] = executable
    env = dict(os.environ, PODMAN=executables["podman"])
    commit = run([executables["git"], "-C", repo, "rev-parse", "--verify",
                  "--end-of-options", f"{job.git_rev}^{{commit}}"], env=env, capture=True)
    # Share the shell helper's lock: local tags and build scratch files are shared.
    lock_path = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / f"bluefin-build-push-{os.getuid()}.lock"
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another Bluefin build-and-push job is already running") from None
        build_parent.mkdir(parents=True, exist_ok=True)
        checkout = Path(tempfile.mkdtemp(prefix=f"bluefin-{job.tag or 'streams'}.", dir=build_parent))
        print(f"Building commit {commit}; checkout retained at {checkout}", flush=True)
        run([executables["git"], "clone", "--quiet", "--no-hardlinks", "--no-checkout",
             "--", repo, checkout], env=env)
        run([executables["git"], "checkout", "--quiet", "--detach", commit], cwd=checkout, env=env)
        run([executables["git"], "submodule", "update", "--init", "--recursive"], cwd=checkout, env=env)
        built = []
        for image in plan:
            run([executables["just"], "build", image.base, image.stream, image.flavor], cwd=checkout, env=env)
            image_id = run([executables["podman"], "image", "inspect", image.source,
                            "--format", "{{.Id}}"], cwd=checkout, env=env, capture=True)
            if not image_id:
                raise ValueError(f"no image ID returned for {image.source}")
            built.append((image, image_id))
        for image, image_id in built:
            digest = checkout / f"{image.image}-{image.stream}.digest"
            run([executables["podman"], "tag", image_id, image.destination], cwd=checkout, env=env)
            run([executables["podman"], "push", image_id, f"docker://{image.destination}"], cwd=checkout, env=env)
            # Preserve the second push: it includes layer annotations after upload.
            run([executables["podman"], "push", "--digestfile", digest,
                 image_id, f"docker://{image.destination}"], cwd=checkout, env=env)
            print(f"Published {image.destination} ({digest.read_text().strip()})", flush=True)
        return checkout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("bluefin-build.yml"))
    parser.add_argument("--repo", type=Path, help="local Bluefin repository (or BLUEFIN_REPO)")
    parser.add_argument("--build-dir", type=Path, help="retained checkout parent (or BLUEFIN_BUILD_DIR)")
    parser.add_argument("--dry-run", action="store_true", help="show builds and pushes without executing them")
    parser.add_argument("--no-notify", action="store_true", help="disable completion notification")
    args = parser.parse_args()
    outcome = "failed"
    exit_code = 0
    description = str(args.config)
    try:
        if not args.config.is_file():
            raise ValueError(f"configuration does not exist: {args.config}; save a job in the editor first")
        job = load_job(args.config)
        description = f"{job.git_rev} (tag: {job.tag or 'stream names'})"
        build_and_push(job, repo=args.repo, build_parent=args.build_dir, dry_run=args.dry_run)
        outcome = "succeeded"
    except (OSError, ValueError, yaml.YAMLError, subprocess.CalledProcessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        exit_code = 1
    except KeyboardInterrupt:
        print("Build interrupted; any checkout is retained.", file=sys.stderr)
        outcome = "interrupted"
        exit_code = 130
    if not args.dry_run and not args.no_notify:
        delivery = notify(f"Build and push {outcome}: {description}")
        print(f"Pushover: {delivery}", flush=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
