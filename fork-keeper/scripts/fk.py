"""fork-keeper command line: register, status, check, mark-checked, record-verdict, reapply,
install, verify, inventory.

Exit codes: 0 ok, 1 differences or refusal, 2 usage or registry error, 3 needs
approval (the plan was printed and nothing was written).

The audit lock: a pin needs an audit record to be registered; ``reapply`` and
``install`` refuse until ``record-verdict`` has stored a delta-audit verdict
for the checked commit, and ``DO NOT INSTALL`` stops both. Writes to the
registry, the copy and its installs happen only with ``--yes``, which the
skill passes after the user agrees. Nothing from an original is executed.
Text that comes from an original (commit subjects, authors, paths) is escaped
before it is printed, so it cannot drive the terminal.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse  # noqa: E402
import dataclasses  # noqa: E402
import datetime  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402

import check as check_mod  # noqa: E402
import gitsafe  # noqa: E402
import inventory as inventory_mod  # noqa: E402
import patches  # noqa: E402
import reapply as reapply_mod  # noqa: E402
import registry  # noqa: E402
import tree  # noqa: E402
import verify as verify_mod  # noqa: E402
from gitsafe import GitError, SafeGit  # noqa: E402
from registry import RegistryError  # noqa: E402

OK, DIFF, USAGE, APPROVAL = 0, 1, 2, 3
GIT_FLOOR = (2, 32, 0)
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
MAX_SIGNATURES = 30
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SHORT = re.compile(r"[0-9a-f]{7,39}")
_FULL = re.compile(r"[0-9a-f]{40}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ERRORS = (GitError, tree.TreeError, patches.PatchError, check_mod.CheckError, reapply_mod.ReapplyError,
           verify_mod.VerifyError, OSError)

_ALLOW_FILE = False
_TODAY = None


class Stop(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# -- test hooks and seams ------------------------------------------------------

def _set_allow_file(flag):
    """Test hook: allow file:// origins. Never reachable from the command line."""
    global _ALLOW_FILE
    _ALLOW_FILE = bool(flag)
    registry._set_allow_file(flag)


def _set_today(day):
    global _TODAY
    _TODAY = day


def _today():
    return _TODAY or datetime.date.today().isoformat()


def _write_file(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _git_version():
    try:
        r = subprocess.run(["git", "--version"], capture_output=True, text=True)
    except OSError:
        return (0, 0, 0)
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", r.stdout or "")
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


# -- small helpers -------------------------------------------------------------

def clean(text):
    """Escape everything that is not printable, so upstream text cannot drive the terminal."""
    out = []
    for ch in str(text):
        if ch.isprintable() or ch == "\t":
            out.append(ch)
        elif ord(ch) < 0x100:
            out.append("\\x{:02x}".format(ord(ch)))
        else:
            out.append("\\u{:04x}".format(ord(ch)))
    return "".join(out)


def _rmtree(path):
    def retry(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if Path(path).exists():
        shutil.rmtree(path, onexc=retry)


def _same(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _registry(a):
    return Path(a.registry)


def _config(a):
    return registry.load_config(_registry(a))


def _audit_text(cfg):
    path = cfg.get("auditRecords")
    if not path:
        return ""
    p = registry.expand(path)
    return p.read_text(encoding="utf-8-sig") if p.is_file() else ""


def _audited(text, record, sha):
    return any(sha[:7] in line for line in inventory_mod.audit_lines(text, record))


def _load(a, name):
    if not _NAME.fullmatch(name or ""):
        raise Stop(USAGE, "{!r} is not a copy name".format(name))
    folder = _registry(a) / "copies" / name
    if not (folder / "copy.json").is_file():
        raise Stop(USAGE, "no copy named {!r} in {}".format(name, _registry(a)))
    return registry.load_copy(folder)


def _pick_origin(c, oid):
    if oid:
        for o in c.origins:
            if o.id == oid:
                return o
        raise Stop(USAGE, "{} has no origin {!r}".format(c.name, oid))
    if len(c.origins) == 1:
        return c.origins[0]
    raise Stop(USAGE, "{} has several origins ({}); name one with --origin".format(
        c.name, ", ".join(o.id for o in c.origins)))


def _srcs(entries):
    return [] if any(e.src == "." for e in entries) else [e.src for e in entries]


def _mirror_git(folder, origin, refs):
    path = Path(folder) / (origin.id + ".git")
    g = SafeGit.init(path, bare=True, allow_file=_ALLOW_FILE) if not path.exists() \
        else SafeGit(path, allow_file=_ALLOW_FILE)
    g.fetch(origin.url, refs)
    return g


def _prefetch(g, commit, entries):
    """One batched fetch of the mapped blobs, instead of one lazy fetch per file."""
    try:
        g.diff(EMPTY_TREE, commit, _srcs(entries), stat=True)
    except GitError:
        pass


def _build(dest, parts, online=True):
    if online:
        for _, g, commit, entries in parts:
            _prefetch(g, commit, entries)
    return tree.build_base(parts, dest)


def _work(a, name):
    return Path(a.scratch) / ("fk-" + name)


def _state_path(a, name):
    return _work(a, name) / "state.json"


def _read_state(a, c):
    p = _state_path(a, c.name)
    if not p.is_file():
        raise Stop(USAGE, "no check in progress for {}; run check first".format(c.name))
    state = json.loads(p.read_text(encoding="utf-8"))
    if state.get("copy") != c.name:
        raise Stop(USAGE, "{} belongs to another copy".format(p))
    return state


def _write_state(a, c, state):
    _write_file(_state_path(a, c.name), (json.dumps(state, indent=2) + "\n").encode("utf-8"))


def _locked_state(a, c):
    state = _read_state(a, c)
    verdict = state.get("verdict")
    if verdict is None:
        raise Stop(DIFF, "no delta-audit verdict is recorded for {} at {}; audit the snapshot, then run "
                         "record-verdict".format(c.name, state["new"][:12]))
    if verdict == "DO NOT INSTALL":
        raise Stop(DIFF, "the delta audit of {} at {} said DO NOT INSTALL; the update stops here".format(
            c.name, state["new"][:12]))
    return state


def _emit(a, lines, data):
    if getattr(a, "json", False):
        print(json.dumps(data, indent=2, ensure_ascii=True))
    else:
        for line in lines:
            print(line)


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:50].rstrip("-") or "local-changes")


def _repo_of(path):
    p = Path(path).resolve()
    for d in (p, *p.parents):
        if (d / ".git").exists():
            return d
    return None


# -- register --------------------------------------------------------------------

def _resolve_pins(raw, folder):
    """Turn short pins into full SHAs by fetching each origin's track; returns {origin id: SafeGit}."""
    gits = {}
    for o in raw["origins"]:
        pin = o["pin"]
        origin = registry.Origin(o["id"], o["url"], "0" * 40, o["track"], "", [], None, None)
        if _FULL.fullmatch(pin):
            gits[o["id"]] = _mirror_git(folder, origin, [pin])
            continue
        g = _mirror_git(folder, origin, [o["track"]])
        found = [s for s in g.run("rev-list", gitsafe.name_ref(o["track"])).decode().split() if s.startswith(pin)]
        if len(found) != 1:
            raise Stop(USAGE, "origin {}: pin {} matches {} commits on {}; give the full SHA".format(
                o["id"], pin, len(found), o["track"]))
        full = found[0]
        o["pin"] = full
        audit = o.get("audit")
        if isinstance(audit, dict) and isinstance(audit.get("commit"), str) and full.startswith(audit["commit"]):
            audit["commit"] = full
        g.fetch(o["url"], [full])
        gits[o["id"]] = g
    return gits


def cmd_register(a):
    reg = _registry(a)
    cfg = registry.load_config(reg)
    try:
        raw = json.loads(Path(a.draft).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise Stop(USAGE, "cannot read the draft {}: {}".format(a.draft, e))
    name = raw.get("name") if isinstance(raw, dict) else None
    if not _NAME.fullmatch(name or ""):
        raise Stop(USAGE, "the draft needs a plain name (letters, digits, . _ -)")
    target = reg / "copies" / name
    if target.exists():
        raise Stop(DIFF, "{} is already registered at {}".format(name, target))
    probe = json.loads(json.dumps(raw))
    for o in probe.get("origins") or []:
        if isinstance(o, dict):
            for holder, key in ((o, "pin"), (o.get("audit") or {}, "commit")):
                if isinstance(holder, dict) and isinstance(holder.get(key), str) and _SHORT.fullmatch(holder[key]):
                    holder[key] = "0" * 40
    registry.parse_copy(probe, target)

    work = Path(tempfile.mkdtemp(prefix="fk-register-{}-".format(name), dir=a.scratch))
    gits = _resolve_pins(raw, work)
    c = registry.parse_copy(raw, target)
    text = _audit_text(cfg)
    if not cfg.get("auditRecords"):
        raise Stop(DIFF, "auditRecords is not set in fork-keeper.json; the audit lock needs it")
    for o in c.origins:
        if o.audit is None or o.audit.commit != o.pin:
            raise Stop(DIFF, "origin {}: the pin needs an audit (audit.commit must equal the pin)".format(o.id))
        if o.audit.verdict == "DO NOT INSTALL":
            raise Stop(DIFF, "origin {}: the audit said DO NOT INSTALL".format(o.id))
        if not _audited(text, o.audit.record, o.pin):
            raise Stop(DIFF, "origin {}: no audit record for `{}` at {} in {}".format(
                o.id, o.audit.record, o.pin[:7], cfg["auditRecords"]))
    if not Path(c.copy).is_dir():
        raise Stop(USAGE, "the copy folder {} does not exist".format(c.copy))

    ignore = cfg.get("ignore", [])
    base = work / "base"
    _build(base, [(o.id, gits[o.id], o.pin, o.map) for o in c.origins])
    eol = patches.eol_mismatches(base, c.copy)
    if eol:
        raise Stop(DIFF, "line endings differ between the original and the copy in: {}. Fix the copy's line "
                         "endings (or git's autocrlf) and register again.".format(", ".join(clean(p) for p in eol)))
    diff = patches.generate(base, c.copy, work / "gen", ignore)
    manifest = verify_mod.hash_tree(c.copy, ignore)
    patch_name = "0001-{}.patch".format(_slug(a.summary))
    series = []
    if diff:
        header = patches.Dep3(description=a.summary, applies_to="{}@{}".format(c.origins[0].id, c.origins[0].pin),
                              last_update=_today())
        rendered = patches.render(header, diff)
        staged = work / "patches" / patch_name
        _write_file(staged, rendered)
        series = [staged]
    results = reapply_mod.start(base, base, series, work / "roundtrip", ignore)
    if any(r.status != "clean" for r in results) or \
            verify_mod.hash_tree(reapply_mod.result_tree(work / "roundtrip"), ignore) != manifest:
        raise Stop(DIFF, "the generated patch does not rebuild the copy exactly; nothing was written")

    lines = ["Would write {}:".format(target), "  copy.json ({} origin(s), pins {})".format(
        len(c.origins), ", ".join(o.pin[:12] for o in c.origins)),
        "  manifest.json ({} files)".format(len(manifest))]
    lines.append("  series and patches/{} ({} bytes)".format(patch_name, len(rendered)) if diff
                 else "  series (empty: the copy equals the original)")
    if not registry.has_text_rule(reg):
        lines.append("WARNING: {} has no '{}' rule in .gitattributes; git may rewrite the patches' line "
                     "endings".format(reg, registry.TEXT_RULE))
    if not a.yes:
        lines.append("Nothing written. Run again with --yes once the user agrees.")
        _emit(a, lines, {"target": str(target), "manifest": len(manifest), "patch": patch_name if diff else None})
        return APPROVAL
    try:
        target.mkdir(parents=True)
        registry.save_copy(c)
        registry.save_manifest(target, manifest, _today())
        registry.write_series(target, [patch_name] if diff else [])
        if diff:
            _write_file(target / "patches" / patch_name, rendered)
    except (OSError, RegistryError) as e:
        _rmtree(target)
        raise Stop(DIFF, "writing {} failed ({}); nothing was kept".format(target, e))
    lines[0] = "Wrote {}:".format(target)
    lines += ["To commit (asks first):", '  git -C "{}" add copies/{}'.format(reg, name),
              '  git -C "{}" commit -m "feat({}): register copy"'.format(reg, name)]
    _emit(a, lines, {"target": str(target), "manifest": len(manifest), "patch": patch_name if diff else None})
    return OK


# -- status, mark-checked --------------------------------------------------------

def cmd_status(a):
    cfg = _config(a)
    text = _audit_text(cfg)
    today = datetime.date.fromisoformat(_today())
    lines, rows, missing = [], [], False
    for c in registry.list_copies(_registry(a)):
        for o in c.origins:
            audited = o.audit is not None and o.audit.commit == o.pin and _audited(text, o.audit.record, o.pin)
            days = (today - datetime.date.fromisoformat(o.last_checked)).days if o.last_checked else None
            if a.older_than is not None and days is not None and days <= a.older_than:
                continue
            missing = missing or not audited
            rows.append({"copy": c.name, "origin": o.id, "pin": o.pin, "audited": audited, "days": days})
            lines.append("{}  {}  {}  {}  {}".format(c.name, o.id, o.pin[:12], "audited" if audited else "NO AUDIT RECORD",
                                                    "never checked" if days is None else "{} days since last check".format(days)))
    if not rows:
        lines.append("No copies to show.")
    _emit(a, lines, {"copies": rows})
    return DIFF if missing else OK


def cmd_mark_checked(a):
    c = _load(a, a.copy)
    origins = [_pick_origin(c, a.origin)] if a.origin else list(c.origins)
    ids = {o.id for o in origins}
    lines = ["{} / {}: lastChecked {} -> {}".format(c.name, o.id, o.last_checked, _today()) for o in origins]
    if not a.yes:
        _emit(a, lines + ["Nothing written. Run again with --yes once the user agrees."], {"change": lines})
        return APPROVAL
    new = [dataclasses.replace(o, last_checked=_today()) if o.id in ids else o for o in c.origins]
    registry.save_copy(dataclasses.replace(c, origins=new))
    _emit(a, lines, {"change": lines})
    return OK


# -- check, record-verdict -------------------------------------------------------

def cmd_check(a):
    c = _load(a, a.copy)
    cfg = _config(a)
    o = _pick_origin(c, a.origin)
    ref = a.ref or o.track
    work = _work(a, c.name)
    if work.exists():
        if not (work / ".fork-keeper").is_file():
            raise Stop(USAGE, "{} exists and was not made by fork-keeper; choose another --scratch".format(work))
        _rmtree(work)
    work.mkdir(parents=True)
    (work / ".fork-keeper").write_text("scratch folder made by fork-keeper\n")
    gits = {}
    for x in c.origins:
        refs = [x.pin] + ([ref] if x.id == o.id and ref != x.pin else [])
        gits[x.id] = _mirror_git(work, x, refs)
    g = gits[o.id]
    new = ref if _FULL.fullmatch(ref) else g.rev_parse(gitsafe.name_ref(ref))
    report = check_mod.summarize(g, o, new)
    try:
        extracted = check_mod.extract(g, o, new, work / "audit")
        old_parts = [(x.id, gits[x.id], x.pin, x.map) for x in c.origins]
        new_parts = [(x.id, gits[x.id], new if x.id == o.id else x.pin, x.map) for x in c.origins]
        _build(work / "base-old", old_parts)
        _build(work / "base-new", new_parts)
    except (check_mod.CheckError, tree.TreeError) as e:
        raise Stop(DIFF, "ALARM refused: {}".format(clean(e)))
    _write_file(work / "delta.diff", g.diff(o.pin, new, _srcs(o.map)))

    lines = ["ALARM {}: {}".format(al.kind, clean(al.detail)) for al in report.alarms]
    lines.append("{} / {}: {} -> {} ({})".format(c.name, o.id, o.pin[:12], new[:12], clean(ref)))
    commits = []
    for n, cm in enumerate(report.commits):
        sig = check_mod.signature(o.url, cm["sha"]) if n < MAX_SIGNATURES else "not checked"
        commits.append({**cm, "signature": sig})
        lines.append("  {} {} {} [{}] {}".format(cm["sha"][:12], cm["date"][:10], clean(cm["author"]), sig,
                                                 clean(cm["subject"])))
    if report.mapped_changes:
        lines.append("Mapped changes:")
        lines += ["  {} {}".format(s, clean(p)) for s, p in report.mapped_changes]
        lines += ["  " + clean(l) for l in report.stat.splitlines()]
    if report.outside:
        lines.append("Outside the map: " + ", ".join("{} {}".format(clean(k), v) for k, v in sorted(report.outside.items())))
    state = {"copy": c.name, "origin": o.id, "old": o.pin, "new": new, "ref": ref,
             "repos": {x: str(work / (x + ".git")) for x in gits}, "audit": str(work / "audit"),
             "diff": str(work / "delta.diff"), "checked": _today(), "verdict": None, "record": None, "date": None}
    _write_state(a, c, state)
    data = {"origin": o.id, "old": o.pin, "new": new, "alarms": [dataclasses.asdict(x) for x in report.alarms],
            "commits": commits, "mapped_changes": report.mapped_changes, "outside": report.outside,
            "audit": state["audit"], "diff": state["diff"]}
    if not report.mapped_changes and not report.alarms:
        lines.append("No mapped changes. To record today's check (asks first): mark-checked --copy {} --origin {} --yes"
                     .format(c.name, o.id))
        _emit(a, lines, data)
        return OK
    lines += ["Audit snapshot: {} new or changed files in {}".format(len(extracted), state["audit"]),
              "Full diff of the mapped paths: {}".format(state["diff"]),
              "Next: audit the delta with vet-before-install, then record-verdict."]
    _emit(a, lines, data)
    return DIFF


def cmd_record_verdict(a):
    c = _load(a, a.copy)
    state = _read_state(a, c)
    if a.verdict not in registry.VERDICTS:
        raise Stop(USAGE, "verdict must be one of: {}".format(", ".join(registry.VERDICTS)))
    try:
        ok = bool(_DATE.fullmatch(a.date)) and datetime.date.fromisoformat(a.date)
    except ValueError:
        ok = False
    if not ok:
        raise Stop(USAGE, "date must be YYYY-MM-DD")
    state.update({"verdict": a.verdict, "record": a.record, "date": a.date})
    _write_state(a, c, state)
    _emit(a, ["{} at {}: verdict {} recorded (scratch only)".format(c.name, state["new"][:12], a.verdict)],
          {"verdict": a.verdict})
    return OK


# -- reapply, install --------------------------------------------------------------

def _describe(results):
    lines = []
    for r in results:
        lines.append("{}: {}".format(r.name, r.status))
        for p, n in r.conflicts:
            lines.append("  {}:{}".format(clean(p), n) if n else
                         "  {}: file-level clash (deleted upstream, added on both sides, or not applicable)".format(clean(p)))
        if r.status == "conflict" and r.detail:
            lines += ["  | " + clean(l) for l in r.detail.splitlines()[:5]]
    return lines


def cmd_reapply(a):
    c = _load(a, a.copy)
    cfg = _config(a)
    _locked_state(a, c)
    work = _work(a, c.name)
    series = registry.read_series(c.folder)
    if a.cont:
        results = reapply_mod.resume(work / "work", series, accept_empty=a.accept_empty)
    else:
        if not (work / "base-old").is_dir() or not (work / "base-new").is_dir():
            raise Stop(USAGE, "the bases are missing; run check first")
        _rmtree(work / "work")
        results = reapply_mod.start(work / "base-old", work / "base-new", series, work / "work", cfg.get("ignore", []))
    done = all(r.status in ("clean", "merged") for r in results)
    lines = _describe(results)
    lines.append("All patches applied; install shows what would change." if done else
                 "Resolve the conflict in {}, then reapply --continue (add --accept-empty to drop the patch's change)."
                 .format(work / "work"))
    _emit(a, lines, {"results": [dataclasses.asdict(r) for r in results]})
    return OK if done else DIFF


def _changes(result_files, target, ignore):
    current = verify_mod.hash_tree(target, ignore)
    add = sorted(set(result_files) - set(current))
    change = sorted(k for k in result_files if k in current and current[k] != result_files[k])
    delete = sorted(set(current) - set(result_files))
    return current, add, change, delete


def cmd_install(a):
    c = _load(a, a.copy)
    cfg = _config(a)
    ignore = cfg.get("ignore", [])
    state = _locked_state(a, c)
    o = _pick_origin(c, state["origin"])
    work = _work(a, c.name)
    try:
        result = reapply_mod.result_tree(work / "work")
    except reapply_mod.ReapplyError as e:
        raise Stop(DIFF, "nothing to install: {}".format(e))
    res_files = verify_mod.hash_tree(result, ignore)
    targets = [Path(c.copy)] + [Path(i) for i in c.installs if not _same(i, c.copy)]
    lines, plan = [], []
    for t in targets:
        current, add, change, delete = _changes(res_files, t, ignore)
        plan.append((t, current))
        lines.append("{}: {} added, {} changed, {} deleted".format(t, len(add), len(change), len(delete)))
        lines += ["  + " + clean(p) for p in add] + ["  ~ " + clean(p) for p in change] + ["  - DELETE " + clean(p) for p in delete]
    lines.append("Registry: {} moves {} -> {}; patches regenerated; manifest rewritten".format(
        o.id, state["old"][:12], state["new"][:12]))
    if not a.yes:
        lines.append("Nothing written. Run again with --yes once the user agrees.")
        _emit(a, lines, {"targets": [str(t) for t in targets]})
        return APPROVAL

    backup = work / "backup"
    _rmtree(backup)
    shutil.copytree(c.folder, backup / "registry")
    for n, (t, current) in enumerate(plan):
        for rel in current:
            dst = backup / str(n) / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(t / rel, dst)
    journal = []
    try:
        for n, (t, current) in enumerate(plan):
            for rel in sorted(res_files):
                dst, data = t / rel, (result / rel).read_bytes()
                if dst.is_file() and dst.read_bytes() == data:
                    continue
                journal.append((dst, backup / str(n) / rel if rel in current else None))
                _write_file(dst, data)
            for rel in sorted(set(current) - set(res_files)):
                journal.append((t / rel, backup / str(n) / rel))
                (t / rel).unlink()
        diffs = reapply_mod.per_patch_diffs(work / "work")
        for path, d in zip(registry.read_series(c.folder), diffs):
            h, _ = patches.parse(path.read_bytes())
            h = dataclasses.replace(h, applies_to="{}@{}".format(o.id, state["new"]), last_update=_today())
            _write_file(path, patches.render(h, d))
            if not d:
                lines.append("Note: {} is now empty; remove it from series if it is no longer needed.".format(path.name))
        audit = registry.Audit(state["new"], state["verdict"], state["date"], state["record"])
        origins = [dataclasses.replace(x, pin=state["new"], audit=audit, last_checked=_today()) if x.id == o.id else x
                   for x in c.origins]
        registry.save_copy(dataclasses.replace(c, origins=origins))
        registry.save_manifest(c.folder, res_files, _today())
    except (*_ERRORS, RegistryError) as e:
        for dst, src in reversed(journal):
            if src is not None:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
            else:
                Path(dst).unlink(missing_ok=True)
        _rmtree(c.folder)
        shutil.copytree(backup / "registry", c.folder)
        raise Stop(DIFF, "install failed ({}); the copy, its installs and the registry entry were restored "
                         "from {}".format(e, backup))
    state["installed"] = state["new"]
    _write_state(a, c, state)
    reg = _registry(a)
    lines += ["Installed {} at {}.".format(c.name, state["new"][:12]), "To commit (asks first):",
              '  git -C "{}" add copies/{}'.format(reg, c.name),
              '  git -C "{}" commit -m "chore({}): update {} to {}"'.format(reg, c.name, o.id, state["new"][:12])]
    repo = _repo_of(c.copy)
    if repo is not None and not _same(repo, reg):
        lines += ['  git -C "{}" add -A'.format(repo),
                  '  git -C "{}" commit -m "chore: update from {} {}"'.format(repo, o.id, state["new"][:12])]
    lines.append("Reinstall anything built from this copy (for example a packaged .skill).")
    _emit(a, lines, {"installed": state["new"]})
    return OK


# -- verify, inventory --------------------------------------------------------------

def _rebuild(a, c, cfg):
    ignore = cfg.get("ignore", [])
    tmp = Path(tempfile.mkdtemp(prefix="fk-verify-{}-".format(c.name), dir=a.scratch))
    parts = [(o.id, _mirror_git(tmp, o, [o.pin]), o.pin, o.map) for o in c.origins]
    _build(tmp / "base", parts)
    results = reapply_mod.start(tmp / "base", tmp / "base", registry.read_series(c.folder), tmp / "work", ignore)
    if any(r.status != "clean" for r in results):
        return ["  rebuild: the series does not apply cleanly on the pins"]
    built = verify_mod.hash_tree(reapply_mod.result_tree(tmp / "work"), ignore)
    manifest = registry.load_manifest(c.folder)
    return [] if built == manifest else ["  rebuild: pins plus patches differ from the manifest"]


def cmd_verify(a):
    cfg = _config(a)
    if a.rebuild and not a.scratch:
        raise Stop(USAGE, "--rebuild needs --scratch")
    copies = [_load(a, a.copy)] if a.copy else registry.list_copies(_registry(a))
    lines, rows, bad = [], [], False
    for c in copies:
        manifest = registry.load_manifest(c.folder)
        targets = [Path(c.copy)] + [Path(i) for i in c.installs if not _same(i, c.copy)]
        for t in targets:
            r = verify_mod.verify_tree(t, manifest, cfg.get("ignore", []))
            bad = bad or not r.ok
            rows.append({"copy": c.name, "path": str(t), **dataclasses.asdict(r)})
            lines.append("{}  {}  {}".format(c.name, t, "ok" if r.ok else "DIFFERS"))
            for k in ("modified", "missing", "extra"):
                lines += ["  {} {}".format(k, clean(p)) for p in getattr(r, k)]
        if a.rebuild:
            problems = _rebuild(a, c, cfg)
            bad = bad or bool(problems)
            lines += problems or ["  rebuild: pins plus patches match the manifest"]
    _emit(a, lines, {"results": rows})
    return DIFF if bad else OK


def cmd_inventory(a):
    cfg = _config(a)
    home = Path(a.home) if a.home else Path.home()
    items = inventory_mod.run(cfg, registry.list_copies(_registry(a)), _audit_text(cfg), home)
    lines = []
    for colour in inventory_mod.COLOURS:
        group = [i for i in items if i.colour == colour]
        if group:
            lines.append("{} ({})".format(colour.upper(), len(group)))
            lines += ["  {} {}  {}  {}".format(i.kind, clean(i.name), clean(i.path), "; ".join(clean(r) for r in i.reasons))
                      for i in group]
    _emit(a, lines or ["Nothing installed was found."], {"items": [dataclasses.asdict(i) for i in items]})
    return OK


# -- entry point ---------------------------------------------------------------------

def _parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--registry", required=True)
    common.add_argument("--json", action="store_true")
    p = argparse.ArgumentParser(prog="fk.py", description="Keep patched copies of third-party skills and repos.")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("register", parents=[common])
    s.add_argument("--draft", required=True)
    s.add_argument("--scratch", required=True)
    s.add_argument("--summary", required=True)
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_register)

    s = sub.add_parser("status", parents=[common])
    s.add_argument("--older-than", type=int, dest="older_than")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("check", parents=[common])
    s.add_argument("--copy", required=True)
    s.add_argument("--scratch", required=True)
    s.add_argument("--origin")
    s.add_argument("--ref")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("mark-checked", parents=[common])
    s.add_argument("--copy", required=True)
    s.add_argument("--origin")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_mark_checked)

    s = sub.add_parser("record-verdict", parents=[common])
    s.add_argument("--copy", required=True)
    s.add_argument("--scratch", required=True)
    s.add_argument("--verdict", required=True)
    s.add_argument("--record", required=True)
    s.add_argument("--date", required=True)
    s.set_defaults(func=cmd_record_verdict)

    s = sub.add_parser("reapply", parents=[common])
    s.add_argument("--copy", required=True)
    s.add_argument("--scratch", required=True)
    s.add_argument("--continue", action="store_true", dest="cont")
    s.add_argument("--accept-empty", action="store_true", dest="accept_empty")
    s.set_defaults(func=cmd_reapply)

    s = sub.add_parser("install", parents=[common])
    s.add_argument("--copy", required=True)
    s.add_argument("--scratch", required=True)
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_install)

    s = sub.add_parser("verify", parents=[common])
    s.add_argument("--copy")
    s.add_argument("--rebuild", action="store_true")
    s.add_argument("--scratch")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("inventory", parents=[common])
    s.add_argument("--home")
    s.set_defaults(func=cmd_inventory)
    return p


def main(argv=None):
    try:
        a = _parser().parse_args(argv)
    except SystemExit as e:
        return OK if e.code in (0, None) else USAGE
    version = _git_version()
    if version < GIT_FLOOR:
        print("git 2.32 or later is required (found {})".format(".".join(map(str, version))))
        return USAGE
    try:
        return a.func(a)
    except Stop as s:
        print(s.message)
        return s.code
    except RegistryError as e:
        print("registry error: {}".format(e))
        return USAGE
    except _ERRORS as e:
        print("error: {}".format(clean(e)))
        return DIFF


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
