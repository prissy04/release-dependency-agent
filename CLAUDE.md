# CLAUDE.md

Context for Claude Code working in this repository. Read this before doing anything. The full spec is in `PRD.md` — read it too.

## What this project is

`release-dependency-agent` — a set of GitHub Actions workflows that reduce dependency-related disruption during release cycles. Three independent layers: (1) a dependency freeze-window / early-warning system, (2) a breaking-change shield with tiered approval based on semver, (3) label-driven backport automation across release branches. See `PRD.md` for the full design.

This is an **open-source portfolio project**. It is intentionally generic. It must contain **no employer-specific information, no proprietary infrastructure, and no internal process details** — ever, in any file, comment, commit message, or example. The pain points it addresses are universal to large-scale development.

## Who you're working with

I'm a Technical Program Manager. My Python is **automation-and-scripting level**, not software-engineering level. That means:
- Explain what code does and *why* in plain language, not just what to type.
- Don't assume I'll catch subtle bugs on my own — point out the parts I should understand to defend this project in an interview.
- I need to be able to explain every design decision three levels deep, because this is portfolio work I'll talk about with hiring managers. If you add something clever, tell me how it works.

## How I want you to work

- **Go step by step and pause at checkpoints.** Don't build all three layers in one shot. Follow the build phases in `PRD.md` §10. After each phase, stop and let me review before moving on.
- **Give me the "why," not just the "what."** When you recommend an approach, include the reasoning and the trade-offs.
- **Be direct about risk.** If something is fragile, expensive, or likely to break, say so plainly. Don't optimistically gloss over problems. I'd rather hear the caveat now.
- **Write like a person, not a bot.** Any user-facing text the tool generates (PR comments, the weekly digest, README prose) should sound natural and conversational — no corporate jargon, no "I'm excited to leverage synergies" filler.
- **Confirm before destructive actions** (force-push, branch deletion, rewriting history, deleting files).

## My environment

- **OS:** Windows. I navigate with File Explorer's address bar.
- **Terminal:** Command Prompt is my primary terminal. VS Code's integrated terminal has caused me problems, so give me Command Prompt commands when I need to run something locally.
- **Known gotcha:** downloaded files sometimes get double extensions (e.g. `README.md.md`). If that happens, the reliable fix is `ren` or `copy` in Command Prompt with an explicit destination filename.
- **Tools installed:** Python, Node.js, Git, VS Code.
- **GitHub:** username `prissy04`. Repos are public unless I say otherwise.

## Tech stack and conventions

- **Python 3.11.** Standard library first; `PyGithub` for GitHub API; `PyYAML` for config.
- **GitHub Actions** for all automation. Prefer free-tier-friendly patterns.
- **Config-driven:** everything tunable lives in `config.yml`. No hardcoded repo names, dates, thresholds, or labels in the Python — read them from config with sensible defaults.
- **Secrets** (`GITHUB_TOKEN`, `ANTHROPIC_API_KEY`) come from GitHub Secrets / env vars. **Never** hardcode a token, never print a secret, never commit one. If you see one, stop and tell me.
- **Comments:** comment the non-obvious parts so a reviewer (and future me) can follow the logic.
- **Small, single-purpose functions** with clear names over clever one-liners.
- **Each Python file is runnable and testable on its own** where reasonable.

## Cost constraints (important)

- I have a **hard $20/month cap** on the Anthropic API, with an alert at $16. Treat API spend as scarce.
- Use the **Haiku** model (`claude-haiku-4-5-20251001`) for any AI calls.
- AI is used in **at most two optional places** (Layer 1 weekly digest, Layer 2 changelog summary), both **off by default** behind config flags.
- **Never** call the API inside an unbounded loop. Respect `ai.max_calls_per_run` in config as a hard ceiling.
- When you wire up an AI call, tell me roughly how many tokens/calls a typical run will make so I can sanity-check the cost.

## What to avoid

- No proprietary or employer-specific references anywhere.
- No auto-merging dependency PRs and no auto-resolving merge conflicts — a human decides on anything risky (see PRD non-goals).
- Don't introduce heavy frameworks or dependencies I'd struggle to explain. Keep the footprint small.
- Don't over-engineer. v1 should be the simplest thing that works for the three pain points.
- Don't put GitHub links, internal docs, or this CLAUDE.md content into anything employer-facing — this repo and any internal proposal stay strictly separate.
