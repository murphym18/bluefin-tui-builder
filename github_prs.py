"""Read open Bluefin pull requests associated with a local committed revision."""

import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def open_pull_requests(revision):
    repo = Path(os.environ.get("BLUEFIN_REPO") or
                Path(__file__).resolve().parent.parent / "bluefin").resolve()
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify",
                             "--end-of-options", f"{revision}^{{commit}}"],
                            check=True, capture_output=True, text=True, timeout=10)
    sha = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
        raise ValueError("Git returned an invalid commit hash")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "bluefin-build-editor"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    matches = {}
    deadline = time.monotonic() + 20
    page = 1
    checking_heads = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("GitHub lookup timed out; try again")
        endpoint = "pulls?state=open&" if checking_heads else f"commits/{sha}/pulls?"
        request = Request(f"https://api.github.com/repos/ublue-os/bluefin/{endpoint}per_page=100&page={page}",
                          headers=headers)
        try:
            with urlopen(request, timeout=min(10, remaining)) as response:
                data = json.load(response)
        except HTTPError as error:
            if error.code == 404:
                raise ValueError("commit not found on GitHub in ublue-os/bluefin") from None
            if error.code in (403, 429):
                raise ValueError("GitHub denied the lookup or rate limit reached; try GH_TOKEN") from None
            raise ValueError(f"GitHub lookup failed (HTTP {error.code})") from None
        except (URLError, TimeoutError):
            raise ValueError("could not reach GitHub; check your connection and try again") from None
        if not isinstance(data, list) or any(not isinstance(pr, dict) for pr in data):
            raise ValueError("unexpected response from GitHub")
        for pr in data:
            if (pr.get("state") == "open" and
                    pr.get("base", {}).get("repo", {}).get("full_name") == "ublue-os/bluefin" and
                    (not checking_heads or pr.get("head", {}).get("sha", "").lower() == sha.lower())):
                number = pr.get("number")
                title = pr.get("title")
                if not isinstance(number, int) or not isinstance(title, str):
                    raise ValueError("unexpected pull request data from GitHub")
                matches[number] = {"number": number, "title": "".join(c if c.isprintable() else " " for c in title)}
        if len(data) < 100:
            if not matches and not checking_heads:
                # GitHub can omit a fork PR from commit associations even when
                # this exact commit is the open PR's head. Compare heads directly.
                checking_heads = True
                page = 1
                continue
            return sorted(matches.values(), key=lambda pr: pr["number"])
        page += 1
