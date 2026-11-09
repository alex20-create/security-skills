"""Offline inventory of installed skills, plugins and MCP servers, each with a colour and its reasons.

Colours: green (registered and matching, or matching its own source, or an
audited managed set), amber (linked, audited but not registered, plugin that
updates in place, unpinned npx package), red (modified, unaudited, commit
differs), unclassified (nothing to compare with). The first rule that
matches sets the colour; every matching rule adds its reason.

Nothing is executed and no network is used. MCP ``env`` values are never read.
"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from registry import RegistryError, load_manifest
from verify import VerifyError, hash_tree, verify_tree

COLOURS = ("red", "amber", "unclassified", "green")
_HEX = re.compile(r"\b[0-9a-f]{7,40}\b")
_SEMVER = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")
_SKIP_DIRS = {"node_modules", "__pycache__"}


@dataclass
class Item:
    kind: str
    name: str
    path: str
    colour: str
    reasons: list = field(default_factory=list)


class _Verdict:
    def __init__(self):
        self.colour = None
        self.reasons = []

    def hit(self, colour, *reasons):
        if self.colour is None:
            self.colour = colour
        self.reasons += [r for r in reasons if r]


def audit_lines(audit_text, name):
    needle = "`{}`".format(name)
    return [l for l in audit_text.splitlines() if needle in l]


def _expand(spec, home):
    if spec == "~" or spec.startswith(("~/", "~\\")):
        return Path(home) / spec[2:]
    return Path(spec)


def _is_link(p):
    return p.is_symlink() or p.is_junction()


def _same(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


# -- skills -----------------------------------------------------------------

def _own_sources(roots, home):
    found = {}
    for spec in roots:
        root = _expand(spec, home)
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in _SKIP_DIRS)
            if "SKILL.md" in filenames:
                found.setdefault(Path(dirpath).name, []).append(Path(dirpath))
    return found


def _within(installed, source):
    return bool(installed) and all(source.get(k) == v for k, v in installed.items())


def _skill(child, config, copies, audit, sources):
    name, ignore = child.name, config.get("ignore", [])
    v = _Verdict()
    if _is_link(child):
        v.hit("amber", "linked, not copied")
    copy = next((c for c in copies if any(_same(i, child) for i in c.installs)), None)
    managed = next(((p, r) for p, r in config.get("managedSets", {}).items() if name.startswith(p)), None)
    if copy is not None:
        try:
            res = verify_tree(child, load_manifest(copy.folder), ignore)
        except (RegistryError, VerifyError) as e:
            v.hit("red", "cannot verify against the registry: {}".format(e))
        else:
            if res.ok:
                v.hit("green", "registered copy, matches its manifest")
            else:
                v.hit("red", *["{}: {}".format(k, ", ".join(getattr(res, k)))
                              for k in ("modified", "missing", "extra", "bytecode") if getattr(res, k)])
    elif name in sources:
        try:
            installed = hash_tree(child, ignore)
            match = next((s for s in sources[name] if _within(installed, hash_tree(s, ignore))), None)
        except VerifyError as e:
            v.hit("amber", "cannot compare with its source: {}".format(e))
        else:
            if match is not None:
                v.hit("green", "matches its source {}".format(match))
            else:
                v.hit("amber", "installed copy differs from its source")
    elif managed is not None:
        prefix, record = managed
        if audit_lines(audit, record):
            v.hit("green", "managed set {}, audited as `{}`".format(prefix, record))
        else:
            v.hit("amber", "managed set {}, no audit record for `{}`".format(prefix, record))
    elif audit_lines(audit, name):
        v.hit("amber", "audited, not registered")
    elif config.get("ownSourceRoots"):
        v.hit("red", "no registry entry, no audit record")
    else:
        v.hit("unclassified", "no registry entry, no audit record, no own source roots to compare with")
    return Item("skill", name, str(child), v.colour, v.reasons)


def _skills(config, copies, audit, home):
    sources = _own_sources(config.get("ownSourceRoots", []), home)
    items = []
    for spec in config.get("skillsRoots", []):
        root = _expand(spec, home)
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir(), key=lambda p: p.name):
            if child.name.startswith(".") or not child.is_dir() or not (child / "SKILL.md").is_file():
                continue
            items.append(_skill(child, config, copies, audit, sources))
    return items


# -- plugins and MCP --------------------------------------------------------

def _command(cfg):
    cmd = str(cfg.get("command") or "")
    raw = cfg.get("args") or []
    args = [str(a) for a in raw if isinstance(a, (str, int, float))] if isinstance(raw, list) else []
    base = cmd.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".cmd", ".exe", ".bat"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
    if base == "cmd" and len(args) >= 2 and args[0].lower() == "/c":
        return _command({"command": args[1], "args": args[2:]})
    return base, args


def _pinned(args):
    spec = next((a for a in args if not a.startswith("-")), None)
    if spec is None:
        return False
    name, sep, version = (spec[1:] if spec.startswith("@") else spec).partition("@")
    return bool(name and sep and _SEMVER.fullmatch(version))


def _mcp(name, cfg, path, owner=None, owner_colour=None):
    cmd, args = _command(cfg)
    origin = ["from plugin {}".format(owner)] if owner else []
    if cmd == "npx" and not _pinned(args):
        return Item("mcp", name, path, "amber", ["npx package not pinned to a version"] + origin)
    return Item("mcp", name, path, owner_colour or "unclassified", origin)


def _server_map(data, direct_allowed):
    if not isinstance(data, dict):
        return {}
    servers = data.get("mcpServers") if "mcpServers" in data or not direct_allowed else data
    return {k: v for k, v in servers.items() if isinstance(v, dict)} if isinstance(servers, dict) else {}


def _plugin_colour(key, sha, audit):
    lines = audit_lines(audit, key)
    short = key.split("@", 1)[0]
    if short != key:
        lines += [l for l in audit_lines(audit, short) if l not in lines]
    tokens = _HEX.findall("\n".join(lines))
    if not lines:
        return "red", "no audit record"
    if sha and any(sha.startswith(t) for t in tokens):
        return "amber", "marketplace plugin updates in place"
    if tokens:
        return "red", "installed commit differs from audited"
    return "red", "audit record names no commit"


def _plugins(audit, home):
    path = Path(home) / ".claude" / "plugins" / "installed_plugins.json"
    if not path.is_file():
        return [], []
    try:
        data = _read_json(path)
    except (OSError, ValueError) as e:
        return [Item("plugin", path.name, str(path), "unclassified", ["could not read: {}".format(e)])], []
    plugins = data.get("plugins") if isinstance(data, dict) else None
    items, servers = [], []
    for key in sorted(plugins if isinstance(plugins, dict) else {}):
        entries = plugins[key] if isinstance(plugins[key], list) else []
        for e in entries:
            if not isinstance(e, dict):
                continue
            colour, reason = _plugin_colour(key, str(e.get("gitCommitSha") or ""), audit)
            install = str(e.get("installPath") or "")
            items.append(Item("plugin", key, install, colour, [reason]))
            mcp_file = Path(install) / ".mcp.json" if install else None
            if mcp_file is not None and mcp_file.is_file():
                try:
                    found = _server_map(_read_json(mcp_file), direct_allowed=True)
                except (OSError, ValueError) as err:
                    items.append(Item("mcp", mcp_file.name, str(mcp_file), "unclassified", ["could not read: {}".format(err)]))
                    continue
                servers += [_mcp(n, c, str(mcp_file), key, colour) for n, c in sorted(found.items())]
    return items, servers


def _claude_json(home):
    path = Path(home) / ".claude.json"
    if not path.is_file():
        return []
    try:
        data = _read_json(path)
    except (OSError, ValueError) as e:
        return [Item("mcp", path.name, str(path), "unclassified", ["could not read: {}".format(e)])]
    out = [_mcp(n, c, str(path), None) for n, c in sorted(_server_map(data, False).items())]
    projects = data.get("projects") if isinstance(data, dict) else None
    for proj in sorted(projects if isinstance(projects, dict) else {}):
        where = "{}#projects/{}".format(path, proj)
        out += [_mcp(n, c, where, None) for n, c in sorted(_server_map(projects[proj], False).items())]
    return out


def _project_folders(home):
    path = Path(home) / ".claude.json"
    try:
        data = _read_json(path) if path.is_file() else {}
    except (OSError, ValueError):
        return []
    projects = data.get("projects") if isinstance(data, dict) else None
    return [Path(p) for p in sorted(projects)] if isinstance(projects, dict) else []


def _mcp_files(config, home):
    """Every .mcp.json in the own source roots, the installed skill folders and the known project folders."""
    found = []
    for spec in config.get("ownSourceRoots", []):
        root = _expand(spec, home)
        if root.is_dir():
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in _SKIP_DIRS)
                if ".mcp.json" in filenames:
                    found.append(Path(dirpath) / ".mcp.json")
    for spec in config.get("skillsRoots", []):
        root = _expand(spec, home)
        if root.is_dir():
            found += [c / ".mcp.json" for c in sorted(root.iterdir(), key=lambda p: p.name) if (c / ".mcp.json").is_file()]
    found += [p / ".mcp.json" for p in _project_folders(home) if (p / ".mcp.json").is_file()]
    seen, out = set(), []
    for f in found:
        key = os.path.normcase(os.path.abspath(f))
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def _file_servers(config, home):
    items = []
    for f in _mcp_files(config, home):
        try:
            servers = _server_map(_read_json(f), direct_allowed=True)
        except (OSError, ValueError) as e:
            items.append(Item("mcp", f.name, str(f), "unclassified", ["could not read: {}".format(e)]))
            continue
        items += [_mcp(n, c, str(f), None) for n, c in sorted(servers.items())]
    return items


def run(config, copies, audit_text, home):
    plugins, plugin_servers = _plugins(audit_text, home)
    return (_skills(config, copies, audit_text, home) + plugins + plugin_servers + _claude_json(home)
            + _file_servers(config, home))
