import unittest
from pathlib import Path

import helpers
from helpers import git, make_repo, tmpdir

from gitsafe import SafeGit
from registry import MapEntry
import tree
from tree import TreeError

# Built at runtime so the folder name never appears literally in the repo.
ODD_FOLDER = "dir with spaces/Dise\u00f1o"


def add_special(repo, mode, target, path):
    """Stage a symlink (120000) or gitlink (160000) entry and commit it."""
    if mode == "120000":
        obj = git(repo, "hash-object", "-w", "--stdin", input=target.encode()).decode().strip()
    else:
        obj = target
    git(repo, "update-index", "--add", "--cacheinfo", "{},{},{}".format(mode, obj, path))
    git(repo, "commit", "-q", "-m", "special")
    return git(repo, "rev-parse", "HEAD").decode().strip()


class TreeTests(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir(self) / ODD_FOLDER
        self.up = self.root / "upstream"
        self.files = {
            "skills/x/SKILL.md": b"---\nname: x\n---\nbody\n",
            "skills/x/scripts/a.py": b"print('a')\n",
            "LICENSE": b"MIT\n",
            "tools/t.py": b"tool\n",
            "tests/t.py": b"test\n",
        }
        self.sha = make_repo(self.up, self.files, "first")
        self.git = SafeGit(self.up)
        self.dest = self.root / "base"
        self.entries = [MapEntry("skills/x", "."), MapEntry("LICENSE", "LICENSE")]

    def test_folder_and_file_maps(self):
        tree.build_base([("upstream", self.git, self.sha, self.entries)], self.dest)
        self.assertEqual((self.dest / "SKILL.md").read_bytes(), self.files["skills/x/SKILL.md"])
        self.assertEqual((self.dest / "scripts" / "a.py").read_bytes(), self.files["skills/x/scripts/a.py"])
        self.assertEqual((self.dest / "LICENSE").read_bytes(), b"MIT\n")

    def test_returns_destination_to_source(self):
        got = tree.build_base([("upstream", self.git, self.sha, self.entries)], self.dest)
        self.assertEqual(got, {
            "SKILL.md": "skills/x/SKILL.md",
            "scripts/a.py": "skills/x/scripts/a.py",
            "LICENSE": "LICENSE",
        })

    def test_folder_map_to_subfolder(self):
        tree.build_base([("upstream", self.git, self.sha, [MapEntry("skills/x/scripts", "bin")])], self.dest)
        self.assertEqual((self.dest / "bin" / "a.py").read_bytes(), b"print('a')\n")

    def test_two_origins(self):
        other = self.root / "guidelines"
        osha = make_repo(other, {"command.md": b"rules\n", "README.md": b"r\n"}, "g")
        parts = [("upstream", self.git, self.sha, self.entries),
                 ("guidelines", SafeGit(other), osha, [MapEntry("command.md", "reference/guidelines.md")])]
        tree.build_base(parts, self.dest)
        self.assertEqual((self.dest / "reference" / "guidelines.md").read_bytes(), b"rules\n")
        self.assertTrue((self.dest / "SKILL.md").exists())
        self.assertFalse((self.dest / "README.md").exists())

    def test_two_origins_same_file_refused(self):
        other = self.root / "other"
        osha = make_repo(other, {"SKILL.md": b"other\n"}, "o")
        parts = [("upstream", self.git, self.sha, self.entries), ("other", SafeGit(other), osha, [MapEntry("SKILL.md", "SKILL.md")])]
        with self.assertRaises(TreeError) as cm:
            tree.build_base(parts, self.dest)
        self.assertIn("SKILL.md", str(cm.exception))
        self.assertIn("upstream", str(cm.exception))
        self.assertIn("other", str(cm.exception))

    def test_case_only_clash_refused(self):
        other = self.root / "other"
        osha = make_repo(other, {"skill.md": b"other\n"}, "o")
        parts = [("upstream", self.git, self.sha, self.entries), ("other", SafeGit(other), osha, [MapEntry("skill.md", "skill.md")])]
        with self.assertRaises(TreeError):
            tree.build_base(parts, self.dest)

    def test_unmapped_paths_excluded(self):
        tree.build_base([("upstream", self.git, self.sha, self.entries)], self.dest)
        self.assertFalse((self.dest / "tools").exists())
        self.assertFalse((self.dest / "tests").exists())

    def test_symlink_refused(self):
        sha = add_special(self.up, "120000", "/etc/passwd", "skills/x/link")
        with self.assertRaises(TreeError) as cm:
            tree.build_base([("upstream", SafeGit(self.up), sha, self.entries)], self.dest)
        self.assertIn("skills/x/link", str(cm.exception))
        self.assertIn("symlink", str(cm.exception))

    def test_submodule_refused(self):
        sha = add_special(self.up, "160000", self.sha, "skills/x/sub")
        with self.assertRaises(TreeError) as cm:
            tree.build_base([("upstream", SafeGit(self.up), sha, self.entries)], self.dest)
        self.assertIn("skills/x/sub", str(cm.exception))
        self.assertIn("submodule", str(cm.exception))

    def test_unmapped_symlink_is_ignored(self):
        sha = add_special(self.up, "120000", "/etc/passwd", "tools/link")
        tree.build_base([("upstream", SafeGit(self.up), sha, self.entries)], self.dest)
        self.assertTrue((self.dest / "SKILL.md").exists())

    def test_missing_source_path(self):
        with self.assertRaises(TreeError) as cm:
            tree.build_base([("upstream", self.git, self.sha, [MapEntry("skills/nope", ".")])], self.dest)
        self.assertIn("skills/nope", str(cm.exception))
        self.assertIn(self.sha[:12], str(cm.exception))

    def test_destination_escape_refused(self):
        with self.assertRaises(TreeError):
            tree.build_base([("upstream", self.git, self.sha, [MapEntry("LICENSE", "../outside")])], self.dest)
        self.assertFalse((self.root / "outside").exists())

    def test_never_overwrites_a_file(self):
        (self.dest).mkdir(parents=True)
        (self.dest / "LICENSE").write_bytes(b"already here\n")
        with self.assertRaises(TreeError):
            tree.build_base([("upstream", self.git, self.sha, self.entries)], self.dest)
        self.assertEqual((self.dest / "LICENSE").read_bytes(), b"already here\n")

    def test_unsafe_component_names(self):
        for bad in ("a\\b", "..", "x/../y", ".git/config", "GIT~1/hooks", "x:y", "trail.", "trail ", "CON", "nul.txt", "com1"):
            with self.subTest(path=bad):
                with self.assertRaises(TreeError):
                    tree.check_path_safe(bad, "test")
        self.assertEqual(tree.check_path_safe("ok/name.md", "test"), "ok/name.md")

    def test_c1_and_bidi_controls_refused(self):
        for bad in ("a\u0085b.md", "a\u009bb.md", "a\x7fb.md", "evil‮gpj.md", "x⁦y.md", "x‏y.md"):
            with self.subTest(bad=bad):
                with self.assertRaises(TreeError):
                    tree.check_path_safe(bad, "test")
        self.assertEqual(tree.check_path_safe("café/ñ.md", "test"), "café/ñ.md")

    def test_prefix_is_not_a_folder_match(self):
        make_repo(self.up, {"skills/xy/other.md": b"o\n"}, "sibling")
        sha = git(self.up, "rev-parse", "HEAD").decode().strip()
        got = tree.mapped_files(SafeGit(self.up), sha, [MapEntry("skills/x", ".")])
        self.assertNotIn("other.md", got)


if __name__ == "__main__":
    unittest.main()
