"""Run git on untrusted repositories without letting them, or the user's config, act.

Every call ignores the global and system config (so no hooks, filters,
credential helpers or line-ending conversion of the user's apply), allows
only https (file:// only when a test asks for it), never follows symlinks or
submodules, and never prompts. Repositories made with ``SafeGit.init`` also
override every attribute an upstream ``.gitattributes`` could set, so an
upstream cannot hide files from an archive or turn a diff into "binary
files differ". Fetched objects are checked, and trees naming ``.git`` (in
any spelling git knows) or ``..`` are refused.

Offline instances refuse to fetch and allow no transport at all, so git's
lazy fetching of missing blobs cannot reach the network by accident.

Revisions are accepted only as full SHAs, ``HEAD`` or refs under
``refs/fk/``, so a value from a registry file can never become an option.
"""

import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

_SHA = re.compile(r"[0-9a-f]{40}")
_COMPONENT = r"[A-Za-z0-9_][A-Za-z0-9._+-]*"
_NAME = re.compile(r"{0}(/{0})*".format(_COMPONENT))
_SCRATCH = None

# Highest-precedence attributes: they beat anything in the fetched trees.
NEUTRAL_ATTRIBUTES = ("* -export-ignore -export-subst -text -eol -ident !diff !merge !filter "
                      "!working-tree-encoding !crlf conflict-marker-size=7\n")

_FSCK = ("hasDotgit", "hasDotdot", "hasDot")
_DROP_ENV = ("SSLKEYLOGFILE",)


class GitError(Exception):
    pass


def _scratch():
    """An empty config file and an empty hooks folder, made once per process."""
    global _SCRATCH
    if _SCRATCH is None:
        d = Path(tempfile.mkdtemp(prefix="fk-git-"))
        (d / "empty.gitconfig").write_text("")
        (d / "hooks").mkdir()
        _SCRATCH = d
    return _SCRATCH


def check_url(url, allow_file=False):
    """Accept only a plain https URL with a host and no credentials (file:// only if allowed)."""
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        raise GitError("URL {!r} contains whitespace or control characters".format(url))
    try:
        parts = urlsplit(url)
    except ValueError as e:
        raise GitError("URL {!r} does not parse: {}".format(url, e))
    if parts.scheme == "https" and url.startswith("https://"):
        if not parts.hostname:
            raise GitError("URL {!r} has no host".format(url))
        if "@" in parts.netloc:
            raise GitError("URL {!r} carries credentials; remove them".format(url))
        return
    if allow_file and parts.scheme == "file" and url.startswith("file://"):
        return
    raise GitError("only https:// URLs are allowed, got {!r}".format(url))


def check_name(name):
    """A branch or tag name safe to put in a refspec."""
    if not isinstance(name, str) or not _NAME.fullmatch(name) or ".." in name \
            or any(c.endswith((".lock", ".")) for c in name.split("/")):
        raise GitError("{!r} is not an acceptable branch or tag name".format(name))
    return name


def name_ref(name):
    """Where a fetched branch or tag is kept in a fork-keeper mirror."""
    return "refs/fk/name/" + check_name(name)


def sha_ref(sha):
    return "refs/fk/sha/" + _check_sha(sha)


def _check_sha(sha):
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise GitError("{!r} is not a full 40-character SHA".format(sha))
    return sha


def check_rev(rev):
    """A revision fork-keeper may pass to git: a full SHA, HEAD or a ref under refs/fk/."""
    if isinstance(rev, str):
        if _SHA.fullmatch(rev) or rev == "HEAD":
            return rev
        if rev.startswith("refs/fk/") and _NAME.fullmatch(rev[len("refs/fk/"):]) and ".." not in rev \
                and not any(c.endswith((".lock", ".")) for c in rev.split("/")):
            return rev
    raise GitError("{!r} is not an accepted revision (full SHA, HEAD or refs/fk/...)".format(rev))


def _decode_path(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise GitError("git returned a path that is not UTF-8: {!r}".format(raw))


class SafeGit:
    def __init__(self, repo, *, allow_file=False, offline=False):
        self.repo = Path(repo)
        self.allow_file = allow_file
        self.offline = offline

    # -- plumbing -----------------------------------------------------------

    def _env(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k not in _DROP_ENV}
        empty = str(_scratch() / "empty.gitconfig")
        env.update({
            "GIT_CONFIG_GLOBAL": empty,
            "GIT_CONFIG_SYSTEM": empty,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_LITERAL_PATHSPECS": "1",
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
        })
        if self.offline:
            env["GIT_NO_LAZY_FETCH"] = "1"
        return env

    def _flags(self):
        flags = [
            "protocol.allow=never",
            "core.hooksPath=" + (_scratch() / "hooks").as_posix(),
            "core.excludesFile=" + (_scratch() / "empty.gitconfig").as_posix(),
            "core.attributesFile=" + (_scratch() / "empty.gitconfig").as_posix(),
            "core.symlinks=false",
            "core.autocrlf=false",
            "core.safecrlf=false",
            "core.fsmonitor=false",
            "core.quotepath=false",
            "core.longpaths=true",
            "core.protectNTFS=true",
            "core.protectHFS=true",
            "submodule.recurse=false",
            "fetch.recurseSubmodules=false",
            "fetch.fsckObjects=true",
            "credential.helper=",
            "http.followRedirects=initial",
            "commit.gpgsign=false",
            "tag.gpgsign=false",
            "init.defaultBranch=main",
            "advice.detachedHead=false",
            "user.name=fork-keeper",
            "user.email=fork-keeper@localhost",
        ]
        flags += ["fetch.fsck.{}=error".format(m) for m in _FSCK]
        if not self.offline:
            flags.append("protocol.https.allow=always")
            if self.allow_file:
                flags.append("protocol.file.allow=always")
        out = []
        for f in flags:
            out += ["-c", f]
        return out

    def run(self, *args, input=None, ok_codes=(0,)):
        r = subprocess.run(["git", *self._flags(), *args], cwd=str(self.repo), input=input,
                           capture_output=True, env=self._env())
        if r.returncode not in ok_codes:
            msg = r.stderr.decode("utf-8", "replace").strip() or "exit {}".format(r.returncode)
            raise GitError("git {}: {}".format(args[0] if args else "", msg))
        self.last_returncode = r.returncode
        self.last_stderr = r.stderr
        return r.stdout

    @classmethod
    def init(cls, path, *, bare, allow_file=False, offline=False):
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        g = cls(path, allow_file=allow_file, offline=offline)
        g.run("init", "-q", *(["--bare"] if bare else []))
        info = (path if bare else path / ".git") / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "attributes").write_bytes(NEUTRAL_ATTRIBUTES.encode())
        return g

    # -- network ------------------------------------------------------------

    def fetch(self, url, refs):
        """Fetch ``refs`` (full SHAs, branch or tag names) from ``url``.

        A SHA is kept at refs/fk/sha/<sha>; a name at refs/fk/name/<name>,
        taken from the branch of that name, or the tag when there is no branch.
        """
        if self.offline:
            raise GitError("fetch refused: this git instance is offline")
        check_url(url, self.allow_file)
        shas = [r for r in refs if isinstance(r, str) and _SHA.fullmatch(r)]
        names = [check_name(r) for r in refs if r not in shas]
        remotes = self.run("remote").decode("utf-8", "replace").split()
        if "origin" in remotes:
            self.run("remote", "set-url", "origin", url)
        else:
            self.run("remote", "add", "origin", url)
        specs = ["+{}:{}".format(s, sha_ref(s)) for s in shas]
        if names:
            wanted = []
            for n in names:
                wanted += ["refs/heads/" + n, "refs/tags/" + n]
            out = self.run("ls-remote", "origin", *wanted).decode("utf-8", "replace")
            found = {line.split("\t", 1)[1] for line in out.splitlines() if "\t" in line}
            for n in names:
                for src in ("refs/heads/" + n, "refs/tags/" + n):
                    if src in found:
                        specs.append("+{}:{}".format(src, name_ref(n)))
                        break
                else:
                    raise GitError("{!r} is neither a branch nor a tag at {}".format(n, url))
        if specs:
            self.run("fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--filter=blob:none",
                     "origin", *specs)

    # -- reading ------------------------------------------------------------

    def rev_parse(self, ref):
        return self.run("rev-parse", "--verify", "--quiet", "--end-of-options",
                        check_rev(ref) + "^{commit}").decode().strip()

    def is_ancestor(self, a, b):
        self.run("merge-base", "--is-ancestor", "--end-of-options", check_rev(a), check_rev(b), ok_codes=(0, 1))
        return self.last_returncode == 0

    def log(self, a, b):
        rng = "{}..{}".format(check_rev(a), check_rev(b))
        out = self.run("log", "-z", "--no-color", "--format=%H%x00%an%x00%aI%x00%s", "--end-of-options", rng)
        out = out.rstrip(b"\x00")
        if not out:
            return []
        parts = out.split(b"\x00")
        if len(parts) % 4:
            raise GitError("git log returned {} fields, not a multiple of 4".format(len(parts)))
        commits = []
        for i in range(0, len(parts), 4):
            sha, author, date, subject = (p.decode("utf-8", "replace") for p in parts[i:i + 4])
            commits.append({"sha": sha.strip("\n"), "author": author, "date": date, "subject": subject})
        return commits

    def changed(self, a, b):
        """(status letter, path) for every path that differs, without rename detection."""
        out = self.run("diff", "--name-status", "--no-renames", "--no-ext-diff", "--no-textconv", "-z",
                       "--end-of-options", check_rev(a), check_rev(b))
        parts = [p for p in out.split(b"\x00") if p != b""]
        if len(parts) % 2:
            raise GitError("git diff --name-status returned an odd number of fields")
        return [(parts[i].decode("ascii")[0], _decode_path(parts[i + 1])) for i in range(0, len(parts), 2)]

    def diff(self, a, b, paths, stat=False):
        args = ["diff", "--binary", "-M", "--full-index", "--no-ext-diff", "--no-textconv", "--no-color"]
        if stat:
            args.append("--stat")
        return self.run(*args, "--end-of-options", check_rev(a), check_rev(b), "--", *paths)

    def ls_tree(self, commit, path):
        args = ["ls-tree", "-r", "-z", "--full-tree", "--end-of-options", check_rev(commit)]
        if path not in ("", "."):
            args.append(path)
        out = self.run(*args)
        entries = []
        for rec in out.split(b"\x00"):
            if not rec:
                continue
            meta, p = rec.split(b"\t", 1)
            mode, _type, blob = meta.decode("ascii").split(" ")
            entries.append((mode, blob, _decode_path(p)))
        return entries

    def cat_blob(self, blob):
        return self.run("cat-file", "blob", _check_sha(blob))

    def archive(self, commit, paths):
        return self.run("archive", "--format=tar", "--end-of-options", check_rev(commit), *paths)

    # -- work repos ---------------------------------------------------------

    def stage_exactly(self, files):
        """Make the index hold exactly ``files`` (relative POSIX paths), whatever any ignore rule says."""
        self.run("rm", "-r", "--cached", "-q", "--ignore-unmatch", "--", ".")
        if files:
            self.run("add", "--force", "--pathspec-from-file=-", "--pathspec-file-nul",
                     input=b"\x00".join(f.encode("utf-8") for f in files))

    def commit_all(self, message, files=None):
        """Commit the work tree: every file (ignore rules overridden), or exactly ``files``."""
        if files is None:
            self.run("add", "-A", "--force")
        else:
            self.stage_exactly(files)
        self.run("commit", "-q", "--allow-empty", "--no-verify", "-m", message)
        return self.rev_parse("HEAD")
