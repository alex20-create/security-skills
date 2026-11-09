import unittest
from pathlib import Path

import helpers
from helpers import SHA_A, SHA_B, sample_copy, tmpdir, write_copy_json

import registry
from registry import RegistryError


class LoadCopyTests(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir(self)
        self.folder = self.root / "copies" / "sample"

    def load(self, data):
        write_copy_json(self.folder, data)
        return registry.load_copy(self.folder)

    def test_round_trip(self):
        write_copy_json(self.folder, sample_copy())
        before = (self.folder / "copy.json").read_bytes()
        registry.save_copy(registry.load_copy(self.folder))
        self.assertEqual((self.folder / "copy.json").read_bytes(), before)

    def test_rejects_short_sha(self):
        data = sample_copy()
        data["origins"][0]["pin"] = "74053d6"
        with self.assertRaises(RegistryError) as cm:
            self.load(data)
        self.assertIn("pin", str(cm.exception))
        self.assertIn("40", str(cm.exception))

    def test_rejects_short_audit_commit(self):
        data = sample_copy()
        data["origins"][0]["audit"]["commit"] = "74053d6"
        with self.assertRaises(RegistryError) as cm:
            self.load(data)
        self.assertIn("audit commit", str(cm.exception))
        self.assertIn("40-character", str(cm.exception))

    def test_rejects_sha_with_trailing_newline(self):
        data = sample_copy()
        data["origins"][0]["pin"] = SHA_A + "\n"
        with self.assertRaises(RegistryError):
            self.load(data)

    def test_rejects_string_installs(self):
        with self.assertRaises(RegistryError) as cm:
            self.load(sample_copy(installs="~/s"))
        self.assertIn("installs", str(cm.exception))

    def test_rejects_wrong_types(self):
        cases = [
            ("copy", 5), ("name", None), ("origins", None),
        ]
        for key, value in cases:
            with self.subTest(key=key):
                with self.assertRaises(RegistryError):
                    self.load(sample_copy(**{key: value}))
        for key, value in [("id", None), ("track", 3), ("license", None), ("map", None)]:
            with self.subTest(origin_key=key):
                data = sample_copy()
                data["origins"][0][key] = value
                with self.assertRaises(RegistryError):
                    self.load(data)

    def test_rejects_unknown_fields(self):
        with self.assertRaises(RegistryError) as cm:
            self.load(sample_copy(extra="x"))
        self.assertIn("extra", str(cm.exception))
        data = sample_copy()
        data["origins"][0]["pinned"] = SHA_A
        with self.assertRaises(RegistryError):
            self.load(data)

    def test_rejects_escaping_map_paths(self):
        for bad in ("../evil", "a/../../evil", "/abs", "C:/evil", "C:\\evil", "a\\b", "x:y"):
            for side in ("from", "to"):
                with self.subTest(side=side, path=bad):
                    data = sample_copy()
                    data["origins"][0]["map"][1][side] = bad
                    with self.assertRaises(RegistryError):
                        self.load(data)

    def test_map_paths_normalised(self):
        data = sample_copy()
        data["origins"][0]["map"][1] = {"from": "./LICENSE", "to": "r/./LICENSE"}
        c = self.load(data)
        self.assertEqual(c.origins[0].map[1], registry.MapEntry("LICENSE", "r/LICENSE"))

    def test_case_insensitive_clash(self):
        data = sample_copy()
        data["origins"][0]["map"] = [{"from": "a.md", "to": "X.md"}, {"from": "b.md", "to": "x.md"}]
        with self.assertRaises(RegistryError):
            self.load(data)

    def test_rejects_url_with_credentials_or_no_host(self):
        for bad in ("https://user:tok@github.com/a/b", "https:///a/b", "https://github.com/a b", "https://github.com/a\n"):
            with self.subTest(url=bad):
                data = sample_copy()
                data["origins"][0]["url"] = bad
                with self.assertRaises(RegistryError):
                    self.load(data)

    def test_rejects_track_starting_with_dash(self):
        data = sample_copy()
        data["origins"][0]["track"] = "--upload-pack=x"
        with self.assertRaises(RegistryError):
            self.load(data)

    def test_save_writes_changed_paths(self):
        import dataclasses
        write_copy_json(self.folder, sample_copy())
        c = registry.load_copy(self.folder)
        moved = dataclasses.replace(c, copy=self.root / "new place")
        registry.save_copy(moved)
        self.assertEqual(registry.load_copy(self.folder).copy, self.root / "new place")

    def test_round_trip_non_ascii_and_windows_path(self):
        data = sample_copy(copy="C:/Users/someone/Dise\u00f1o/sample", installs=["C:/Users/someone/skills/sample"])
        data["origins"][0]["license"] = "Apache-2.0 \u00a9"
        write_copy_json(self.folder, data)
        before = (self.folder / "copy.json").read_bytes()
        registry.save_copy(registry.load_copy(self.folder))
        self.assertEqual((self.folder / "copy.json").read_bytes(), before)

    def test_bom_and_bad_encoding(self):
        self.folder.mkdir(parents=True)
        import json as _json
        (self.folder / "copy.json").write_bytes(b"\xef\xbb\xbf" + _json.dumps(sample_copy()).encode())
        self.assertEqual(registry.load_copy(self.folder).name, "sample")
        (self.folder / "copy.json").write_bytes(b"\xff\xfe{")
        with self.assertRaises(RegistryError):
            registry.load_copy(self.folder)

    def test_rejects_non_https_url(self):
        data = sample_copy()
        data["origins"][0]["url"] = "http://github.com/example/sample"
        with self.assertRaises(RegistryError) as cm:
            self.load(data)
        self.assertIn("https", str(cm.exception))

    def test_file_url_only_through_test_hook(self):
        data = sample_copy()
        data["origins"][0]["url"] = "file:///tmp/upstream"
        with self.assertRaises(RegistryError):
            self.load(data)
        registry._set_allow_file(True)
        self.addCleanup(registry._set_allow_file, False)
        self.assertEqual(self.load(data).origins[0].url, "file:///tmp/upstream")

    def test_rejects_unknown_kind(self):
        with self.assertRaises(RegistryError) as cm:
            self.load(sample_copy(kind="theme"))
        self.assertIn("kind", str(cm.exception))

    def test_rejects_unknown_verdict(self):
        data = sample_copy()
        data["origins"][0]["audit"]["verdict"] = "SAFE"
        with self.assertRaises(RegistryError) as cm:
            self.load(data)
        self.assertIn("verdict", str(cm.exception))

    def test_rejects_missing_field(self):
        data = sample_copy()
        del data["origins"][0]["map"]
        with self.assertRaises(RegistryError) as cm:
            self.load(data)
        self.assertIn("map", str(cm.exception))

    def test_overlapping_destinations(self):
        data = sample_copy()
        second = {
            "id": "guidelines",
            "url": "https://github.com/example/guidelines",
            "pin": SHA_B,
            "track": "main",
            "license": "MIT",
            "map": [{"from": "command.md", "to": "reference/x.md"}],
            "audit": None,
            "lastChecked": None,
        }
        data["origins"][0]["map"].append({"from": "docs/x.md", "to": "reference/x.md"})
        data["origins"].append(second)
        with self.assertRaises(RegistryError) as cm:
            self.load(data)
        self.assertIn("upstream", str(cm.exception))
        self.assertIn("guidelines", str(cm.exception))

    def test_folder_destination_contains_file_destination(self):
        data = sample_copy()
        data["origins"][0]["map"] = [
            {"from": "skills/sample", "to": "reference"},
            {"from": "extra.md", "to": "reference/extra.md"},
        ]
        with self.assertRaises(RegistryError):
            self.load(data)

    def test_root_destination_does_not_clash_with_siblings(self):
        # "." maps a folder onto the copy root; a file beside it ("LICENSE")
        # is not a registry clash. Two files mapped to the same path are.
        c = self.load(sample_copy())
        self.assertEqual(len(c.origins[0].map), 2)
        data = sample_copy()
        data["origins"][0]["map"].append({"from": "COPYING", "to": "LICENSE"})
        with self.assertRaises(RegistryError):
            self.load(data)

    def test_tilde_expanded(self):
        c = self.load(sample_copy(copy="~/x", installs=["~/x"]))
        self.assertEqual(c.copy, Path.home() / "x")
        self.assertEqual(c.installs, [Path.home() / "x"])

    def test_audit_optional(self):
        data = sample_copy()
        data["origins"][0]["audit"] = None
        c = self.load(data)
        self.assertIsNone(c.origins[0].audit)

    def test_list_copies(self):
        write_copy_json(self.root / "copies" / "b", sample_copy(name="b"))
        write_copy_json(self.root / "copies" / "a", sample_copy(name="a"))
        names = [c.name for c in registry.list_copies(self.root)]
        self.assertEqual(names, ["a", "b"])


class SeriesAndManifestTests(unittest.TestCase):
    def setUp(self):
        self.folder = tmpdir(self)

    def test_series_order(self):
        names = ["0002-b.patch", "0001-a.patch", "0003-c.patch"]
        registry.write_series(self.folder, names)
        got = [p.name for p in registry.read_series(self.folder)]
        self.assertEqual(got, names)

    def test_series_missing_is_empty(self):
        self.assertEqual(registry.read_series(self.folder), [])

    def test_series_rejects_paths(self):
        for bad in ("../x.patch", "C:/x.patch", "sub/x.patch", "a\\b.patch"):
            with self.subTest(name=bad):
                (self.folder / "series").write_text(bad + "\n")
                with self.assertRaises(RegistryError):
                    registry.read_series(self.folder)

    def test_manifest_rejects_bad_hash(self):
        (self.folder / "manifest.json").write_text('{"files": {"a": "xyz"}}')
        with self.assertRaises(RegistryError):
            registry.load_manifest(self.folder)
        (self.folder / "manifest.json").write_text('[]')
        with self.assertRaises(RegistryError):
            registry.load_manifest(self.folder)

    def test_manifest_round_trip(self):
        files = {"SKILL.md": "a" * 64, "scripts/x.py": "b" * 64}
        registry.save_manifest(self.folder, files, "2026-11-09")
        self.assertEqual(registry.load_manifest(self.folder), files)
        text = (self.folder / "manifest.json").read_bytes()
        self.assertNotIn(b"\r\n", text)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir(self)

    def test_text_rule(self):
        self.assertFalse(registry.has_text_rule(self.root))
        (self.root / ".gitattributes").write_text("*.md text\n")
        self.assertFalse(registry.has_text_rule(self.root))
        (self.root / ".gitattributes").write_text("*.md text\ncopies/** -text\n")
        self.assertTrue(registry.has_text_rule(self.root))

    def test_config_defaults(self):
        cfg = registry.load_config(self.root)
        self.assertEqual(cfg["skillsRoots"], ["~/.claude/skills"])
        self.assertEqual(cfg["ownSourceRoots"], [])
        self.assertEqual(cfg["managedSets"], {})
        self.assertEqual(cfg["ignore"], [])
        self.assertIsNone(cfg["auditRecords"])

    def test_config_rejects_bad_shapes(self):
        for body in ("[]", '{"ignore": null}', '{"skillsRoots": "x"}'):
            with self.subTest(body=body):
                (self.root / "fork-keeper.json").write_text(body)
                with self.assertRaises(RegistryError):
                    registry.load_config(self.root)

    def test_config_file_overrides(self):
        (self.root / "fork-keeper.json").write_text('{"ignore": ["*.bak"]}')
        cfg = registry.load_config(self.root)
        self.assertEqual(cfg["ignore"], ["*.bak"])
        self.assertEqual(cfg["skillsRoots"], ["~/.claude/skills"])


if __name__ == "__main__":
    unittest.main()
