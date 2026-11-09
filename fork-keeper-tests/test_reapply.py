import unittest
from pathlib import Path

import helpers
from helpers import git, make_repo, tmpdir, write_files, SHA_B

import patches
import reapply
from patches import Dep3
from reapply import PatchResult, ReapplyError

LINES = [b"line %d\n" % i for i in range(1, 11)]


def skill(**changes):
    """SKILL.md as ten numbered lines, with line N replaced by changes['lN']."""
    out = list(LINES)
    for key, value in changes.items():
        out[int(key[1:]) - 1] = value
    return b"".join(out)


OLD = {"SKILL.md": skill(), "scripts/a.py": b"print('a')\n", "README.md": b"readme\n"}
COPY1 = {**OLD, "SKILL.md": skill(l5=b"line 5 patched\n")}
COPY2 = {**COPY1, "scripts/a.py": b"print('a, patched')\n"}


def read_tree(root):
    root = Path(root)
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(root).parts}


class ReapplyTests(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir(self)
        self.old = self.root / "old"
        write_files(self.old, OLD)
        c1, c2 = self.root / "c1", self.root / "c2"
        write_files(c1, COPY1)
        write_files(c2, COPY2)
        self.series = []
        for name, a, b in (("01-skill.patch", self.old, c1), ("02-script.patch", c1, c2)):
            diff = patches.generate(a, b, self.root / ("gen-" + name), [])
            h = Dep3(description="Patch " + name, applies_to="upstream@" + SHA_B, last_update="2026-11-09")
            path = self.root / "patches" / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(patches.render(h, diff))
            self.series.append(path)
        self.work = self.root / "work"

    def new_base(self, files):
        new = self.root / "new"
        write_files(new, {k: v for k, v in {**OLD, **files}.items() if v is not None})
        return new

    def statuses(self, results):
        return [(r.name, r.status) for r in results]

    def test_clean(self):
        new = self.new_base({"README.md": b"readme 2\n"})
        got = reapply.start(self.old, new, self.series, self.work)
        self.assertEqual(self.statuses(got), [("01-skill.patch", "clean"), ("02-script.patch", "clean")])
        tree = read_tree(reapply.result_tree(self.work))
        self.assertEqual(tree, {**COPY2, "README.md": b"readme 2\n"})

    def test_offset_is_clean(self):
        new = self.new_base({"SKILL.md": b"new first line\n" + skill()})
        got = reapply.start(self.old, new, self.series, self.work)
        self.assertEqual(got[0].status, "clean")

    def test_merged(self):
        new = self.new_base({"SKILL.md": skill(l7=b"line 7 upstream\n")})
        got = reapply.start(self.old, new, self.series, self.work)
        self.assertEqual(self.statuses(got), [("01-skill.patch", "merged"), ("02-script.patch", "clean")])
        self.assertEqual(read_tree(reapply.result_tree(self.work))["SKILL.md"],
                         skill(l5=b"line 5 patched\n", l7=b"line 7 upstream\n"))

    def test_conflict_reported(self):
        new = self.new_base({"SKILL.md": skill(l5=b"line 5 upstream\n")})
        got = reapply.start(self.old, new, self.series, self.work)
        self.assertEqual(got, [PatchResult("01-skill.patch", "conflict", [("SKILL.md", 5)]),
                               PatchResult("02-script.patch", "pending", [])])
        self.assertIn(b"<<<<<<< ", (self.work / "SKILL.md").read_bytes())
        with self.assertRaises(ReapplyError):
            reapply.result_tree(self.work)
        with self.assertRaises(ReapplyError):
            reapply.per_patch_diffs(self.work)

    def test_upstream_deleted_patched_file(self):
        new = self.new_base({"SKILL.md": None})
        got = reapply.start(self.old, new, self.series, self.work)
        self.assertEqual(self.statuses(got), [("01-skill.patch", "conflict"), ("02-script.patch", "pending")])
        self.assertEqual(got[0].conflicts, [("SKILL.md", 0)])
        self.assertTrue(got[0].detail)

    def test_both_sides_add_same_file(self):
        c3 = self.root / "c3"
        write_files(c3, {**OLD, "extra.md": b"ours\n"})
        diff = patches.generate(self.old, c3, self.root / "gen-extra", [])
        h = Dep3(description="Add extra", applies_to="upstream@" + SHA_B, last_update="2026-11-09")
        p = self.root / "patches" / "03-extra.patch"
        p.write_bytes(patches.render(h, diff))
        new = self.new_base({"extra.md": b"theirs\n"})
        got = reapply.start(self.old, new, [p], self.work)
        self.assertEqual(got[0].status, "conflict")
        self.assertEqual([c[0] for c in got[0].conflicts], ["extra.md"])

    def test_resume(self):
        new = self.new_base({"SKILL.md": skill(l5=b"line 5 upstream\n")})
        reapply.start(self.old, new, self.series, self.work)
        resolved = skill(l5=b"line 5 upstream, patched\n")
        (self.work / "SKILL.md").write_bytes(resolved)
        got = reapply.resume(self.work, self.series)
        self.assertEqual(self.statuses(got), [("01-skill.patch", "merged"), ("02-script.patch", "clean")])
        tree = read_tree(reapply.result_tree(self.work))
        self.assertEqual(tree["SKILL.md"], resolved)
        self.assertEqual(tree["scripts/a.py"], COPY2["scripts/a.py"])

    def test_resume_with_markers_left(self):
        new = self.new_base({"SKILL.md": skill(l5=b"line 5 upstream\n")})
        reapply.start(self.old, new, self.series, self.work)
        head = git(self.work, "rev-parse", "HEAD")
        got = reapply.resume(self.work, self.series)
        self.assertEqual(self.statuses(got), [("01-skill.patch", "conflict"), ("02-script.patch", "pending")])
        self.assertEqual(got[0].conflicts, [("SKILL.md", 5)])
        self.assertEqual(git(self.work, "rev-parse", "HEAD"), head)

    def test_resume_refuses_other_series(self):
        new = self.new_base({"SKILL.md": skill(l5=b"line 5 upstream\n")})
        reapply.start(self.old, new, self.series, self.work)
        with self.assertRaises(ReapplyError):
            reapply.resume(self.work, self.series[:1])

    def test_start_refuses_used_work_folder(self):
        self.work.mkdir()
        (self.work / "x").write_bytes(b"x")
        with self.assertRaises(ReapplyError):
            reapply.start(self.old, self.new_base({}), self.series, self.work)

    def test_per_patch_diffs_apply_on_new_base(self):
        new = self.new_base({"SKILL.md": skill(l7=b"line 7 upstream\n"), "README.md": b"readme 2\n"})
        reapply.start(self.old, new, self.series, self.work)
        diffs = reapply.per_patch_diffs(self.work)
        self.assertEqual(len(diffs), 2)
        target = self.root / "target"
        make_repo(target, read_tree(new), "new base")
        for d in diffs:
            git(target, "apply", input=d)
        self.assertEqual(read_tree(target), read_tree(reapply.result_tree(self.work)))

    def test_progress_never_in_result(self):
        reapply.start(self.old, self.new_base({}), self.series, self.work)
        names = list(read_tree(reapply.result_tree(self.work))) + git(self.work, "ls-files").decode().split()
        self.assertFalse([n for n in names if "progress" in n], names)

    def test_work_repo_is_offline(self):
        reapply.start(self.old, self.new_base({}), self.series, self.work)
        self.assertTrue(reapply._git(self.work).offline)

    def test_gitignore_in_new_base_hides_nothing(self):
        new = self.new_base({".gitignore": b"scripts/\n"})
        got = reapply.start(self.old, new, self.series, self.work)
        self.assertEqual([r.status for r in got], ["clean", "clean"])
        self.assertEqual(read_tree(reapply.result_tree(self.work))["scripts/a.py"], COPY2["scripts/a.py"])

    def test_conflict_marker_size_attribute_ignored(self):
        new = self.new_base({".gitattributes": b"* conflict-marker-size=9\n",
                             "SKILL.md": skill(l5=b"line 5 upstream\n")})
        got = reapply.start(self.old, new, self.series, self.work)
        self.assertEqual(got[0].conflicts, [("SKILL.md", 5)])
        again = reapply.resume(self.work, self.series)
        self.assertEqual(again[0].status, "conflict")

    def test_upstream_marker_lines_do_not_block_resume(self):
        new = self.new_base({"README.md": b"<<<<<<< this line is upstream text\n",
                             "SKILL.md": skill(l5=b"line 5 upstream\n")})
        reapply.start(self.old, new, self.series, self.work)
        (self.work / "SKILL.md").write_bytes(skill(l5=b"line 5 resolved\n"))
        got = reapply.resume(self.work, self.series)
        self.assertEqual([r.status for r in got], ["merged", "clean"])

    def test_resume_file_level_clash_needs_a_change(self):
        new = self.new_base({"SKILL.md": None})
        reapply.start(self.old, new, self.series, self.work)
        got = reapply.resume(self.work, self.series)
        self.assertEqual(got[0].status, "conflict")
        (self.work / "SKILL.md").write_bytes(COPY1["SKILL.md"])
        got = reapply.resume(self.work, self.series)
        self.assertEqual([r.status for r in got], ["merged", "clean"])
        self.assertEqual(read_tree(reapply.result_tree(self.work))["SKILL.md"], COPY1["SKILL.md"])

    def test_resume_can_accept_an_empty_patch(self):
        new = self.new_base({"SKILL.md": None})
        reapply.start(self.old, new, self.series, self.work)
        got = reapply.resume(self.work, self.series, accept_empty=True)
        self.assertEqual([r.status for r in got], ["merged", "clean"])
        self.assertEqual(reapply.per_patch_diffs(self.work)[0], b"")

    def test_system_files_never_reach_a_resumed_patch(self):
        new = self.new_base({"SKILL.md": skill(l5=b"line 5 upstream\n")})
        reapply.start(self.old, new, self.series, self.work)
        (self.work / "SKILL.md").write_bytes(skill(l5=b"line 5 resolved\n"))
        (self.work / "Desktop.ini").write_bytes(b"[.ShellClassInfo]\r\n")
        reapply.resume(self.work, self.series)
        self.assertNotIn(b"esktop.ini", b"".join(reapply.per_patch_diffs(self.work)))

    def test_per_patch_diffs_with_renames_deletions_and_binaries(self):
        c3 = self.root / "c3"
        files = {k: v for k, v in COPY1.items() if k != "README.md"}
        files["scripts/b.py"] = files.pop("scripts/a.py")
        files["assets/x.bin"] = bytes(range(256))
        write_files(c3, files)
        diff = patches.generate(self.old, c3, self.root / "gen-rich", [])
        p = self.root / "patches" / "01-rich.patch"
        p.write_bytes(patches.render(Dep3("Rich", "upstream@" + SHA_B, "2026-11-09"), diff))
        new = self.new_base({"SKILL.md": skill(l7=b"line 7 upstream\n")})
        got = reapply.start(self.old, new, [p], self.work)
        self.assertEqual(got[0].status, "merged")
        target = self.root / "target"
        make_repo(target, read_tree(new), "new base")
        for d in reapply.per_patch_diffs(self.work):
            git(target, "apply", input=d)
        self.assertEqual(read_tree(target), read_tree(reapply.result_tree(self.work)))


if __name__ == "__main__":
    unittest.main()
