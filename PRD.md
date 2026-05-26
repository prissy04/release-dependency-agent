# PRD — Release Dependency Agent

*A process-improvement toolkit that reduces dependency-related disruption during release cycles, built as a set of GitHub Actions workflows.*

---

## 1. The problem (start here)

Large engineering teams don't fail because they lack tools — they fail because dependency work doesn't fit cleanly into a release cycle. Three things repeatedly disrupt feature development and ship dates:

1. **Late-arriving dependency updates.** Required updates land close to a ship date, leaving no time to test them properly.
2. **Breaking changes.** A dependency quietly changes behavior and breaks the product or the test suite, and it's slow to tell whether a failure is the dependency's fault or the team's own code.
3. **Multi-version patching (backports).** Once a fix lands on the main branch, applying it across several maintained release branches is tedious, manual, and error-prone.

These aren't bugs to fix once — they recur every cycle. So the goal isn't to "solve" them; it's to build a **repeatable process, backed by automation,** that absorbs them with less manual effort and less last-minute scramble. The differentiator here is not the technology — Dependabot, Renovate, and backport tooling already exist — it's how a program-management lens sequences and gates the work so the process is sustainable and doesn't depend on one person remembering to do things.

> **Framing note for this repo:** This is a portfolio project grounded in conversations with practitioners in enterprise R&D. It is intentionally generic and open-source. It contains no proprietary information, no employer-specific infrastructure, and no internal process details. Keep it that way.

---

## 2. Who it's for

- Release/QA engineers and TPMs on teams that maintain **multiple shipped versions** of a product.
- Teams already using GitHub and (ideally) Dependabot or Renovate, who want the *process* around those tools tightened up.

---

## 3. Goals and non-goals

### Goals
- Give a team **early warning** when dependency updates are at risk of arriving too late to test safely.
- Make breaking-change risk **visible and gated** before a dependency update merges, scaled to how risky the update is.
- **Automate the mechanical parts** of backporting a fix across maintained release branches.
- Be **config-driven** so a team can adopt it without editing code.
- Be cheap to run (minimal AI usage, free GitHub Actions minutes where possible).

### Non-goals
- This is **not** a replacement for Dependabot/Renovate — it sits on top of them and adds process.
- It does **not** auto-merge dependency updates or auto-resolve merge conflicts. A human always makes the call on anything risky.
- It is **not** a security scanner or SBOM generator (those can be referenced as integration points, but they're out of scope for v1).
- It is **not** a hosted service — it's GitHub Actions running in the team's own repo.

---

## 4. The solution — three layers

Each layer is an independent GitHub Actions workflow. A team can adopt one, two, or all three.

### Layer 1 — Early-warning system + dependency freeze window
**What it does:** Watches incoming dependency-update PRs (from Dependabot/Renovate), reads a configured ship date and freeze window, and flags updates that arrive inside the freeze window or too late to test. Posts a weekly summary of incoming dependency risk.

- **Trigger:** scheduled (weekly cron) + on dependency-update PR opened.
- **Logic:** compute days-to-ship-date; compare against `freeze_window_days`; classify each open dependency PR as `safe` / `tight` / `inside-freeze`.
- **Output:** a label on the PR (e.g. `freeze:inside`) and a weekly GitHub Issue summarizing what's incoming and what's at risk.
- **AI (optional, Haiku):** summarize/prioritize the weekly digest in plain language. Keep this to **one** API call per weekly run.

### Layer 2 — Breaking-change shield (tiered approval)
**What it does:** On any dependency-update PR, classifies the version bump (major / minor / patch via semver) and applies tiered rules — bigger jumps require more scrutiny before merge.

- **Trigger:** on dependency-update PR opened / synchronized.
- **Logic:** parse old vs. new version, derive bump type, apply tier rules from config (e.g. `patch` → auto-pass CI is enough; `minor` → require regression label; `major` → require explicit reviewer sign-off + changelog review).
- **Output:** a tier label (`tier:patch|minor|major`), a status comment explaining what's required, and a required-check gate for higher tiers.
- **AI (optional, Haiku):** given a changelog/release-notes URL, summarize likely breaking changes in 3–5 bullets. **One** API call per qualifying PR, only for `major`/`minor`.

### Layer 3 — Label-driven backport automation
**What it does:** When a merged PR is labeled `backport:<branch>`, automatically cherry-picks the change onto each target release branch and opens a backport PR. Clean cherry-picks open a ready-to-review PR; conflicts open a draft PR that flags exactly where manual resolution is needed.

- **Trigger:** on PR closed (merged) with one or more `backport:*` labels.
- **Logic:** for each target branch, create a branch, attempt cherry-pick, open a PR (ready if clean, draft + conflict note if not).
- **Output:** one backport PR per target branch, linked back to the original.
- **AI:** none. This is pure git/automation — keep it deterministic.

---

## 5. Tech stack

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.11 | Matches existing portfolio repos; "automation and scripting" depth |
| CI/automation | GitHub Actions | Native, free minutes, where the work lives |
| GitHub access | PyGithub + `GITHUB_TOKEN` / a PAT in Secrets | Consistent with multi-repo-dependency-risk-agent |
| AI (optional layers) | Anthropic Claude API, **Haiku** model | Cheapest model; respects the $20/month cap |
| Dependency updates | Assumes Dependabot or Renovate already running | We add process, not the updater |
| Backports | `git` cherry-pick via Actions (consider `git-backporting`) | Deterministic, no AI |
| Config | `config.yml` | Adopt without editing code |

**Cost guardrail:** AI is used in at most two places (Layer 1 weekly digest, Layer 2 changelog summary), both optional and both togglable in config. Default to Haiku. Never call the API inside a loop over PRs without a hard cap.

---

## 6. Proposed repo structure

```
release-dependency-agent/
├── README.md                       # portfolio-facing: leads with the PM problem
├── config.yml                      # all tunable settings, no code edits needed
├── requirements.txt
├── CLAUDE.md                       # context for Claude Code (do not ship secrets)
├── src/
│   ├── freeze_check.py             # Layer 1
│   ├── breaking_change_shield.py   # Layer 2
│   ├── backport.py                 # Layer 3
│   └── common.py                   # shared GitHub/config/semver helpers
├── .github/
│   └── workflows/
│       ├── freeze-window.yml       # Layer 1 (cron + PR)
│       ├── breaking-change.yml     # Layer 2 (PR)
│       └── backport.yml            # Layer 3 (PR merged + label)
├── tests/
│   └── test_*.py
└── docs/
    └── ARCHITECTURE.md             # diagrams + how the three layers fit
```

---

## 7. Configuration (draft `config.yml`)

```yaml
# Layer 1 — freeze window
release:
  ship_date: "2026-07-15"      # next ship date (ISO)
  freeze_window_days: 10        # no non-security dep updates within N days of ship
  weekly_digest: true
  ai_summary: false             # set true to use Claude (Haiku) for the digest

# Layer 2 — breaking-change shield
tiers:
  patch:  { require_ci: true,  require_review: false }
  minor:  { require_ci: true,  require_review: true,  require_label: "regression-tested" }
  major:  { require_ci: true,  require_review: true,  require_changelog_review: true }
  ai_changelog_summary: false   # set true to summarize release notes via Claude (Haiku)

# Layer 3 — backport
backport:
  label_prefix: "backport:"     # e.g. backport:release/2.1
  open_as_draft_on_conflict: true

# shared
dependency_labels: ["dependencies", "dependabot", "renovate"]
ai:
  model: "claude-haiku-4-5-20251001"
  max_calls_per_run: 5          # hard safety cap
```

---

## 8. Success criteria

- Each layer runs green in a demo repo with a realistic test scenario.
- A reviewer can adopt any single layer by editing only `config.yml`.
- README leads with the *problem and the process*, not the tooling.
- The weekly digest and tier comments read like a person wrote them, not a bot.
- Total Anthropic spend to build and demo stays well under the monthly cap.

**Portfolio story:** "Three universal release-cycle pain points → a process framework → lightweight GitHub-native automation that operationalizes it." Lead with the program-management judgment (when to gate, what to freeze, who decides), not the Python.

---

## 9. Assumptions & open questions

This design is built around the **three reported pain points**. Validate it against the discovery answers before committing to the full build:

- **Freeze window length?** Is there already a freeze cutoff, or do updates arrive right up to ship? (Sets Layer 1's defaults.)
- **How are breaking changes discovered today** — CI, manual testing, or post-deploy? (Decides how much Layer 2 can lean on existing CI.)
- **How many active release branches**, and who decides what gets backported? (Shapes Layer 3's label scheme.)
- **Are tests coupled to dependency APIs or abstracted?** (Affects how much Layer 2's gating actually buys.)
- **Compliance hooks** (security sign-off, SBOM)? (May add a required check to Layer 2.)

If the answers diverge from these assumptions, revise this PRD first, then build.

---

## 10. Build phases (checkpoints)

1. **Scaffold** — repo structure, `config.yml`, `common.py`, `requirements.txt`, empty workflows. *Checkpoint: structure reviewed.*
2. **Layer 3 (backport) first** — most mechanical, highest visible payoff, no AI. *Checkpoint: backport PR opens correctly on a test repo.*
3. **Layer 2 (breaking-change shield)** — semver tiering + gating, AI summary off by default. *Checkpoint: tiers label and gate correctly.*
4. **Layer 1 (freeze window + digest)** — scheduling + weekly issue. *Checkpoint: digest reads naturally.*
5. **Docs** — README (PM-first) + ARCHITECTURE.md with diagrams.
6. **Optional AI pass** — wire Haiku summaries behind config flags, test cost.

> Build order is deliberately Layer 3 → 2 → 1: start with the most self-contained, deterministic layer to get a win on the board, then add the layers that depend on more judgment and config.
