import hashlib
import json
import os
import unittest
from pathlib import Path

import helpers
from helpers import tmpdir, write_files

import inventory
from registry import Copy

SKILL = {"SKILL.md": b"---\nname: x\n---\nbody\n", "references/a.md": b"a\n"}
PLUGIN_SHA = "0123456789abcdef0123456789abcdef01234567"


def manifest_of(files):
    return {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir(self)
        self.home = self.tmp / "home"
        self.skills = self.home / ".claude" / "skills"
        self.skills.mkdir(parents=True)
        self.src = self.tmp / "src"
        self.config = {"auditRecords": None, "skillsRoots": ["~/.claude/skills"],
                       "ownSourceRoots": [str(self.src)], "managedSets": {"gstack": "gstack"}, "ignore": []}
        self.copies = []
        self.audit = ""

    # -- helpers ------------------------------------------------------------

    def skill(self, name, files=SKILL):
        write_files(self.skills / name, files)
        return self.skills / name

    def run_inventory(self):
        return inventory.run(self.config, self.copies, self.audit, self.home)

    def item(self, name, kind="skill"):
        found = [i for i in self.run_inventory() if i.name == name and i.kind == kind]
        self.assertEqual(len(found), 1, found)
        return found[0]

    def register(self, name, files=SKILL):
        folder = self.tmp / "registry" / "copies" / name
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(json.dumps({"generated": "2026-11-09", "files": manifest_of(files)}))
        self.copies.append(Copy(name=name, kind="skill", copy=self.tmp / "own" / name,
                                installs=[self.skills / name], origins=[], folder=folder))

    def plugins(self, entries):
        p = self.home / ".claude" / "plugins" / "installed_plugins.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"version": 2, "plugins": entries}))

    def plugin(self, key, sha=PLUGIN_SHA, mcp=None):
        path = self.home / ".claude" / "plugins" / "cache" / key.replace("@", "-")
        path.mkdir(parents=True)
        if mcp is not None:
            (path / ".mcp.json").write_text(json.dumps(mcp))
        return {key: [{"installPath": str(path), "version": "1.0.0", "gitCommitSha": sha, "scope": "user"}]}

    # -- skills -------------------------------------------------------------

    def test_linked_skill_is_amber(self):
        target = self.tmp / "elsewhere" / "linked"
        write_files(target, SKILL)
        try:
            os.symlink(target, self.skills / "linked", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this system cannot create symlinks without extra rights")
        i = self.item("linked")
        self.assertEqual(i.colour, "amber")
        self.assertIn("linked, not copied", i.reasons)

    @unittest.skipUnless(os.name == "nt", "junctions exist only on Windows")
    def test_junction_skill_is_amber(self):
        import _winapi
        target = self.tmp / "elsewhere" / "joined"
        write_files(target, SKILL)
        _winapi.CreateJunction(str(target), str(self.skills / "joined"))
        i = self.item("joined")
        self.assertEqual((i.colour, i.reasons[0]), ("amber", "linked, not copied"))

    def test_registered_copy_green(self):
        self.skill("kept")
        self.register("kept")
        self.assertEqual(self.item("kept").colour, "green")

    def test_registered_copy_modified_red(self):
        self.skill("kept")
        self.register("kept")
        (self.skills / "kept" / "SKILL.md").write_bytes(b"tampered\n")
        write_files(self.skills / "kept", {"new.md": b"n\n"})
        i = self.item("kept")
        self.assertEqual(i.colour, "red")
        self.assertIn("modified: SKILL.md", i.reasons)
        self.assertIn("extra: new.md", i.reasons)

    def test_registered_copy_ignores_system_files(self):
        self.skill("kept", {**SKILL, "desktop.ini": b"x"})
        self.register("kept")
        self.assertEqual(self.item("kept").colour, "green")

    def test_own_source_match_green(self):
        self.skill("mine")
        write_files(self.src / "Theme" / "repo" / "mine", {**SKILL, "evals/evals.json": b"{}"})
        i = self.item("mine")
        self.assertEqual(i.colour, "green")

    def test_own_source_differs_amber(self):
        self.skill("mine")
        write_files(self.src / "Theme" / "repo" / "mine", {**SKILL, "SKILL.md": b"newer\n"})
        i = self.item("mine")
        self.assertEqual(i.colour, "amber")
        self.assertIn("installed copy differs from its source", i.reasons)

    def test_own_source_ignores_git_and_node_modules(self):
        self.skill("mine")
        write_files(self.src / "repo" / "node_modules" / "mine", SKILL)
        write_files(self.src / "repo" / ".git" / "mine", SKILL)
        self.assertEqual(self.item("mine").colour, "red")

    def test_managed_set(self):
        self.skill("gstack-browse")
        self.assertEqual(self.item("gstack-browse").colour, "amber")
        self.audit = "- `gstack` 1.98 at abc1234, USE WITH CAUTION\n"
        self.assertEqual(self.item("gstack-browse").colour, "green")

    def test_audited_not_registered(self):
        self.skill("archify")
        self.audit = "## `archify` @ 11b0d60\nUSE WITH CAUTION\n"
        i = self.item("archify")
        self.assertEqual((i.colour, i.reasons), ("amber", ["audited, not registered"]))

    def test_audit_name_must_be_in_backticks(self):
        self.skill("archify")
        self.audit = "archify was looked at once\n"
        self.assertEqual(self.item("archify").colour, "red")

    def test_unregistered_red(self):
        self.skill("stranger")
        i = self.item("stranger")
        self.assertEqual((i.colour, i.reasons), ("red", ["no registry entry, no audit record"]))

    def test_no_own_roots_gives_unclassified(self):
        self.config["ownSourceRoots"] = []
        self.skill("stranger")
        self.assertEqual(self.item("stranger").colour, "unclassified")

    def test_folders_without_skill_file_are_skipped(self):
        write_files(self.skills / "not-a-skill", {"notes.txt": b"x"})
        self.assertEqual([i.name for i in self.run_inventory()], [])

    def test_audit_lines(self):
        text = "a `x` b\nno x here\n`xy` only\n`x` again\n"
        self.assertEqual(inventory.audit_lines(text, "x"), ["a `x` b", "`x` again"])

    # -- plugins ------------------------------------------------------------

    def test_plugin_updates_in_place(self):
        self.plugins(self.plugin("tool@market"))
        self.audit = "`tool@market` at 0123456, USE WITH CAUTION\n"
        i = self.item("tool@market", "plugin")
        self.assertEqual((i.colour, i.reasons), ("amber", ["marketplace plugin updates in place"]))

    def test_plugin_commit_differs(self):
        self.plugins(self.plugin("tool@market"))
        self.audit = "`tool@market` at fedcba9, USE WITH CAUTION\n"
        i = self.item("tool@market", "plugin")
        self.assertEqual((i.colour, i.reasons), ("red", ["installed commit differs from audited"]))

    def test_plugin_without_audit(self):
        self.plugins(self.plugin("tool@market"))
        self.assertEqual(self.item("tool@market", "plugin").colour, "red")

    def test_plugin_short_name_counts(self):
        self.plugins(self.plugin("tool@market"))
        self.audit = "`tool` 0123456789 ok\n"
        self.assertEqual(self.item("tool@market", "plugin").colour, "amber")

    # -- MCP ----------------------------------------------------------------

    def test_mcp_from_plugin_takes_plugin_colour(self):
        self.plugins(self.plugin("tool@market", mcp={"mcpServers": {"remote": {"type": "http", "url": "https://x"}}}))
        self.audit = "`tool@market` at 0123456\n"
        i = self.item("remote", "mcp")
        self.assertEqual(i.colour, "amber")
        self.assertIn("from plugin tool@market", i.reasons)

    def test_mcp_direct_shape(self):
        self.plugins(self.plugin("tool@market", mcp={"direct": {"command": "node", "args": ["server.js"]}}))
        self.assertEqual(self.item("direct", "mcp").colour, "red")

    def test_mcp_unpinned_npx_amber(self):
        self.plugins(self.plugin("tool@market", mcp={"mcpServers": {"npx-one": {"command": "npx", "args": ["-y", "some-pkg"]}}}))
        self.audit = "`tool@market` at 0123456\n"
        i = self.item("npx-one", "mcp")
        self.assertEqual(i.colour, "amber")
        self.assertIn("npx package not pinned to a version", i.reasons)

    def test_scoped_npx_package(self):
        cases = {"@scope/pkg": "amber", "@scope/pkg@1.2.3": "unclassified", "pkg@latest": "amber",
                 "pkg@^1.2.0": "amber", "pkg@2.0.1": "unclassified"}
        servers = {"s%d" % n: {"command": "npx", "args": ["-y", spec]} for n, spec in enumerate(cases)}
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": servers}))
        got = {i.name: i.colour for i in self.run_inventory() if i.kind == "mcp"}
        self.assertEqual(got, {"s%d" % n: colour for n, colour in enumerate(cases.values())})

    def test_windows_cmd_wrapper(self):
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {
            "wrapped": {"command": "cmd", "args": ["/c", "npx", "-y", "some-pkg"]},
            "dotcmd": {"command": "npx.cmd", "args": ["some-pkg@1.0.0"]}}}))
        got = {i.name: i.colour for i in self.run_inventory() if i.kind == "mcp"}
        self.assertEqual(got, {"wrapped": "amber", "dotcmd": "unclassified"})

    def test_mcp_from_claude_json_projects(self):
        (self.home / ".claude.json").write_text(json.dumps({
            "mcpServers": {"top": {"command": "uvx", "args": ["thing"]}},
            "projects": {"C:/work/app": {"mcpServers": {"proj": {"type": "http", "url": "https://y"}}},
                         "C:/work/empty": {}}}))
        items = {i.name: i for i in self.run_inventory() if i.kind == "mcp"}
        self.assertEqual(sorted(items), ["proj", "top"])
        self.assertEqual(items["proj"].colour, "unclassified")
        self.assertIn("C:/work/app", items["proj"].path)

    def test_mcp_json_in_own_source_roots(self):
        write_files(self.src / "repo", {".mcp.json": json.dumps({"mcpServers": {
            "local-tool": {"command": "npx", "args": ["-y", "some-pkg"]}}}).encode()})
        write_files(self.src / "repo" / "node_modules" / "dep", {".mcp.json": json.dumps({"mcpServers": {
            "vendored": {"command": "node", "args": ["x.js"]}}}).encode()})
        items = {i.name: i for i in self.run_inventory() if i.kind == "mcp"}
        self.assertEqual(sorted(items), ["local-tool"])
        self.assertEqual(items["local-tool"].colour, "amber")
        self.assertTrue(items["local-tool"].path.endswith(".mcp.json"))

    def test_mcp_json_in_claude_json_project_folders(self):
        project = self.tmp / "work" / "app"
        write_files(project, {".mcp.json": json.dumps({"mcpServers": {
            "project-tool": {"type": "http", "url": "https://z"}}}).encode()})
        (self.home / ".claude.json").write_text(json.dumps({"projects": {project.as_posix(): {}}}))
        items = {i.name: i for i in self.run_inventory() if i.kind == "mcp"}
        self.assertEqual(sorted(items), ["project-tool"])
        self.assertIn(".mcp.json", items["project-tool"].path)

    def test_reasons_never_carry_env_values(self):
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {
            "secret": {"command": "npx", "args": ["pkg"], "env": {"API_KEY": "sk-live-123"}}}}))
        text = repr(self.run_inventory())
        self.assertNotIn("sk-live-123", text)

    def test_empty_home(self):
        self.assertEqual(inventory.run(self.config, [], "", self.tmp / "nobody"), [])


if __name__ == "__main__":
    unittest.main()
