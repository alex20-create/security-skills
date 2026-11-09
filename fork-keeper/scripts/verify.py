"""Compare an installed copy with its manifest of SHA-256 hashes, offline.

The walk is the one patches use: the default ignore patterns (system files
such as desktop.ini) plus the caller's, ``.git`` skipped, links refused.
"""

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from patches import PatchError, walk_files

_CHUNK = 1 << 20


class VerifyError(Exception):
    pass


@dataclass
class VerifyResult:
    modified: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    extra: list = field(default_factory=list)

    @property
    def ok(self):
        return not (self.modified or self.missing or self.extra)


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


def verify_tree(root, manifest, ignore):
    actual = hash_tree(root, ignore)
    return VerifyResult(
        modified=sorted(p for p in manifest if p in actual and actual[p] != manifest[p]),
        missing=sorted(p for p in manifest if p not in actual),
        extra=sorted(p for p in actual if p not in manifest),
    )
