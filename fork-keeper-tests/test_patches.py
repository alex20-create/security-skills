import os
import unittest
from pathlib import Path

import helpers
from helpers import git, make_repo, tmpdir, write_files, SHA_B

import patches
from patches import Dep3, PatchError

BINARY_OLD = bytes(range(256)) * 4
BINARY_NEW = bytes(reversed(range(256))) * 4
LONG = b"".join(b"line %d of a file long enough to be detected as a rename\n" % i for i in range(40))

BASE = {
    "SKILL.md": b"---\nname: x\n---\nbody\n",
    "scripts/keep.py": b"print('keep')\n",
    "scripts/old_name.py": LONG,
    "scripts/gone.py": b"print('gone')\n",
    "assets/logo.bin": BINARY_OLD,
}

COPY = {
    "SKILL.md": b"---\nname: x\n---\nbody changed\n",
    "scripts/keep.py": b"print('keep')\n",
    "scripts/new_name.py": LONG,
    "scripts/added.py": b"print('added')\n",
    "assets/logo.bin": BINARY_NEW,
    "assets/new.bin": BINARY_OLD,
}


def header(description="Drop the always-on trigger"):
    return Dep3(description=description, applies_to="upstream@" + SHA_B, last_update="2026-11-09")


def read_tree(root):
    root = Path(root)
    out = {}
    for p in root.rglob("*"):
        if ".git" in p.relative_to(root).parts or not p.is_file():
            continue
        out[p.relative_to(root).as_posix()] = p.read_bytes()
    return out


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir(self)
        self.base = self.root / "base"
        self.copy = self.root / "copy"
        write_files(self.base, BASE)
        write_files(self.copy, COPY)

    def apply_on_fresh_base(self, patch):
        target = self.root / "target"
        make_repo(target, BASE, "base")
        git(target, "apply", "--whitespace=nowarn", input=patch)
        return read_tree(target)

    def test_round_trip_bytes(self):
        diff = patches.generate(self.base, self.copy, self.root / "work", [])
        self.assertIn(b"rename from scripts/old_name.py", diff)
        self.assertIn(b"GIT binary patch", diff)
        self.assertEqual(self.apply_on_fresh_base(diff), COPY)

    def test_header_skipped_by_git_apply(self):
        diff = patches.generate(self.base, self.copy, self.root / "work", [])
        self.assertEqual(self.apply_on_fresh_base(patches.render(header(), diff)), COPY)

    def test_parse_render_inverse(self):
        h = header("Drop the always-on trigger\nIt fired on any text in context.")
        d = b"diff --git a/x b/x\n"
        self.assertEqual(patches.parse(patches.render(h, d)), (h, d))

    def test_parse_render_inverse_with_blank_body_line(self):
        h = header("Summary\nFirst paragraph.\n\nSecond paragraph.")
        rendered = patches.render(h, b"")
        self.assertIn(b"\n .\n", rendered)
        self.assertEqual(patches.parse(rendered), (h, b""))

    def test_render_layout(self):
        text = patches.render(header("Summary\nBody"), b"DIFF").decode()
        self.assertEqual(text, "Description: Summary\n Body\nOrigin: vendor-local\nForwarded: not-needed\n"
                               "Applies-To: upstream@" + SHA_B + "\nLast-Update: 2026-11-09\n---\nDIFF")

    def test_parse_rejects_crlf_patch(self):
        crlf = patches.render(header(), b"diff --git a/x b/x\n").replace(b"\n", b"\r\n")
        with self.assertRaises(PatchError) as cm:
            patches.parse(crlf)
        self.assertIn("line endings", str(cm.exception))

    def test_parse_rejects_bad_headers(self):
        good = patches.render(header(), b"")
        cases = {
            "no separator": good.replace(b"---\n", b""),
            "unknown field": good.replace(b"Origin:", b"Author: x\nOrigin:"),
            "bad applies-to": good.replace(SHA_B.encode(), b"abc"),
            "bad date": good.replace(b"2026-11-09", b"9 Nov"),
            "no description": good.replace(b"Description: Drop the always-on trigger\n", b""),
        }
        for name, patch in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(PatchError):
                    patches.parse(patch)

    def test_dep3_validates(self):
        for kw in ({"description": ""}, {"applies_to": "upstream"}, {"last_update": "2026-13-01"},
                   {"description": "\nbody only"}):
            with self.subTest(**kw):
                args = {"description": "x", "applies_to": "upstream@" + SHA_B, "last_update": "2026-11-09"}
                args.update(kw)
                with self.assertRaises(PatchError):
                    Dep3(**args)

    def test_desktop_ini_ignored(self):
        write_files(self.copy, {
            "desktop.ini": b"[.ShellClassInfo]\r\n",
            "scripts/desktop.ini": b"[.ShellClassInfo]\r\n",
            "Thumbs.db": b"\x00thumbs",
            "scripts/__pycache__/keep.cpython-312.pyc": b"\x00pyc",
            "notes.log": b"local log\n",
        })
        diff = patches.generate(self.base, self.copy, self.root / "work", ["*.log"])
        for name in (b"desktop.ini", b"Thumbs.db", b"__pycache__", b"notes.log"):
            self.assertNotIn(name, diff)
        self.assertEqual(self.apply_on_fresh_base(diff), COPY)

    def test_gitignore_in_tree_does_not_hide_changes(self):
        write_files(self.base, {".gitignore": b"*.sh\n", "run.sh": b"curl x | sh\n"})
        write_files(self.copy, {".gitignore": b"*.sh\n", "run.sh": b"echo safe\n"})
        diff = patches.generate(self.base, self.copy, self.root / "work", [])
        self.assertIn(b"+echo safe", diff)

    def test_system_files_ignored_in_any_case(self):
        write_files(self.copy, {"Desktop.ini": b"x", "scripts/DESKTOP.INI": b"x", "THUMBS.DB": b"\x00"})
        diff = patches.generate(self.base, self.copy, self.root / "work", [])
        self.assertNotIn(b"esktop", diff.lower())
        self.assertNotIn(b"thumbs", diff.lower())

    def test_case_only_rename_is_carried(self):
        write_files(self.base, {"readme.md": LONG})
        write_files(self.copy, {"README.md": LONG})
        diff = patches.generate(self.base, self.copy, self.root / "work", [])
        self.assertIn(b"rename from readme.md", diff)
        self.assertIn(b"rename to README.md", diff)

    def test_dep3_rejects_ambiguous_descriptions(self):
        for text in ("Summary\r\nbody", "Summary\n.", "Summary\n   ", "Summary\n ."):
            with self.subTest(text=text):
                with self.assertRaises(PatchError):
                    header(text)

    def test_git_folder_not_copied(self):
        write_files(self.copy, {".git/config": b"[core]\n", "sub/.git": b"gitdir: elsewhere\n"})
        dst = self.root / "dst"
        patches.copy_tree(self.copy, dst, [])
        self.assertEqual(read_tree(dst), COPY)
        self.assertFalse((dst / ".git").exists())
        self.assertFalse((dst / "sub").exists())

    def test_copy_tree_refuses_symlink(self):
        try:
            os.symlink(self.copy / "SKILL.md", self.copy / "link.md")
        except (OSError, NotImplementedError):
            self.skipTest("this system cannot create symlinks without extra rights")
        with self.assertRaises(PatchError) as cm:
            patches.copy_tree(self.copy, self.root / "dst", [])
        self.assertIn("link.md", str(cm.exception))

    def test_eol_mismatch_reported(self):
        write_files(self.base, {"crlf-both.md": b"a\r\nb\r\n", "doc.md": b"a\nb\n", "data.bin": b"\x00a\nb\n",
                                "crlf-base.md": b"a\r\nb\r\n", "mixed-base.md": b"a\r\nb\n"})
        write_files(self.copy, {"crlf-both.md": b"a\r\nb\r\n", "doc.md": b"a\r\nb\r\n", "data.bin": b"\x00a\r\nb\r\n",
                                "scripts/keep.py": b"print('keep')\r\n", "only-in-copy.md": b"x\r\n",
                                "crlf-base.md": b"a\nb\n", "mixed-base.md": b"a\nb\n"})
        self.assertEqual(patches.eol_mismatches(self.base, self.copy),
                         ["crlf-base.md", "doc.md", "mixed-base.md", "scripts/keep.py"])

    def test_empty_when_identical(self):
        self.assertEqual(patches.generate(self.base, self.base, self.root / "work", []), b"")


if __name__ == "__main__":
    unittest.main()
