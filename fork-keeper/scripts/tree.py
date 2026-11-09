"""Build a copy's base tree: every mapped file of every origin at its commit.

Files come straight from git objects; nothing is checked out. Symlinks and
submodules inside a mapped path are refused, never followed, and no file is
written outside the destination folder.
"""

from pathlib import Path

_SYMLINK = "120000"
_GITLINK = "160000"
_WINDOWS_BAD = set('<>:"|?*\\')


class TreeError(Exception):
    pass


_RESERVED = {"con", "prn", "aux", "nul"} | {"{}{}".format(d, n) for d in ("com", "lpt") for n in range(1, 10)}
# Bidirectional controls reorder how a name is shown ("evil‮gpj.md" displays as "evildm.jpg").
_BIDI = set("؜‎‏‪‫‬‭‮⁦⁧⁨⁩")


def _control(c):
    """C0 and C1 controls, DEL and the bidirectional controls."""
    return ord(c) < 32 or 0x7f <= ord(c) <= 0x9f or c in _BIDI


def check_path_safe(path, where):
    """Refuse a relative path that could escape, reach git's own folder or misbehave on Windows."""
    if not path or path.startswith("/") or "//" in path:
        raise TreeError("{}: unsafe path {!r}".format(where, path))
    for p in path.split("/"):
        low = p.casefold()
        if p in ("", ".", "..") or low in (".git", "git~1") or low.split(".", 1)[0] in _RESERVED \
                or any(c in _WINDOWS_BAD or _control(c) for c in p) or p.endswith((" ", ".")):
            raise TreeError("{}: unsafe path {!r}".format(where, path))
    return path


def _join(dst, rel):
    return rel if dst == "." else "{}/{}".format(dst, rel)


def mapped_files(git, commit, entries):
    """Destination path -> (source path, blob) for every file the map selects at ``commit``."""
    out = {}
    for e in entries:
        listed = git.ls_tree(commit, e.src) if e.src != "." else git.ls_tree(commit, "")
        if e.src == ".":
            selected = [(m, b, p, p) for m, b, p in listed]
        elif len(listed) == 1 and listed[0][2] == e.src:
            m, b, p = listed[0]
            selected = [(m, b, p, None)]
        else:
            prefix = e.src + "/"
            selected = [(m, b, p, p[len(prefix):]) for m, b, p in listed if p.startswith(prefix)]
        if not selected:
            raise TreeError("map source {!r} does not exist at {}".format(e.src, commit[:12]))
        for mode, blob, src, rel in selected:
            if mode == _SYMLINK:
                raise TreeError("{} is a symlink at {}; fork-keeper does not follow links".format(src, commit[:12]))
            if mode == _GITLINK:
                raise TreeError("{} is a submodule at {}; fork-keeper does not follow submodules".format(src, commit[:12]))
            dst = e.dst if rel is None else _join(e.dst, rel)
            check_path_safe(dst, "destination of " + src)
            out[dst] = (src, blob)
    return out


def build_base(parts, dest):
    """Write the base tree of ``parts`` into ``dest``; return destination -> source path.

    ``parts`` is a list of (origin id, SafeGit, commit, map entries).
    """
    dest = Path(dest)
    plan = {}
    owners = {}
    seen_fold = {}
    for origin_id, git, commit, entries in parts:
        for dst, (src, blob) in mapped_files(git, commit, entries).items():
            key = dst.casefold()
            if key in seen_fold:
                other_dst, other_origin = seen_fold[key]
                raise TreeError("{} ({}) and {} ({}) map to the same file".format(
                    other_dst, other_origin, dst, origin_id))
            seen_fold[key] = (dst, origin_id)
            plan[dst] = (git, blob)
            owners[dst] = src
    root = dest.resolve()
    for dst, (git, blob) in plan.items():
        target = (dest / dst)
        if root not in target.resolve().parents:
            raise TreeError("destination {!r} escapes the base folder".format(dst))
        target.parent.mkdir(parents=True, exist_ok=True)
        data = git.cat_blob(blob)
        try:
            with open(target, "xb") as f:
                f.write(data)
        except FileExistsError:
            raise TreeError("{!r} already exists in the base folder; two paths may name one file on this "
                            "system (case, short 8.3 names)".format(dst))
    return owners
