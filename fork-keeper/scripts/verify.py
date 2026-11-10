"""Compare an installed copy with its manifest of SHA-256 hashes, offline.

The walk is the one patches use: the default ignore patterns (system files
such as desktop.ini) plus the caller's, ``.git`` skipped, links refused.

Python bytecode is the exception. ``__pycache__`` stays out of manifests and
patches, but ``verify`` lists every ``.pyc`` or ``.pyo`` file and every file
inside a ``__pycache__`` folder (outside ignored folders, links never
entered) as ``bytecode``: Python loads a cached file
whose header matches its source, so a planted one runs while the source and
the manifest still agree. A cache Python wrote itself looks the same, which
is why it is reported rather than judged; deleting it is always safe.
"""

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from patches import PatchError, _ignored, walk_files
from registry import DEFAULT_IGNORE

_CHUNK = 1 << 20


class VerifyError(Exception):
    pass


@dataclass
class VerifyResult:
    modified: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    extra: list = field(default_factory=list)
    bytecode: list = field(default_factory=list)

    @property
    def ok(self):
        return not (self.modified or self.missing or self.extra or self.bytecode)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_tree(root, ignore):
    root = Path(root)
    if not root.is_dir():
        return {}
    try:
        files = walk_files(root, ignore)
    except PatchError as e:
        raise VerifyError(str(e))
    return {rel: _sha256(root / rel) for rel in files}


def _is_link(path):
    return path.is_symlink() or getattr(os.path, "isjunction", lambda _: False)(path)


def bytecode_files(root, ignore=()):
    """Every Python bytecode file under ``root``, as sorted relative paths.

    Folders are pruned as ``walk_files`` prunes them (``.git``, the default
    and caller's ignore patterns) and links are never entered, except that a
    ``__pycache__`` folder is always searched: that is where bytecode lives.
    """
    root = Path(root)
    patterns = list(DEFAULT_IGNORE) + list(ignore)
    out = []
    for folder, dirs, files in os.walk(root, followlinks=False):
        here = Path(folder)
        dirs[:] = [d for d in dirs
                   if not _is_link(here / d)
                   and d.casefold() != ".git"
                   and (d.casefold() == "__pycache__" or not _ignored(d, True, patterns))]
        cache = here.name.casefold() == "__pycache__"
        for name in files:
            if cache or name.casefold().endswith((".pyc", ".pyo")):
                out.append((here / name).relative_to(root).as_posix())
    return sorted(out)


def verify_tree(root, manifest, ignore):
    actual = hash_tree(root, ignore)
    return VerifyResult(
        modified=sorted(p for p in manifest if p in actual and actual[p] != manifest[p]),
        missing=sorted(p for p in manifest if p not in actual),
        extra=sorted(p for p in actual if p not in manifest),
        bytecode=bytecode_files(root, ignore) if Path(root).is_dir() else [],
    )
