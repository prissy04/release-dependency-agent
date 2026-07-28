"""
freeze_check.py — Layer 1: Dependency freeze window + early-warning system.

Two modes (the workflow tells us which via the FREEZE_TRIGGER env var):

  PR mode   (FREEZE_TRIGGER=pr):
    Triggered when a dependency PR is opened. Classifies that single PR
    against the release timeline and applies a freeze:* label.

  Digest mode (FREEZE_TRIGGER=digest, the default):
    Triggered on the weekly cron (every Monday). Scans all open dependency PRs,
    classifies each one, applies labels, then creates or updates a GitHub Issue
    with a plain-language risk summary.

No auto-merging. No auto-closing. Labels and issues only.

How to run this manually:
    Set GITHUB_TOKEN, GITHUB_REPOSITORY, and (optionally) FREEZE_TRIGGER, then:
    python src/freeze_check.py
"""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from common import (
    load_config,
    get_github_client,
    get_repo_name,
    read_event_payload,
    is_dependency_pr,
    ensure_label,
)

# Freeze status levels — these map directly to label names and digest sections
FREEZE_SAFE   = "safe"
FREEZE_TIGHT  = "tight"
FREEZE_INSIDE = "inside-freeze"

# Label colors: green/amber/red matches the urgency of each status
FREEZE_LABEL_COLORS = {
    FREEZE_SAFE:   "0e8a16",  # green
    FREEZE_TIGHT:  "e4a11b",  # amber
    FREEZE_INSIDE: "d73a4a",  # red
}

DIGEST_ISSUE_TITLE_PREFIX = "📦 Weekly Dependency Risk Digest"


# ─────────────────────────────────────────────────────────────
# Core freeze window logic
# ─────────────────────────────────────────────────────────────

def days_until_ship(ship_date_str):
    """
    Compute how many calendar days remain until the configured ship date.

    Returns a negative number if the ship date has already passed.
    The caller uses this to classify PRs and generate timeline commentary.

    Args:
        ship_date_str: ISO format date string, e.g. "2026-07-15".

    Raises:
        ValueError: If ship_date_str is not a valid ISO date. The caller
                    (main()) catches this and exits with a clear message.
    """
    ship = date.fromisoformat(ship_date_str)
    return (ship - date.today()).days


def classify_freeze_status(days_to_ship, freeze_window_days, tight_window_days):
    """
    Classify a dependency PR's risk level based on how close we are to the ship date.

    Three zones:
      inside-freeze  ≤ freeze_window_days to ship
                     The freeze is on. Only security fixes should merge.
                     This is the hard stop — any dependency update inside this window
                     could introduce a regression with no time to catch it before ship.

      tight          ≤ tight_window_days to ship (but outside freeze)
                     Not blocked, but needs attention now. If a reviewer doesn't look
                     at these today, there may not be enough time to test and fix issues.

      safe           > tight_window_days to ship
                     Normal review cadence applies. No urgency.

    This classification encodes a PM judgment: how much test time does the team
    need to be confident a dependency update won't break the release? The defaults
    (10-day freeze, 21-day tight window) match common enterprise release cadences,
    but they're fully configurable in config.yml.

    Args:
        days_to_ship:       Result of days_until_ship().
        freeze_window_days: Hard freeze threshold (from config).
        tight_window_days:  Early-warning threshold (from config).

    Returns:
        FREEZE_INSIDE | FREEZE_TIGHT | FREEZE_SAFE
    """
    if days_to_ship <= freeze_window_days:
        return FREEZE_INSIDE
    if days_to_ship <= tight_window_days:
        return FREEZE_TIGHT
    return FREEZE_SAFE

def normalize_ship_date(ship_date_str, auto_advance_days):
    """
    Advance a stale ship date by repeated fixed intervals until it reaches today or later.

    This avoids repeated "ship date has passed" warnings when the configured
    date is a stale milestone for a regularly scheduled release cadence.
    """
    ship = date.fromisoformat(ship_date_str)
    if auto_advance_days <= 0:
        return ship_date_str, (ship - date.today()).days

    while ship < date.today():
        ship += timedelta(days=auto_advance_days)

    return ship.isoformat(), (ship - date.today()).days

# ─────────────────────────────────────────────────────────────
# Label application
# ─────────────────────────────────────────────────────────────

def apply_freeze_label(repo, pr, status):
    """
    Apply a freeze:* label to the PR. Removes any stale freeze:* labels first.

    This is idempotent — running the workflow multiple times on the same PR
    won't stack labels; the status just gets updated.
    """
    for label in list(pr.labels):
        if label.name.startswith("freeze:"):
            pr.remove_from_labels(label)

    label_name = f"freeze:{status}"
    color = FREEZE_LABEL_COLORS.get(status, "ededed")
    ensure_label(repo, label_name, color, f"Dependency PR freeze status: {status}")
    pr.add_to_labels(label_name)


# ─────────────────────────────────────────────────────────────
# Digest building
# ─────────────────────────────────────────────────────────────

def build_digest_body(dep_prs_by_status, days_to_ship, ship_date_str, freeze_window_days, tight_window_days):
    """
    Build the Markdown body for the weekly digest issue.

    Tone: written for a busy engineer or TPM who's doing a Monday morning review.
    The goal is to make the risk landscape instantly clear — what needs action today,
    what can wait, and where the team stands relative to the release.

    No jargon, no passive voice, no "please be advised" filler.
    """
    today_str = date.today().isoformat()

    # Timeline status line — the first thing a reader should absorb
    if days_to_ship < 0:
        timeline_note = (
            f"⚠️ Ship date ({ship_date_str}) has passed. "
            f"Update `release.ship_date` in `config.yml` with the next release date."
        )
    elif days_to_ship == 0:
        timeline_note = "🚨 **Shipping today.** All dependency updates are inside the freeze window."
    elif days_to_ship <= freeze_window_days:
        timeline_note = (
            f"🚨 **Inside the {freeze_window_days}-day freeze window** "
            f"({days_to_ship} days to {ship_date_str}). "
            f"Only security fixes should merge."
        )
    elif days_to_ship <= tight_window_days:
        timeline_note = (
            f"⚠️ **{days_to_ship} days to ship** ({ship_date_str}). "
            f"The window is getting tight — prioritize any open dependency PRs this week."
        )
    else:
        timeline_note = (
            f"✅ **{days_to_ship} days to ship** ({ship_date_str}). "
            f"Good runway — normal review cadence applies."
        )

    inside = dep_prs_by_status.get(FREEZE_INSIDE, [])
    tight  = dep_prs_by_status.get(FREEZE_TIGHT, [])
    safe   = dep_prs_by_status.get(FREEZE_SAFE, [])
    total  = len(inside) + len(tight) + len(safe)

    def pr_row(pr):
        return f"- [{pr.title}]({pr.html_url}) (#{pr.number})"

    sections = []

    if inside:
        section_lines = [
            f"### 🚨 Inside the freeze window — do not merge ({len(inside)})\n",
            f"These arrived within {freeze_window_days} days of ship. "
            f"Merge only if it's a confirmed security fix, and only with explicit sign-off.\n",
        ]
        section_lines.extend(pr_row(p) for p in inside)
        sections.append("\n".join(section_lines))

    if tight:
        section_lines = [
            f"### ⚠️ Review urgently — inside the tight window ({len(tight)})\n",
            f"These are within {tight_window_days} days of ship. "
            f"If they're not reviewed and tested this week, they'll likely miss the release.\n",
        ]
        section_lines.extend(pr_row(p) for p in tight)
        sections.append("\n".join(section_lines))

    if safe:
        section_lines = [
            f"### ✅ Safe runway — normal review cadence ({len(safe)})\n",
        ]
        section_lines.extend(pr_row(p) for p in safe)
        sections.append("\n".join(section_lines))

    body_content = (
        "\n\n---\n\n".join(sections) if sections
        else "No open dependency PRs found this week. 🎉"
    )

    digest = (
        f"<!-- release-dependency-agent:digest -->\n\n"
        f"*Generated {today_str} · {total} open dependency PR(s)*\n\n"
        f"{timeline_note}\n\n"
        f"---\n\n"
        f"{body_content}\n\n"
        f"---\n\n"
        f"<sub>Updated automatically each Monday. "
        f"Freeze window: {freeze_window_days} days. "
        f"Tight window: {tight_window_days} days. "
        f"Configured in `config.yml`.</sub>"
    )

    return digest


# ─────────────────────────────────────────────────────────────
# Optional AI digest summary
# ─────────────────────────────────────────────────────────────

def get_ai_digest_intro(dep_prs_by_status, release_config, ai_config, max_calls_tracker):
    """
    (Optional) Call Claude Haiku to write a 2-3 sentence plain-English intro for the digest.

    The AI intro sits above the detailed sections and gives a quick "read this first"
    summary for busy readers. The code-generated content below it is always present
    regardless of whether AI is enabled.

    This only fires when:
      - release.ai_summary is true in config.yml
      - ANTHROPIC_API_KEY is set in the environment
      - We haven't hit the ai.max_calls_per_run limit

    Cost: 1 Haiku call per weekly run ≈ $0.001/month at typical token counts.
    Safe fallback: if the call fails for any reason, we return None and the digest
    is posted without an AI intro — no errors, no gaps in the output.

    Returns:
        A summary string, or None if the feature is disabled or skipped.
    """
    if not release_config.get("ai_summary", False):
        return None

    if max_calls_tracker["count"] >= max_calls_tracker["limit"]:
        print("AI call limit reached — skipping digest intro.")
        return None

    try:
        import anthropic
    except ImportError:
        print("anthropic package not installed — skipping AI digest intro.")
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY not set — skipping AI digest intro.")
        return None

    inside = dep_prs_by_status.get(FREEZE_INSIDE, [])
    tight  = dep_prs_by_status.get(FREEZE_TIGHT, [])
    safe   = dep_prs_by_status.get(FREEZE_SAFE, [])

    pr_lines = []
    for p in inside:
        pr_lines.append(f"INSIDE FREEZE: {p.title}")
    for p in tight:
        pr_lines.append(f"TIGHT WINDOW:  {p.title}")
    for p in safe:
        pr_lines.append(f"SAFE:          {p.title}")

    pr_summary = "\n".join(pr_lines) if pr_lines else "No open dependency PRs."

    prompt = (
        "I manage releases for a software team. Here's this week's snapshot of open "
        "dependency update PRs, labeled by their risk level relative to our upcoming release:\n\n"
        f"{pr_summary}\n\n"
        "Write 2-3 sentences for an engineer doing Monday morning triage: what needs immediate "
        "attention and what can wait. Be direct and specific. Don't use jargon or filler phrases."
    )

    model = ai_config.get("model", "claude-haiku-4-5-20251001")
    client = anthropic.Anthropic(api_key=api_key)
    max_calls_tracker["count"] += 1

    try:
        response = client.messages.create(
            model=model,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except Exception as exc:
        # Never let an AI call failure break the digest — fall back gracefully
        print(f"AI summary call failed: {exc}. Continuing without it.")
        return None


# ─────────────────────────────────────────────────────────────
# Digest issue management
# ─────────────────────────────────────────────────────────────

def find_or_update_digest_issue(repo, title, body, digest_label):
    """
    Find an existing open digest issue and update it, or create a new one.

    We identify the digest issue by:
    1. It has the configured digest_label.
    2. Its title starts with DIGEST_ISSUE_TITLE_PREFIX.

    Using a label for lookup is more reliable than matching the exact title,
    since the title includes the date (which changes every week). If multiple
    matching issues exist somehow, we update the first one (most recently updated).

    Why update instead of close-and-reopen?
      A running issue preserves the comment thread and notification history.
      Teams that discuss dependencies in the issue thread don't lose that context.
    """
    ensure_label(repo, digest_label, "0075ca", "Weekly dependency risk digest issue")

    for issue in repo.get_issues(state="open", labels=[digest_label]):
        if issue.title.startswith(DIGEST_ISSUE_TITLE_PREFIX):
            issue.edit(title=title, body=body)
            print(f"Updated existing digest issue #{issue.number}: {issue.html_url}")
            return issue

    # No existing issue found — create a fresh one
    new_issue = repo.create_issue(
        title=title,
        body=body,
        labels=[digest_label],
    )
    print(f"Created new digest issue #{new_issue.number}: {new_issue.html_url}")
    return new_issue


# ─────────────────────────────────────────────────────────────
# Mode handlers
# ─────────────────────────────────────────────────────────────

def process_single_pr(repo, pr_number, days_to_ship, freeze_window_days, tight_window_days, dep_labels):
    """
    PR mode: classify and label a single dependency PR.
    Called when a new PR is opened (fast, minimal API calls).
    """
    pr = repo.get_pull(pr_number)
    print(f"Checking PR #{pr.number}: {pr.title}")

    if not is_dependency_pr(pr, dep_labels):
        print("  Not a dependency PR — skipping.")
        return

    status = classify_freeze_status(days_to_ship, freeze_window_days, tight_window_days)
    apply_freeze_label(repo, pr, status)
    print(f"  Status: {status} ({days_to_ship} days to ship)")


def process_all_prs_for_digest(repo, days_to_ship, freeze_window_days, tight_window_days, ship_date_str, config):
    """
    Digest mode: scan all open dependency PRs, classify them, and create/update the weekly issue.
    Called on the weekly cron and workflow_dispatch.
    """
    release_config = config["release"]
    dep_labels     = config["dependency_labels"]
    ai_config      = config.get("ai", {})
    digest_label   = release_config.get("digest_label", "dependency-digest")

    dep_prs_by_status = {FREEZE_INSIDE: [], FREEZE_TIGHT: [], FREEZE_SAFE: []}

    for pr in repo.get_pulls(state="open"):
        if not is_dependency_pr(pr, dep_labels):
            continue
        status = classify_freeze_status(days_to_ship, freeze_window_days, tight_window_days)
        apply_freeze_label(repo, pr, status)
        dep_prs_by_status[status].append(pr)

    total = sum(len(v) for v in dep_prs_by_status.values())
    print(f"Classified {total} open dependency PR(s).")

    # (Optional) AI intro — gated behind config flag
    max_calls_tracker = {"count": 0, "limit": ai_config.get("max_calls_per_run", 5)}
    ai_intro = get_ai_digest_intro(dep_prs_by_status, release_config, ai_config, max_calls_tracker)

    digest_body = build_digest_body(
        dep_prs_by_status, days_to_ship, ship_date_str, freeze_window_days, tight_window_days
    )

    # If we have an AI intro, splice it in just after the timeline note (after the first ---)
    if ai_intro:
        digest_body = digest_body.replace(
            "---\n\n",
            f"---\n\n> **At a glance (AI summary):** {ai_intro}\n\n",
            1,  # replace only the first occurrence
        )

    today_str = date.today().isoformat()
    digest_title = f"{DIGEST_ISSUE_TITLE_PREFIX} — {today_str}"
    find_or_update_digest_issue(repo, digest_title, digest_body, digest_label)


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main():
    # Load config here in main(), not at module level.
    # Module-level loading would run on import and require config.yml to be present
    # even when just importing a helper function (e.g. in tests). Loading in main()
    # keeps imports side-effect-free.
    config         = load_config()
    release_config = config["release"]
    dep_labels     = set(config["dependency_labels"])

    ship_date_str = release_config.get("ship_date")
    if not ship_date_str:
        print("ERROR: release.ship_date is not configured in config.yml.")
        print('Set it to your next ship date in ISO format, e.g.: ship_date: "2026-07-15"')
        sys.exit(1)

    # Validate the date format early. date.fromisoformat() raises ValueError for
    # wrong formats like "07/15/2026" (MM/DD/YYYY). Catching it here gives the user
    # a plain-English message instead of a raw Python traceback.
    try:
        ship_date_str, days_to_ship = normalize_ship_date(
            ship_date_str, release_config.get("auto_advance_days", 30)
        )
    except ValueError:
        print(f"ERROR: ship_date '{ship_date_str}' is not a valid date.")
        print('The format must be YYYY-MM-DD (ISO 8601), e.g.: ship_date: "2026-07-15"')
        sys.exit(1)

    if ship_date_str != release_config.get("ship_date"):
        print(
            f"INFO: configured ship_date has passed; auto-advanced to {ship_date_str}."
        )

    freeze_window_days = release_config.get("freeze_window_days", 10)
    tight_window_days  = release_config.get("tight_window_days", freeze_window_days * 2)

    print(f"Ship date: {ship_date_str} | Days remaining: {days_to_ship}")
    print(f"Freeze window: {freeze_window_days} days | Tight window: {tight_window_days} days")

    repo_name = get_repo_name()
    gh = get_github_client()
    repo = gh.get_repo(repo_name)

    # The workflow sets FREEZE_TRIGGER so we know which mode to run.
    # Default to "digest" (the cron/manual run mode) if the env var isn't set.
    trigger = os.environ.get("FREEZE_TRIGGER", "digest")

    if trigger == "pr":
        payload = read_event_payload()
        if payload and "pull_request" in payload:
            pr_number = payload["pull_request"]["number"]
            process_single_pr(
                repo, pr_number, days_to_ship, freeze_window_days, tight_window_days, dep_labels
            )
        else:
            print("PR trigger set but no pull_request in event payload — nothing to do.")
    else:
        if release_config.get("weekly_digest", True):
            process_all_prs_for_digest(
                repo, days_to_ship, freeze_window_days, tight_window_days, ship_date_str, config
            )
        else:
            print("weekly_digest is disabled in config.yml — skipping digest creation.")


if __name__ == "__main__":
    main()
