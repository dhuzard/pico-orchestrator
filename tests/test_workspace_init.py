#!/usr/bin/env python3
"""Tests for the approval-gated workspace manifest builder on real Git repositories.

`gh` is a fake on PATH answering only the read-only calls the builder makes
(`auth status`, `repo view`, `api -X GET`); `pi` and `claude` are fakes that
log any invocation, which the launcher tests assert never happens.
"""
from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import unittest
import unittest.mock
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import workspace_init as wi  # noqa: E402
import workspace_plan as wp  # noqa: E402
from test_launcher_preflight_chain import _CacheFixture  # noqa: E402

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
cfg = json.load(open(os.environ["FAKE_GH_CONFIG"]))
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "gh_host": os.environ.get("GH_HOST")}) + "\n")
args = sys.argv[1:]
def fail(message, code=1):
    sys.stderr.write(message + "\n")
    sys.exit(code)
if args[:2] == ["auth", "status"]:
    if cfg.get("auth", True):
        print("Logged in to github.com")
        sys.exit(0)
    fail("You are not logged into any GitHub hosts. To log in, run: gh auth login")
repos = cfg.get("repos", {})
if args[:2] == ["repo", "view"]:
    repo = repos.get(args[2].lower())
    if repo is None or repo.get("deny"):
        fail("GraphQL: Could not resolve to a Repository with the name '%s'. (repository)" % args[2])
    name = repo.get("view_name", repo["name"])
    print(json.dumps({"nameWithOwner": name, "owner": {"login": name.split("/")[0]},
                      "name": name.split("/")[-1], "isPrivate": True, "url": "https://github.com/" + name}))
    sys.exit(0)
if args[:3] == ["api", "-X", "GET"]:
    parts = args[3].split("/")
    repo = repos.get("/".join(parts[1:3]).lower())
    if repo is None:
        fail("gh: Not Found (HTTP 404)")
    if len(parts) == 3:
        print(json.dumps({"full_name": repo["name"], "default_branch": repo.get("default_branch", "main")}))
        sys.exit(0)
    head = repo.get("heads", {}).get("/".join(parts[6:]))
    if head is None:
        fail("gh: Not Found (HTTP 404)")
    print(json.dumps({"ref": "refs/heads/" + "/".join(parts[6:]), "object": {"type": "commit", "sha": head}}))
    sys.exit(0)
fail("unexpected fake gh call: " + " ".join(args), 99)
'''

REMOTE_HEAD = "c" * 40


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(path: Path, remotes: dict[str, str] | None = None, *, commit: bool = True) -> str | None:
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main", str(path))
    for name, url in (remotes or {}).items():
        git("-C", str(path), "remote", "add", name, url)
    if not commit:
        return None
    (path / "README.md").write_bytes(os.fsencode(f"# {path.name}\n"))
    git("-C", str(path), "add", "README.md")
    git("-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "init")
    return git("-C", str(path), "rev-parse", "HEAD")


def tree(path: Path) -> list[str]:
    """Everything below `path` except Git metadata and the fake gh call log."""
    return sorted(str(p.relative_to(path)) for p in path.rglob("*") if ".git" not in p.parts and p.name != "gh.log")


class ScriptedTTY(io.StringIO):
    """stdin for the interactive builder: answers in order; KeyboardInterrupt raises it; running out is EOF."""

    def __init__(self, answers: list):
        super().__init__()
        self.answers = list(answers)

    def isatty(self) -> bool:
        return True

    def readline(self, *args) -> str:
        if not self.answers:
            return ""
        answer = self.answers.pop(0)
        if answer is KeyboardInterrupt:
            raise KeyboardInterrupt
        return answer + "\n"


class TTYOut(io.StringIO):
    def isatty(self) -> bool:
        return True


class _Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.root = self.base / "root"
        self.alpha_sha = make_repo(self.root / "alpha", {"origin": "git@github.com:acme/alpha.git"})
        self.beta_sha = make_repo(self.root / "beta", {"origin": "https://github.com/acme/beta.git"})
        self.bin = self.base / "bin"
        self.bin.mkdir()
        (self.bin / "gh").write_text(FAKE_GH)
        (self.bin / "gh").chmod(0o755)
        self.gh_log = self.base / "gh.log"
        self.gh_config = self.base / "gh.json"
        self.repos = {
            "acme/alpha": {"name": "acme/alpha", "heads": {"main": self.alpha_sha}},
            "acme/beta": {"name": "acme/beta", "default_branch": "trunk", "heads": {"trunk": "b" * 40}},
            "acme/remote": {"name": "acme/remote", "heads": {"main": REMOTE_HEAD}},
        }
        self.gh_auth = True
        env = {"PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
               "FAKE_GH_CONFIG": str(self.gh_config), "FAKE_GH_LOG": str(self.gh_log)}
        patcher = unittest.mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("GH_HOST", *wi.GIT_ENV_CONFLICTS):
            os.environ.pop(name, None)

    def tearDown(self):
        self._tmp.cleanup()

    def write_gh(self) -> None:
        self.gh_config.write_text(json.dumps({"auth": self.gh_auth, "repos": self.repos}))

    def gh_calls(self) -> list[list[str]]:
        if not self.gh_log.exists():
            return []
        return [json.loads(line)["argv"] for line in self.gh_log.read_text().splitlines()]

    def spec(self, data: dict | None = None, *, name: str = "spec.yaml", text: str | None = None, **overrides) -> Path:
        if data is None:
            data = {"version": 1, "root": "root", "id": "ws-init", "goal": "Cross-repository change",
                    "repositories": [{"id": "alpha", "path": "alpha"},
                                     {"id": "beta", "path": "beta", "task": {"title": "Beta change",
                                                                            "objective": "Change beta only."}}]}
            data.update(overrides)
        path = self.base / name
        path.write_text(text if text is not None else yaml.safe_dump(data, sort_keys=False))
        self.write_gh()
        return path

    def run_spec(self, spec: Path, *extra: str, cwd: Path | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = wi.main(["--spec", str(spec), *extra], stdout=out, stderr=err, cwd=cwd or self.base)
        return code, out.getvalue(), err.getvalue()


class DiscoveryTest(_Fixture):
    def test_sorted_discovery_prunes_excluded_trees_and_keeps_nested_repositories(self):
        for pruned in ("node_modules/dep", "vendor/lib", ".venv/pkg", "build/out", "alpha/.pi/worktrees/w1",
                       ".claude/worktrees/x", ".cache/pi-coding-orchestrator/repo", "pkg.egg-info/r", "dist/d"):
            make_repo(self.root / pruned, {"origin": f"https://github.com/acme/{pruned.split('/')[-1]}"})
        nested = self.root / "alpha" / "tools" / "nested"
        make_repo(nested, {"origin": "https://github.com/acme/nested"})
        found = wi.discover(self.root)
        self.assertEqual([c.label for c in found.candidates], ["alpha", "alpha/tools/nested", "beta"])
        self.assertEqual([c.identities for c in found.candidates], [["acme/alpha"], ["acme/nested"], ["acme/beta"]])
        self.assertEqual(found.truncated, [])
        self.assertTrue(PRUNED_LIST_DOCUMENTED.issuperset(wi.PRUNED_DIRECTORY_NAMES))

    def test_linked_worktrees_subdirectories_and_symlinks_are_not_candidates(self):
        git("-C", str(self.root / "alpha"), "worktree", "add", "-q", str(self.root / "alpha-wt"), "-b", "side")
        outside = self.base / "outside"
        make_repo(outside, {"origin": "https://github.com/acme/outside"})
        (self.root / "link-to-outside").symlink_to(outside, target_is_directory=True)
        fake = self.root / "dotgit-link"
        fake.mkdir()
        (fake / ".git").symlink_to(outside / ".git")
        separate = self.root / "separate"
        separate.mkdir()
        git("init", "-q", "--separate-git-dir", str(self.base / "elsewhere.git"), str(separate))
        found = wi.discover(self.root)
        self.assertEqual([c.label for c in found.candidates], ["alpha", "beta"])
        reasons = dict(found.skipped)
        self.assertIn("linked worktree", reasons["alpha-wt"])
        self.assertIn("symlink", reasons["dotgit-link"])
        self.assertIn("outside the discovery root", reasons["separate"])
        self.assertNotIn("link-to-outside", reasons)
        # A subdirectory of a checkout is never reported as its repository (no upward discovery).
        (self.root / "alpha" / "src").mkdir()
        self.assertEqual(wi.discover(self.root / "alpha" / "src").candidates, [])

    def test_unresolved_unborn_ambiguous_and_duplicate_identity_are_reported(self):
        make_repo(self.root / "plain")
        make_repo(self.root / "unborn", {"origin": "https://github.com/acme/unborn"}, commit=False)
        make_repo(self.root / "multi", {"origin": "https://github.com/acme/fork",
                                        "upstream": "https://github.com/other/multi"})
        make_repo(self.root / "clone2", {"origin": "https://github.com/ACME/alpha"})
        found = wi.discover(self.root)
        by_label = {c.label: c for c in found.candidates}
        self.assertEqual(by_label["multi"].identities, ["acme/fork", "other/multi"])
        self.assertTrue(by_label["multi"].ambiguous)
        self.assertIn("clone2", by_label)
        reasons = dict(found.skipped)
        self.assertIn("no GitHub repository remote", reasons["plain"])
        self.assertIn("unborn", reasons["unborn"])

    def test_malformed_remote_is_not_selectable_and_the_rest_are_discovered(self):
        # The GitHub adapter's URL parser raises ValueError for a non-numeric port or a stray '['.
        make_repo(self.root / "bad", {"origin": "https://github.com:BAD/acme/r"})
        make_repo(self.root / "bad6", {"origin": "https://github.com\x1b[2J/acme/r"})
        found = wi.discover(self.root)
        self.assertEqual([c.label for c in found.candidates], ["alpha", "beta"])
        reasons = dict(found.skipped)
        self.assertIn("WORKSPACE_INIT_REMOTE_MALFORMED", reasons["bad"])
        self.assertIn("Port could not be cast", reasons["bad"])
        self.assertIn("WORKSPACE_INIT_REMOTE_MALFORMED", reasons["bad6"])
        self.write_gh()
        out, err = TTYOut(), io.StringIO()
        rc = wi.main([], stdin=ScriptedTTY(["", "q"]), stdout=out, stderr=err, cwd=self.root)
        self.assertEqual(rc, 1, out.getvalue() + err.getvalue())
        text = out.getvalue() + err.getvalue()
        self.assertNotIn("Traceback", text)
        self.assertEqual(raw_controls(text), [])
        self.assertIn("  not selectable: bad: WORKSPACE_INIT_REMOTE_MALFORMED: ", text)
        self.assertIn("  not selectable: bad6: WORKSPACE_INIT_REMOTE_MALFORMED: ", text)
        self.assertIn("acme/alpha", text)
        self.assertIn("acme/beta", text)

    def test_bounds_are_reported(self):
        make_repo(self.root / "a" / "b" / "c" / "deep", {"origin": "https://github.com/acme/deep"})
        found = wi.discover(self.root, max_depth=2)
        self.assertNotIn("a/b/c/deep", [c.label for c in found.candidates])
        self.assertTrue(any("deeper than 2 levels" in t for t in found.truncated))
        self.assertTrue(any("stopped after 2 directories" in t for t in wi.discover(self.root, max_directories=2).truncated))
        capped = wi.discover(self.root, max_candidates=1)
        self.assertEqual(len(capped.candidates), 1)
        self.assertTrue(any("first 1 repositories" in t for t in capped.truncated))

    @unittest.skipIf(os.geteuid() == 0, "root reads unreadable directories")
    def test_unreadable_directory_is_reported(self):
        locked = self.root / "locked"
        locked.mkdir()
        locked.chmod(0)
        try:
            found = wi.discover(self.root)
        finally:
            locked.chmod(0o755)
        self.assertIn("locked", [where for where, _ in found.unreadable])


PRUNED_LIST_DOCUMENTED = frozenset(
    (REPO_ROOT / "docs" / "workspace-interactive-design.md").read_text().split("<!-- pruned -->")[1]
    .replace("`", " ").replace(",", " ").split()
)


class SpecValidationTest(_Fixture):
    def assert_invalid(self, spec: Path, needle: str, code: int = 2):
        before = tree(self.base)
        rc, out, err = self.run_spec(spec, "--approve")
        self.assertEqual(rc, code, out + err)
        self.assertIn(needle, err)
        self.assertEqual(tree(self.base), before)

    def test_unknown_fields_and_duplicate_keys_are_rejected(self):
        self.assert_invalid(self.spec(extra=1), "unknown field 'extra'")
        good = {"version": 1, "root": "root", "id": "w", "goal": "g",
                "repositories": [{"id": "alpha", "path": "alpha", "pinned_commit": "x"}, {"id": "beta", "path": "beta"}]}
        self.assert_invalid(self.spec(good), "repositories/0: unknown field 'pinned_commit'")
        good["repositories"][0] = {"id": "alpha", "path": "alpha", "task": {"title": "t", "depends_on": []}}
        self.assert_invalid(self.spec(good), "unknown field 'depends_on'")
        text = "version: 1\nroot: root\nid: w\nid: v\ngoal: g\nrepositories: []\n"
        self.assert_invalid(self.spec(text=text), "duplicate key 'id'")
        text = ("version: 1\nroot: root\nid: w\ngoal: g\nrepositories:\n"
                "  - {id: alpha, path: alpha, path: beta}\n  - {id: beta, path: beta}\n")
        self.assert_invalid(self.spec(text=text), "duplicate key 'path'")
        json_text = json.dumps({"version": 1, "root": "root", "id": "w", "goal": "g", "repositories": []})
        self.assert_invalid(self.spec(text=json_text[:-1] + ', "goal": "h"}', name="spec.json"), "duplicate key 'goal'")
        self.assert_invalid(self.spec(text="{1: a}"), "is not a string")
        self.assert_invalid(self.spec(text="- a\n"), "spec must be a mapping")
        self.assert_invalid(self.spec(text="{bad"), "not valid YAML/JSON")

    def test_malformed_types_and_shapes_are_rejected(self):
        cases = [
            ({"version": "1"}, "version must be the integer 1"),
            ({"version": True}, "version must be the integer 1"),
            ({"max_parallel": True}, "max_parallel must be an integer"),
            ({"max_parallel": 65}, "max_parallel must be an integer"),
            ({"id": "bad id"}, "id must match"),
            ({"goal": ""}, "goal must be a string"),
            ({"root": "missing"}, "root is not an existing directory"),
            ({"repositories": {"alpha": "alpha"}}, "repositories must be a list"),
            ({"repositories": [{"id": "alpha", "path": "alpha"}]}, "2 to 64 entries"),
            ({"repositories": [{"id": f"r{i}", "path": f"r{i}"} for i in range(65)]}, "2 to 64 entries"),
            ({"repositories": [{"id": "alpha", "path": "alpha", "expected_path": "x"}, {"id": "b", "path": "beta"}]},
             "exactly one of path"),
            ({"repositories": [{"id": "alpha"}, {"id": "b", "path": "beta"}]}, "exactly one of path"),
            ({"repositories": [{"id": "r", "expected_path": "r"}, {"id": "b", "path": "beta"}]},
             "expected_path needs an explicit repository"),
            ({"repositories": [{"id": "alpha", "path": "alpha"}, {"id": "alpha", "path": "beta"}]},
             "duplicate repository id 'alpha'"),
            ({"repositories": [{"id": "a", "path": "alpha", "repository": "acme/alpha"},
                               {"id": "b", "path": "beta", "repository": "ACME/Alpha"}]}, "same repository"),
            ({"repositories": [{"id": "a", "path": "alpha"}, {"id": "b", "path": "./alpha/"}]}, "same path as a"),
            ({"repositories": [{"id": "a", "path": "alpha", "repository": "not-a-repo"}, {"id": "b", "path": "beta"}]},
             "must be owner/repository"),
            ({"repositories": [{"id": "a", "path": "alpha", "task": "t"}, {"id": "b", "path": "beta"}]},
             "task: must be a mapping"),
            ({"repositories": [{"id": "a", "path": "alpha", "task": {"title": 3}}, {"id": "b", "path": "beta"}]},
             "title: must be one line"),
            ({"repositories": [{"id": "a", "path": "alpha", "task": {"objective": "one\ntwo"}},
                               {"id": "b", "path": "beta"}]}, "objective must be one line"),
            ({"repositories": [{"id": "a", "path": "alpha", "task": {"objective": "use ghp_" + "x" * 40}},
                               {"id": "b", "path": "beta"}]}, "looks like it contains a secret"),
        ]
        for overrides, needle in cases:
            with self.subTest(needle=needle):
                self.assert_invalid(self.spec(**overrides), needle)
        self.assertEqual(self.gh_calls(), [])

    def test_usage_errors(self):
        for argv, needle in ((["--approve"], "only valid with --spec"),
                             (["--spec", "x", "--root", "y"], "--root is interactive only")):
            err = io.StringIO()
            self.assertEqual(wi.main(argv, stdout=io.StringIO(), stderr=err, cwd=self.base), 2)
            self.assertIn(needle, err.getvalue())
        err = io.StringIO()
        self.assertEqual(wi.main([], stdin=io.StringIO(), stdout=io.StringIO(), stderr=err, cwd=self.base), 2)
        self.assertIn("WORKSPACE_INIT_TTY_REQUIRED", err.getvalue())

    def test_conflicting_environment_is_refused_before_any_call(self):
        for name, value in (("GH_HOST", "ghe.example.com"), ("GIT_DIR", str(self.root / "alpha" / ".git"))):
            with self.subTest(name=name), unittest.mock.patch.dict(os.environ, {name: value}):
                rc, _, err = self.run_spec(self.spec())
                self.assertEqual(rc, 2)
                self.assertIn(name, err)
        self.assertEqual(self.gh_calls(), [])
        with unittest.mock.patch.dict(os.environ, {"GH_HOST": "GitHub.com"}):
            self.assertEqual(self.run_spec(self.spec())[0], 0)
        with unittest.mock.patch.dict(os.environ, {"GH_HOST": "ghe.example.com"}):
            with self.assertRaises(wi.GithubFailure) as ctx:
                wi.verify_github("acme/alpha")
        self.assertEqual(ctx.exception.code, "WORKSPACE_INIT_GH_HOST_CONFLICT")


class ResolutionTest(_Fixture):
    def test_spec_preview_writes_nothing_and_shows_pins_placeholders_and_plan(self):
        spec = self.spec()
        before = tree(self.base)
        rc, out, err = self.run_spec(spec)
        self.assertEqual(rc, 0, err)
        self.assertEqual(tree(self.base), before)
        destination = self.root / "ws-init.workspace.yaml"
        self.assertIn(f"Destination: {destination}", out)
        self.assertIn(f"pin {self.alpha_sha} = local HEAD", out)
        self.assertIn("task alpha has a placeholder title", out)
        self.assertIn("TODO: define the task for repository alpha", out)
        self.assertIn("Workspace ws-init: PLANNED", out)
        self.assertIn("Access check: skipped", out)
        self.assertIn("does not clone, fetch or pull", out)
        self.assertIn("Preview only: nothing was written", out)
        for call in self.gh_calls():
            self.assertTrue(call[:2] in (["auth", "status"], ["repo", "view"]) or call[:3] == ["api", "-X", "GET"], call)

    def test_approved_save_matches_preview_and_real_planner(self):
        spec = self.spec(max_parallel=2)
        out_file = self.base / "out" / "ws.yaml"
        out_file.parent.mkdir()
        spec_obj = wi.validate_spec(wi.load_spec(spec), base=spec.parent)
        prepared = wi.prepare(spec_obj, out_file)
        rc, out, err = self.run_spec(spec, "--output", "out/ws.yaml", "--approve")
        self.assertEqual(rc, 0, err)
        self.assertIn(f"Saved {out_file}", out)
        self.assertEqual(out_file.read_text(), prepared.text)
        self.assertIn(prepared.text.rstrip("\n"), out)
        manifest = yaml.safe_load(out_file.read_text())
        self.assertEqual(wp.validate_manifest(manifest), [])
        self.assertEqual(manifest["repositories"], [
            {"id": "alpha", "repository": "acme/alpha", "path": str(self.root / "alpha"),
             "pinned_commit": self.alpha_sha, "base_branch": "main"},
            {"id": "beta", "repository": "acme/beta", "path": str(self.root / "beta"),
             "pinned_commit": self.beta_sha, "base_branch": "trunk"}])
        self.assertEqual(manifest["tasks"], [
            {"id": "alpha", "repository": "alpha", "title": "TODO: define the task for repository alpha"},
            {"id": "beta", "repository": "beta", "title": "Beta change", "objective": "Change beta only."}])
        self.assertEqual(manifest["max_parallel"], 2)
        self.assertNotIn(manifest["goal"], [t.get("objective") for t in manifest["tasks"]])
        self.assertEqual(wp.plan(out_file, access_check=None), prepared.plan)
        self.assertEqual(wp.render(wp.plan(out_file, access_check=None)), wp.render(prepared.plan))

    def test_remote_only_entry_pins_default_head_and_plans_blocked(self):
        spec = self.spec(repositories=[{"id": "alpha", "path": "alpha"},
                                       {"id": "remote", "repository": "Acme/Remote", "expected_path": "remote"}])
        rc, out, err = self.run_spec(spec, "--approve")
        self.assertEqual(rc, 0, err)
        saved = self.root / "ws-init.workspace.yaml"
        manifest = yaml.safe_load(saved.read_text())
        remote = manifest["repositories"][1]
        self.assertEqual(remote, {"id": "remote", "repository": "acme/remote", "path": str(self.root / "remote"),
                                  "pinned_commit": REMOTE_HEAD, "base_branch": "main"})
        self.assertIn("remote-only, expected at", out)
        self.assertIn("WORKSPACE_REPOSITORY_PATH_MISSING", out)
        self.assertIn("Workspace ws-init: BLOCKED", out)
        result = wp.plan(saved, access_check=None)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual([b["code"] for b in result["blockers"]], ["WORKSPACE_REPOSITORY_PATH_MISSING"])
        self.assertEqual(result["repositories"][1]["access"]["status"], "skipped")
        self.assertFalse((self.root / "remote").exists())
        self.assertFalse(any(c[:2] in (["repo", "clone"], ["repo", "fork"]) for c in self.gh_calls()))

    def test_expected_path_must_be_absent_including_dangling_symlink(self):
        (self.root / "dangling").symlink_to(self.base / "nowhere")
        for expected in ("beta", "dangling"):
            with self.subTest(expected=expected):
                spec = self.spec(repositories=[{"id": "alpha", "path": "alpha"},
                                               {"id": "r", "repository": "acme/remote", "expected_path": expected}])
                rc, _, err = self.run_spec(spec, "--approve")
                self.assertEqual(rc, 1)
                self.assertIn("WORKSPACE_INIT_EXPECTED_PATH_EXISTS", err)
        self.assertFalse((self.root / "ws-init.workspace.yaml").exists())

    def test_ambiguous_remotes_need_an_explicit_identity_verified_against_the_remotes(self):
        make_repo(self.root / "multi", {"origin": "https://github.com/acme/fork",
                                        "upstream": "https://github.com/other/multi"})
        self.repos["other/multi"] = {"name": "other/multi", "heads": {"main": "d" * 40}}
        self.repos["acme/fork"] = {"name": "acme/fork", "heads": {"main": "e" * 40}}
        rc, _, err = self.run_spec(self.spec(repositories=[{"id": "alpha", "path": "alpha"},
                                                           {"id": "multi", "path": "multi"}]))
        self.assertEqual(rc, 1)
        self.assertIn("TARGET_REPOSITORY_AMBIGUOUS", err)
        self.assertIn("acme/fork, other/multi", err)
        rc, _, err = self.run_spec(self.spec(repositories=[
            {"id": "alpha", "path": "alpha"}, {"id": "multi", "path": "multi", "repository": "acme/absent"}]))
        self.assertEqual(rc, 1)
        self.assertIn("TARGET_REPOSITORY_CONFLICT", err)
        rc, out, err = self.run_spec(self.spec(repositories=[
            {"id": "alpha", "path": "alpha"}, {"id": "multi", "path": "multi", "repository": "other/multi"}]))
        self.assertEqual(rc, 0, err)
        self.assertIn("multi: other/multi local checkout", out)

    def test_local_checkout_failures(self):
        make_repo(self.root / "unborn", {"origin": "https://github.com/acme/unborn"}, commit=False)
        make_repo(self.root / "clone2", {"origin": "https://github.com/acme/alpha"})
        (self.root / "alpha" / "sub").mkdir()
        cases = [("unborn", "WORKSPACE_INIT_HEAD_UNBORN"), ("alpha/sub", "WORKSPACE_INIT_NOT_CHECKOUT_ROOT"),
                 ("missing", "WORKSPACE_REPOSITORY_PATH_MISSING"), ("clone2", "WORKSPACE_REPOSITORY_DUPLICATE")]
        self.repos["acme/unborn"] = {"name": "acme/unborn", "heads": {"main": "f" * 40}}
        for path, code in cases:
            with self.subTest(path=path):
                rc, _, err = self.run_spec(self.spec(repositories=[{"id": "alpha", "path": "alpha"},
                                                                   {"id": "other", "path": path}]))
                self.assertEqual(rc, 1, err)
                self.assertIn(code, err)
        linked = self.root / "alpha-link"
        linked.symlink_to(self.root / "alpha", target_is_directory=True)
        rc, _, err = self.run_spec(self.spec(repositories=[{"id": "alpha", "path": "alpha"},
                                                           {"id": "again", "path": "alpha-link"}]))
        self.assertEqual(rc, 1)
        self.assertIn("same Git repository as entry alpha", err)

    def test_malformed_remote_fails_its_entry_and_the_rest_still_resolve(self):
        make_repo(self.root / "bad", {"origin": "https://github.com:BAD/acme/r"})
        spec = self.spec(repositories=[{"id": "alpha", "path": "alpha"}, {"id": "bad", "path": "bad"},
                                       {"id": "gone", "path": "gone"}, {"id": "beta", "path": "beta"}])
        rc, out, err = self.run_spec(spec)
        self.assertEqual(rc, 1, err)
        self.assertNotIn("Traceback", out + err)
        self.assertEqual(raw_controls(out + err), [])
        lines = err.split("\n")
        self.assertEqual(lines[0], "WORKSPACE_INIT_RESOLUTION_FAILED: repositories could not be resolved:")
        self.assertTrue(lines[1].startswith("  WORKSPACE_INIT_REMOTE_MALFORMED (bad): "), lines)
        self.assertIn("Port could not be cast", lines[1])
        self.assertTrue(lines[2].startswith("  WORKSPACE_REPOSITORY_PATH_MISSING (gone): "), lines)
        self.assertEqual(len(lines), 4)
        viewed = [call for call in self.gh_calls() if call[:2] == ["repo", "view"]]
        self.assertEqual(sorted(call[2] for call in viewed), ["acme/alpha", "acme/beta"])

    def test_github_failures_fail_closed(self):
        def case(mutate, code):
            original = json.loads(json.dumps(self.repos))
            mutate()
            rc, _, err = self.run_spec(self.spec(), "--approve")
            self.repos = original
            self.gh_auth = True
            self.assertEqual(rc, 1, err)
            self.assertIn(code, err)
            self.assertFalse((self.root / "ws-init.workspace.yaml").exists())

        case(lambda: self.repos["acme/alpha"].update(view_name="acme/alpha-renamed"), "WORKSPACE_REPOSITORY_REDIRECTED")
        case(lambda: self.repos["acme/alpha"].update(name="other/alpha"), "WORKSPACE_REPOSITORY_REDIRECTED")
        case(lambda: self.repos["acme/alpha"].update(deny=True), "WORKSPACE_REPOSITORY_ACCESS_DENIED")
        case(lambda: self.repos.pop("acme/beta"), "WORKSPACE_REPOSITORY_ACCESS_DENIED")
        case(lambda: setattr(self, "gh_auth", False), "GITHUB_ACCESS_UNVERIFIED")
        case(lambda: self.repos["acme/alpha"].update(heads={"main": "a" * 39}), "WORKSPACE_INIT_GITHUB_MALFORMED")
        case(lambda: self.repos["acme/alpha"].update(heads={}), "WORKSPACE_INIT_DEFAULT_BRANCH_MISSING")
        with unittest.mock.patch.object(wi.gh, "gh_available", return_value=False):
            rc, _, err = self.run_spec(self.spec(), "--approve")
        self.assertEqual(rc, 1)
        self.assertIn("GITHUB_ACCESS_UNVERIFIED", err)

    def test_canonical_case_comes_from_github(self):
        self.repos["acme/alpha"]["name"] = "Acme/Alpha"
        self.repos["acme/alpha"]["view_name"] = "Acme/Alpha"
        rc, out, err = self.run_spec(self.spec(), "--approve")
        self.assertEqual(rc, 0, err)
        saved = yaml.safe_load((self.root / "ws-init.workspace.yaml").read_text())
        self.assertEqual(saved["repositories"][0]["repository"], "Acme/Alpha")
        self.assertEqual(wp.plan(self.root / "ws-init.workspace.yaml", access_check=None)["status"], "PLANNED")

    def test_json_spec_and_relative_paths(self):
        nested = self.base / "specs" / "deep"
        nested.mkdir(parents=True)
        data = {"version": 1, "root": "../../root", "id": "ws-json", "goal": "g",
                "repositories": [{"id": "alpha", "path": "alpha"}, {"id": "beta", "path": str(self.root / "beta")}]}
        spec = nested / "spec.json"
        spec.write_text(json.dumps(data))
        self.write_gh()
        caller = self.base / "caller"
        caller.mkdir()
        rc, _, err = self.run_spec(Path("../specs/deep/spec.json"), "--output", "ws.yaml", "--approve", cwd=caller)
        self.assertEqual(rc, 0, err)
        manifest = yaml.safe_load((caller / "ws.yaml").read_text())
        self.assertEqual([r["path"] for r in manifest["repositories"]], [str(self.root / "alpha"), str(self.root / "beta")])

    def test_secret_like_objective_is_rejected_by_the_manifest_guard(self):
        spec = wi.Spec(root=self.root, id="w", goal="g", max_parallel=1, entries=[
            wi.Entry(id="alpha", repository=None, path=self.root / "alpha", local=True,
                     objective="token ghp_" + "y" * 40),
            wi.Entry(id="beta", repository=None, path=self.root / "beta", local=True)])
        self.write_gh()
        with self.assertRaises(wi.InitError) as ctx:
            wi.prepare(spec, self.root / "w.yaml")
        self.assertEqual(ctx.exception.code, "WORKSPACE_MANIFEST_INVALID")


class SaveTest(_Fixture):
    def test_existing_destination_is_never_replaced(self):
        target = self.base / "taken.yaml"
        for make in (lambda: target.write_text("keep\n"), lambda: target.mkdir(),
                     lambda: target.symlink_to(self.base / "nowhere.yaml"),
                     lambda: target.symlink_to(self.base / "real.yaml")):
            (self.base / "real.yaml").write_text("real\n")
            make()
            with self.subTest(kind=target.is_symlink() and "symlink" or target.is_dir() and "dir" or "file"):
                rc, _, err = self.run_spec(self.spec(), "--output", str(target), "--approve")
                self.assertEqual(rc, 1)
                self.assertIn("WORKSPACE_INIT_OUTPUT_EXISTS", err)
                with self.assertRaises(wi.InitError):
                    wi.save_exclusive(target, "x\n")
            self.assertEqual((self.base / "real.yaml").read_text(), "real\n")
            self.assertFalse((self.base / "nowhere.yaml").exists())
            if target.is_dir() and not target.is_symlink():
                target.rmdir()
            else:
                self.assertNotEqual(target.read_text() if target.exists() else "", "x\n")
                target.unlink()

    def test_missing_parent_is_invalid(self):
        rc, _, err = self.run_spec(self.spec(), "--output", "no/such/dir/ws.yaml", "--approve")
        self.assertEqual(rc, 2)
        self.assertIn("WORKSPACE_INIT_OUTPUT_PARENT_MISSING", err)

    def test_concurrent_saves_create_exactly_one_file(self):
        target = self.base / "race.yaml"
        results: list[str] = []
        barrier = threading.Barrier(8)

        def save(n: int) -> None:
            barrier.wait()
            try:
                wi.save_exclusive(target, f"writer {n}\n" * 1000)
                results.append(f"ok {n}")
            except wi.InitError as exc:
                results.append(exc.code)

        threads = [threading.Thread(target=save, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [r for r in results if r.startswith("ok")]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(results.count("WORKSPACE_INIT_OUTPUT_EXISTS"), 7)
        self.assertEqual(target.read_text(), f"writer {winners[0].split()[1]}\n" * 1000)

    def test_failed_write_removes_only_the_owned_partial_file(self):
        target = self.base / "partial.yaml"
        real_write = os.write

        def short_then_fail(fd, data):
            if getattr(short_then_fail, "done", False):
                raise OSError(28, "No space left on device")
            short_then_fail.done = True
            return real_write(fd, bytes(data[:5]))

        with unittest.mock.patch.object(wi.os, "write", side_effect=short_then_fail):
            with self.assertRaises(wi.InitError) as ctx:
                wi.save_exclusive(target, "0123456789" * 10)
        self.assertEqual(ctx.exception.code, "WORKSPACE_INIT_OUTPUT_UNWRITABLE")
        self.assertFalse(os.path.lexists(target))

        def replaced_then_interrupted(fd, data):
            os.replace(self.base / "other.yaml", target)  # someone else's file now holds the name
            raise KeyboardInterrupt

        (self.base / "other.yaml").write_text("not ours\n")
        with unittest.mock.patch.object(wi.os, "write", side_effect=replaced_then_interrupted):
            with self.assertRaises(KeyboardInterrupt):
                wi.save_exclusive(target, "ours\n")
        self.assertEqual(target.read_text(), "not ours\n")


class InteractiveTest(_Fixture):
    def setUp(self):
        super().setUp()
        self.write_gh()

    def interact(self, answers: list, *argv: str) -> tuple[int, str, str]:
        out, err = TTYOut(), io.StringIO()
        code = wi.main(list(argv), stdin=ScriptedTTY(answers), stdout=out, stderr=err, cwd=self.root)
        return code, out.getvalue(), err.getvalue()

    HAPPY = ["", "1 2", "d", "ws-i", "Ship the cross-repo change", "", "", "Alpha title", "", "", "", "Beta only.", ""]

    def test_happy_path_saves_only_after_yes(self):
        rc, out, err = self.interact(self.HAPPY + ["yes"])
        self.assertEqual(rc, 0, out + err)
        saved = self.root / "ws-i.workspace.yaml"
        manifest = yaml.safe_load(saved.read_text())
        self.assertEqual([t["title"] for t in manifest["tasks"]],
                         ["Alpha title", "TODO: define the task for repository beta"])
        self.assertEqual(manifest["tasks"][1]["objective"], "Beta only.")
        self.assertNotIn("objective", manifest["tasks"][0])
        self.assertIn(f"Discovery root [{self.root}]", out)
        self.assertIn("Workspace ws-i: PLANNED", out)
        self.assertEqual(wp.render(wp.plan(saved, access_check=None)) in out, True)

    def test_every_cancellation_writes_nothing(self):
        before = tree(self.base)
        cases = {
            "eof at root": ([], 1), "eof at selection": ([""], 1), "quit": (["", "q"], 1),
            "blank id": (["", "1 2", "d", ""], 1), "blank approval": (self.HAPPY + [""], 1),
            "declined": (self.HAPPY + ["no"], 1), "eof at approval": (self.HAPPY, 1),
            "ctrl-c at selection": (["", KeyboardInterrupt], 130),
            "ctrl-c at approval": (self.HAPPY + [KeyboardInterrupt], 130),
        }
        for name, (answers, code) in cases.items():
            with self.subTest(name=name):
                rc, out, err = self.interact(answers)
                self.assertEqual(rc, code, out + err)
                self.assertIn("nothing was written", out + err)
        self.assertEqual(tree(self.base), before)

    def test_selection_search_bounds_and_remote_addition(self):
        rc, out, _ = self.interact(["", "/bet", "9", "1", "d", "r", "acme/remote", "", "d", "q"])
        self.assertEqual(rc, 1)
        self.assertIn("Not a selection", out)
        self.assertIn("Select 2 to 64 repositories (now 1)", out)
        filtered = out.split("Select> ")[1]
        self.assertIn("beta", filtered)
        self.assertNotIn("acme/alpha", filtered)
        self.assertIn(f"remote-only at {self.root / 'remote'} (not cloned)", out)

    def test_ambiguous_checkout_requires_a_choice_without_origin_preference(self):
        make_repo(self.root / "multi", {"origin": "https://github.com/acme/fork",
                                        "upstream": "https://github.com/other/multi"})
        self.repos["other/multi"] = {"name": "other/multi", "heads": {"main": "d" * 40}}
        self.write_gh()
        rc, out, _ = self.interact(["", "1 3", "d", ""])
        self.assertEqual(rc, 1)
        self.assertIn("there is no default", out)
        answers = ["", "1 3", "d", "2", "ws-m", "goal", "", "", "", "", "", "", "", "", "yes"]
        rc, out, err = self.interact(answers)
        self.assertEqual(rc, 0, out + err)
        manifest = yaml.safe_load((self.root / "ws-m.workspace.yaml").read_text())
        self.assertEqual(manifest["repositories"][1]["repository"], "other/multi")
        self.assertIn("explicitly selected among remotes acme/fork, other/multi", out)

    def test_numbers_isdigit_accepts_but_int_refuses_are_reasked(self):
        # '²' passes str.isdigit() but int() refuses it; a 5000-digit decimal exceeds int()'s digit limit.
        for text in ("²", "1-²", "²-3", "9" * 5000, "1-" + "9" * 5000, "0", "3-1", "4", "1-"):
            with self.subTest(text=text[:12]):
                self.assertIsNone(wi._parse_numbers(text, 3))
        self.assertEqual(wi._parse_numbers("0003, 1-2", 3), [2, 0, 1])
        hostile = ["²", "9" * 5000, "0", "65"]
        rc, out, err = self.interact(self.HAPPY[:5] + hostile + ["3"] + self.HAPPY[6:] + ["yes"])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.count("Enter a whole number from 1 to 64."), len(hostile))
        self.assertEqual(yaml.safe_load((self.root / "ws-i.workspace.yaml").read_text())["max_parallel"], 3)

        make_repo(self.root / "multi", {"origin": "https://github.com/acme/fork",
                                        "upstream": "https://github.com/other/multi"})
        self.repos["other/multi"] = {"name": "other/multi", "heads": {"main": "d" * 40}}
        self.write_gh()
        answers = ["", "1 3", "d", "²", "9" * 5000, "3", "2", "ws-m", "goal", "", "", "", "", "", "", "", "", "yes"]
        rc, out, err = self.interact(answers)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.count("Enter one of the listed numbers."), 3)
        manifest = yaml.safe_load((self.root / "ws-m.workspace.yaml").read_text())
        self.assertEqual(manifest["repositories"][1]["repository"], "other/multi")

    def test_long_zero_runs_never_reach_int_unbounded(self):
        # Leading zeros are stripped before int(), so 5000 zeros cannot hit Python's digit limit.
        zeros = "0" * 5000
        self.assertIsNone(wi._number(zeros, 3))
        self.assertIsNone(wi._number(zeros + "9" * 5000, 3))
        self.assertIsNone(wi._number(zeros + "4", 3))
        self.assertEqual(wi._number(zeros + "1", 3), 1)
        self.assertEqual(wi._number("0003", 3), 3)
        for text in (zeros, zeros + "-2", "1-" + zeros, zeros + "-" + zeros, "1-" + zeros + "4"):
            with self.subTest(text=text[:12]):
                self.assertIsNone(wi._parse_numbers(text, 3))
        self.assertEqual(wi._parse_numbers("1-" + zeros + "2", 3), [0, 1])
        self.assertEqual(wi._parse_numbers(zeros + "1-" + zeros + "2", 3), [0, 1])

        answers = ["", zeros, "1-" + zeros, "1-" + zeros + "2"] + self.HAPPY[2:5] + [zeros, zeros + "3"]
        rc, out, err = self.interact(answers + self.HAPPY[6:] + ["yes"])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.count("Not a selection"), 2)
        self.assertEqual(out.count("Enter a whole number from 1 to 64."), 1)
        manifest = yaml.safe_load((self.root / "ws-i.workspace.yaml").read_text())
        self.assertEqual(manifest["max_parallel"], 3)
        self.assertEqual(len(manifest["repositories"]), 2)

        make_repo(self.root / "multi", {"origin": "https://github.com/acme/fork",
                                        "upstream": "https://github.com/other/multi"})
        self.repos["other/multi"] = {"name": "other/multi", "heads": {"main": "d" * 40}}
        self.write_gh()
        answers = ["", "1 3", "d", zeros, zeros + "9" * 5000, zeros + "2", "ws-m", "goal",
                   "", "", "", "", "", "", "", "", "yes"]
        rc, out, err = self.interact(answers)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.count("Enter one of the listed numbers."), 2)
        manifest = yaml.safe_load((self.root / "ws-m.workspace.yaml").read_text())
        self.assertEqual(manifest["repositories"][1]["repository"], "other/multi")

    def test_duplicate_identity_selection_is_refused(self):
        make_repo(self.root / "clone2", {"origin": "https://github.com/acme/alpha"})
        rc, out, _ = self.interact(["", "1 3", "d", "q"])
        self.assertEqual(rc, 1)
        self.assertIn("same GitHub identity", out)

    def test_existing_output_is_reasked_not_overwritten(self):
        existing = self.root / "ws-i.workspace.yaml"
        existing.write_text("keep\n")
        rc, out, err = self.interact(self.HAPPY[:-1] + ["", "other.yaml", "yes"])
        self.assertEqual(rc, 0, out + err)
        self.assertIn("never overwritten", out)
        self.assertEqual(existing.read_text(), "keep\n")
        self.assertTrue((self.root / "other.yaml").is_file())


ESC_NAME = "1esc\x1b[31mred\x1b]0;title\x07"
NEWLINE_NAME = "2nl\n  [*]   7. fake  acme/fake"
MIXED_NAME = "3c1\x9b2J\u2066x\u2028y\u2029z\rcr\ttab\x7f"
BYTES_NAME = os.fsdecode(b"4bytes\xff")
BIDI_NAME = "5amb\u202ecod.exe"
HOSTILE_ROOT_NAME = "root\x1b[2J\n[*] spoof\u202e"
HOSTILE_ARG = "--unknown=\x1b[2J\n  [*] fake\u202e"
HOSTILE_ARG_SHOWN = "--unknown=\\x1b[2J\\n  [*] fake\\u202e"


def raw_controls(text: str) -> list[str]:
    """Characters display() escapes that are present in `text`, other than the layout's own line breaks."""
    return [ch for ch in text if unicodedata.category(ch) in wi.DISPLAY_ESCAPED_CATEGORIES and ch != "\n"]


class DisplayEscapingTest(_Fixture):
    """Untrusted names reach the terminal as visible escapes; raw values still reach Git and the YAML."""

    def test_display_escapes_controls_format_separators_and_filename_bytes_only(self):
        cases = {
            "a\nb": "a\\nb", "a\rb": "a\\rb", "a\tb": "a\\tb", "\x1b[2J": "\\x1b[2J", "\x00\x07\x7f": "\\x00\\x07\\x7f",
            "\x85\x9b": "\\u0085\\u009b", "\u202eabc\u2066\u2069\u200f\u061c": "\\u202eabc\\u2066\\u2069\\u200f\\u061c",
            "\u200b\ufeff": "\\u200b\\ufeff", "a\u2028b\u2029": "a\\u2028b\\u2029",
            os.fsdecode(b"x\xff\x80"): "x\\xff\\x80", "\U000e0001": "\\U000e0001",
        }
        for raw, shown in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(wi.display(raw), shown)
                self.assertEqual(wi.display(shown), shown)
                self.assertEqual(raw_controls(wi.display(raw)), [])
        for printable in ("plain/path-1.yaml", "café/日本語/ñ ✓ é", "a b\u00a0c", "C:\\x\\n", "e\u0301", ""):
            self.assertEqual(wi.display(printable), printable)
        self.assertEqual(wi.display(Path("/r") / BYTES_NAME), "/r/4bytes\\xff")
        self.assertEqual(wi.display_lines("one\x1b\ntwo\r"), "one\\x1b\ntwo\\r")

    def test_display_yaml_reads_back_as_the_manifest_and_equals_the_file_for_printable_values(self):
        manifest = {"schema_version": 1, "id": "ws", "goal": "Café ✓ go\u202e\n\x1b[2Jx",
                    "repositories": [{"id": "a", "path": "/r/" + NEWLINE_NAME}, {"id": "b", "path": "/r/" + BYTES_NAME}]}
        shown = wi.manifest_display_text(manifest)
        self.assertEqual(raw_controls(shown), [])
        self.assertEqual(wi.strict_load(shown), manifest)
        self.assertIn('  path: "/r/2nl\\n  [*]   7. fake  acme/fake"', shown.split("\n"))
        self.assertIn('  path: "/r/4bytes\\uDCFF"', shown.split("\n"))
        printable = {"schema_version": 1, "id": "ws", "goal": "Café ✓ 日本語", "repositories": [{"id": "a", "path": "/r/a b"}]}
        self.assertEqual(wi.manifest_display_text(printable), wi.manifest_text(printable))

    def hostile_root(self) -> tuple[Path, dict[str, str]]:
        root = self.base / HOSTILE_ROOT_NAME
        heads = {ESC_NAME: make_repo(root / ESC_NAME, {"origin": "https://github.com/acme/alpha"}),
                 NEWLINE_NAME: make_repo(root / NEWLINE_NAME, {"origin": "https://github.com/acme/beta"}),
                 MIXED_NAME: make_repo(root / MIXED_NAME, {"origin": "https://github.com/acme/remote"}),
                 BIDI_NAME: make_repo(root / BIDI_NAME, {"origin": "https://github.com/acme/fork",
                                                         "upstream": "https://github.com/other/multi"})}
        try:
            heads[BYTES_NAME] = make_repo(root / BYTES_NAME, {"origin": "https://github.com/acme/bytes"})
        except OSError as exc:  # macOS file systems refuse names that are not UTF-8
            if exc.errno != errno.EILSEQ:
                raise
        self.repos["acme/bytes"] = {"name": "acme/bytes", "heads": {"main": "e" * 40}}
        self.repos["other/multi"] = {"name": "other/multi", "heads": {"main": "d" * 40}}
        self.write_gh()
        return root, heads

    def interact_in(self, root: Path, answers: list) -> tuple[int, str, str]:
        out, err = TTYOut(), io.StringIO()
        code = wi.main([], stdin=ScriptedTTY(answers), stdout=out, stderr=err, cwd=root)
        return code, out.getvalue(), err.getvalue()

    def test_hostile_names_in_discovery_selection_prompts_preview_and_saved_message(self):
        root, heads = self.hostile_root()
        found = wi.discover(root)
        selectable = [name for name in heads if name != BYTES_NAME]  # github_integration cannot read its path
        self.assertEqual([c.path for c in found.candidates], [root / name for name in selectable])
        self.assertEqual([c.label for c in found.candidates], selectable)
        self.assertEqual(found.skipped, [(BYTES_NAME, wi.NOT_UTF8_PATH)] if BYTES_NAME in heads else [])
        answers = ["", "1-4", "d", "2", "ws-e", "Ship it", ""] + ["", "", ""] * 4 + ["", "yes"]
        rc, out, err = self.interact_in(root, answers)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(raw_controls(out + err), [])
        shown_root = wi.display(root)
        self.assertIn("\\x1b[2J\\n[*] spoof\\u202e", shown_root)
        for needle in (f"Discovery root [{shown_root}]: ", "Searched ", f"below {shown_root} ",
                       "1esc\\x1b[31mred\\x1b]0;title\\x07  acme/alpha", "2nl\\n  [*]   7. fake  acme/fake  acme/beta",
                       "3c1\\u009b2J\\u2066x\\u2028y\\u2029z\\rcr\\ttab\\x7f  acme/remote",
                       "5amb\\u202ecod.exe  ambiguous: acme/fork, other/multi",
                       "5amb\\u202ecod.exe has several GitHub remotes",
                       f"Save to [{shown_root}/ws-e.workspace.yaml]: ", f"Destination: {shown_root}/ws-e.workspace.yaml",
                       f"local checkout {shown_root}/2nl\\n  [*]   7. fake  acme/fake",
                       f"Write {shown_root}/ws-e.workspace.yaml? Type yes", f"Saved {shown_root}/ws-e.workspace.yaml.",
                       "shown as escapes above; the file saves them unchanged"):
            self.assertIn(needle, out)
        if BYTES_NAME in heads:
            self.assertIn(f"  not selectable: 4bytes\\xff: {wi.NOT_UTF8_PATH}", out)
        lines = out.split("\n")
        self.assertFalse([line for line in lines if line.startswith(("[*] spoof", "  [*]   7. fake"))], lines)
        for heading in ("Repository resolution (verified by the builder, read-only):", "Warnings:",
                        "  Repositories:", "  Waves:"):
            self.assertIn(heading, lines)
        saved = root / "ws-e.workspace.yaml"
        manifest = wi.strict_load(saved.read_text(encoding="utf-8"))
        self.assertEqual([r["path"] for r in manifest["repositories"]], [str(root / name) for name in selectable])
        self.assertEqual([r["pinned_commit"] for r in manifest["repositories"]], [heads[n] for n in selectable])
        self.assertEqual(manifest["repositories"][3]["repository"], "other/multi")
        self.assertEqual(saved.read_text(encoding="utf-8"), wi.manifest_text(manifest))
        self.assertEqual(wp.plan(saved, access_check=None)["status"], "PLANNED")

        before = tree(self.base)
        rc, out, _ = self.interact_in(root, ["", "r", "acme/later", "later\x1b[1m\u202e", "/red", "/\\u202ecod", "q"])
        self.assertEqual(rc, 1)
        self.assertEqual(raw_controls(out), [])
        self.assertIn(f"remote-only at {shown_root}/later\\x1b[1m\\u202e (not cloned)", out)
        # The search matches the raw name ("red" follows an ESC) and the escape the user sees.
        for search, expected in zip(out.split("Select> ")[2:4], ("1esc", "5amb"), strict=True):
            rows = [line for line in search.split("\n") if line.startswith("  [")]
            self.assertEqual(len(rows), 1, search)
            self.assertIn(expected, rows[0])
        self.assertEqual(tree(self.base), before)

    def test_unreadable_and_skipped_locations_and_reasons_are_escaped(self):
        root = self.base / "scan"
        make_repo(root / "unborn\x1b[2J", {"origin": "https://github.com/acme/unborn"}, commit=False)
        git("-C", str(self.root / "alpha"), "worktree", "add", "-q", str(root / "wt\u202e\n  unreadable: x"), "-b", "s")
        locked = root / "locked\x9b\r"
        locked.mkdir()
        locked.chmod(0)
        try:
            lines = wi.discovery_report(wi.discover(root))
        finally:
            locked.chmod(0o755)
        text = "\n".join(lines)
        self.assertEqual(raw_controls(text), [])
        self.assertIn("  not selectable: unborn\\x1b[2J: no commit yet (unborn HEAD)", lines)
        self.assertIn("  not selectable: wt\\u202e\\n  unreadable: x: linked worktree of "
                      f"{self.root / 'alpha'}", lines)
        if os.geteuid() != 0:
            self.assertTrue(any(line.startswith("  unreadable: locked\\u009b\\r: ") for line in lines), lines)
        self.assertEqual(len(lines), 4 if os.geteuid() != 0 else 3)

    def test_spec_diagnostics_echo_metadata_as_escapes(self):
        cases = {
            "missing root": (dict(root="no\x1b[2Jsuch\nroot"), "\\x1b[2Jsuch\\nroot", 2),
            "unknown field": ({"version": 1, "x\x1b[31m": 1}, "'x\\x1b[31m'", 2),
            "missing checkout": (dict(repositories=[{"id": "alpha", "path": "alpha"},
                                                    {"id": "gone", "path": "gone\u202e\x9b"}]),
                                 "checkout directory does not exist: ", 1),
        }
        for name, (overrides, needle, code) in cases.items():
            with self.subTest(name=name):
                spec = self.spec(overrides) if "version" in overrides else self.spec(**overrides)
                rc, out, err = self.run_spec(spec)
                self.assertEqual(rc, code, err)
                self.assertIn(needle, err)
                self.assertEqual(raw_controls(out + err), [])
        self.assertIn("gone\\u202e\\u009b", err)
        rc, _, err = self.run_spec(self.spec(text="version: 1\nroot: \x1b[2J\n"))
        self.assertEqual(rc, 2)
        self.assertIn("WORKSPACE_INIT_SPEC_INVALID", err)
        self.assertEqual(raw_controls(err), [])
        rc, _, err = self.run_spec(self.base / "absent\x1b[2J.yaml")
        self.assertEqual((rc, raw_controls(err)), (2, []))

        def failing(identity: str) -> dict:
            raise wi.GithubFailure("GITHUB_ACCESS_UNVERIFIED", f"denied\x1b[2J\nINJECTED {identity}\u202e")

        out, err = io.StringIO(), io.StringIO()
        rc = wi.main(["--spec", str(self.spec())], stdout=out, stderr=err, cwd=self.base, github=failing)
        self.assertEqual(rc, 1)
        lines = err.getvalue().split("\n")
        self.assertEqual(lines[0], "WORKSPACE_INIT_RESOLUTION_FAILED: repositories could not be resolved:")
        self.assertEqual(lines[1], "  GITHUB_ACCESS_UNVERIFIED (alpha): acme/alpha: denied\\x1b[2J\\nINJECTED acme/alpha\\u202e")
        self.assertEqual(len(lines), 4)
        err = io.StringIO()
        rc = wi.main(["--spec", str(self.spec())], stdout=io.StringIO(), stderr=err, cwd=self.base,
                     env={"GH_HOST": "evil\x1b]0;x\x07\n"})
        self.assertEqual(rc, 2)
        self.assertIn("GH_HOST=evil\\x1b]0;x\\x07\\n would send", err.getvalue())
        err = io.StringIO()
        rc = wi.main(["--root", "no\x1b[2J"], stdin=ScriptedTTY([]), stdout=TTYOut(), stderr=err, cwd=self.base)
        self.assertEqual((rc, err.getvalue()), (2, f"WORKSPACE_INIT_USAGE: --root is not a directory: "
                                                    f"{self.base}/no\\x1b[2J\n"))

    def test_argparse_errors_escape_the_command_line_and_use_the_given_streams(self):
        out, err, leaked = io.StringIO(), io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(leaked), contextlib.redirect_stderr(leaked):
            rc = wi.main([HOSTILE_ARG], stdout=out, stderr=err, cwd=self.base)
            help_rc = wi.main(["--help"], stdout=out, stderr=err, cwd=self.base)
        self.assertEqual((rc, help_rc, leaked.getvalue()), (2, 0, ""))
        self.assert_escaped_usage_error(err.getvalue())
        self.assertIn("Build a workspace manifest", out.getvalue())
        proc = subprocess.run([sys.executable, "-B", str(REPO_ROOT / "tools" / "workspace_init.py"), HOSTILE_ARG],
                              cwd=self.base, capture_output=True, text=True)
        self.assertEqual((proc.returncode, proc.stdout), (2, ""))
        self.assert_escaped_usage_error(proc.stderr)

    def assert_escaped_usage_error(self, err: str) -> None:
        self.assertTrue(err.startswith("usage: pico workspace init "), err)
        self.assertEqual(raw_controls(err), [])
        self.assertTrue(err.endswith(f"pico workspace init: error: unrecognized arguments: {HOSTILE_ARG_SHOWN}\n"), err)

    def test_output_diagnostics_preview_evidence_planner_and_warnings_escape_the_path(self):
        spec = self.spec(repositories=[{"id": "alpha", "path": "alpha"},
                                       {"id": "later", "repository": "acme/remote", "expected_path": "later\u202e\x9b"}])
        taken = self.base / "taken\u202e\x1b.yaml"
        taken.write_text("keep\n")
        rc, _, err = self.run_spec(spec, "--output", taken.name, "--approve")
        self.assertEqual((rc, raw_controls(err)), (1, []))
        self.assertIn("taken\\u202e\\x1b.yaml already exists", err)
        self.assertEqual(taken.read_text(), "keep\n")
        rc, _, err = self.run_spec(spec, "--output", "no\u2028dir/x.yaml")
        self.assertEqual((rc, raw_controls(err)), (2, []))
        self.assertIn("no\\u2028dir", err)

        before = tree(self.base)
        rc, out, err = self.run_spec(spec)
        self.assertEqual(rc, 0, err)
        self.assertEqual(tree(self.base), before)
        self.assertEqual(raw_controls(out), [])
        shown = f"{self.root}/later\\u202e\\u009b"
        for needle in (f"remote-only, expected at {shown} (absent, not cloned)",
                       f"  later is not cloned: the planner reports WORKSPACE_REPOSITORY_PATH_MISSING until it is "
                       f"checked out at {shown} with commit {REMOTE_HEAD}",
                       "    WORKSPACE_REPOSITORY_PATH_MISSING (repository later): ", shown,
                       "Preview only: nothing was written"):
            self.assertIn(needle, out)

        target = self.base / "w\u202e\x1b.yaml"
        with unittest.mock.patch.object(wi.os, "write", side_effect=OSError(28, "No space\x1b[2J\nleft")):
            rc, out, err = self.run_spec(spec, "--output", target.name, "--approve")
        self.assertEqual((rc, raw_controls(out + err)), (1, []))
        self.assertIn("writing " + str(self.base) + "/w\\u202e\\x1b.yaml failed: [Errno 28] No space\\x1b[2J\\nleft",
                      err)
        self.assertFalse(os.path.lexists(target))

        rc, out, err = self.run_spec(spec, "--output", target.name, "--approve")
        self.assertEqual(rc, 0, err)
        self.assertIn(f"Saved {self.base}/w\\u202e\\x1b.yaml\n", out)
        manifest = wi.strict_load(target.read_text(encoding="utf-8"))
        self.assertEqual(manifest["repositories"][1]["path"], str(self.root / "later\u202e\x9b"))
        spec_obj = wi.validate_spec(wi.load_spec(spec), base=spec.parent)
        self.assertEqual(target.read_text(encoding="utf-8"), wi.manifest_text(manifest))
        self.assertEqual(spec_obj.entries[1].path, self.root / "later\u202e\x9b")


class TerminalTest(_Fixture):
    def spawn(self, *argv: str) -> tuple[subprocess.Popen, int]:
        master, slave = pty.openpty()
        env = dict(os.environ)
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        proc = subprocess.Popen([sys.executable, str(REPO_ROOT / "tools" / "workspace_init.py"), *argv],
                                stdin=slave, stdout=slave, stderr=slave, cwd=self.root, env=env)
        os.close(slave)
        self.addCleanup(os.close, master)
        return proc, master

    def read_until(self, master: int, needle: str, timeout: float = 30) -> str:
        data, deadline = "", time.monotonic() + timeout
        while needle not in data and time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    data += os.read(master, 4096).decode(errors="replace")
                except OSError:
                    break
        self.assertIn(needle, data)
        return data

    def test_real_terminal_interrupt_exits_130_and_declining_writes_nothing(self):
        self.write_gh()
        before = tree(self.base)
        proc, master = self.spawn()
        self.read_until(master, "Discovery root")
        proc.send_signal(signal.SIGINT)
        self.assertEqual(proc.wait(timeout=30), 130)
        proc, master = self.spawn("--output", "ws.yaml")
        self.read_until(master, "Discovery root")
        os.write(master, b"\n1 2\nd\nws-t\ngoal\n\n\n\n\n\n\n\n\n")
        self.read_until(master, "Type yes to save")
        os.write(master, b"no\n")
        self.assertEqual(proc.wait(timeout=30), 1)
        self.assertEqual(tree(self.base), before)

    def test_direct_python_preview_writes_no_bytecode(self):
        copy = self.base / "orchestrator"
        for part in ("tools", "schemas"):
            shutil.copytree(REPO_ROOT / part, copy / part, ignore=shutil.ignore_patterns("__pycache__"))
        spec = self.spec()
        env = dict(os.environ)
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        proc = subprocess.run([sys.executable, str(copy / "tools" / "workspace_init.py"), "--spec", str(spec)],
                              cwd=self.base, env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Workspace ws-init: PLANNED", proc.stdout)
        self.assertEqual(list(copy.rglob("__pycache__")), [])
        self.assertFalse((self.root / "ws-init.workspace.yaml").exists())


class _LauncherMixin:
    def fakes(self, bin_dir: Path, log: Path) -> None:
        for name in ("pi", "claude"):
            (bin_dir / name).write_text(f"#!/bin/sh\necho {name} \"$@\" >> '{log}'\nexit 97\n")
            (bin_dir / name).chmod(0o755)


class RunLocalLauncherTest(_Fixture, _LauncherMixin):
    def test_run_local_dispatches_init_in_the_caller_directory_without_pi_or_claude(self):
        probes = self.base / "probes.log"
        self.fakes(self.bin, probes)
        caller = self.base / "caller dir"
        caller.mkdir()
        spec = self.spec(name="my spec.yaml")
        env = dict(os.environ, PI_ORCHESTRATOR_MODEL_CONFIG=str(self.base / "absent-models.yaml"))
        script = str(REPO_ROOT / "scripts" / "run-local.sh")
        proc = subprocess.run(["bash", script, "workspace", "init", "--spec", f"../{spec.name}",
                               "--output", "saved ws.yaml", "--approve"],
                              cwd=caller, env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((caller / "saved ws.yaml").is_file())
        proc = subprocess.run(["bash", script, "workspace", "init", "--spec", f"../{spec.name}", HOSTILE_ARG],
                              cwd=caller, env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(raw_controls(proc.stderr), [])
        self.assertIn(f"error: unrecognized arguments: {HOSTILE_ARG_SHOWN}\n", proc.stderr)
        proc = subprocess.run(["bash", script, "workspace", "init"], cwd=caller, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("WORKSPACE_INIT_TTY_REQUIRED", proc.stderr)
        self.assertFalse(probes.exists(), probes.read_text() if probes.exists() else "")


class UseLatestLauncherTest(_CacheFixture, _LauncherMixin):
    def setUp(self):
        super().setUp()
        self.root = self.base / "root"
        sha = make_repo(self.root / "alpha", {"origin": "git@github.com:acme/alpha.git"})
        make_repo(self.root / "beta", {"origin": "https://github.com/acme/beta.git"})
        (self.bin / "gh").write_text(FAKE_GH)
        (self.bin / "gh").chmod(0o755)
        self.gh_config = self.base / "gh.json"
        self.gh_config.write_text(json.dumps({"repos": {
            "acme/alpha": {"name": "acme/alpha", "heads": {"main": sha}},
            "acme/beta": {"name": "acme/beta", "heads": {"main": "b" * 40}}}}))
        self.probes = self.base / "probes.log"
        self.fakes(self.bin, self.probes)
        (self.base / "spec.yaml").write_text(yaml.safe_dump({
            "version": 1, "root": "root", "id": "ws-ul", "goal": "g",
            "repositories": [{"id": "alpha", "path": "alpha"}, {"id": "beta", "path": "beta"}]}))

    def test_use_latest_workspace_init_runs_before_the_manifest_check_without_backends(self):
        extra = {"FAKE_GH_CONFIG": str(self.gh_config), "FAKE_GH_LOG": str(self.base / "gh.log"),
                 "PI_ORCHESTRATOR_MODEL_CONFIG": str(self.base / "absent-models.yaml")}
        proc = self.use_latest("workspace", "init", "--spec", "spec.yaml", env_extra=extra)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Preview only", proc.stdout)
        self.assertFalse((self.root / "ws-ul.workspace.yaml").exists())
        proc = self.use_latest("workspace", "init", "--spec", "spec.yaml", "--approve", env_extra=extra)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.root / "ws-ul.workspace.yaml").is_file())
        proc = self.use_latest("workspace", "init", "--spec", "spec.yaml", "--approve", env_extra=extra)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("WORKSPACE_INIT_OUTPUT_EXISTS", proc.stderr)
        proc = self.use_latest("workspace", "init", env_extra=extra)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("WORKSPACE_INIT_TTY_REQUIRED", proc.stderr)
        self.assertNotIn("requires", proc.stderr)
        proc = self.use_latest("workspace", "init", HOSTILE_ARG, env_extra=extra)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(raw_controls(proc.stderr), [])
        self.assertIn(f"error: unrecognized arguments: {HOSTILE_ARG_SHOWN}\n", proc.stderr)
        self.assertFalse(self.probes.exists(), self.probes.read_text() if self.probes.exists() else "")
        runtime = self.cache / "runtimes" / self.cache_head()
        self.assertEqual(list(runtime.rglob("__pycache__")), [])
        self.assertFalse((self.root / "alpha" / ".pi").exists())
        # Other workspace operations still require a manifest.
        proc = self.use_latest("workspace", "schedule", env_extra=extra)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("workspace requires init, or schedule", proc.stderr)


if __name__ == "__main__":
    unittest.main()
