import contextlib
import hashlib
import io
import json
import unittest
from pathlib import Path

import helpers
from helpers import git, make_repo, tmpdir, write_files

import fk
import registry

LINES = [b"line %d\n" % i for i in range(1, 11)]
SKILL = b"".join(LINES)
PATCHED = SKILL.replace(b"line 5\n", b"line 5 patched\n")


def tree_bytes(root):
    root = Path(root)
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and ".git" not in p.relative_to(root).parts}


class FkTests(unittest.TestCase):
    def setUp(self):
        fk._set_allow_file(True)
        fk._set_today("2026-11-20")
        self.addCleanup(fk._set_allow_file, False)
        self.addCleanup(fk._set_today, None)
        self.tmp = tmpdir(self)
        self.up = self.tmp / "upstream"
        self.pin = make_repo(self.up, {
            "skills/sample/SKILL.md": SKILL,
            "skills/sample/scripts/a.py": b"print('a')\n",
            "LICENSE": b"MIT\n",
            "tools/t.py": b"tool\n",
        }, "first")
        git(self.up, "config", "uploadpack.allowAnySHA1InWant", "true")
        git(self.up, "config", "uploadpack.allowFilter", "true")
        self.copy = self.tmp / "copy"
        self.install = self.tmp / "install"
        for target in (self.copy, self.install):
            write_files(target, {"SKILL.md": PATCHED, "scripts/a.py": b"print('a')\n", "LICENSE": b"MIT\n"})
        write_files(self.copy, {"desktop.ini": b"[.ShellClassInfo]\r\n"})
        self.reg = self.tmp / "registry"
        self.audit = self.tmp / "audit.md"
        self.audit.write_text("- [x] `sample` - example @ `{}` USE WITH CAUTION\n".format(self.pin[:7]))
        write_files(self.reg, {".gitattributes": b"copies/** -text\n"})
        (self.reg / "fork-keeper.json").write_text(json.dumps({"auditRecords": str(self.audit)}))
        self.scratch = self.tmp / "scratch"
        self.scratch.mkdir()
        self.draft = self.tmp / "draft.json"
        self.write_draft()

    def write_draft(self, url=None, pin=None):
        pin = pin or self.pin[:7]
        self.draft.write_text(json.dumps({
            "name": "sample", "kind": "skill", "copy": str(self.copy), "installs": [str(self.install)],
            "origins": [{
                "id": "upstream", "url": url or self.up.resolve().as_uri(), "pin": pin, "track": "main",
                "license": "MIT", "map": [{"from": "skills/sample", "to": "."}, {"from": "LICENSE", "to": "LICENSE"}],
                "audit": {"commit": pin, "verdict": "USE WITH CAUTION", "date": "2026-11-03", "record": "sample"},
                "lastChecked": "2026-11-03"}]}))

    def fk(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = fk.main([args[0], "--registry", str(self.reg), *args[1:]])
        self.last = out.getvalue()
        return code

    def register(self):
        code = self.fk("register", "--draft", str(self.draft), "--scratch", str(self.scratch),
                       "--summary", "Patch line five", "--yes")
        self.assertEqual(code, 0, self.last)

    def scratch_args(self):
        return ("--copy", "sample", "--scratch", str(self.scratch))

    def upstream_change(self, files):
        return make_repo(self.up, files, "upstream change")

    def checked_with_verdict(self, files, verdict="USE WITH CAUTION"):
        self.register()
        new = self.upstream_change(files)
        self.assertEqual(self.fk("check", *self.scratch_args()), 1, self.last)
        self.assertEqual(self.fk("record-verdict", *self.scratch_args(), "--verdict", verdict,
                                 "--record", "sample", "--date", "2026-11-20"), 0, self.last)
        return new

    def entry(self):
        return registry.load_copy(self.reg / "copies" / "sample")

    # -- register ---------------------------------------------------------

    def test_register_dry_run_writes_nothing(self):
        code = self.fk("register", "--draft", str(self.draft), "--scratch", str(self.scratch), "--summary", "Patch line five")
        self.assertEqual(code, 3, self.last)
        self.assertFalse((self.reg / "copies").exists())
        self.assertIn("0001-patch-line-five.patch", self.last)

    def test_register_writes_entry(self):
        self.register()
        folder = self.reg / "copies" / "sample"
        self.assertEqual([x.name for x in registry.read_series(folder)], ["0001-patch-line-five.patch"])
        c = self.entry()
        self.assertEqual(c.origins[0].pin, self.pin)
        self.assertEqual(c.origins[0].audit.commit, self.pin)
        patch = (folder / "patches" / "0001-patch-line-five.patch").read_bytes()
        self.assertIn(b"Applies-To: upstream@" + self.pin.encode(), patch)
        self.assertIn(b"+line 5 patched", patch)
        self.assertNotIn(b"desktop.ini", patch)
        manifest = registry.load_manifest(folder)
        self.assertEqual(sorted(manifest), ["LICENSE", "SKILL.md", "scripts/a.py"])
        self.assertEqual(manifest["SKILL.md"], hashlib.sha256(PATCHED).hexdigest())

    def test_register_refuses_existing_entry(self):
        self.register()
        code = self.fk("register", "--draft", str(self.draft), "--scratch", str(self.scratch), "--summary", "x", "--yes")
        self.assertEqual(code, 1)

    def test_register_stops_on_crlf(self):
        (self.copy / "scripts" / "a.py").write_bytes(b"print('a')\r\n")
        code = self.fk("register", "--draft", str(self.draft), "--scratch", str(self.scratch), "--summary", "x", "--yes")
        self.assertEqual(code, 1, self.last)
        self.assertIn("line endings", self.last)
        self.assertIn("scripts/a.py", self.last)
        self.assertFalse((self.reg / "copies").exists())

    def test_register_needs_audit_record(self):
        self.audit.write_text("nothing about it\n")
        code = self.fk("register", "--draft", str(self.draft), "--scratch", str(self.scratch), "--summary", "x", "--yes")
        self.assertEqual(code, 1, self.last)
        self.assertIn("audit record", self.last)
        self.assertFalse((self.reg / "copies").exists())

    def test_register_rejects_unknown_short_pin(self):
        self.write_draft(pin="abcdef1")
        code = self.fk("register", "--draft", str(self.draft), "--scratch", str(self.scratch), "--summary", "x", "--yes")
        self.assertEqual(code, 2, self.last)

    def test_cli_rejects_file_url(self):
        fk._set_allow_file(False)
        code = self.fk("register", "--draft", str(self.draft), "--scratch", str(self.scratch), "--summary", "x", "--yes")
        self.assertEqual(code, 2, self.last)
        self.assertFalse((self.reg / "copies").exists())

    def test_git_version_floor(self):
        real = fk._git_version
        fk._git_version = lambda: (2, 31, 0)
        self.addCleanup(setattr, fk, "_git_version", real)
        self.assertEqual(self.fk("status"), 2)
        self.assertIn("git 2.32 or later is required", self.last)

    # -- status, mark-checked, verify, inventory ---------------------------

    def test_status_flags_missing_audit(self):
        self.register()
        self.assertEqual(self.fk("status"), 0, self.last)
        self.assertIn("audited", self.last)
        self.assertIn(self.pin[:12], self.last)
        self.audit.write_text("")
        self.assertEqual(self.fk("status"), 1)
        self.assertIn("NO AUDIT RECORD", self.last)

    def test_status_older_than(self):
        self.register()
        self.fk("status", "--older-than", "30")
        self.assertNotIn("sample", self.last)
        fk._set_today("2026-12-31")
        self.fk("status", "--older-than", "30")
        self.assertIn("sample", self.last)
        self.assertIn("58 days", self.last)

    def test_mark_checked(self):
        self.register()
        before = (self.reg / "copies" / "sample" / "copy.json").read_bytes()
        self.assertEqual(self.fk("mark-checked", "--copy", "sample"), 3)
        self.assertEqual((self.reg / "copies" / "sample" / "copy.json").read_bytes(), before)
        self.assertEqual(self.fk("mark-checked", "--copy", "sample", "--yes"), 0, self.last)
        self.assertEqual(self.entry().origins[0].last_checked, "2026-11-20")

    def test_verify_detects_tampering(self):
        self.register()
        self.assertEqual(self.fk("verify"), 0, self.last)
        (self.install / "scripts" / "a.py").write_bytes(b"tampered\n")
        self.assertEqual(self.fk("verify", "--copy", "sample"), 1)
        self.assertIn("scripts/a.py", self.last)

    def test_verify_rebuild(self):
        self.register()
        self.assertEqual(self.fk("verify", "--rebuild", "--scratch", str(self.scratch)), 0, self.last)

    def test_inventory_runs(self):
        home = self.tmp / "home"
        write_files(home / ".claude" / "skills" / "loose", {"SKILL.md": b"x\n"})
        self.assertEqual(self.fk("inventory", "--home", str(home)), 0, self.last)
        self.assertIn("loose", self.last)

    # -- the update flow ----------------------------------------------------

    def test_end_to_end_clean_update(self):
        self.register()
        new = self.upstream_change({"skills/sample/scripts/a.py": b"print('a2')\n"})
        self.assertEqual(self.fk("check", *self.scratch_args()), 1, self.last)
        self.assertIn("a.py", self.last)
        audit_dir = self.scratch / "fk-sample" / "audit"
        self.assertEqual((audit_dir / "skills/sample/scripts/a.py").read_bytes(), b"print('a2')\n")
        self.assertEqual(self.fk("reapply", *self.scratch_args()), 1)
        self.assertIn("verdict", self.last)
        self.assertEqual(self.fk("record-verdict", *self.scratch_args(), "--verdict", "USE WITH CAUTION",
                                 "--record", "sample", "--date", "2026-11-20"), 0, self.last)
        self.assertEqual(self.fk("reapply", *self.scratch_args()), 0, self.last)
        self.assertIn("clean", self.last)
        before = tree_bytes(self.copy)
        self.assertEqual(self.fk("install", *self.scratch_args()), 3, self.last)
        self.assertEqual(tree_bytes(self.copy), before)
        self.assertEqual(self.fk("install", *self.scratch_args(), "--yes"), 0, self.last)
        self.assertIn("git -C", self.last)
        for target in (self.copy, self.install):
            t = tree_bytes(target)
            self.assertEqual(t["scripts/a.py"], b"print('a2')\n")
            self.assertEqual(t["SKILL.md"], PATCHED)
        self.assertIn("desktop.ini", tree_bytes(self.copy))
        self.assertEqual(self.fk("verify"), 0, self.last)
        o = self.entry().origins[0]
        self.assertEqual((o.pin, o.audit.commit, o.audit.verdict, o.last_checked),
                         (new, new, "USE WITH CAUTION", "2026-11-20"))
        patch = (self.reg / "copies" / "sample" / "patches" / "0001-patch-line-five.patch").read_bytes()
        self.assertIn(b"Applies-To: upstream@" + new.encode(), patch)
        self.assertIn(b"Last-Update: 2026-11-20", patch)

    def test_check_escapes_upstream_text(self):
        self.register()
        make_repo(self.up, {"skills/sample/scripts/a.py": b"x\n"}, "evil \x1b[2J\x1b]0;title\x07 subject")
        self.fk("check", *self.scratch_args())
        self.assertNotIn("\x1b", self.last)
        self.assertNotIn("\x07", self.last)
        self.assertIn("subject", self.last)

    def test_reapply_continue_accept_empty(self):
        self.checked_with_verdict({"skills/sample/SKILL.md": None})
        self.assertEqual(self.fk("reapply", *self.scratch_args()), 1, self.last)
        self.assertEqual(self.fk("reapply", *self.scratch_args(), "--continue"), 1, self.last)
        self.assertEqual(self.fk("reapply", *self.scratch_args(), "--continue", "--accept-empty"), 0, self.last)

    def test_check_without_mapped_changes(self):
        self.register()
        self.upstream_change({"tools/t.py": b"tool 2\n"})
        self.assertEqual(self.fk("check", *self.scratch_args()), 0, self.last)
        self.assertIn("mark-checked", self.last)

    def test_check_reports_alarm_first(self):
        self.register()
        self.upstream_change({"LICENSE": b"GPL\n"})
        self.assertEqual(self.fk("check", *self.scratch_args()), 1)
        first = self.last.lstrip().splitlines()[0]
        self.assertTrue(first.startswith("ALARM license"), self.last[:200])

    def test_do_not_install_stops(self):
        self.checked_with_verdict({"skills/sample/scripts/a.py": b"x\n"}, verdict="DO NOT INSTALL")
        self.assertEqual(self.fk("reapply", *self.scratch_args()), 1)
        self.assertIn("DO NOT INSTALL", self.last)
        self.assertEqual(self.fk("install", *self.scratch_args(), "--yes"), 1)

    def test_install_refuses_conflict(self):
        self.checked_with_verdict({"skills/sample/SKILL.md": SKILL.replace(b"line 5\n", b"line 5 upstream\n")})
        self.assertEqual(self.fk("reapply", *self.scratch_args()), 1, self.last)
        self.assertIn("conflict", self.last)
        self.assertIn("SKILL.md:5", self.last)
        self.assertEqual(self.fk("install", *self.scratch_args(), "--yes"), 1)

    def test_install_restores_on_failure(self):
        self.checked_with_verdict({"skills/sample/scripts/a.py": b"print('a2')\n"})
        self.assertEqual(self.fk("reapply", *self.scratch_args()), 0, self.last)
        before_copy, before_install = tree_bytes(self.copy), tree_bytes(self.install)
        before_entry = tree_bytes(self.reg / "copies")
        real = fk._write_file

        def failing(path, data):
            if Path(path).name == "a.py" and Path(path).is_relative_to(self.install):
                raise OSError("disk full (simulated)")
            real(path, data)
        fk._write_file = failing
        self.addCleanup(setattr, fk, "_write_file", real)
        self.assertEqual(self.fk("install", *self.scratch_args(), "--yes"), 1)
        self.assertIn("restored", self.last)
        self.assertEqual(tree_bytes(self.copy), before_copy)
        self.assertEqual(tree_bytes(self.install), before_install)
        self.assertEqual(tree_bytes(self.reg / "copies"), before_entry)


if __name__ == "__main__":
    unittest.main()
