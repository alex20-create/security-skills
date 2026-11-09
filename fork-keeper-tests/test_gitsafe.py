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


def commit_with_message_file(repo, message):
    msg = Path(repo) / ".msg"
    msg.write_bytes(message)
    git(repo, "commit", "-q", "--allow-empty", "-F", str(msg))
    msg.unlink()
    return git(repo, "rev-parse", "HEAD").decode().strip()


class UrlTests(unittest.TestCase):
    def test_rejects_http_and_ssh_and_file(self):
        for bad in ("http://github.com/a/b", "git@github.com:a/b.git", "ssh://git@github.com/a/b",
                    "file:///tmp/x", "ext::sh -c touch% /tmp/pwned", "/tmp/repo",
                    "https://user:tok@github.com/a/b", "https:///a/b", "https://github.com/a b",
                    "https://github.com/a\n", "-https://github.com/a/b"):
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

    def test_fetch_branch_lands_under_refs_fk_name(self):
        g = self.bare()
        g.fetch(file_url(self.up), ["main", self.first])
        self.assertEqual(g.rev_parse(gitsafe.name_ref("main")), self.second)

    def test_branch_wins_over_tag_of_same_name(self):
        git(self.up, "tag", "main", self.first)
        g = self.bare()
        g.fetch(file_url(self.up), ["main"])
        self.assertEqual(g.rev_parse(gitsafe.name_ref("main")), self.second)

    def test_fetch_tag_when_no_branch(self):
        git(self.up, "tag", "v1.0", self.first)
        g = self.bare()
        g.fetch(file_url(self.up), ["v1.0"])
        self.assertEqual(g.rev_parse(gitsafe.name_ref("v1.0")), self.first)

    def test_rejects_bad_ref_names(self):
        g = self.bare()
        for bad in ("-x", "refs/heads/*", "a:b", "a^", "a..b", "a b", "", "x.lock", "a/.b", "a//b", "a/"):
            with self.subTest(ref=bad):
                with self.assertRaises(GitError):
                    g.fetch(file_url(self.up), [bad])

    def test_fetch_refuses_trees_with_git_folder_names(self):
        blob = git(self.up, "hash-object", "-w", "--stdin", input=b"x\n").decode().strip()
        for name in (".git", "GIT~1", ".."):
            with self.subTest(name=name):
                entry = "100644 blob {}\t{}\n".format(blob, name).encode()
                try:
                    tree_sha = git(self.up, "mktree", "--missing", input=entry).decode().strip()
                except Exception:
                    self.skipTest("this git refuses to build the hostile tree")
                sha = git(self.up, "commit-tree", tree_sha, "-m", "hostile").decode().strip()
                g = SafeGit.init(self.root / ("m-" + str(len(name)) + ".git"), bare=True, allow_file=True)
                with self.assertRaises(GitError):
                    g.fetch(file_url(self.up), [sha])

    def test_fetch_refuses_file_url_without_flag(self):
        g = SafeGit.init(self.root / "strict.git", bare=True)
        with self.assertRaises(GitError):
            g.fetch(file_url(self.up), ["main"])

    def test_git_layer_blocks_file_protocol(self):
        g = SafeGit.init(self.root / "strict.git", bare=True)
        with self.assertRaises(GitError):
            g.run("fetch", file_url(self.up), "main")

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

    def test_log_survives_hostile_subject(self):
        hostile = commit_with_message_file(self.up, b"bad\x1esubject\x1f here\n\nbody\n")
        g = SafeGit(self.up)
        log = g.log(self.second, hostile)
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["sha"], hostile)
        self.assertIn("subject", log[0]["subject"])

    def test_is_ancestor_after_rewrite(self):
        g = self.bare()
        g.fetch(file_url(self.up), ["main"])
        old_head = g.rev_parse(gitsafe.name_ref("main"))
        git(self.up, "commit", "-q", "--amend", "-m", "rewritten")
        g.fetch(file_url(self.up), ["main", old_head])
        new_head = g.rev_parse(gitsafe.name_ref("main"))
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

    def test_offline_guard_holds_without_lazy_fetch_variable(self):
        class OldGit(SafeGit):
            def _env(self):
                env = super()._env()
                env.pop("GIT_NO_LAZY_FETCH", None)
                return env

        g = self.bare()
        g.fetch(file_url(self.up), ["main"])
        blob = dict((p, b) for _, b, p in g.ls_tree(self.second, ""))["b.txt"]
        with self.assertRaises(GitError):
            OldGit(self.root / "mirror.git", allow_file=True, offline=True).cat_blob(blob)

    def test_offline_fetch_refused(self):
        g = SafeGit.init(self.root / "off.git", bare=True, allow_file=True, offline=True)
        with self.assertRaises(GitError):
            g.fetch(file_url(self.up), ["main"])


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir(self)
        self.repo = self.root / "r"
        self.a = make_repo(self.repo, {"a.txt": b"one\n"}, "a")
        self.b = make_repo(self.repo, {"a.txt": b"two\n"}, "b")
        self.g = SafeGit(self.repo)

    def test_rejects_option_like_revisions(self):
        out = self.root / "written"
        evil = "--output=" + out.as_posix()
        calls = [
            lambda: self.g.diff(evil, self.b, []),
            lambda: self.g.changed(evil, self.b),
            lambda: self.g.archive(evil, ["a.txt"]),
            lambda: self.g.log(evil, self.b),
            lambda: self.g.rev_parse(evil),
            lambda: self.g.is_ancestor(evil, self.b),
            lambda: self.g.ls_tree(evil, ""),
            lambda: self.g.cat_blob(evil),
        ]
        for i, call in enumerate(calls):
            with self.subTest(call=i):
                with self.assertRaises(GitError):
                    call()
                self.assertFalse(out.exists())
                self.assertFalse(Path(str(out) + ".." + self.b).exists())

    def test_accepts_sha_head_and_fk_refs(self):
        self.assertEqual(self.g.rev_parse(self.b), self.b)
        self.assertEqual(self.g.rev_parse("HEAD"), self.b)
        with self.assertRaises(GitError):
            self.g.rev_parse("main")


class AttributeTests(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir(self)
        self.up = self.root / "upstream"
        self.base = make_repo(self.up, {"a.py": b"x = 1\n"}, "base")

    def test_export_ignore_cannot_hide_files(self):
        sha = make_repo(self.up, {".gitattributes": b"evil.py export-ignore\n", "evil.py": b"bad()\n"}, "hide")
        g = SafeGit.init(self.root / "m.git", bare=True, allow_file=True)
        serve(self.up)
        g.fetch(file_url(self.up), [sha])
        import io, tarfile
        names = tarfile.open(fileobj=io.BytesIO(g.archive(sha, ["evil.py", "a.py"]))).getnames()
        self.assertIn("evil.py", names)

    def test_export_subst_not_applied(self):
        sha = make_repo(self.up, {".gitattributes": b"*.txt export-subst\n", "v.txt": b"$Format:%H$\n"}, "subst")
        g = SafeGit.init(self.root / "m.git", bare=True, allow_file=True)
        serve(self.up)
        g.fetch(file_url(self.up), [sha])
        import io, tarfile
        tf = tarfile.open(fileobj=io.BytesIO(g.archive(sha, ["v.txt"])))
        self.assertEqual(tf.extractfile("v.txt").read(), b"$Format:%H$\n")

    def test_diff_attribute_cannot_force_binary(self):
        sha = make_repo(self.up, {".gitattributes": b"*.py -diff\n", "a.py": b"x = 2\n"}, "binary")
        g = SafeGit.init(self.root / "m.git", bare=True, allow_file=True)
        serve(self.up)
        g.fetch(file_url(self.up), [self.base, sha])
        out = g.diff(self.base, sha, ["a.py"])
        self.assertIn(b"+x = 2", out)
        self.assertNotIn(b"GIT binary patch", out)


class PathTests(unittest.TestCase):
    def test_non_utf8_path_is_an_error(self):
        root = tmpdir(self)
        repo = root / "r"
        make_repo(repo, {"ok.txt": b"ok\n"}, "ok")
        blob = git(repo, "hash-object", "-w", "--stdin", input=b"x\n").decode().strip()
        git(repo, "update-index", "--index-info", input=b"100644 blob " + blob.encode() + b"\tbad\xff.txt\n")
        git(repo, "commit", "-q", "-m", "bad name")
        sha = git(repo, "rev-parse", "HEAD").decode().strip()
        with self.assertRaises(GitError):
            SafeGit(repo).ls_tree(sha, "")


class IsolationTests(unittest.TestCase):
    """The user's global config and the repository's own config must not reach SafeGit."""

    KEYS = ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM")

    def setUp(self):
        self.root = tmpdir(self)
        self.saved = {k: os.environ.get(k) for k in self.KEYS}
        self.addCleanup(self.restore)
        self.home = self.root / "home"
        self.home.mkdir()
        for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"):
            os.environ.pop(k, None)
        os.environ["HOME"] = str(self.home)
        os.environ["USERPROFILE"] = str(self.home)
        os.environ["XDG_CONFIG_HOME"] = str(self.home / ".config")
        self.marker = self.root / "marker"
        self.hooks = self.root / "hooks"
        self.hooks.mkdir()
        hook = self.hooks / "post-commit"
        hook.write_text("#!/bin/sh\necho ran > '{}'\n".format(self.marker.as_posix()), newline="\n")
        hook.chmod(0o755)

    def restore(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def plain_git(self, cwd, *args):
        r = subprocess.run(["git", "-c", "user.name=x", "-c", "user.email=x@example.invalid", *args],
                           cwd=str(cwd), capture_output=True)
        return r

    def plain_commit(self, repo, name, data):
        (repo / name).write_bytes(data)
        self.plain_git(repo, "add", name)
        self.plain_git(repo, "commit", "-q", "-m", "c")

    def test_global_hooks_not_run(self):
        (self.home / ".gitconfig").write_text("[core]\n\thooksPath = {}\n".format(self.hooks.as_posix()))
        control = self.root / "control"
        control.mkdir()
        self.plain_git(control, "init", "-q")
        self.plain_commit(control, "f", b"x")
        self.assertTrue(self.marker.exists(), "control: the global hook should run under plain git")
        self.marker.unlink()

        work = self.root / "work"
        g = SafeGit.init(work, bare=False)
        (work / "f").write_bytes(b"x")
        g.commit_all("c")
        self.assertFalse(self.marker.exists())

    def test_repository_hooks_and_config_not_used(self):
        def arm(repo):
            self.plain_git(repo, "config", "core.hooksPath", self.hooks.as_posix())
            self.plain_git(repo, "config", "core.autocrlf", "true")

        control = self.root / "control"
        control.mkdir()
        self.plain_git(control, "init", "-q")
        arm(control)
        self.plain_commit(control, "f.txt", b"a\nb\n")
        self.assertTrue(self.marker.exists(), "control: the repository hook should run under plain git")
        self.marker.unlink()
        (control / "f.txt").unlink()
        self.plain_git(control, "checkout", "--", "f.txt")
        self.assertEqual((control / "f.txt").read_bytes(), b"a\r\nb\r\n", "control: autocrlf should convert")

        work = self.root / "work"
        g = SafeGit.init(work, bare=False)
        arm(work)
        (work / "f.txt").write_bytes(b"a\nb\n")
        g.commit_all("c")
        self.assertFalse(self.marker.exists())
        (work / "f.txt").unlink()
        g.run("checkout", "--", "f.txt")
        self.assertEqual((work / "f.txt").read_bytes(), b"a\nb\n")

    def test_global_autocrlf_ignored(self):
        (self.home / ".gitconfig").write_text("[core]\n\tautocrlf = true\n")
        control = self.root / "control"
        control.mkdir()
        self.plain_git(control, "init", "-q")
        self.plain_commit(control, "f.txt", b"a\nb\n")
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
            g.rev_parse("0" * 40)
        self.assertTrue(str(cm.exception))


if __name__ == "__main__":
    unittest.main()
