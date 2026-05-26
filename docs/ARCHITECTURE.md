# Architecture — release-dependency-agent

How the three layers are structured, what triggers them, and how they make decisions.

---

## Overview

Each layer is an independent workflow + Python script pair. They share a common helper module (`common.py`) for config loading, GitHub authentication, and semver parsing. Nothing else is shared — you can delete any one layer's files without affecting the others.

```
GitHub Events
     │
     ├─ PR opened/updated ──────► Layer 1 (PR mode) ──► freeze:* label on PR
     │
     ├─ PR opened/updated ──────► Layer 2 ──────────────► tier:* label + status comment
     │
     ├─ PR merged + backport:* ─► Layer 3 ──────────────► backport PR(s) on target branches
     │
     └─ Cron (Monday 09:00 UTC) ► Layer 1 (digest mode) ► GitHub Issue digest
```

---

## Layer 1 — Freeze Window + Early-Warning System

**Files:** `src/freeze_check.py`, `.github/workflows/freeze-window.yml`

### Decision logic

```
days_to_ship = ship_date − today

if days_to_ship ≤ freeze_window_days → freeze:inside-freeze  (default: 10 days)
elif days_to_ship ≤ tight_window_days → freeze:tight          (default: 21 days)
else                                  → freeze:safe
```

### PR trigger flow

```mermaid
flowchart TD
    A[PR opened] --> B{Is it a dependency PR?}
    B -- No --> C[Skip — no action]
    B -- Yes --> D[Compute days to ship_date]
    D --> E{Days remaining?}
    E -- "> tight_window_days" --> F[Apply freeze:safe label]
    E -- "≤ tight_window_days" --> G[Apply freeze:tight label]
    E -- "≤ freeze_window_days" --> H[Apply freeze:inside-freeze label]
```

### Cron/digest flow

```mermaid
flowchart TD
    A[Monday 09:00 UTC cron] --> B[Scan all open PRs]
    B --> C{Is this a dependency PR?}
    C -- No --> D[Skip]
    C -- Yes --> E[Classify + apply freeze:* label]
    E --> F[Group by status]
    F --> G{ai_summary enabled?}
    G -- Yes --> H[Call Claude Haiku for intro text]
    G -- No --> I[Use code-generated intro]
    H --> J[Build Markdown digest]
    I --> J
    J --> K{Existing digest issue open?}
    K -- Yes --> L[Update existing issue body]
    K -- No --> M[Create new issue with dependency-digest label]
```

### Detection heuristics

A PR is considered a dependency update if **any** of the following are true:
1. It has a label in the configured `dependency_labels` list (e.g. `dependencies`, `renovate`)
2. The author login contains `dependabot` or `renovate`
3. The title matches the pattern `"from X.Y.Z to A.B.C"`

Three signals with OR logic makes this robust across different Dependabot/Renovate configurations.

---

## Layer 2 — Breaking-Change Shield

**Files:** `src/breaking_change_shield.py`, `.github/workflows/breaking-change.yml`

### Semver classification

```
parse_semver("2.1.3") → (2, 1, 3)

if new_major ≠ old_major → "major"
elif new_minor ≠ old_minor → "minor"
else → "patch"
```

Using `!=` instead of `>` on major/minor catches both upgrades and downgrades. A major version change in either direction warrants scrutiny.

### Tier → requirements mapping

| Tier | Label | CI | Reviewer | Extra requirement |
|---|---|---|---|---|
| patch | `tier:patch` | ✅ required | ❌ | — |
| minor | `tier:minor` | ✅ required | ✅ required | `regression-tested` label must be applied |
| major | `tier:major` | ✅ required | ✅ required | Changelog reviewed + noted in PR comment |
| unknown | `tier:unknown` | ✅ required | ✅ required | — (conservative default) |

### PR flow

```mermaid
flowchart TD
    A[PR opened or updated] --> B{Is it a dependency PR?}
    B -- No --> C[Skip]
    B -- Yes --> D[Extract old/new version from PR title]
    D --> E{Parse successful?}
    E -- No --> F[Bump type: unknown]
    E -- Yes --> G[classify_bump old→new]
    G --> H[patch / minor / major]
    F --> I
    H --> I[Remove stale tier:* labels]
    I --> J[Apply tier:bump_type label]
    J --> K{ai_changelog_summary enabled + minor/major?}
    K -- Yes --> L[Call Claude Haiku with PR body]
    K -- No --> M[Skip AI step]
    L --> N[Build status comment]
    M --> N
    N --> O{Existing shield comment on PR?}
    O -- Yes --> P[Update comment in place]
    O -- No --> Q[Post new comment]
```

### Comment idempotency

Every comment the shield posts contains a hidden HTML marker:
```html
<!-- release-dependency-agent:breaking-change-shield -->
```
On each PR sync event, the script scans existing comments for this marker and edits in place rather than posting a new comment. This prevents comment spam on busy PRs.

---

## Layer 3 — Label-driven Backport Automation

**Files:** `src/backport.py`, `.github/workflows/backport.yml`

### Trigger condition

```yaml
if: |
  github.event.pull_request.merged == true &&
  contains(toJson(github.event.pull_request.labels.*.name), 'backport:')
```

The `if` condition at the job level means the runner doesn't even start for PRs that don't match — no wasted compute.

### Backport flow

```mermaid
flowchart TD
    A[PR merged with backport:X label] --> B[Parse backport:* labels → target branches]
    B --> C{For each target branch}
    C --> D[Verify target branch exists]
    D -- Not found --> E[Log warning, skip]
    D -- Found --> F["git fetch origin <target>"]
    F --> G["git checkout -B backport/<pr>-to-<target> origin/<target>"]
    G --> H[For each commit SHA in PR]
    H --> I["git cherry-pick <sha>"]
    I --> J{Exit code?}
    J -- 0 success --> K{More commits?}
    K -- Yes --> H
    K -- No --> L[All clean]
    J -- nonzero conflict --> M[Record conflicted commit SHA]
    M --> N["git cherry-pick --abort"]
    N --> O[Stop processing this target]
    L --> P["git push origin backport-branch --force-with-lease"]
    O --> P
    P --> Q{Any conflicts?}
    Q -- No --> R[Open ready-to-review PR]
    Q -- Yes --> S{open_as_draft_on_conflict?}
    S -- true --> T[Open draft PR with manual instructions]
    S -- false --> U[Skip PR creation for this target]
```

### Why abort on conflict instead of committing conflict markers?

Committed conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`) look like code changes and can confuse reviewers. An empty clean branch + a draft PR with step-by-step instructions is a much clearer signal that manual work is needed. The developer opens the PR, reads the instructions, and knows exactly what to do.

### Branch naming convention

`backport/<pr-number>-to-<sanitized-target>`

Example: PR #42 with label `backport:release/2.1` → branch `backport/42-to-release-2.1`

Slashes and other special characters in the target branch name are replaced with dashes.

---

## Shared module — common.py

| Function | Purpose |
|---|---|
| `load_config(path)` | Load `config.yml` with defaults for all missing keys |
| `get_github_client()` | Authenticated PyGithub client from `GITHUB_TOKEN` env var |
| `parse_semver(version_str)` | Parse version string → `(major, minor, patch)` tuple or `None` |
| `classify_bump(old, new)` | Classify version change as `"patch"`, `"minor"`, `"major"`, or `"unknown"` |
| `ensure_label(repo, name, color, desc)` | Create a repo label if it doesn't exist (idempotent) |

### parse_semver handles these inputs

| Input | Output |
|---|---|
| `"2.1.3"` | `(2, 1, 3)` |
| `"v2.1.3"` | `(2, 1, 3)` |
| `"2.1"` | `(2, 1, 0)` |
| `"2"` | `(2, 0, 0)` |
| `"1.2.3.4"` | `(1, 2, 3)` |
| `"abc"` | `None` |
| `None` | `None` |

Returning `None` (not `(0,0,0)`) lets `classify_bump()` distinguish a parse failure from a genuine `0.0.0` version.

---

## Security and secrets

| Secret | Used by | How it's set |
|---|---|---|
| `GITHUB_TOKEN` | All three layers | Injected automatically by Actions runner |
| `ANTHROPIC_API_KEY` | Layers 1 and 2 (optional AI features) | GitHub Secrets, only if AI features are enabled |

Neither secret is ever logged, printed, written to a file, or embedded in code. The scripts read them only from environment variables.

---

## Adding a new layer

1. Create `src/your_layer.py` — import `common.py` for config and GitHub access.
2. Create `.github/workflows/your-layer.yml` — set only the permissions you need.
3. Add any new config keys to `config.yml` with defaults in `load_config()`.

No changes to the existing layers are needed.
