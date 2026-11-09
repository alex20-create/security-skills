"""DEP-3 patch files: generate a copy's diff against its base, render and parse the header.

A patch is a DEP-3 header, a ``---`` line, then a git diff made with
``--binary -M --full-index``. ``git apply`` skips everything before the first
``diff --git`` line, so a stored patch applies as it is.
"""

import fnmatch
import os
import re
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from gitsafe import SafeGit
from registry import DEFAULT_IGNORE

_ONE_ORIGIN = r"[A-Za-z0-9][A-Za-z0-9._-]*@[0-9a-f]{40}"
_APPLIES_TO = re.compile(r"{0}(?:, {0})*".format(_ONE_ORIGIN))
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_FIELDS = ("Description", "Origin", "Forwarded", "Applies-To", "Last-Update")
_TEXT_PROBE = 8000


class PatchError(Exception):
    pass


@dataclass(frozen=True)
class Dep3:
    description: str
    applies_to: str
    last_update: str
    origin: str = "vendor-local"
    forwarded: str = "not-needed"

    def __post_init__(self):
        lines = self.description.split("\n") if isinstance(self.description, str) else [""]
        if not lines[0].strip():
            raise PatchError("a patch description needs a summary on its first line")
        if "\r" in self.description:
            raise PatchError("a patch description must not contain carriage returns")
        for l in lines[1:]:
            if l.strip() == "." or (l and not l.strip()):
                raise PatchError("description line {!r} would not survive a round trip; "
                                 "use an empty line or real text".format(l))
        if not isinstance(self.applies_to, str) or not _APPLIES_TO.fullmatch(self.applies_to):
            raise PatchError("Applies-To must be <origin id>@<full SHA>, comma-separated for several origins, "
                             "got {!r}".format(self.applies_to))
        try:
            ok = bool(_DATE.fullmatch(self.last_update)) and date.fromisoformat(self.last_update)
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise PatchError("Last-Update must be a date YYYY-MM-DD, got {!r}".format(self.last_update))
        for name, value in (("Origin", self.origin), ("Forwarded", self.forwarded)):
            if not isinstance(value, str) or not value.strip() or "\n" in value:
                raise PatchError("{} must be one non-empty line".format(name))


def render(h, diff):
    lines = h.description.split("\n")
    out = ["Description: " + lines[0]]
    out += [" " + l if l.strip() else " ." for l in lines[1:]]
    out += ["Origin: " + h.origin, "Forwarded: " + h.forwarded,
            "Applies-To: " + h.applies_to, "Last-Update: " + h.last_update, "---", ""]
    return "\n".join(out).encode("utf-8") + diff


def parse(patch):
    crlf = PatchError("the patch has CRLF line endings; the registry needs a '-text' rule for its patches")
    end = patch.find(b"\n---\n")
    if end < 0:
        raise crlf if b"\r\n" in patch else PatchError("no '---' line closes the patch header")
    head, diff = patch[:end], patch[end + len(b"\n---\n"):]
    if b"\r" in head:
        raise crlf
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        raise PatchError("the patch header is not UTF-8")
    fields = {}
    key = None
    for line in text.split("\n"):
        if line.startswith(" ") and key == "Description":
            fields[key].append("" if line == " ." else line[1:])
            continue
        name, sep, value = line.partition(": ")
        if not sep or name not in _FIELDS:
            raise PatchError("unexpected patch header line {!r}".format(line))
        if name in fields:
            raise PatchError("patch header field {} appears twice".format(name))
        fields[name] = [value]
        key = name
    for need in ("Description", "Applies-To", "Last-Update"):
        if need not in fields:
            raise PatchError("patch header has no {} field".format(need))
    h = Dep3(description="\n".join(fields["Description"]), applies_to=fields["Applies-To"][0],
             last_update=fields["Last-Update"][0],
             origin=fields.get("Origin", ["vendor-local"])[0],
             forwarded=fields.get("Forwarded", ["not-needed"])[0])
    return h, diff


# -- trees ------------------------------------------------------------------

def _ignored(name, is_dir, patterns):
    """Case-insensitive, because Windows treats Desktop.ini and desktop.ini as one file."""
    name = name.casefold()
    for p in patterns:
        p = p.casefold()
        if p.endswith("/"):
            if is_dir and fnmatch.fnmatchcase(name, p[:-1]):
                return True
        elif fnmatch.fnmatchcase(name, p):
            return True
    return False


def walk_files(root, ignore=()):
    """Relative POSIX paths of the files under ``root``, skipping .git, ignore patterns and refusing links."""
    root = Path(root)
    patterns = list(DEFAULT_IGNORE) + list(ignore)
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        keep = []
        for d in sorted(dirnames):
            p = here / d
            if p.is_symlink() or getattr(os.path, "isjunction", lambda _: False)(p):
                raise PatchError("{} is a link; fork-keeper does not follow links".format(p.relative_to(root).as_posix()))
            if d == ".git" or _ignored(d, True, patterns):
                continue
            keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            p = here / f
            if f == ".git" or _ignored(f, False, patterns):
                continue
            if p.is_symlink():
                raise PatchError("{} is a link; fork-keeper does not follow links".format(p.relative_to(root).as_posix()))
            out.append(p.relative_to(root).as_posix())
    return sorted(out)


def copy_tree(src, dst, ignore):
    src, dst = Path(src), Path(dst)
    files = walk_files(src, ignore)
    for rel in files:
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / rel, target)


def is_text(data):
    return b"\x00" not in data[:_TEXT_PROBE]


def eol_style(data):
    """"lf", "crlf", "mixed" or "none" (no line break at all)."""
    lf, crlf = data.count(b"\n"), data.count(b"\r\n")
    if lf == 0:
        return "none"
    return "crlf" if crlf == lf else "lf" if crlf == 0 else "mixed"


def eol_mismatches(base, copy):
    """Text files present in both whose line-ending style differs, in either direction."""
    base, copy = Path(base), Path(copy)
    out = []
    for rel in walk_files(copy):
        b = base / rel
        if not b.is_file():
            continue
        c_data, b_data = (copy / rel).read_bytes(), b.read_bytes()
        if is_text(c_data) and is_text(b_data):
            styles = {eol_style(c_data), eol_style(b_data)} - {"none"}
            if len(styles) > 1:
                out.append(rel)
    return out


def clear_work_tree(work):
    for child in Path(work).iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def generate(base, copy, work, ignore):
    """The diff that turns ``base`` into ``copy``, as ``git apply`` takes it; b"" when they match."""
    work = Path(work)
    if work.exists() and any(work.iterdir()):
        raise PatchError("work folder {} is not empty".format(work))
    g = SafeGit.init(work, bare=False, offline=True)
    g.run("config", "core.fileMode", "false")
    copy_tree(base, work, ignore)
    g.commit_all("base", walk_files(work, ignore))
    clear_work_tree(work)
    copy_tree(copy, work, ignore)
    g.stage_exactly(walk_files(work, ignore))
    return g.run("diff", "--cached", "--binary", "-M", "--full-index", "--no-ext-diff", "--no-textconv",
                 "--no-color")
