"""
breaking_change_shield.py — Layer 2: Tiered approval based on semver bump type.

Triggered on every dependency-update PR open or sync event.

Logic:
  1. Detect whether the PR is a dependency update (by label, bot author, or title pattern).
  2. Extract old and new version strings from the PR title.
  3. Classify the bump: patch / minor / major (using semver helper from common.py).
  4. Apply a tier:* label to the PR.
  5. Post (or update) a human-readable status comment explaining what's required.
  6. (Optional, config-gated) Call Claude Haiku to summarize the changelog for major/minor bumps.

No auto-merging. No auto-approving. Labels and comments only — a human decides on everything.

How to run this manually (for testing):
    Set GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_EVENT_PATH, then:
    python src/breaking_change_shield.py
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from common import load_config, get_github_client, classify_bump, ensure_label

CONFIG = load_config()
TIER_CONFIG = CONFIG["tiers"]
DEP_LABELS = set(CONFIG["dependency_labels"])

# This marker is embedded in every comment we post so we can find and update it
# next time instead of posting a new comment on each PR synchronize event.
COMMENT_MARKER = "<!-- release-dependency-agent:breaking-change-shield -->"

# Label colors: a consistent visual language — blue=safe, amber=caution, red=high-risk
TIER_LABEL_COLORS = {
    "patch":   "0075ca",  # blue
    "minor":   "e4a11b",  # amber
    "major":   "d73a4a",  # red
    "unknown": "ededed",  # grey (couldn't parse versions)
}


# ─────────────────────────────────────────────────────────────
# Environment helpers
# ─────────────────────────────────────────────────────────────

def read_event_payload():
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        raise EnvironmentError("GITHUB_EVENT_PATH is not set.")
    with open(event_path, encoding="utf-8") as f:
        return json.load(f)


def get_repo_name():
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        raise EnvironmentError("GITHUB_REPOSITORY is not set.")
    return repo


# ─────────────────────────────────────────────────────────────
# Dependency PR detection
# ─────────────────────────────────────────────────────────────

def is_dependency_pr(pr, dep_labels):
    """
    Determine whether a PR is a dependency update.

    Uses three signals (OR logic — any one is sufficient):
    1. Label match: the PR has a label in the configured dependency_labels list
                    (e.g. "dependencies", "dependabot", "renovate").
    2. Bot author:  the PR was opened by dependabot[bot] or renovate[bot].
    3. Title pattern: the title matches "from X.Y.Z to A.B.C" (common in both tools).

    Why three signals?
      Teams configure Dependabot/Renovate differently. Some apply labels; some don't.
      Some use the default bot account; some proxy through a custom app. Covering all
      three patterns makes the detection robust without requiring any specific setup.
    """
    pr_label_names = {label.name.lower() for label in pr.labels}
    if pr_label_names & {lbl.lower() for lbl in dep_labels}:
        return True

    author = pr.user.login.lower()
    if "dependabot" in author or "renovate" in author:
        return True

    if re.search(r"\bfrom\s+v?[\d]+(?:\.[\d]+)*\s+to\s+v?[\d]+(?:\.[\d]+)*", pr.title, re.IGNORECASE):
        return True

    return False


# ─────────────────────────────────────────────────────────────
# Version extraction
# ─────────────────────────────────────────────────────────────

def extract_versions_from_title(title):
    """
    Parse old and new versions from a Dependabot or Renovate PR title.

    Handles the two most common formats:
      "Bump requests from 2.28.0 to 2.29.0"
      "chore(deps): bump requests from 2.28.0 to 2.29.0"

    The regex is intentionally permissive on what counts as a version component
    (just digits and dots) so it works across Python, JS, Ruby, and other ecosystems.

    Returns:
        (old_version, new_version) as strings, or (None, None) if not found.
    """
    pattern = r"from\s+(v?[\d]+(?:\.[\d]+)*)\s+to\s+(v?[\d]+(?:\.[\d]+)*)"
    match = re.search(pattern, title, re.IGNORECASE)
    if match:
        return match.group(1), match.group(2)
    return None, None


# ─────────────────────────────────────────────────────────────
# Label management
# ─────────────────────────────────────────────────────────────

def apply_tier_label(repo, pr, bump_type):
    """
    Apply the appropriate tier:* label to the PR.

    Removes any stale tier:* labels first — if a PR is edited and the version
    changes, we don't want the old label lingering alongside the new one.
    """
    # Remove all existing tier labels before applying the new one
    for label in list(pr.labels):
        if label.name.startswith("tier:"):
            pr.remove_from_labels(label)

    label_name = f"tier:{bump_type}"
    color = TIER_LABEL_COLORS.get(bump_type, "ededed")
    ensure_label(repo, label_name, color, f"Dependency bump tier: {bump_type}")
    pr.add_to_labels(label_name)
    print(f"  Applied label: {label_name}")


# ─────────────────────────────────────────────────────────────
# Comment building
# ─────────────────────────────────────────────────────────────

def build_status_comment(bump_type, old_version, new_version):
    """
    Build the tier status comment text.

    Tone: direct and informative, not bureaucratic. A developer reading this
    should immediately understand what they need to do and why — not feel like
    they're reading a policy document.
    """
    tier = TIER_CONFIG.get(bump_type, {})

    bump_descriptions = {
        "patch":   "a **patch bump** — bug fixes only, low risk",
        "minor":   "a **minor version bump** — new features, should be backwards-compatible",
        "major":   "a **major version bump** — may include breaking changes, needs careful review",
        "unknown": "a **version change we couldn't classify** (couldn't parse the version numbers from the title)",
    }
    description = bump_descriptions.get(bump_type, "a version change")

    version_info = ""
    if old_version and new_version:
        version_info = f" (`{old_version}` → `{new_version}`)"

    # Build the checklist of what's required before this PR can merge
    requirements = []
    if tier.get("require_ci"):
        requirements.append("CI must pass")
    if tier.get("require_review"):
        requirements.append("at least one reviewer must approve")
    if tier.get("require_label"):
        req_label = tier["require_label"]
        requirements.append(
            f"label `{req_label}` must be added — this is your signal that regression "
            f"testing ran and passed"
        )
    if tier.get("require_changelog_review"):
        requirements.append(
            "the changelog / release notes must be reviewed and any breaking changes noted "
            "in a PR comment"
        )

    if requirements:
        req_lines = "\n".join(f"- [ ] {r.capitalize()}" for r in requirements)
        req_block = f"**Before this merges:**\n\n{req_lines}"
    else:
        req_block = "No additional requirements — CI passing is enough for a patch bump."

    comment = (
        f"{COMMENT_MARKER}\n\n"
        f"### 🛡️ Breaking-change shield\n\n"
        f"This is {description}{version_info}. "
        f"Assigned tier: **`tier:{bump_type}`**.\n\n"
        f"{req_block}\n\n"
        f"---\n"
        f"<sub>Tier thresholds are configured in `config.yml`. "
        f"This comment updates automatically if the PR is edited.</sub>"
    )
    return comment


# ─────────────────────────────────────────────────────────────
# Comment posting
# ─────────────────────────────────────────────────────────────

def find_existing_shield_comment(pr):
    """
    Look for a previous shield comment on this PR.

    We identify our comment by the hidden COMMENT_MARKER string at the top.
    This lets us update the comment on PR sync instead of posting duplicates —
    nobody wants a wall of bot comments every time a PR is rebased.
    """
    for comment in pr.get_issue_comments():
        if COMMENT_MARKER in comment.body:
            return comment
    return None


def post_or_update_comment(pr, body):
    """Post a new comment or update our existing one."""
    existing = find_existing_shield_comment(pr)
    if existing:
        existing.edit(body)
        print(f"  Updated existing shield comment (id={existing.id}).")
    else:
        pr.create_issue_comment(body)
        print("  Posted new shield comment.")


# ─────────────────────────────────────────────────────────────
# Optional AI changelog summary
# ─────────────────────────────────────────────────────────────

def get_ai_changelog_summary(pr, bump_type, ai_config, max_calls_tracker):
    """
    (Optional) Use Claude Haiku to summarize potential breaking changes from the PR body.

    This only fires when:
      - tiers.ai_changelog_summary is true in config.yml
      - The bump is minor or major (patch changes rarely have breaking content worth summarizing)
      - We haven't hit the ai.max_calls_per_run limit
      - ANTHROPIC_API_KEY is set in the environment

    Cost per call: ~1,000 input tokens + ~300 output tokens ≈ $0.0004 at Haiku rates.
    A team with 10 major/minor dep PRs/month would spend ~$0.004/month on this feature.

    Returns:
        A summary string, or None if the feature is disabled or skipped.
    """
    if not TIER_CONFIG.get("ai_changelog_summary", False):
        return None  # Feature is off by default

    if bump_type not in ("minor", "major"):
        return None  # Not worth summarizing patch-level changes

    if max_calls_tracker["count"] >= max_calls_tracker["limit"]:
        print("  AI call limit reached — skipping changelog summary.")
        return None

    try:
        import anthropic
    except ImportError:
        print("  anthropic package not installed — skipping AI changelog summary.")
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("  ANTHROPIC_API_KEY not set — skipping AI changelog summary.")
        return None

    pr_body = pr.body or ""
    if not pr_body.strip():
        print("  PR body is empty — nothing to summarize.")
        return None

    model = ai_config.get("model", "claude-haiku-4-5-20251001")

    prompt = (
        f"I'm reviewing a dependency update PR (a {bump_type} version bump). "
        f"Here is the PR description, which may include release notes or a changelog:\n\n"
        f"{pr_body[:3000]}\n\n"  # cap at ~3000 chars to control token cost
        f"In 3-5 bullet points, summarize the most important things a developer should know — "
        f"especially any breaking changes, removed APIs, or behavior differences. "
        f"Be specific and concise. If there are no obvious breaking changes, say so plainly."
    )

    client = anthropic.Anthropic(api_key=api_key)
    max_calls_tracker["count"] += 1

    response = client.messages.create(
        model=model,
        max_tokens=350,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main():
    payload = read_event_payload()
    pr_data = payload.get("pull_request", {})

    repo_name = get_repo_name()
    gh = get_github_client()
    repo = gh.get_repo(repo_name)
    pr = repo.get_pull(pr_data["number"])

    print(f"Processing PR #{pr.number}: {pr.title}")

    if not is_dependency_pr(pr, DEP_LABELS):
        print("  Not a dependency PR — skipping.")
        return

    # Extract versions from the PR title
    old_version, new_version = extract_versions_from_title(pr.title)
    if old_version and new_version:
        bump_type = classify_bump(old_version, new_version)
    else:
        bump_type = "unknown"
        print("  Could not extract version numbers from PR title.")

    print(f"  Versions: {old_version or '?'} → {new_version or '?'} | Bump type: {bump_type}")

    # Apply the tier label
    apply_tier_label(repo, pr, bump_type)

    # (Optional) AI changelog summary — gated behind config flag and call limit
    ai_config = CONFIG.get("ai", {})
    max_calls_tracker = {
        "count": 0,
        "limit": ai_config.get("max_calls_per_run", 5),
    }
    ai_summary = get_ai_changelog_summary(pr, bump_type, ai_config, max_calls_tracker)

    # Build the comment body
    comment_body = build_status_comment(bump_type, old_version, new_version)
    if ai_summary:
        comment_body += (
            f"\n\n---\n\n"
            f"**📝 AI-generated changelog summary (Haiku):**\n\n"
            f"{ai_summary}\n\n"
            f"<sub>AI summary is experimental. Always review the actual release notes.</sub>"
        )

    post_or_update_comment(pr, comment_body)

    print(f"  Done. Tier: {bump_type}")


if __name__ == "__main__":
    main()
