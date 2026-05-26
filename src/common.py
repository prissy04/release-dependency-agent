"""
common.py — Shared helpers used by all three layers.

Provides:
  - load_config()       Load and validate config.yml with sensible defaults.
  - get_github_client() Authenticated PyGithub client from GITHUB_TOKEN env var.
  - parse_semver()      Parse a version string → (major, minor, patch) tuple or None.
  - classify_bump()     Given two version strings, return "patch" / "minor" / "major" / "unknown".
  - ensure_label()      Create a repo label if it doesn't already exist.

Why a shared module?
  Each layer is an independent script, but they all need GitHub access, config, and semver logic.
  Centralizing here means one place to fix bugs and one place to add logging later.
"""

import os
import yaml

# PyGithub — the main library we use to talk to the GitHub API (create PRs, labels, issues).
from github import Github, GithubException


# ─────────────────────────────────────────────────────────────
# Config loading
# ─────────────────────────────────────────────────────────────

def load_config(path=None):
    """
    Load config.yml and return it as a plain Python dict with sensible defaults filled in.

    Why defaults?
      If someone uses only Layer 3, they shouldn't have to define Layer 1 settings.
      Defaults here match the values documented in config.yml so behavior is predictable
      even if a key is missing.

    Args:
        path: Explicit path to config.yml. If None, searches the current directory first
              (the normal case when running from the repo root in GitHub Actions), then
              falls back to one level above this file's location (useful in local testing).
    """
    if path is None:
        candidates = [
            # Running from repo root (standard in GitHub Actions and most local runs)
            "config.yml",
            # Running tests or scripts from inside src/ or tests/
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yml"),
        ]
        for candidate in candidates:
            if os.path.exists(candidate):
                path = candidate
                break
        if path is None:
            raise FileNotFoundError(
                "config.yml not found. Run from the repo root, or pass an explicit path."
            )

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    config = raw or {}

    # Layer 1 defaults
    config.setdefault("release", {})
    config["release"].setdefault("freeze_window_days", 10)
    config["release"].setdefault("tight_window_days", config["release"]["freeze_window_days"] * 2)
    config["release"].setdefault("weekly_digest", True)
    config["release"].setdefault("digest_label", "dependency-digest")
    config["release"].setdefault("ai_summary", False)

    # Layer 2 defaults
    config.setdefault("tiers", {})
    config["tiers"].setdefault("patch", {"require_ci": True, "require_review": False})
    config["tiers"].setdefault(
        "minor",
        {"require_ci": True, "require_review": True, "require_label": "regression-tested"},
    )
    config["tiers"].setdefault(
        "major",
        {"require_ci": True, "require_review": True, "require_changelog_review": True},
    )
    config["tiers"].setdefault("ai_changelog_summary", False)

    # Layer 3 defaults
    config.setdefault("backport", {})
    config["backport"].setdefault("label_prefix", "backport:")
    config["backport"].setdefault("open_as_draft_on_conflict", True)

    # Shared defaults
    config.setdefault("dependency_labels", ["dependencies", "dependabot", "renovate"])
    config.setdefault("ai", {})
    config["ai"].setdefault("model", "claude-haiku-4-5-20251001")
    config["ai"].setdefault("max_calls_per_run", 5)

    return config


# ─────────────────────────────────────────────────────────────
# GitHub client
# ─────────────────────────────────────────────────────────────

def get_github_client():
    """
    Create an authenticated PyGithub client using the GITHUB_TOKEN environment variable.

    Why env var?
      Tokens must never be hardcoded. In GitHub Actions, GITHUB_TOKEN is injected
      automatically by the runner. For local testing, set it manually in your shell.
      Never print it, log it, or write it to a file.

    Raises:
        EnvironmentError: If GITHUB_TOKEN is not set.
    """
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise EnvironmentError(
            "GITHUB_TOKEN environment variable is not set.\n"
            "In GitHub Actions, this is provided automatically.\n"
            "For local testing, run: set GITHUB_TOKEN=<your-personal-access-token>"
        )
    return Github(token)


# ─────────────────────────────────────────────────────────────
# Semver parsing and bump classification
# ─────────────────────────────────────────────────────────────

def parse_semver(version_str):
    """
    Parse a version string into a (major, minor, patch) tuple of integers.

    Handles real-world quirks from Dependabot/Renovate PR titles:
      - Leading 'v' prefix:  "v1.2.3" → (1, 2, 3)
      - Two-part versions:   "3.1"    → (3, 1, 0)
      - One-part versions:   "4"      → (4, 0, 0)
      - Extra parts ignored: "1.2.3.4" → (1, 2, 3)
      - Unparseable input:   "abc"    → None  (signals caller to treat as unknown)
      - None or empty:                → None

    Returns:
        A (major, minor, patch) tuple of ints, or None if the string can't be parsed.

    Why return None instead of (0, 0, 0)?
      (0, 0, 0) is itself a valid version (pre-release packages often start there).
      Returning None lets classify_bump() distinguish "genuinely 0.0.0" from "failed to parse."
    """
    if version_str is None:
        return None

    version_str = str(version_str).strip().lstrip("v")

    if not version_str:
        return None

    parts = version_str.split(".")
    try:
        major = int(parts[0]) if len(parts) > 0 else 0
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0
        return (major, minor, patch)
    except (ValueError, IndexError):
        return None


def classify_bump(old_version_str, new_version_str):
    """
    Given two version strings, classify the change as "major", "minor", "patch", or "unknown".

    This is the PM judgment encoded in code: semver encodes intent.
      - "major" signals breaking changes → require the most scrutiny (Layer 2 tier rules).
      - "minor" signals new features, backwards-compatible → require regression testing.
      - "patch" signals bug fixes → CI is enough.
      - "unknown" means we couldn't parse one or both versions → caller should treat conservatively.

    Why != instead of > for major/minor?
      Semver resets lower components on a major/minor bump (e.g. 1.9.9 → 2.0.0).
      Using != on the major field catches both upgrades and (rare) downgrades.
      A major version change in either direction warrants scrutiny.

    Returns:
        "major" | "minor" | "patch" | "unknown"
    """
    old = parse_semver(old_version_str)
    new = parse_semver(new_version_str)

    if old is None or new is None:
        # Can't classify — be safe and flag as unknown so the caller can decide how to handle it
        return "unknown"

    if new[0] != old[0]:
        return "major"

    if new[1] != old[1]:
        return "minor"

    # Major and minor are the same → patch-level change (or no change at all)
    return "patch"


# ─────────────────────────────────────────────────────────────
# Label management
# ─────────────────────────────────────────────────────────────

def ensure_label(repo, name, color, description=""):
    """
    Ensure a label exists on the repo. Creates it if it doesn't exist yet.

    Why do this?
      Applying a label that doesn't exist yet raises a GitHub API error.
      This helper makes label application idempotent: calling it twice is safe.

    Args:
        repo:        PyGithub Repository object.
        name:        Label name (e.g. "tier:major").
        color:       6-digit hex color string without '#' (e.g. "d73a4a").
        description: Optional short description shown in the GitHub UI.

    Returns:
        The PyGithub Label object (existing or newly created).
    """
    try:
        return repo.get_label(name)
    except GithubException:
        # Label doesn't exist — create it
        return repo.create_label(name=name, color=color, description=description)
