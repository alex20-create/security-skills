import hashlib
import os
import unittest

import helpers
from helpers import tmpdir, write_files

import verify
from verify import VerifyError, VerifyResult

FILES = {"SKILL.md": b"skill\n", "scripts/a.py": b"a\n", "references/b.md": b"b\n"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


MANIFEST = {k: sha(v) for k, v in FILES.items()}


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir(self) / "installed"
        write_files(self.root, FILES)

    def test_hash_tree(self):
        self.assertEqual(verify.hash_tree(self.root, []), MANIFEST)

    def test_ok(self):
        r = verify.verify_tree(self.root, MANIFEST, [])
        self.assertEqual(r, VerifyResult([], [], []))
        self.assertTrue(r.ok)

    def test_modified(self):
        (self.root / "scripts/a.py").write_bytes(b"changed\n")
        r = verify.verify_tree(self.root, MANIFEST, [])
        self.assertEqual(r.modified, ["scripts/a.py"])
        self.assertFalse(r.ok)

    def test_missing(self):
        (self.root / "references/b.md").unlink()
        r = verify.verify_tree(self.root, MANIFEST, [])
        self.assertEqual((r.missing, r.ok), (["references/b.md"], False))

    def test_missing_root(self):
        r = verify.verify_tree(self.root.parent / "nowhere", MANIFEST, [])
        self.assertEqual(r.missing, sorted(MANIFEST))

    def test_extra(self):
        write_files(self.root, {"scripts/new.py": b"n\n"})
        r = verify.verify_tree(self.root, MANIFEST, [])
        self.assertEqual((r.extra, r.ok), (["scripts/new.py"], False))

    def test_case_only_rename_is_reported(self):
        os.replace(self.root / "SKILL.md", self.root / "tmp-name")
        os.replace(self.root / "tmp-name", self.root / "skill.md")
        r = verify.verify_tree(self.root, MANIFEST, [])
        self.assertEqual((r.missing, r.extra), (["SKILL.md"], ["skill.md"]))

    def test_ignored_system_files(self):
        write_files(self.root, {"desktop.ini": b"x", "scripts/desktop.ini": b"x", "Thumbs.db": b"x",
                                ".DS_Store": b"x", "local.log": b"x", ".git/config": b"x"})
        self.assertTrue(verify.verify_tree(self.root, MANIFEST, ["*.log"]).ok)

    def test_bytecode_reported_not_hashed(self):
        write_files(self.root, {"scripts/__pycache__/x.cpython-312.pyc": b"x", "old.pyc": b"y",
                                ".git/objects/z.pyc": b"z"})
        r = verify.verify_tree(self.root, MANIFEST, [])
        # A loose .pyc is not a cache folder, so it is also an extra file.
        self.assertEqual((r.modified, r.missing, r.extra), ([], [], ["old.pyc"]))
        self.assertEqual(r.bytecode, ["old.pyc", "scripts/__pycache__/x.cpython-312.pyc"])
        self.assertFalse(r.ok)

    def test_folder_pattern_matches_folders_only(self):
        write_files(self.root, {"cache/x": b"x", "notes/cache": b"file named cache\n"})
        r = verify.verify_tree(self.root, MANIFEST, ["cache/"])
        self.assertEqual(r.extra, ["notes/cache"])

    def test_sorted_output(self):
        for name in ("z.md", "a.md", "m/b.md"):
            write_files(self.root, {name: b"x"})
        for name in ("scripts/a.py", "SKILL.md"):
            (self.root / name).write_bytes(b"changed")
        r = verify.verify_tree(self.root, {**MANIFEST, "zz.md": sha(b"x"), "aa.md": sha(b"x")}, [])
        self.assertEqual(r.extra, ["a.md", "m/b.md", "z.md"])
        self.assertEqual(r.modified, ["SKILL.md", "scripts/a.py"])
        self.assertEqual(r.missing, ["aa.md", "zz.md"])

    def test_link_refused(self):
        try:
            os.symlink(self.root / "SKILL.md", self.root / "link.md")
        except (OSError, NotImplementedError):
            self.skipTest("this system cannot create symlinks without extra rights")
        with self.assertRaises(VerifyError) as cm:
            verify.verify_tree(self.root, MANIFEST, [])
        self.assertIn("link.md", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
