# claude-code-review-skill

A [Claude Code](https://claude.com/claude-code) skill that runs code review with two independent CLI agents — Claude and [agy](https://antigravity.google/) (Google Antigravity) — reading your local files directly from disk, then cross-checks every finding through a blind peer-validation pass before showing it to you.

## Why

A single review pass from one model has a predictable failure mode: plausible-sounding findings that don't hold up once you check them, and no way to tell which ones to trust. This skill runs two models in parallel, each reading the repo independently with **no shared context bundle**, then has both models re-score every consolidated finding under a neutral prompt that hides who originally flagged it. The result is a report where each finding carries a confidence label — **Agreed**, **Disputed**, **Rejected**, or **Inconclusive** — based purely on how the validators voted, not on which model found it first.

## Features

- **Two independent reviewers.** Claude and agy each read the target files from disk on their own — no risk of one model's framing biasing the other's first pass.
- **Blind peer validation.** Every finding both models produced is re-evaluated by both, using a prompt stripped of reviewer attribution — self-recognition bias is reduced, not eliminated (residual stylistic fingerprints in the finding text can still leak authorship).
- **Confidence labels, not just a list.** Findings are grouped as Agreed / Disputed / Rejected / Inconclusive so you can triage by trust level instead of reading everything.
- **Reachability-gated findings.** Reviewers are required to trace a concrete caller/test/config that triggers each issue — speculative "this would break if X were None" claims with no real trigger are dropped before you ever see them.
- **Automatic subset splitting.** Large projects are split into cohesive, independently-reviewed subsets so review quality doesn't degrade on big diffs.
- **Graceful degradation.** If agy is rate-limited or fails, Claude stands in for the missing slot so the review still runs — and the report says so.
- **Every finding ships with a fix.** No dead-end reports — each surviving finding includes a concrete one-line suggested fix.

## Requirements

- [Claude Code](https://claude.com/claude-code) (`claude` CLI).
- [`agy`](https://antigravity.google/) (Google Antigravity CLI), installed and authenticated.

## Install

Clone this repo directly into your personal skills directory:

```bash
git clone https://github.com/bw2/claude-code-review-skill.git ~/.claude/skills/review
```

Restart Claude Code (or start a new session) and the `/review` command is available in any project.

## Usage

```
/review
```

With no argument, reviews `git diff main...HEAD` if there is one, otherwise the whole current directory. You can also target something specific:

```
/review path/to/file.py path/to/other.py
/review --diff main...HEAD
/review focus on error handling in the auth module
```

## How it works

1. **Setup** — creates a scratch `.code_review/` directory with one subdirectory per agent.
2. **Detect** — Claude and agy each read the target from disk independently and report findings as structured JSON (file, lines, severity, claim, evidence, suggested fix).
3. **Consolidate** — duplicate findings across the two agents are merged; each unique finding gets a stable ID.
4. **Validate** — both agents re-read every consolidated finding and vote `agree` / `disagree` / `unsure`, under a prompt that hides which model originally flagged it and shuffles item order to defeat position bias.
5. **Synthesize** — findings are grouped by severity, each with a confidence label derived purely from the vote split, and a concrete fix.

Everything under `.code_review/` is scratch state for a single run — safe to delete between reviews.

## License

MIT
