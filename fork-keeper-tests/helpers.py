"""Shared test helpers: puts the scripts on sys.path and builds fixture repos.

Fixture repos are made with plain git, isolated from the user's and the
system's git config so line endings and identity are the same everywhere.
"""

import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "fork-keeper" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_EMPTY_CONFIG = Path(tempfile.mkdtemp(prefix="fk-test-config-")) / "empty"
_EMPTY_CONFIG.write_text("")

GIT_FLAGS = [
    "-c", "core.autocrlf=false",
    "-c", "core.safecrlf=false",
    "-c", "user.name=fixture",
    "-c", "user.email=fixture@example.invalid",
    "-c", "init.defaultBranch=main",
    "-c", "commit.gpgsign=false",
]


def git_env():
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = str(_EMPTY_CONFIG)
    env["GIT_CONFIG_SYSTEM"] = str(_EMPTY_CONFIG)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def git(root, *args, input=None):
    """Run plain git in ``root`` with the fixture flags; return stdout bytes."""
    r = subprocess.run(["git", *GIT_FLAGS, *args], cwd=str(root), input=input,
                       capture_output=True, env=git_env())
    if r.returncode != 0:
        raise RuntimeError("git {} failed: {}".format(" ".join(args), r.stderr.decode("utf-8", "replace")))
    return r.stdout


def write_files(root, files):
    for rel, data in files.items():
        path = Path(root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def make_repo(root, files, message="commit"):
    """Create ``root`` as a repo if needed, write ``files``, commit; return the full SHA.

    A value of None in ``files`` deletes that path.
    """
    root = Path(root)
    if not (root / ".git").exists():
        root.mkdir(parents=True, exist_ok=True)
        git(root, "init", "-q")
    for rel, data in files.items():
        if data is None:
            (root / rel).unlink()
    write_files(root, {k: v for k, v in files.items() if v is not None})
    git(root, "add", "-A")
    git(root, "commit", "-q", "--allow-empty", "-m", message)
    return git(root, "rev-parse", "HEAD").decode().strip()


def tmpdir(test):
    """A temporary folder removed when ``test`` (a TestCase) finishes."""
    d = tempfile.TemporaryDirectory(prefix="fk-test-")
    test.addCleanup(d.cleanup)
    return Path(d.name)


SHA_A = "74053d6f5631adb5dcb066633c29d360bc22ceaa"
SHA_B = "11b0d6031853bdae89eed57e7979aabbf0034097"

_SAMPLE = {
    "name": "sample",
    "kind": "skill",
    "copy": "~/skills/sample",
    "installs": ["~/skills/sample"],
    "origins": [
        {
            "id": "upstream",
            "url": "https://github.com/example/sample",
            "pin": SHA_A,
            "track": "main",
            "license": "MIT",
            "map": [
                {"from": "skills/sample", "to": "."},
                {"from": "LICENSE", "to": "LICENSE"},
            ],
            "audit": {
                "commit": SHA_A,
                "verdict": "LIKELY SAFE",
                "date": "2026-11-03",
                "record": "sample",
            },
            "lastChecked": "2026-11-03",
        }
    ],
}


def sample_copy(**overrides):
    data = copy.deepcopy(_SAMPLE)
    data.update(overrides)
    return data


def write_copy_json(folder, data):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    (folder / "copy.json").write_bytes(text.encode("utf-8"))
