import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace

import helpers
from helpers import git, make_repo, tmpdir

import check
from check import Alarm, CheckError
from gitsafe import SafeGit
from registry import MapEntry, Origin

SKILL = b"---\nname: x\ndescription: Draws diagrams when asked.\n---\nbody\n"
FOLDED = b"---\nname: x\ndescription: >\n  Draws diagrams\n  when asked.\nlicense: MIT\n---\nbody\n"


def serve(root):
    git(root, "config", "uploadpack.allowAnySHA1InWant", "true")
    git(root, "config", "uploadpack.allowFilter", "true")


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir(self)
        self.up = self.root / "upstream"
        self.pin = make_repo(self.up, {
            "skills/x/SKILL.md": SKILL,
            "skills/x/scripts/a.py": b"print('a')\n",
            "LICENSE": b"MIT\n",
            "tools/t.py": b"tool\n",
            "README.md": b"readme\n",
        }, "first")
        serve(self.up)
        self.url = self.up.resolve().as_uri()
        self.origin = Origin(id="upstream", url=self.url, pin=self.pin, track="main", license="MIT",
                             map=[MapEntry("skills/x", "."), MapEntry("LICENSE", "LICENSE")],
                             audit=None, last_checked=None)

    def mirror(self, *shas):
        g = SafeGit.init(self.root / "mirror.git", bare=True, allow_file=True)
        g.fetch(self.url, [self.pin, *shas])
        return g

    def summarize_after(self, files, message="change"):
        new = make_repo(self.up, files, message)
        return check.summarize(self.mirror(new), self.origin, new), new

    def kinds(self, report):
        return [a.kind for a in report.alarms]

    def test_no_changes(self):
        r, new = self.summarize_after({"tools/t.py": b"tool 2\n"})
        self.assertEqual((r.origin, r.old, r.new), ("upstream", self.pin, new))
        self.assertEqual(r.mapped_changes, [])
        self.assertEqual(r.outside, {"tools": 1})
        self.assertEqual(r.alarms, [])
        self.assertEqual([c["subject"] for c in r.commits], ["change"])
        self.assertEqual(r.stat, "")

    def test_outside_counts_root_files(self):
        r, _ = self.summarize_after({"README.md": b"new\n", "tools/t.py": b"2\n", "tools/u.py": b"u\n"})
        self.assertEqual(r.outside, {".": 1, "tools": 2})

    def test_history_alarm(self):
        head = make_repo(self.up, {"skills/x/scripts/a.py": b"print('b')\n"}, "second")
        git(self.up, "commit", "-q", "--amend", "-m", "second, rewritten")
        new = git(self.up, "rev-parse", "HEAD").decode().strip()
        origin = Origin(**{**self.origin.__dict__, "pin": head})
        r = check.summarize(self.mirror(head, new), origin, new)
        self.assertIn("history", self.kinds(r))
        self.assertIn(head[:12], [a for a in r.alarms if a.kind == "history"][0].detail)

    def test_license_alarm(self):
        r, _ = self.summarize_after({"LICENSE": b"GPL\n"})
        self.assertEqual(self.kinds(r), ["license"])
        self.assertEqual(r.mapped_changes, [("M", "LICENSE")])

    def test_license_alarm_on_any_licence_name(self):
        r, _ = self.summarize_after({"skills/x/COPYING.txt": b"x\n", "skills/x/Licence.md": b"y\n"})
        self.assertEqual(self.kinds(r), ["license", "license"])

    def test_contract_alarm_on_description(self):
        r, _ = self.summarize_after({"skills/x/SKILL.md": SKILL.replace(b"when asked", b"always")})
        self.assertEqual(self.kinds(r), ["contract"])
        self.assertIn("skills/x/SKILL.md", r.alarms[0].detail)

    def test_contract_alarm_on_folded_description(self):
        make_repo(self.up, {"skills/x/SKILL.md": FOLDED}, "folded")
        self.origin = Origin(**{**self.origin.__dict__, "pin": git(self.up, "rev-parse", "HEAD").decode().strip()})
        self.pin = self.origin.pin
        r, _ = self.summarize_after({"skills/x/SKILL.md": FOLDED.replace(b"when asked.", b"on any text.")})
        self.assertEqual(self.kinds(r), ["contract"])

    def test_body_change_is_not_contract(self):
        r, _ = self.summarize_after({"skills/x/SKILL.md": SKILL.replace(b"body", b"new body")})
        self.assertEqual(r.alarms, [])
        self.assertEqual(r.mapped_changes, [("M", "skills/x/SKILL.md")])

    def test_contract_alarm_on_manifests(self):
        r, _ = self.summarize_after({"skills/x/.claude-plugin/plugin.json": b"{}\n", "skills/x/.mcp.json": b"{}\n",
                                     "skills/x/hooks/hooks.json": b"{}\n"})
        self.assertEqual(self.kinds(r), ["contract", "contract", "contract"])

    def test_contract_alarm_on_added_skill_file(self):
        r, _ = self.summarize_after({"skills/x/sub/SKILL.md": SKILL})
        self.assertEqual(self.kinds(r), ["contract"])

    def test_stat_covers_mapped_paths_only(self):
        r, _ = self.summarize_after({"skills/x/scripts/a.py": b"print('b')\n", "tools/t.py": b"2\n"})
        self.assertIn("a.py", r.stat)
        self.assertNotIn("t.py", r.stat)

    def test_extract_only_changed(self):
        new = make_repo(self.up, {"skills/x/scripts/a.py": b"print('b')\n", "skills/x/scripts/b.py": b"b\n",
                                  "tools/t.py": b"2\n"}, "change")
        g = self.mirror(new)
        dest = self.root / "delta"
        got = check.extract(g, self.origin, new, dest)
        self.assertEqual(got, ["skills/x/scripts/a.py", "skills/x/scripts/b.py"])
        self.assertEqual((dest / "skills/x/scripts/a.py").read_bytes(), b"print('b')\n")
        self.assertFalse((dest / "skills/x/SKILL.md").exists())
        self.assertFalse((dest / "LICENSE").exists())
        self.assertFalse((dest / "tools").exists())

    def test_extract_rejects_symlink(self):
        blob = git(self.up, "hash-object", "-w", "--stdin", input=b"/etc/passwd").decode().strip()
        make_repo(self.up, {"skills/x/scripts/b.py": b"b\n"}, "plain file")
        git(self.up, "update-index", "--add", "--cacheinfo", "120000,{},skills/x/link".format(blob))
        git(self.up, "commit", "-q", "-m", "link")
        new = git(self.up, "rev-parse", "HEAD").decode().strip()
        self.assertEqual(git(self.up, "ls-tree", new, "skills/x/link").decode().split()[0], "120000")
        dest = self.root / "delta"
        with self.assertRaises(CheckError) as cm:
            check.extract(self.mirror(new), self.origin, new, dest)
        self.assertIn("skills/x/link", str(cm.exception))
        self.assertFalse(dest.exists() and any(dest.rglob("*")))


class SignatureTests(unittest.TestCase):
    SHA = "a" * 40

    def runner(self, stdout="", returncode=0, exc=None):
        calls = []

        def run(args, **kw):
            calls.append(args)
            if exc:
                raise exc
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")
        return run, calls

    def test_signature(self):
        url = "https://github.com/owner/repo"
        run, calls = self.runner("true\n")
        self.assertEqual(check.signature(url, self.SHA, runner=run), "verified")
        self.assertEqual(calls[0][:3], ["gh", "api", "repos/owner/repo/commits/" + self.SHA])
        run, _ = self.runner("false\n")
        self.assertEqual(check.signature(url + ".git", self.SHA, runner=run), "unverified")
        run, _ = self.runner(exc=FileNotFoundError())
        self.assertEqual(check.signature(url, self.SHA, runner=run), "not checked")
        run, _ = self.runner(exc=subprocess.TimeoutExpired("gh", 30))
        self.assertEqual(check.signature(url, self.SHA, runner=run), "not checked")
        run, _ = self.runner("", returncode=1)
        self.assertEqual(check.signature(url, self.SHA, runner=run), "not checked")
        run, calls = self.runner("true\n")
        for other in ("https://gitlab.com/owner/repo", "https://github.com/owner", "https://github.com/o/r/x",
                      "https://github.com.evil.example/o/r"):
            self.assertEqual(check.signature(other, self.SHA, runner=run), "not checked")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
