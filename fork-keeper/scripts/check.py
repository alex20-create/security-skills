"""What changed upstream since a pin: commits, mapped changes, alarms, and the files to audit.

Nothing here executes upstream content. ``extract`` writes the new and changed
mapped files of the new commit, blob by blob, after checking each one's mode,
so a link or submodule is refused before anything reaches the disk.
"""

import fnmatch
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from tree import check_path_safe, TreeError

_SYMLINK = "120000"
_GITLINK = "160000"
_LICENSE_NAMES = ("licen[cs]e*", "copying*")
_MANIFESTS = {"plugin.json", "marketplace.json", ".mcp.json", "hooks.json"}
_GH_PART = re.compile(r"[A-Za-z0-9_.-]+")
_SHA = re.compile(r"[0-9a-f]{40}")


class CheckError(Exception):
    pass


@dataclass(frozen=True)
class Alarm:
    kind: str
    detail: str


@dataclass
class CheckReport:
    origin: str
    old: str
    new: str
    commits: list = field(default_factory=list)
    stat: str = ""
    outside: dict = field(default_factory=dict)
    alarms: list = field(default_factory=list)
    mapped_changes: list = field(default_factory=list)


def in_map(path, entries):
    return any(e.src == "." or path == e.src or path.startswith(e.src + "/") for e in entries)


def _frontmatter(data):
    """The whole frontmatter of a SKILL.md, normalised for BOM and line endings; None if there is none.

    The whole block is compared, not only ``description:``: tools, hooks, model
    and invocation keys change what runs too, and a second ``description:``
    key would be read by a last-wins loader.
    """
    text = data.decode("utf-8-sig", "replace").replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    return "\n".join(l.rstrip() for l in text[4:end].split("\n")).strip("\n")


def _blob_at(git, commit, path):
    for mode, blob, p in git.ls_tree(commit, path):
        if p == path:
            return blob
    return None


def _alarms_for(git, old, new, status, path):
    name = PurePosixPath(path).name
    low = name.casefold()
    out = []
    if any(fnmatch.fnmatchcase(low, p) for p in _LICENSE_NAMES):
        out.append(Alarm("license", "{} {}: licence file changed; check the terms still allow the copy".format(status, path)))
    if low == "skill.md":
        if status in ("A", "D"):
            out.append(Alarm("contract", "{} {}: a skill file was {}".format(
                status, path, "added" if status == "A" else "removed")))
        else:
            before, after = _blob_at(git, old, path), _blob_at(git, new, path)
            f_old = _frontmatter(git.cat_blob(before)) if before else None
            f_new = _frontmatter(git.cat_blob(after)) if after else None
            if f_old is None or f_new is None:
                out.append(Alarm("contract", "{} {}: the frontmatter could not be read, so the change to when "
                                             "and how the skill runs cannot be ruled out".format(status, path)))
            elif f_old != f_new:
                out.append(Alarm("contract", "{} {}: the frontmatter changed (description, tools or other keys), "
                                             "so when or how the skill runs may have changed".format(status, path)))
    elif low in _MANIFESTS:
        out.append(Alarm("contract", "{} {}: a plugin, hook or MCP manifest changed".format(status, path)))
    return out


def summarize(git, origin, new):
    old = origin.pin
    report = CheckReport(origin=origin.id, old=old, new=new)
    if not git.is_ancestor(old, new):
        report.alarms.append(Alarm("history", "pin {} is not an ancestor of {}: the upstream history was rewritten".format(
            old[:12], new[:12])))
    report.commits = git.log(old, new)
    for status, path in git.changed(old, new):
        if in_map(path, origin.map):
            report.mapped_changes.append((status, path))
            report.alarms += _alarms_for(git, old, new, status, path)
        else:
            top = path.split("/", 1)[0] if "/" in path else "."
            report.outside[top] = report.outside.get(top, 0) + 1
    if report.mapped_changes:
        srcs = [] if any(e.src == "." for e in origin.map) else [e.src for e in origin.map]
        report.stat = git.diff(old, new, srcs, stat=True).decode("utf-8", "replace")
    return report


def extract(git, origin, new, dest):
    """Write the new and changed mapped files of ``new`` under ``dest`` at their upstream paths."""
    dest = Path(dest)
    wanted = sorted(p for s, p in git.changed(origin.pin, new) if s != "D" and in_map(p, origin.map))
    if not wanted:
        return []
    entries = {p: (m, b) for m, b, p in git.ls_tree(new, "")}
    refused = [p for p in wanted if entries.get(p, ("", ""))[0] in (_SYMLINK, _GITLINK)]
    if refused:
        raise CheckError("refused to extract links or submodules: {}".format(", ".join(refused)))
    for p in wanted:
        try:
            check_path_safe(p, "upstream path")
        except TreeError as e:
            raise CheckError(str(e))
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    for p in wanted:
        target = dest / p
        if root not in target.resolve().parents:
            raise CheckError("upstream path {!r} escapes the extraction folder".format(p))
        target.parent.mkdir(parents=True, exist_ok=True)
        data = git.cat_blob(entries[p][1])
        try:
            with open(target, "xb") as f:
                f.write(data)
        except FileExistsError:
            raise CheckError("{!r} already exists in the extraction folder; two upstream paths may name one "
                             "file on this system (case, short 8.3 names), so neither is trusted".format(p))
    return wanted


def signature(url, sha, runner=subprocess.run):
    """GitHub's verdict on the commit signature, through gh: "verified", "unverified" or "not checked"."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "not checked"
    segs = parts.path.strip("/").split("/")
    if parts.scheme != "https" or parts.netloc != "github.com" or len(segs) != 2 or not _SHA.fullmatch(sha or ""):
        return "not checked"
    owner, repo = segs[0], segs[1][:-4] if segs[1].endswith(".git") else segs[1]
    if not (_GH_PART.fullmatch(owner) and _GH_PART.fullmatch(repo)):
        return "not checked"
    try:
        r = runner(["gh", "api", "repos/{}/{}/commits/{}".format(owner, repo, sha),
                    "--jq", ".commit.verification.verified"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "not checked"
    if r.returncode != 0:
        return "not checked"
    out = (r.stdout or "").strip()
    return {"true": "verified", "false": "unverified"}.get(out, "not checked")
