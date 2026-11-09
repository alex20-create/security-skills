"""Load, validate and save a copy's registry files (copy.json, manifest.json, series)."""

import datetime
import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

VERDICTS = ("LIKELY SAFE", "USE WITH CAUTION", "DO NOT INSTALL")
KINDS = ("skill", "plugin", "repo", "mcp")
DEFAULT_IGNORE = (".DS_Store", "Thumbs.db", "desktop.ini", "__pycache__/")
TEXT_RULE = "copies/** -text"

_SHA = re.compile(r"[0-9a-f]{40}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_HASH = re.compile(r"[0-9a-f]{64}")
_PATCH_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_COPY_KEYS = {"name", "kind", "copy", "installs", "origins"}
_ORIGIN_KEYS = {"id", "url", "pin", "track", "license", "map", "audit", "lastChecked"}
_AUDIT_KEYS = {"commit", "verdict", "date", "record"}
_DEFAULT_CONFIG = {
    "auditRecords": None,
    "skillsRoots": ["~/.claude/skills"],
    "ownSourceRoots": [],
    "managedSets": {},
    "ignore": [],
}
_CONFIG_TYPES = {
    "auditRecords": (str, type(None)),
    "skillsRoots": list,
    "ownSourceRoots": list,
    "managedSets": dict,
    "ignore": list,
}


class RegistryError(Exception):
    pass


@dataclass(frozen=True)
class MapEntry:
    src: str
    dst: str


@dataclass(frozen=True)
class Audit:
    commit: str
    verdict: str
    date: str
    record: str


@dataclass(frozen=True)
class Origin:
    id: str
    url: str
    pin: str
    track: str
    license: str
    map: list
    audit: "Audit | None"
    last_checked: "str | None"


@dataclass(frozen=True)
class Copy:
    name: str
    kind: str
    copy: Path
    installs: list
    origins: list
    folder: Path
    # The strings as written in copy.json. save_copy writes them back only
    # while they still expand to the current paths, so "~/x" survives a round
    # trip and a moved path is never lost.
    copy_spec: str = field(default="", compare=False)
    install_specs: list = field(default_factory=list, compare=False)


def expand(spec):
    return Path(os.path.expanduser(spec))


# -- field checks ------------------------------------------------------------

def _need(obj, key, where, kind=None):
    if not isinstance(obj, dict) or key not in obj:
        raise RegistryError("{}: missing field '{}'".format(where, key))
    value = obj[key]
    if kind is not None and not isinstance(value, kind):
        raise RegistryError("{}: field '{}' has the wrong type ({})".format(where, key, type(value).__name__))
    return value


def _str(obj, key, where):
    value = _need(obj, key, where, str)
    if not value:
        raise RegistryError("{}: field '{}' is empty".format(where, key))
    return value


def _no_extra(obj, allowed, where):
    extra = sorted(set(obj) - allowed)
    if extra:
        raise RegistryError("{}: unknown field(s): {}".format(where, ", ".join(extra)))


def _date(value, what):
    try:
        ok = isinstance(value, str) and bool(_DATE.fullmatch(value)) and datetime.date.fromisoformat(value)
    except ValueError:
        ok = False
    if not ok:
        raise RegistryError("{} must be a real date YYYY-MM-DD, got {!r}".format(what, value))
    return value


def _sha(value, what):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise RegistryError("{} must be a full 40-character lowercase SHA, got {!r}".format(what, value))
    return value


_ALLOW_FILE = False


def _set_allow_file(flag):
    """Test hook: accept file:// origin URLs. The command line never exposes it."""
    global _ALLOW_FILE
    _ALLOW_FILE = bool(flag)


def _url(value, where):
    if _ALLOW_FILE and isinstance(value, str) and value.startswith("file://") \
            and not any(c.isspace() for c in value):
        return value
    if not isinstance(value, str) or not value.startswith("https://") or any(c.isspace() for c in value):
        raise RegistryError("{}: url must be https:// with no whitespace, got {!r}".format(where, value))
    parts = urllib.parse.urlsplit(value)
    if not parts.hostname or parts.username is not None or parts.password is not None:
        raise RegistryError("{}: url must name a host and carry no credentials".format(where))
    return value


def rel_path(value, where):
    """A relative POSIX path that stays inside its root, normalised ('.' allowed)."""
    if not isinstance(value, str) or not value:
        raise RegistryError("{}: path must be a non-empty string".format(where))
    if "\\" in value or ":" in value or value.startswith("/") or any(c in value for c in "\x00\r\n"):
        raise RegistryError("{}: path must be relative and POSIX-style, got {!r}".format(where, value))
    parts = [p for p in PurePosixPath(value).parts if p != "."]
    if any(p == ".." for p in parts):
        raise RegistryError("{}: path must not contain '..', got {!r}".format(where, value))
    if any(p.lower() == ".git" for p in parts):
        raise RegistryError("{}: path must not touch .git, got {!r}".format(where, value))
    return "/".join(parts) or "."


def _dests_clash(a, b):
    a, b = a.casefold(), b.casefold()
    if a == "." or b == ".":
        return False  # the copy root; files under it clash only on equal paths, found by build_base
    return a == b or b.startswith(a + "/") or a.startswith(b + "/")


# -- copy.json ---------------------------------------------------------------

def _origin(raw, where):
    if not isinstance(raw, dict):
        raise RegistryError("{}: each origin must be an object".format(where))
    oid = _str(raw, "id", where)
    where = "{} origin '{}'".format(where, oid)
    _no_extra(raw, _ORIGIN_KEYS, where)
    url = _url(_need(raw, "url", where), where)
    pin = _sha(_need(raw, "pin", where), where + " pin")
    track = _str(raw, "track", where)
    if track.startswith("-") or any(c.isspace() for c in track) or ":" in track:
        raise RegistryError("{}: track must be a branch or tag name, got {!r}".format(where, track))
    entries = []
    for m in _need(raw, "map", where, list):
        if not isinstance(m, dict):
            raise RegistryError("{}: each map entry must be an object".format(where))
        _no_extra(m, {"from", "to"}, where + " map")
        entries.append(MapEntry(rel_path(m.get("from"), where + " map 'from'"),
                                rel_path(m.get("to"), where + " map 'to'")))
    if not entries:
        raise RegistryError("{}: map is empty".format(where))
    audit = raw.get("audit")
    if audit is not None:
        if not isinstance(audit, dict):
            raise RegistryError("{}: audit must be an object or null".format(where))
        _no_extra(audit, _AUDIT_KEYS, where + " audit")
        verdict = _need(audit, "verdict", where + " audit")
        if verdict not in VERDICTS:
            raise RegistryError("{}: audit verdict must be one of {}, got {!r}".format(where, ", ".join(VERDICTS), verdict))
        audit = Audit(_sha(_need(audit, "commit", where + " audit"), where + " audit commit"),
                      verdict, _date(_need(audit, "date", where + " audit"), where + " audit date"),
                      _str(audit, "record", where + " audit"))
    last = raw.get("lastChecked")
    if last is not None:
        _date(last, where + " lastChecked")
    return Origin(oid, url, pin, track, _str(raw, "license", where), entries, audit, last)


def parse_copy(raw, folder):
    where = str(Path(folder) / "copy.json")
    if not isinstance(raw, dict):
        raise RegistryError("{}: must be a JSON object".format(where))
    _no_extra(raw, _COPY_KEYS, where)
    name = _str(raw, "name", where)
    kind = _need(raw, "kind", where)
    if kind not in KINDS:
        raise RegistryError("{}: kind must be one of {}, got {!r}".format(where, ", ".join(KINDS), kind))
    copy_spec = _str(raw, "copy", where)
    install_specs = _need(raw, "installs", where, list)
    if not all(isinstance(s, str) and s for s in install_specs):
        raise RegistryError("{}: installs must be a list of paths".format(where))
    raw_origins = _need(raw, "origins", where, list)
    origins = [_origin(o, where) for o in raw_origins]
    if not origins:
        raise RegistryError("{}: at least one origin is required".format(where))
    ids = [o.id for o in origins]
    if len(set(ids)) != len(ids):
        raise RegistryError("{}: origin ids must be unique".format(where))
    dests = [(o.id, m.dst) for o in origins for m in o.map]
    for i, (oa, da) in enumerate(dests):
        for ob, db in dests[i + 1:]:
            if _dests_clash(da, db):
                raise RegistryError("{}: destinations overlap: '{}' ({}) and '{}' ({})".format(where, da, oa, db, ob))
    return Copy(name, kind, expand(copy_spec), [expand(s) for s in install_specs], origins,
                Path(folder), copy_spec, list(install_specs))


def _read_json(path):
    try:
        return json.loads(Path(path).read_bytes().decode("utf-8-sig"))
    except FileNotFoundError:
        raise RegistryError("{}: not found".format(path))
    except UnicodeDecodeError as e:
        raise RegistryError("{}: not UTF-8: {}".format(path, e))
    except json.JSONDecodeError as e:
        raise RegistryError("{}: invalid JSON: {}".format(path, e))


def load_copy(folder):
    c = parse_copy(_read_json(Path(folder) / "copy.json"), folder)
    if c.name != Path(folder).name:
        raise RegistryError("{}: name {!r} does not match its folder {!r}".format(
            Path(folder) / "copy.json", c.name, Path(folder).name))
    return c


def _spec(spec, path):
    return spec if spec and expand(spec) == path else path.as_posix()


def copy_to_json(c):
    def origin(o):
        return {
            "id": o.id,
            "url": o.url,
            "pin": o.pin,
            "track": o.track,
            "license": o.license,
            "map": [{"from": m.src, "to": m.dst} for m in o.map],
            "audit": None if o.audit is None else {
                "commit": o.audit.commit,
                "verdict": o.audit.verdict,
                "date": o.audit.date,
                "record": o.audit.record,
            },
            "lastChecked": o.last_checked,
        }
    specs = list(c.install_specs) + [""] * max(0, len(c.installs) - len(c.install_specs))
    return {
        "name": c.name,
        "kind": c.kind,
        "copy": _spec(c.copy_spec, Path(c.copy)),
        "installs": [_spec(s, Path(p)) for s, p in zip(specs, c.installs)],
        "origins": [origin(o) for o in c.origins],
    }


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def save_copy(c):
    _write_json(Path(c.folder) / "copy.json", copy_to_json(c))


def list_copies(registry):
    base = Path(registry) / "copies"
    if not base.is_dir():
        return []
    return [load_copy(d) for d in sorted(base.iterdir()) if (d / "copy.json").is_file()]


# -- manifest.json and series ------------------------------------------------

def load_manifest(folder):
    path = Path(folder) / "manifest.json"
    data = _read_json(path)
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        raise RegistryError("{}: missing field 'files'".format(path))
    for rel, digest in files.items():
        rel_path(rel, str(path))
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            raise RegistryError("{}: {} has no valid SHA-256".format(path, rel))
    return files


def save_manifest(folder, files, generated):
    _write_json(Path(folder) / "manifest.json", {"generated": generated, "files": dict(sorted(files.items()))})


def read_series(folder):
    path = Path(folder) / "series"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        if not _PATCH_NAME.fullmatch(name):
            raise RegistryError("{}: entries must be bare patch file names, got {!r}".format(path, name))
        out.append(Path(folder) / "patches" / name)
    return out


def write_series(folder, names):
    path = Path(folder) / "series"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("".join(n + "\n" for n in names))


# -- fork-keeper.json ----------------------------------------------------------

def load_config(registry):
    cfg = json.loads(json.dumps(_DEFAULT_CONFIG))
    path = Path(registry) / "fork-keeper.json"
    if path.is_file():
        data = _read_json(path)
        if not isinstance(data, dict):
            raise RegistryError("{}: must be a JSON object".format(path))
        for key, value in data.items():
            kind = _CONFIG_TYPES.get(key)
            if kind is None:
                raise RegistryError("{}: unknown field '{}'".format(path, key))
            if not isinstance(value, kind):
                raise RegistryError("{}: field '{}' has the wrong type".format(path, key))
            cfg[key] = value
    return cfg


def has_text_rule(registry):
    path = Path(registry) / ".gitattributes"
    if not path.is_file():
        return False
    return any(l.strip() == TEXT_RULE for l in path.read_text(encoding="utf-8").splitlines())
