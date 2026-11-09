import os
import subprocess
import unittest
from pathlib import Path

import helpers
from helpers import git, make_repo, tmpdir

import gitsafe
from gitsafe import GitError, SafeGit


def serve(root):
    """Let a fixture repo answer fetches by SHA and with blob filters, like GitHub does."""
    git(root, "config", "uploadpack.allowAnySHA1InWant", "true")
    git(root, "config", "uploadpack.allowFilter", "true")


def file_url(path):
    return Path(path).resolve().as_uri()


class UrlTests(unittest.TestCase):
    def test_rejects_http_and_ssh_and_file(self):
        for bad in ("http://github.com/a/b", "git@github.com:a/b.git", "ssh://git@github.com/a/b",
                    "file:///tmp/x", "ext::sh -c touch% /tmp/pwned", "/tmp/repo"):
            with self.subTest(url=bad):
                with self.assertRaises(GitError):
                    gitsafe.check_url(bad)
        gitsafe.check_url("https://github.com/a/b")

    def test_file_allowed_only_on_request(self):
        gitsafe.check_url("file:///tmp/x", allow_file=True)
        with self.assertRaises(GitError):
            gitsafe.check_url("http://github.com/a/b", allow_file=True)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir(self)
        self.up = self.root / "upstream"
        self.first = make_repo(self.up, {"a.txt": b"one\n"}, "first")
        self.second = make_repo(self.up, {"a.txt": b"two\n", "b.txt": b"bee\n"}, "second")
        serve(self.up)

    def bare(self, **kw):
        return SafeGit.init(self.root / "mirror.git", bare=True, allow_file=True, **kw)

    def test_fetch_by_sha_from_file_remote(self):
        g = self.bare()
        g.fetch(file_url(self.up), [self.first])
        self.assertEqual(g.rev_parse(self.first), self.first)

    def test_fetch_branch_lands_under_refs_fk(self):
        g = self.bare()
        g.fetch(file_url(self.up), ["main", self.first])
        self.assertEqual(g.rev_parse("refs/fk/main"), self.second)

    def test_fetch_refuses_file_url_without_flag(self):
        g = SafeGit.init(self.root / "strict.git", bare=True)
        with self.assertRaises(GitError):
            g.fetch(file_url(self.up), ["main"])

    def test_reads_after_fetch(self):
        g = self.bare()
        g.fetch(file_url(self.up), ["main", self.first])
        self.assertEqual(g.changed(self.first, self.second), [("M", "a.txt"), ("A", "b.txt")])
        entries = g.ls_tree(self.second, "")
        self.assertEqual([(m, p) for m, _, p in entries], [("100644", "a.txt"), ("100644", "b.txt")])
        blob = dict((p, b) for _, b, p in entries)["a.txt"]
        self.assertEqual(g.cat_blob(blob), b"two\n")
        log = g.log(self.first, self.second)
        self.assertEqual([c["subject"] for c in log], ["second"])
        self.assertEqual(log[0]["sha"], self.second)
        self.assertEqual(log[0]["author"], "fixture")
        self.assertIn(b"+two", g.diff(self.first, self.second, ["a.txt"]))
        self.assertIn(b"a.txt", g.diff(self.first, self.second, [], stat=True))

    def test_is_ancestor_after_rewrite(self):
        g = self.bare()
        g.fetch(file_url(self.up), ["main"])
        old_head = g.rev_parse("refs/fk/main")
        git(self.up, "commit", "-q", "--amend", "-m", "rewritten")
        g.fetch(file_url(self.up), ["main", old_head])
        new_head = g.rev_parse("refs/fk/main")
        self.assertTrue(g.is_ancestor(self.first, new_head))
        self.assertFalse(g.is_ancestor(old_head, new_head))

    def test_offline_never_fetches_missing_blobs(self):
        g = self.bare()
        g.fetch(file_url(self.up), ["main"])
        blob = dict((p, b) for _, b, p in g.ls_tree(self.second, ""))["b.txt"]
        offline = SafeGit(self.root / "mirror.git", allow_file=True, offline=True)
        with self.assertRaises(GitError):
            offline.cat_blob(blob)
        self.assertEqual(g.cat_blob(blob), b"bee\n")


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir(self)
        self.saved = {k: os.environ.get(k) for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM")}
        self.addCleanup(self.restore)

    def restore(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def hostile_config(self, body):
        cfg = self.root / "hostile.gitconfig"
        cfg.write_text(body)
        os.environ["GIT_CONFIG_GLOBAL"] = str(cfg)
        os.environ["GIT_CONFIG_SYSTEM"] = str(cfg)
        os.environ.pop("GIT_CONFIG_NOSYSTEM", None)

    def plain_git(self, cwd, *args):
        return subprocess.run(["git", "-c", "user.name=x", "-c", "user.email=x@example.invalid", *args],
                              cwd=str(cwd), capture_output=True)

    def test_global_hooks_not_run(self):
        hooks = self.root / "hooks"
        hooks.mkdir()
        marker = self.root / "marker"
        hook = hooks / "post-commit"
        hook.write_text("#!/bin/sh\necho ran > '{}'\n".format(marker.as_posix()), newline="\n")
        hook.chmod(0o755)
        self.hostile_config("[core]\n\thooksPath = {}\n".format(hooks.as_posix()))

        control = self.root / "control"
        control.mkdir()
        self.plain_git(control, "init", "-q")
        (control / "f").write_text("x")
        self.plain_git(control, "add", "f")
        self.plain_git(control, "commit", "-q", "-m", "c")
        self.assertTrue(marker.exists(), "control: the hostile hook should run under plain git")
        marker.unlink()

        g = SafeGit.init(self.root / "work", bare=False)
        (self.root / "work" / "f").write_text("x")
        g.commit_all("c")
        self.assertFalse(marker.exists())

    def test_autocrlf_ignored(self):
        self.hostile_config("[core]\n\tautocrlf = true\n")

        control = self.root / "control"
        control.mkdir()
        self.plain_git(control, "init", "-q")
        (control / "f.txt").write_bytes(b"a\nb\n")
        self.plain_git(control, "add", "f.txt")
        self.plain_git(control, "commit", "-q", "-m", "c")
        (control / "f.txt").unlink()
        self.plain_git(control, "checkout", "--", "f.txt")
        self.assertEqual((control / "f.txt").read_bytes(), b"a\r\nb\r\n", "control: autocrlf should convert")

        work = self.root / "work"
        g = SafeGit.init(work, bare=False)
        (work / "f.txt").write_bytes(b"a\nb\n")
        g.commit_all("c")
        (work / "f.txt").unlink()
        g.run("checkout", "--", "f.txt")
        self.assertEqual((work / "f.txt").read_bytes(), b"a\nb\n")


class ErrorTests(unittest.TestCase):
    def test_git_error_carries_stderr(self):
        g = SafeGit.init(tmpdir(self) / "r", bare=True)
        with self.assertRaises(GitError) as cm:
            g.rev_parse("does-not-exist")
        self.assertTrue(str(cm.exception))


if __name__ == "__main__":
    unittest.main()
