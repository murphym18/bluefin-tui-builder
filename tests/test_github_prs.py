import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).parents[1]))
import github_prs


class LookupTests(unittest.TestCase):
    def test_empty_commit_associations_fall_back_to_exact_open_pr_heads(self):
        sha = "b5951a3b0386f678d57539447261a4776bda187c"
        def pr(number, head, state="open"):
            return {"number": number, "title": "Fix SELinux", "state": state,
                    "head": {"sha": head}, "base": {"repo": {"full_name": "ublue-os/bluefin"}}}
        first = [pr(1, "c" * 40)] * 100
        second = [pr(4979, sha), pr(4980, "d" * 40), pr(4981, sha, "closed")]
        for revision in (sha, "juanresendiz813/fix/dx-selinux-subs-dist"):
            with self.subTest(revision=revision), \
                 patch.object(github_prs.subprocess, "run", return_value=Mock(stdout=sha)), \
                 patch.object(github_prs, "urlopen", side_effect=[io.StringIO(json.dumps(data)) for data in ([], first, second)]) as request:
                self.assertEqual(github_prs.open_pull_requests(revision), [{"number": 4979, "title": "Fix SELinux"}])
                self.assertIn("pulls?state=open&", request.call_args.args[0].full_url)
                self.assertIn("page=2", request.call_args.args[0].full_url)

    def test_resolves_revision_filters_open_prs_and_paginates(self):
        def pr(number, state="open", repo="ublue-os/bluefin"):
            return {"number": number, "title": "Title\ntext", "state": state,
                    "base": {"repo": {"full_name": repo}}}
        first = [pr(1, "closed")] * 97 + [pr(4961), pr(2, repo="other/repo"), pr(4982)]
        second = [pr(4961)]
        with patch.object(github_prs.subprocess, "run", return_value=Mock(stdout="a" * 40)) as git, \
             patch.object(github_prs, "urlopen", side_effect=[io.StringIO(json.dumps(first)), io.StringIO(json.dumps(second))]) as request:
            result = github_prs.open_pull_requests("HEAD~1")
        self.assertEqual([pr["number"] for pr in result], [4961, 4982])
        self.assertEqual(result[0]["title"], "Title text")
        self.assertEqual(git.call_args.args[0][-1], "HEAD~1^{commit}")
        self.assertIn("page=2", request.call_args.args[0].full_url)

    def test_lookup_errors(self):
        for error in (HTTPError("url", 404, "missing", {}, None),
                      HTTPError("url", 403, "denied", {}, None), URLError("offline")):
            with self.subTest(error=error), \
                 patch.object(github_prs.subprocess, "run", return_value=Mock(stdout="b" * 40)), \
                 patch.object(github_prs, "urlopen", side_effect=error), self.assertRaises(ValueError):
                github_prs.open_pull_requests("HEAD")

    def test_invalid_revision_does_not_query_github(self):
        with patch.object(github_prs.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["git"])), \
             patch.object(github_prs, "urlopen") as request:
            with self.assertRaises(subprocess.CalledProcessError):
                github_prs.open_pull_requests("missing")
            request.assert_not_called()
