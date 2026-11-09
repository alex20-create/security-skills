"""Re-apply a copy's patch series on a new base, three-way, stopping at the first conflict.

The work repo gets the old base, then the new base on top (so every blob a
patch names is present for ``--3way``), then one commit per applied patch.
Progress lives in ``.git/fk-progress.json``, inside git's own folder, so it is
never committed and never part of the result tree.

Statuses: ``clean`` (applied without three-way), ``merged`` (three-way, no
conflict left), ``conflict`` (stopped here; markers or a file-level clash),
``pending`` (after a conflict, not tried yet).
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

import patches
from gitsafe import GitError, SafeGit

_PROGRESS = "fk-progress.json"
_MARKER = b"<<<<<<< "


class ReapplyError(Exception):
    pass


@dataclass
class PatchResult:
    name: str
    status: str
    conflicts: list = field(default_factory=list)
    detail: str = field(default="", compare=False)


# -- progress ---------------------------------------------------------------

def _progress_path(work):
    return Path(work) / ".git" / _PROGRESS


def _load(work):
    path = _progress_path(work)
    if not path.is_file():
        raise ReapplyError("{} holds no re-apply in progress".format(work))
    return json.loads(path.read_text(encoding="utf-8"))


def _save(work, state):
    text = json.dumps(state, indent=2, ensure_ascii=False) + "\n"
    _progress_path(work).write_bytes(text.encode("utf-8"))


def _to_json(r):
    return {"name": r.name, "status": r.status, "conflicts": [list(c) for c in r.conflicts], "detail": r.detail}


def _from_json(d):
    return PatchResult(d["name"], d["status"], [tuple(c) for c in d["conflicts"]], d.get("detail", ""))


# -- applying ---------------------------------------------------------------

def _markers(work, paths):
    out = []
    for p in paths:
        f = Path(work) / p
        if not f.is_file():
            continue
        for n, line in enumerate(f.read_bytes().split(b"\n"), 1):
            if line.startswith(_MARKER):
                out.append((p, n))
    return out


def _sections(diff):
    """(path, created) for every file section of a git diff whose header is unambiguous."""
    out = []
    for block in (b"\n" + diff).split(b"\ndiff --git ")[1:]:
        lines = block.split(b"\n")
        head = lines[0].decode("utf-8", "replace")
        created = any(l.startswith(b"new file mode ") for l in lines[1:8])
        rename = [l[len(b"rename from "):].decode("utf-8", "replace") for l in lines[1:8] if l.startswith(b"rename from ")]
        if rename:
            out.append((rename[0], False))
            continue
        n = (len(head) - 5) // 2
        if head.startswith("a/") and len(head) == 2 * n + 5 and head[2:2 + n] == head[5 + n:]:
            out.append((head[2:2 + n], created))
    return out


def _file_level_clash(g, diff):
    index = {p.decode("utf-8", "replace") for p in g.run("ls-files", "-z").split(b"\x00") if p}
    sections = _sections(diff)
    failing = [p for p, created in sections if (p in index) == created]
    return sorted(set(failing or [p for p, _ in sections]))


def _apply(g, work, patch):
    _, diff = patches.parse(Path(patch).read_bytes())
    name = Path(patch).name
    if not diff.strip():
        return PatchResult(name, "clean")
    try:
        g.run("apply", "--index", "--whitespace=nowarn", input=diff)
        return PatchResult(name, "clean")
    except GitError:
        pass
    g.run("apply", "--3way", "--index", "--whitespace=nowarn", input=diff, ok_codes=tuple(range(256)))
    detail = g.last_stderr.decode("utf-8", "replace").strip()
    if g.last_returncode == 0:
        return PatchResult(name, "merged", detail=detail)
    unmerged = sorted({p.decode("utf-8", "replace") for p in
                       g.run("diff", "--name-only", "--diff-filter=U", "-z").split(b"\x00") if p})
    found = _markers(work, unmerged)
    if found:
        return PatchResult(name, "conflict", found, detail)
    return PatchResult(name, "conflict", [(p, 0) for p in (unmerged or _file_level_clash(g, diff))], detail)


def _run(g, work, series, state, start_at):
    results = [_from_json(d) for d in state["results"]]
    for i in range(start_at, len(series)):
        r = _apply(g, work, series[i])
        results.append(r)
        if r.status == "conflict":
            state["results"] = [_to_json(x) for x in results]
            state["conflict"] = i
            _save(work, state)
            return results + [PatchResult(Path(p).name, "pending") for p in series[i + 1:]]
        state["commits"].append(g.commit_all("fk: " + r.name))
    state["results"] = [_to_json(x) for x in results]
    state.pop("conflict", None)
    _save(work, state)
    return results


def _git(work):
    return SafeGit(work)


def start(old_base, new_base, series, work):
    work = Path(work)
    if work.exists() and any(work.iterdir()):
        raise ReapplyError("work folder {} is not empty".format(work))
    g = SafeGit.init(work, bare=False)
    g.run("config", "core.fileMode", "false")
    patches.copy_tree(old_base, work, [])
    g.commit_all("fk: old base")
    patches.clear_work_tree(work)
    patches.copy_tree(new_base, work, [])
    base = g.commit_all("fk: new base")
    state = {"series": [Path(p).name for p in series], "base": base, "commits": [], "results": []}
    return _run(g, work, list(series), state, 0)


def resume(work, series):
    """Commit the resolved conflicting patch and go on; report the conflict again if markers remain."""
    work = Path(work)
    state = _load(work)
    names = [Path(p).name for p in series]
    if names != state["series"]:
        raise ReapplyError("the series changed since the re-apply started: {} vs {}".format(names, state["series"]))
    i = state.get("conflict")
    if i is None:
        raise ReapplyError("no conflict is waiting to be resolved")
    g = _git(work)
    stuck = _from_json(state["results"][i])
    left = _markers(work, [p for p, _ in stuck.conflicts])
    if left:
        stuck.conflicts = left
        state["results"][i] = _to_json(stuck)
        _save(work, state)
        return [_from_json(d) for d in state["results"]] + [PatchResult(n, "pending") for n in names[i + 1:]]
    state["commits"].append(g.commit_all("fk: " + stuck.name + " (resolved)"))
    state["results"][i] = _to_json(PatchResult(stuck.name, "merged", [], stuck.detail))
    state.pop("conflict", None)
    return _run(g, work, list(series), state, i + 1)


def _finished(work):
    state = _load(work)
    done = state["results"]
    if "conflict" in state or len(done) != len(state["series"]) \
            or any(d["status"] not in ("clean", "merged") for d in done):
        raise ReapplyError("the re-apply in {} is not finished".format(work))
    if _git(work).run("status", "--porcelain", "--untracked-files=all").strip():
        raise ReapplyError("{} has changes that were not committed by fork-keeper".format(work))
    return state


def per_patch_diffs(work):
    state = _finished(work)
    g = _git(work)
    chain = [state["base"]] + state["commits"]
    return [g.diff(a, b, []) for a, b in zip(chain, chain[1:])]


def result_tree(work):
    _finished(work)
    return Path(work)
