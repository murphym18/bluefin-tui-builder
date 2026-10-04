"""Submit a saved job to a personal HTTP build server."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import yaml
from build import plan_job
from editor import load_job


def submit_job(job, server):
    plan_job(job)
    server = server.rstrip("/")
    parsed = urlsplit(server)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("server must be an HTTP URL, e.g. http://builder:8080")
    # HEAD and branch names must mean the revision chosen on the user's machine.
    repo = Path(os.environ.get("BLUEFIN_REPO") or
                Path(__file__).resolve().parent.parent / "bluefin").resolve()
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--end-of-options",
                             f"{job.git_rev}^{{commit}}"], capture_output=True, text=True, check=True, timeout=10)
    data = asdict(job)
    data["git_rev"] = result.stdout.strip()
    request = Request(server + "/jobs", data=yaml.safe_dump(data, sort_keys=False).encode(),
                      headers={"Content-Type": "application/yaml"}, method="POST")
    try:
        with urlopen(request, timeout=15) as response:
            if response.status != 202:
                raise ValueError(f"unexpected server status: {response.status}")
            accepted = json.load(response)
        accepted["status_url"] = server + accepted["status_url"]
        accepted["log_url"] = server + accepted["log_url"]
        return accepted
    except HTTPError as error:
        raise ValueError(f"server rejected job (HTTP {error.code}): {error.read(4096).decode(errors='replace')}") from None
    except (URLError, TimeoutError):
        raise ValueError("could not reach server; submission outcome may be unknown, check /jobs before retrying") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default=os.environ.get("BLUEFIN_BUILD_SERVER"), required=not os.environ.get("BLUEFIN_BUILD_SERVER"))
    parser.add_argument("--config", type=Path, default=Path("bluefin-build.yml"))
    args = parser.parse_args()
    try:
        if not args.config.is_file():
            raise ValueError(f"configuration does not exist: {args.config}")
        result = submit_job(load_job(args.config), args.server)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError, yaml.YAMLError) as error:
        print(f"Submission failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
