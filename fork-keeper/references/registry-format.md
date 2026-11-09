# Registry format

The registry is a folder, ideally its own private git repository:

```
<registry>/
  fork-keeper.json          settings (optional)
  .gitattributes            must contain: copies/** -text
  copies/
    <name>/
      copy.json             what the copy is and where it comes from
      manifest.json         SHA-256 of every file of the expected result
      series                patch file names, in application order
      patches/
        0001-<topic>.patch
```

Every file is UTF-8 and written with `\n` line endings.

## `copy.json`

```json
{
  "name": "sample-skill",
  "kind": "skill",
  "copy": "~/skills-src/sample-skill",
  "installs": ["~/.agent/skills/sample-skill"],
  "origins": [
    {
      "id": "upstream",
      "url": "https://github.com/example/sample-skill",
      "pin": "0123456789abcdef0123456789abcdef01234567",
      "track": "main",
      "license": "MIT",
      "map": [
        { "from": "skills/sample-skill", "to": "." },
        { "from": "LICENSE", "to": "LICENSE" }
      ],
      "audit": {
        "commit": "0123456789abcdef0123456789abcdef01234567",
        "verdict": "LIKELY SAFE",
        "date": "2026-01-15",
        "record": "sample-skill"
      },
      "lastChecked": "2026-01-15"
    }
  ]
}
```

Every key shown is required except an origin's `audit` and `lastChecked`,
which may be left out or `null`; unknown keys are refused, so a typo cannot
pass silently. Dates are `YYYY-MM-DD` and must be real dates.

| Key | Rule |
|---|---|
| `name` | Letters, digits, `.`, `_`, `-`. Must equal the copy's folder name under `copies/`. |
| `kind` | `skill`, `plugin`, `repo` or `mcp`. |
| `copy` | Where the copy's source lives. `~` is expanded. |
| `installs` | A list of places that must equal `copy`; may include `copy` itself. `verify` and `install` cover each one. |
| `origins` | One or more. A copy assembled from two originals has two. |
| `id` | Short name of the origin, unique in the copy. Used in `--origin` and in `Applies-To`. |
| `url` | `https://` only, with a host, no user or password, no spaces or control characters. |
| `pin` | The full 40-character SHA that was audited. In a draft for `register` a 7 to 39 character prefix is accepted and resolved. |
| `track` | The branch or tag `check` compares against. Cannot start with `-`. |
| `license` | The original's license as the user records it. |
| `map` | What is copied: `from` (path in the original) to `to` (path in the copy). A folder maps recursively; `.` is the root. Paths are relative, use `/`, and never contain `..`. Two destinations may not collide, even by case only, across all origins. |
| `audit` | `null` for an origin not yet audited (then `register` refuses). `commit` must equal `pin` for the lock to open. |
| `verdict` | `LIKELY SAFE`, `USE WITH CAUTION` or `DO NOT INSTALL`, as the audit record says. |
| `record` | The name under which the entry appears in the audit records. |
| `lastChecked` | Date of the last `check` or `mark-checked`, or `null`. |

A derived file may be mapped from its source in the original, so a change to
the source shows up as a conflict to look at rather than going unseen.

## `manifest.json`

```json
{ "generated": "2026-01-15", "files": { "SKILL.md": "<64 hex>", "scripts/run.py": "<64 hex>" } }
```

Paths are relative to `copy`. `register` and `install` write it; nothing else
should. Files matching the ignore patterns are left out.

## `series` and patches

`series` holds one patch file name per line, in order. Blank lines and lines
starting with `#` are skipped; names may not contain a path.

A patch is a DEP-3 header, a line holding only `---`, then a git diff made
with `--binary -M --full-index`:

```
Description: Drop the always-on trigger
 The skill switched modes whenever a phrase appeared in any text in
 context, including scanned files.
Origin: vendor-local
Forwarded: not-needed
Applies-To: upstream@0123456789abcdef0123456789abcdef01234567
Last-Update: 2026-01-15
---
diff --git a/SKILL.md b/SKILL.md
...
```

- These five fields and no others; fork-keeper writes them in this order and
  refuses any other field. Description continuation lines start with one
  space; a line that would be empty is written as ` .`.
- `Applies-To` names each origin and the full SHA of the base the diff was
  made against, as `id@sha`, comma-separated when the copy has several
  origins (`one@<sha>, two@<sha>`). `install` rewrites it, and `Last-Update`,
  every time a pin moves, and regenerates the diff against the new base so
  offsets never pile up.
- Paths in the diff are relative to `copy`, after mapping.
- `register` writes one patch for all of the copy's differences. Splitting it
  into topics is done by hand; fork-keeper does not guess topics.
- A header with CRLF line endings is refused: that is git converting the file
  on checkout, which the `.gitattributes` rule prevents.

## Line endings

Git on some systems rewrites text files to CRLF on checkout. A stored patch
rewritten that way no longer applies to files with LF. The registry's
`.gitattributes` therefore needs the line `copies/** -text`; `register` warns
when it is missing. For the same reason `register` stops when the copy and the
original disagree on line endings in the same file.

## `fork-keeper.json`

Optional; every key has a default.

```json
{
  "auditRecords": "~/notes/security-audits.md",
  "skillsRoots": ["~/.agent/skills"],
  "ownSourceRoots": ["~/skills-src"],
  "managedSets": { "toolkit-": "toolkit" },
  "ignore": ["*.bak"]
}
```

| Key | Default | Meaning |
|---|---|---|
| `auditRecords` | `null` | The file of audit records. `register` needs it; `status` and `inventory` read it. |
| `skillsRoots` | `["~/.claude/skills"]` | Folders whose subfolders `inventory` reports as installed skills. |
| `ownSourceRoots` | `[]` | Folders holding the user's own skill sources. An installed skill matching one is own, not third-party. Without any, unregistered skills are unclassified. |
| `managedSets` | `{}` | Name prefix to audit record name, for a set of skills installed and updated by one audited tool. |
| `ignore` | `[]` | Extra file patterns to leave out of manifests, patches and checks, on top of `.DS_Store`, `Thumbs.db`, `desktop.ini` and `__pycache__/`. |

## Audit records

fork-keeper does not parse a record's content. It looks for a line holding the
record name in backticks and the first seven characters of the pinned SHA, for
example:

```
- [x] `sample-skill` (USE WITH CAUTION) — github.com/example/sample-skill @ `0123456`
```

Write new entries in whatever format the existing ones use, as long as that
line is there.
