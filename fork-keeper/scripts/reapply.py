"""Re-apply a copy's patch series on a new base, three-way, stopping at the first conflict.

The work repo gets the old base, then the new base on top (so every blob a
patch names is present for ``--3way``), then one commit per applied patch.
Every commit stages exactly the files ``patches.walk_files`` lists, so no
``.gitignore`` can hide a file and no system file (desktop.ini) can slip in.
The work repo is offline. Progress lives in ``.git/fk-progress.json``, inside
git's own folder, so it is never committed and never part of the result.

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
_OPEN = b"<<<<<<< "
_CLOSE = b">>>>>>> "


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

def _git(work):
    return SafeGit(work, offline=True)


def _files(work, state):
    return patches.walk_files(work, state.get("ignore", []))


def _commit(g, work, state, message):
    return g.commit_all(message, _files(work, state))


def _marker_count(data):
    return sum(1 for l in data.split(b"\n") if l.startswith((_OPEN, _CLOSE)))


def _markers(g, work, paths):
    """Conflict-start lines git wrote: only files with more marker lines than their committed version."""
    head = {p: b for _, b, p in g.ls_tree("HEAD", "")}
    out = []
    for p in paths:
        f = Path(work) / p
        if not f.is_file():
            continue
        data = f.read_bytes()
        baseline = _marker_count(g.cat_blob(head[p])) if p in head else 0
        if _marker_count(data) > baseline:
            out += [(p, n) for n, line in enumerate(data.split(b"\n"), 1) if line.startswith(_OPEN)]
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
    found = _markers(g, work, unmerged)
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
        state["commits"].append(_commit(g, work, state, "fk: " + r.name))
    state["results"] = [_to_json(x) for x in results]
    state.pop("conflict", None)
    _save(work, state)
    return results


def start(old_base, new_base, series, work, ignore=()):
    work = Path(work)
    if work.exists() and any(work.iterdir()):
        raise ReapplyError("work folder {} is not empty".format(work))
    g = SafeGit.init(work, bare=False, offline=True)
    g.run("config", "core.fileMode", "false")
    state = {"series": [Path(p).name for p in series], "ignore": list(ignore), "base": None,
             "commits": [], "results": []}
    patches.copy_tree(old_base, work, ignore)
    _commit(g, work, state, "fk: old base")
    patches.clear_work_tree(work)
    patches.copy_tree(new_base, work, ignore)
    state["base"] = _commit(g, work, state, "fk: new base")
    return _run(g, work, list(series), state, 0)


def _staged_differs_from_head(g, work, state):
    g.stage_exactly(_files(work, state))
    g.run("diff", "--cached", "--quiet", "HEAD", ok_codes=(0, 1))
    return g.last_returncode == 1


def resume(work, series, accept_empty=False):
    """Commit the resolved conflicting patch and go on.

    The conflict stays open while git's markers remain, or while the work tree
    is unchanged since the conflict, unless ``accept_empty`` says the patch's
    change is to be dropped.
    """
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

    def still_open(conflicts, detail):
        stuck.conflicts, stuck.detail = conflicts, detail
        state["results"][i] = _to_json(stuck)
        _save(work, state)
        return [_from_json(d) for d in state["results"]] + [PatchResult(n, "pending") for n in names[i + 1:]]

    left = _markers(g, work, [p for p, _ in stuck.conflicts])
    if left:
        return still_open(left, "conflict markers are still in the files")
    if not _staged_differs_from_head(g, work, state) and not accept_empty:
        return still_open(stuck.conflicts, "nothing changed since the conflict; resolve it, or accept "
                                           "an empty result to drop this patch's change")
    state["commits"].append(_commit(g, work, state, "fk: " + stuck.name + " (resolved)"))
    state["results"][i] = _to_json(PatchResult(stuck.name, "merged", [], stuck.detail))
    state.pop("conflict", None)
    return _run(g, work, list(series), state, i + 1)


def _finished(work):
    state = _load(work)
    done = state["results"]
    if "conflict" in state or len(done) != len(state["series"]) \
            or any(d["status"] not in ("clean", "merged") for d in done):
        raise ReapplyError("the re-apply in {} is not finished".format(work))
    if _staged_differs_from_head(_git(work), work, state):
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
