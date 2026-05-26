# release-dependency-agent

**Three recurring release-cycle problems, one lightweight process framework.**

---

## The problem

Large engineering teams don't usually miss ship dates because of dramatic failures. They miss them — or scramble at the end — because of slow, grinding dependency friction that never got treated as a first-class process concern.

Three things cause this friction again and again:

**1. Dependency updates arrive too late to test.**
A required security patch or a breaking upgrade shows up five days before ship. There's no time to run the full regression suite, so you merge it anyway and hold your breath — or you punt it to next cycle and ship with a known vulnerability.

**2. Breaking changes are invisible until they hurt.**
Dependabot opens a PR for a major version bump. Nobody flags it as high-risk. It merges. CI passes. Three days later, someone notices that a behavior the product relied on changed quietly. Now you're debugging in a feature freeze.

**3. Backporting fixes across release branches is manual and error-prone.**
You land a critical fix on `main`. Now it needs to go to `release/2.1`, `release/2.0`, and `release/1.9`. Someone has to remember to do that, do it correctly three times, open three PRs, and get three approvals. Stuff gets missed.

These aren't bugs you fix once. They're process gaps that recur every cycle. The goal isn't to eliminate them — it's to build a **repeatable process, backed by automation**, that absorbs them with less scramble and less reliance on people remembering to do things.

---

## What this is

`release-dependency-agent` is a set of three independent GitHub Actions workflows. Each one addresses one of the pain points above. You can adopt one, two, or all three — they're designed to be layered on top of whatever dependency management you're already running (Dependabot, Renovate, manual PRs, doesn't matter).

The differentiator isn't the technology. Backport tools exist. Semver parsers exist. What this project adds is the **program-management layer**: when to gate, what to freeze, who decides, and how the team gets a clear picture of incoming risk before it becomes a last-minute fire.

---

## The three layers

### Layer 1 — Freeze window + early-warning system

**The process it encodes:** Don't let dependency updates arrive inside a test window without flagging them. Give the team a weekly picture of what's in flight and how close each update is to becoming a release risk.

**What it does:**
- Reads a `ship_date` from config. Computes days remaining.
- When any dependency PR is opened, classifies it as:
  - `freeze:safe` — plenty of runway, normal review cadence
  - `freeze:tight` — getting close, needs attention this week
  - `freeze:inside-freeze` — inside the freeze window, should not merge unless it's a security fix
- Every Monday, opens (or updates) a GitHub Issue with a risk digest: which PRs are in each bucket, what needs action today, where the team stands.
- Optional: Claude Haiku can write a 2-3 sentence plain-English intro for the digest (off by default, one API call/week).

**Config knobs:** `ship_date`, `freeze_window_days`, `tight_window_days`, `weekly_digest`, `ai_summary`.

---

### Layer 2 — Breaking-change shield (tiered approval)

**The process it encodes:** Not all dependency bumps carry the same risk. A patch fix to a logging library is not the same as a major version upgrade of your HTTP client. Tier the approval requirements to match the actual risk.

**What it does:**
- On every dependency PR open or update, parses the version bump from the PR title (`from X.Y.Z to A.B.C`).
- Classifies it as patch / minor / major using semver rules.
- Applies a `tier:patch`, `tier:minor`, or `tier:major` label.
- Posts a status comment explaining exactly what's required before the PR can merge:
  - `patch` → CI passing is enough
  - `minor` → CI + reviewer approval + `regression-tested` label
  - `major` → CI + reviewer approval + confirmed changelog review
- If the PR is updated and re-classified, the comment updates in place (no comment spam).
- Optional: Claude Haiku summarizes the changelog/PR body for major and minor bumps (off by default, one API call per qualifying PR).

**Config knobs:** `tiers.patch`, `tiers.minor`, `tiers.major`, `ai_changelog_summary`.

---

### Layer 3 — Label-driven backport automation

**The process it encodes:** Backporting should be automatic for the mechanical part (the git work) and human-required for the judgment part (what to backport and how to resolve conflicts).

**What it does:**
- When a PR is merged with a `backport:<branch>` label (e.g. `backport:release/2.1`), automatically cherry-picks the commits onto that branch and opens a backport PR.
- Multiple `backport:` labels → multiple backport PRs, one per target branch.
- Clean cherry-pick → ready-to-review PR.
- Cherry-pick conflict → draft PR with the specific commit SHAs that conflicted and step-by-step instructions for manual resolution.
- No AI. No auto-merging. No auto-resolving conflicts — a human reviews every backport PR.

**Config knobs:** `backport.label_prefix`, `backport.open_as_draft_on_conflict`.

---

## Setup

### Prerequisites

- A GitHub repo with Dependabot or Renovate already configured (or just regular dependency PRs).
- Python 3.11 (only needed locally for testing — workflows handle their own dependencies).
- `GITHUB_TOKEN` is provided automatically by GitHub Actions. No PAT needed unless your repo uses cross-repo backports.
- `ANTHROPIC_API_KEY` is only needed if you enable the optional AI features (both are off by default).

### Installation

**Step 1 — Copy the workflow files to your repo.**

```
.github/workflows/backport.yml
.github/workflows/breaking-change.yml
.github/workflows/freeze-window.yml
```

Copy only the layers you want. All three are independent.

**Step 2 — Copy the Python scripts.**

```
src/backport.py
src/breaking_change_shield.py
src/freeze_check.py
src/common.py
config.yml
requirements.txt
```

**Step 3 — Edit `config.yml`.**

The only required change is setting your `ship_date`:

```yaml
release:
  ship_date: "2026-07-15"   # change this to your next release date
```

Everything else has sensible defaults. You can tune thresholds, labels, and AI settings later.

**Step 4 — Add secrets (if needed).**

In your repo → Settings → Secrets and variables → Actions:

- `ANTHROPIC_API_KEY` — only if you set `ai_summary: true` or `ai_changelog_summary: true`.
- A PAT with `contents:write` and `pull-requests:write` scopes — only if Layer 3 needs to push to branches that GITHUB_TOKEN can't reach (rare; the default token works for same-repo backports).

**Step 5 — Create your backport label(s) (Layer 3 only).**

In your repo → Labels, create a label like `backport:release/2.1`. You can create as many as you have active release branches.

---

## Usage

### Freeze window (Layer 1)

The freeze check runs automatically when any PR is opened, and every Monday at 09:00 UTC. To trigger the digest manually:

Go to Actions → "Layer 1 — Freeze Window" → Run workflow.

### Breaking-change shield (Layer 2)

Runs automatically on every PR open/update. No manual steps needed. The tier comment appears on the PR within a minute or two.

### Backport (Layer 3)

Label a PR with `backport:<target-branch>` before merging it. When the PR merges, the workflow fires automatically.

Example: label a PR with `backport:release/2.1` and merge it. Within a few minutes, a new PR will appear targeting `release/2.1`.

---

## Running tests locally

```
pip install PyGithub PyYAML pytest
python -m pytest tests/ -v
```

The tests cover the semver parser, bump classifier, and freeze window math — all pure logic, no GitHub API calls needed.

---

## Configuration reference

See `config.yml` for the full list of settings with inline comments. Every tunable value is there — no code edits needed to configure the tool.

---

## Cost

The AI features (off by default) use Claude Haiku — the cheapest Anthropic model.

| Feature | When it fires | Typical cost |
|---|---|---|
| Layer 1 AI digest intro | Once per week | ~$0.001/month |
| Layer 2 AI changelog summary | Once per major/minor dep PR | ~$0.0004/call |

A team with 10 qualifying dependency PRs/month and both AI features enabled would spend roughly **$0.005/month** on Anthropic API calls — well under typical budget caps.

The `ai.max_calls_per_run` setting (default: 5) is a hard ceiling. It's never exceeded regardless of how many PRs are open.

---

## Design decisions

**Why GitHub Actions instead of a hosted service?**
The automation runs inside your own repo with your own token. No third-party service has access to your code, your PRs, or your release schedule. It also means free-tier GitHub Actions minutes cover most usage.

**Why not auto-merge dependency PRs?**
That's a deliberate non-goal. The tool surfaces risk and creates the right gates — but a human always decides what merges. Automated merges create their own category of problems (flaky CI, silent regressions) that are out of scope here.

**Why adopt one layer at a time?**
Because partial adoption is still useful. If you only want backport automation, copy Layer 3 and ignore the rest. The layers don't depend on each other.

**Why Haiku for AI?**
It's cheap, fast, and the tasks here (summarize a digest, read a changelog) don't need a more capable model. Using Haiku keeps the monthly AI cost negligible even if the features run daily.
