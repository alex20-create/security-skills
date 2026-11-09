"""Run git on untrusted repositories without letting them, or the user's config, act.

Every call ignores the global and system config (so no hooks, filters,
credential helpers or line-ending conversion of the user's apply), allows
only https (file:// only when a test asks for it), never follows symlinks or
submodules, and never prompts. Offline instances also refuse git's lazy
fetching of missing blobs, so a command that must not touch the network
cannot do so by accident.
"""

import os
import re
import subprocess
import tempfile
from pathlib import Path

_SHA = re.compile(r"^[0-9a-f]{40}$")
_SCRATCH = None


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
    if url.startswith("https://"):
        return
    if allow_file and url.startswith("file://"):
        return
    raise GitError("only https:// URLs are allowed, got {!r}".format(url))


class SafeGit:
    def __init__(self, repo, *, allow_file=False, offline=False):
        self.repo = Path(repo)
        self.allow_file = allow_file
        self.offline = offline

    # -- plumbing -----------------------------------------------------------

    def _env(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
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
            "protocol.https.allow=always",
            "core.hooksPath=" + (_scratch() / "hooks").as_posix(),
            "core.symlinks=false",
            "core.autocrlf=false",
            "core.safecrlf=false",
            "core.fsmonitor=false",
            "core.quotepath=false",
            "core.longpaths=true",
            "submodule.recurse=false",
            "fetch.recurseSubmodules=false",
            "credential.helper=",
            "commit.gpgsign=false",
            "tag.gpgsign=false",
            "init.defaultBranch=main",
            "advice.detachedHead=false",
            "user.name=fork-keeper",
            "user.email=fork-keeper@localhost",
        ]
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
        return g

    # -- network ------------------------------------------------------------

    def fetch(self, url, refs):
        """Fetch ``refs`` (full SHAs, branch or tag names) from ``url``.

        A SHA is kept at refs/fk/sha/<sha>; a name at refs/fk/<name>.
        """
        if self.offline:
            raise GitError("fetch refused: this git instance is offline")
        check_url(url, self.allow_file)
        remotes = self.run("remote").decode().split()
        if "origin" in remotes:
            self.run("remote", "set-url", "origin", url)
        else:
            self.run("remote", "add", "origin", url)
        specs = []
        for ref in refs:
            if _SHA.match(ref):
                specs.append("+{0}:refs/fk/sha/{0}".format(ref))
            else:
                specs.append("+{0}:refs/fk/{0}".format(ref))
        self.run("fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--filter=blob:none",
                 "origin", *specs)

    # -- reading ------------------------------------------------------------

    def rev_parse(self, ref):
        return self.run("rev-parse", "--verify", "--quiet", ref + "^{commit}").decode().strip()

    def is_ancestor(self, a, b):
        self.run("merge-base", "--is-ancestor", a, b, ok_codes=(0, 1))
        return self.last_returncode == 0

    def log(self, a, b):
        out = self.run("log", "--format=%H%x00%an%x00%aI%x00%s%x1e", "{}..{}".format(a, b)).decode("utf-8", "replace")
        commits = []
        for rec in out.split("\x1e"):
            rec = rec.strip("\n")
            if not rec:
                continue
            sha, author, date, subject = rec.split("\x00", 3)
            commits.append({"sha": sha, "author": author, "date": date, "subject": subject})
        return commits

    def changed(self, a, b):
        """(status letter, path) for every path that differs, without rename detection."""
        out = self.run("diff", "--name-status", "--no-renames", "-z", a, b).decode("utf-8", "replace")
        parts = [p for p in out.split("\x00") if p != ""]
        return [(parts[i][0], parts[i + 1]) for i in range(0, len(parts) - 1, 2)]

    def diff(self, a, b, paths, stat=False):
        args = ["diff", "--binary", "-M", "--full-index"]
        if stat:
            args.append("--stat")
        return self.run(*args, a, b, "--", *paths)

    def ls_tree(self, commit, path):
        args = ["ls-tree", "-r", "-z", "--full-tree", commit]
        if path not in ("", "."):
            args += ["--", path]
        out = self.run(*args).decode("utf-8", "replace")
        entries = []
        for rec in out.split("\x00"):
            if not rec:
                continue
            meta, p = rec.split("\t", 1)
            mode, _type, blob = meta.split(" ")
            entries.append((mode, blob, p))
        return entries

    def cat_blob(self, blob):
        return self.run("cat-file", "blob", blob)

    def archive(self, commit, paths):
        return self.run("archive", "--format=tar", commit, "--", *paths)

    # -- work repos ---------------------------------------------------------

    def commit_all(self, message):
        self.run("add", "-A")
        self.run("commit", "-q", "--allow-empty", "--no-verify", "-m", message)
        return self.rev_parse("HEAD")
