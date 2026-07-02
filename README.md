# claude-code-review-skill

A [Claude Code](https://claude.com/claude-code) skill that runs code review with two independent CLI agents — Claude and [agy](https://antigravity.google/) (Google Antigravity). The two agents indepedently review your code in parallel, then merge the results, and then do a second-pass validation round to see if they agree with eachothers' findings. At the end, you are represented with a list of all findings along with the level of agreement between the two agents (**Agreed**, **Disputed**, etc), and the proposed fixes.

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
