"""
backport.py — Layer 3: Label-driven backport automation.

Triggered when a PR is merged and has one or more "backport:<branch>" labels.

For each target branch:
  - Cherry-pick: creates a backport branch and cherry-picks the PR's commits.
  - Clean result: pushes the branch and opens a ready-to-review PR.
  - Conflict:     aborts the cherry-pick, pushes a clean branch, and opens a DRAFT PR
                  that names the conflicting commits and explains what to resolve manually.

No AI. No auto-merging. This is deterministic git work — a human reviews every backport PR.

How to run this manually (for testing):
    Set GITHUB_TOKEN, GITHUB_REPOSITORY, and GITHUB_EVENT_PATH, then:
    python src/backport.py
"""

import os
import re
import subprocess
import sys

# Adjust path so this file can be run directly from the repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from common import load_config, get_github_client, get_repo_name, read_event_payload

CONFIG = load_config()
LABEL_PREFIX = CONFIG["backport"]["label_prefix"]
OPEN_DRAFT_ON_CONFLICT = CONFIG["backport"]["open_as_draft_on_conflict"]


# ─────────────────────────────────────────────────────────────
# Label parsing
# ─────────────────────────────────────────────────────────────

def get_backport_targets(label_names):
    """
    Extract target branch names from a list of label names.

    Example:
        labels = ["bug", "backport:release/2.1", "backport:release/2.0"]
        → ["release/2.1", "release/2.0"]

    The label prefix is configured in config.yml (default: "backport:").
    """
    targets = []
    for label in label_names:
        if label.startswith(LABEL_PREFIX):
            target = label[len(LABEL_PREFIX):].strip()
            if target:
                targets.append(target)
    return targets


# ─────────────────────────────────────────────────────────────
# Git operations
# ─────────────────────────────────────────────────────────────

def run_git(*args, check=True):
    """
    Run a git command in a subprocess and return (returncode, stdout, stderr).

    Why subprocess instead of a git library?
      Most git Python libraries (gitpython, pygit2) add complexity and dependencies.
      subprocess.run gives us direct access to the same git binary the user has, with
      no surprising behavior differences.

    Args:
        *args:  Git subcommand and arguments, e.g. run_git("cherry-pick", sha).
        check:  If True (default), raise RuntimeError on nonzero exit code.
                Pass check=False when a nonzero exit is expected (e.g. conflict detection).
    """
    workspace = os.environ.get("GITHUB_WORKSPACE", ".")
    result = subprocess.run(
        ["git"] + list(args),
        capture_output=True,
        text=True,
        cwd=workspace,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(str(a) for a in args)} failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout.strip()}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def configure_git_identity():
    """
    Set a git user identity for commits created by this automation.

    GitHub Actions runners don't have a git user configured by default.
    Using the github-actions[bot] identity is the standard convention for
    automation-created commits — it makes them visually distinct in the commit log.
    """
    run_git("config", "user.email", "github-actions[bot]@users.noreply.github.com")
    run_git("config", "user.name", "github-actions[bot]")


def fetch_pr_commits(pr_number):
    """
    Fetch the PR's source commits via GitHub's persistent PR ref.

    Why fetch refs/pull/N/head explicitly?
      After a PR is merged, the feature branch is often deleted. GitHub Actions'
      fetch-depth: 0 fetches all branches (refs/heads/*) and tags, but NOT PR refs.
      This means the individual commit SHAs from a squash-merged or rebase-merged PR
      won't exist locally — cherry-pick would fail with "bad object" errors.

      refs/pull/N/head is a GitHub-managed ref that persists even after branch deletion.
      Fetching it makes the original commit SHAs accessible regardless of merge strategy:
        - Squash merge: individual pre-squash commits exist here → cherry-picks them cleanly
        - Rebase merge: original commit SHAs exist here → equivalent to the rebased versions
        - Merge commit: commits are already in history, but fetching here is still safe

    Args:
        pr_number: The integer PR number.
    """
    pr_ref = f"refs/pull/{pr_number}/head"
    run_git("fetch", "origin", pr_ref)
    print(f"  Fetched {pr_ref} to make PR commits available for cherry-pick.")


def attempt_cherry_pick(commits, target_branch, backport_branch):
    """
    Create a new branch from target_branch and cherry-pick each commit onto it.

    Strategy for conflicts:
      If any cherry-pick fails, we record which commits conflicted, call
      `cherry-pick --abort` to restore a clean state, and return the conflict info.
      The backport branch ends up at the same state as target_branch (no partial picks).
      The caller then opens a draft PR describing what the developer needs to do manually.

    Why abort instead of committing conflict markers?
      Committed conflict markers are confusing — they look like real code changes.
      A clean branch + a clear draft PR body is a much better signal to the developer.

    Args:
        commits:          List of commit SHAs to cherry-pick (in order).
        target_branch:    The release branch we're backporting onto.
        backport_branch:  The new branch name we'll create and push.

    Returns:
        (success: bool, conflicted_commits: list[str])
        success is True only if all commits were cherry-picked without conflicts.
    """
    # Fetch the latest state of the target branch from origin
    run_git("fetch", "origin", target_branch)

    # Create (or reset) the backport branch starting from origin/<target_branch>
    run_git("checkout", "-B", backport_branch, f"origin/{target_branch}")

    conflicted_commits = []

    for sha in commits:
        code, out, err = run_git("cherry-pick", sha, check=False)

        if code != 0:
            # Cherry-pick failed — record this commit as conflicted
            conflicted_commits.append(sha)
            print(f"    Cherry-pick conflict on {sha[:8]}: {err.splitlines()[0] if err else 'unknown error'}")

            # Abort to restore a clean working tree before processing the next target
            run_git("cherry-pick", "--abort", check=False)

            # Stop processing further commits for this target — the conflict
            # means the developer needs to manually review and cherry-pick the remainder.
            break

    return len(conflicted_commits) == 0, conflicted_commits


def push_branch(branch_name):
    """
    Push the backport branch to origin.

    --force-with-lease is safer than --force: it will fail if someone else has
    pushed to this branch since we checked it out, preventing accidental overwrites.
    """
    run_git("push", "origin", branch_name, "--force-with-lease")


# ─────────────────────────────────────────────────────────────
# PR creation
# ─────────────────────────────────────────────────────────────

def build_pr_body(original_pr, target_branch, backport_branch, conflicted_commits):
    """
    Build the pull request body for a backport PR.

    Clean cherry-pick: concise confirmation with a link back to the original PR.
    Conflict:         clear, specific instructions on what the developer needs to do.

    Args:
        original_pr:        PyGithub PullRequest object of the merged source PR.
        target_branch:      The release branch we're backporting onto.
        backport_branch:    The actual name of the branch we created and pushed.
                            Used in the checkout instruction so the user gets the right
                            branch name — previously this was computed incorrectly here.
        conflicted_commits: List of SHAs that failed to cherry-pick (empty if clean).
    """
    header = (
        f"Backport of #{original_pr.number} "
        f"([{original_pr.title}]({original_pr.html_url})) "
        f"onto `{target_branch}`.\n\n"
        f"---\n\n"
    )

    if not conflicted_commits:
        body = (
            f"{header}"
            f"Cherry-pick applied cleanly. Review and merge when ready.\n\n"
            f"*Created automatically by the backport workflow. "
            f"See the original PR for context.*"
        )
    else:
        commit_list = "\n".join(
            f"- `{sha[:8]}` (full SHA: `{sha}`)" for sha in conflicted_commits
        )
        body = (
            f"{header}"
            f"⚠️ **Cherry-pick conflict — this PR needs manual attention before it can merge.**\n\n"
            f"The following commit(s) could not be applied cleanly onto `{target_branch}`:\n\n"
            f"{commit_list}\n\n"
            f"**To resolve:**\n\n"
            f"1. Check out this branch locally:\n"
            f"   ```\n"
            f"   git fetch origin\n"
            f"   git checkout {backport_branch}\n"
            f"   ```\n"
            f"2. Manually cherry-pick the conflicting commit(s) from the original PR:\n"
            f"   ```\n"
            f"   git cherry-pick <sha>\n"
            f"   # resolve conflicts, then: git cherry-pick --continue\n"
            f"   ```\n"
            f"3. Push your changes and mark this PR as ready for review.\n\n"
            f"*Created automatically by the backport workflow. "
            f"A human needs to complete this one.*"
        )

    return body


def create_backport_pr(repo, original_pr, target_branch, backport_branch, conflicted_commits):
    """
    Open the backport PR on GitHub.

    - Clean cherry-pick → ready-to-review PR
    - Conflict → draft PR (if open_as_draft_on_conflict is true in config)

    Draft PRs are used for conflicts so reviewers don't accidentally merge an
    incomplete backport. The draft state is a clear signal that work remains.
    """
    is_clean = len(conflicted_commits) == 0
    open_as_draft = not is_clean and OPEN_DRAFT_ON_CONFLICT

    pr_title = f"[backport] {original_pr.title} → `{target_branch}`"
    pr_body = build_pr_body(original_pr, target_branch, backport_branch, conflicted_commits)

    new_pr = repo.create_pull(
        title=pr_title,
        body=pr_body,
        head=backport_branch,
        base=target_branch,
        draft=open_as_draft,
    )

    status = "✅ clean" if is_clean else "⚠️ conflict (opened as draft)"
    print(f"    Opened PR #{new_pr.number}: {new_pr.html_url} [{status}]")
    return new_pr


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main():
    payload = read_event_payload()
    if not payload:
        raise EnvironmentError(
            "GITHUB_EVENT_PATH is not set. This script is designed to run inside GitHub Actions.\n"
            "For local testing, point GITHUB_EVENT_PATH at a JSON file containing a sample event."
        )
    pr_data = payload.get("pull_request", {})

    # Guard: do nothing if the PR was closed without merging
    if not pr_data.get("merged", False):
        print("PR was closed without merging — nothing to backport.")
        return

    repo_name = get_repo_name()
    gh = get_github_client()
    repo = gh.get_repo(repo_name)

    # Re-fetch the PR via API to get up-to-date labels (the event payload can lag)
    pr = repo.get_pull(pr_data["number"])
    label_names = [label.name for label in pr.labels]
    targets = get_backport_targets(label_names)

    if not targets:
        print(f"PR #{pr.number} has no backport labels — nothing to do.")
        return

    print(f"PR #{pr.number} merged: '{pr.title}'")
    print(f"Backport targets: {targets}")

    # Fetch the PR's source commits from GitHub's persistent PR ref.
    # This is required because:
    #   - For squash/rebase merges, the original commit SHAs aren't in main's history.
    #   - GitHub Actions' fetch-depth: 0 fetches branches and tags, but not refs/pull/*.
    #   - Without this fetch, cherry-pick would fail with "bad object" for those SHAs.
    # See fetch_pr_commits() docstring for a full explanation.
    fetch_pr_commits(pr.number)
    commits = [c.sha for c in pr.get_commits()]

    if not commits:
        print("No commits found in PR — skipping.")
        return

    print(f"Commits to cherry-pick: {[s[:8] for s in commits]}")

    configure_git_identity()

    errors = []
    for target_branch in targets:
        print(f"\nProcessing backport → '{target_branch}'...")

        # Build a branch name that's unique and human-readable.
        # Slashes in target branch names are replaced with dashes since git
        # branch names can contain slashes but they can confuse some tools.
        safe_target = re.sub(r"[^a-zA-Z0-9._-]", "-", target_branch)
        backport_branch = f"backport/{pr.number}-to-{safe_target}"

        try:
            # Verify the target branch actually exists before attempting anything
            try:
                repo.get_branch(target_branch)
            except Exception:
                print(f"    ⚠️  Target branch '{target_branch}' not found in repo — skipping.")
                continue

            success, conflicted_commits = attempt_cherry_pick(
                commits, target_branch, backport_branch
            )
            push_branch(backport_branch)
            create_backport_pr(repo, pr, target_branch, backport_branch, conflicted_commits)

        except Exception as exc:
            msg = f"Error backporting to '{target_branch}': {exc}"
            print(f"    ❌ {msg}")
            errors.append(msg)

    if errors:
        print(f"\n{len(errors)} error(s) occurred during backport:")
        for err in errors:
            print(f"  • {err}")
        sys.exit(1)

    print("\nBackport complete.")


if __name__ == "__main__":
    main()
