# vet-before-install

A Claude Code skill that security-audits things *before* you trust them: an
agent skill or plugin, a GitHub repository, a local project, an MCP server,
or a dependency tree.

It enforces one discipline whatever the target is:

1. **Deterministic sweep** for recall — pattern-based detection of network
   calls, execution, credential reads, persistence, install-time hooks,
   obfuscation, and reviewer-directed text.
2. **Manual contract check** for judgment — does what the project *claims*
   match what the code *does*? A scanner cannot answer that; reading can.

Plus the ground rules that make the audit itself safe: the target is cloned
into an isolated directory, **never executed** (no install, no build, no
tests — postinstall scripts are where the attack runs), and everything
inside it is treated as data to analyze, never instructions to follow.

## Installation

Copy the `vet-before-install/` folder into your skills directory:

```bash
cp -r vet-before-install ~/.claude/skills/
```

Or save the packaged `.skill` file from the latest GitHub release through
the Claude app.

Note: `references/fallback-sweep.md` is a pattern blocklist, so security
scanners pointed at this skill will flag it. That is the false-positive
case the skill itself documents — audit it yourself before trusting it,
as the skill would tell you to.

## Zero dependencies, optional accelerators

The skill works with nothing else installed: every step has a built-in
manual procedure, including a full sweep pattern battery
(`references/fallback-sweep.md`).

If you have security tooling installed, the skill detects and uses it
instead. Recommended companions, all from Trail of Bits' public skills
marketplace:

| Tool | What it accelerates |
|---|---|
| `insecure-defaults` | Step 3: default credentials, fail-open switches, debug leakage, binds |
| `supply-chain-risk-auditor` | Step 4: lockfile and dependency-tree analysis |
| `audit-context-building` | Step 2: architecture mapping on large codebases |

```
/plugin marketplace add trailofbits/skills
/plugin install insecure-defaults@trailofbits
/plugin install supply-chain-risk-auditor@trailofbits
/plugin install audit-context-building@trailofbits
```

If you also have a dedicated skill-audit scanner installed (one that runs a
deterministic regex/AST/YARA pass over a skill folder), the skill hands the
skill/plugin case to it end to end.

Any other security tooling you have installed (a code security reviewer, a
multi-agent vulnerability scanner, a secrets scanner, a security subagent)
is offered as an optional extra pass: the skill lists what it found in one
multi-select question, with what each would add and its rough cost, and runs
only what you pick. Extra tools run read-only on the isolated copy, never
install, build, run or edit the target, and their findings go through the
same contract check; the verdict stays this skill's.

Standalone scanner CLIs on your `PATH` (such as NVIDIA's SkillSpector) are
offered the same way. They run static-only, never in a mode that sends the
target's files to an LLM provider unless you ask for it, and never with
suppression files shipped inside the target.

## Usage

Just ask, in any phrasing:

- "audit this: https://github.com/someone/some-plugin"
- "is this skill safe to install?" (with a path or `.skill` file)
- "vet this MCP server before I add it"
- "check the dependencies of this project"

The report leads with a verdict (`LIKELY SAFE` / `USE WITH CAUTION` /
`DO NOT INSTALL`) and the count of findings that survived review, by
severity. A scanner's raw score never heads the report: it is computed
before anyone reads the code, so it appears only at the end, labelled as
raw. The report also records the exact commit or version audited, cites
`file:line` for every finding, marks false positives explicitly, and says
what was *not* checked.

When something you already vetted publishes a new commit, ask for the update
to be audited ("re-vet this update from `<old SHA>` to `<new SHA>`"). The
skill audits the delta as hard as a new target: new files in full, changed
files read whole with the changes as focus, contracts and install mechanism
compared, and every caveat of the previous audit marked as still present,
fixed or changed. The verdict applies to the new commit only.

## Scope

Defensive use only: this vets third-party things you are deciding whether
to trust. It is not a tool for making software evade review, and not a
replacement for reviewing your own diffs and PRs.

## License

MIT — see [LICENSE](LICENSE).
