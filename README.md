# claude-code-review-skill

A [Claude Code](https://claude.com/claude-code) skill that runs code review using Claude under three different models: Opus, Sonnet and Haiku. The three reviewers independently review your code in parallel, the results are merged into a deduplicated list, and then a second validation pass asks every reviewer whether each finding is accurate and worth fixing. At the end, you are presented with a list of all findings along with the level of agreement between reviewers (`Unanimous`, `Agreed`, `Controversial`, `Rejected` or `Inconclusive`), and a proposed fix for each one.

## What the reviewers look for

1. Security holes that someone without access to your machine could exploit
2. Logic errors and bugs
3. Duplicated code
4. Unnecessarily complicated code
5. Dead code
6. Comments, docstrings and names that refer to things a reader of the repository cannot see (paths on the developer's machine, plans and design notes, AI assistants, earlier review rounds)

Style nits, missing type hints, and theoretical edge cases that cannot happen in practice are deliberately left out.

For Python files, a small script (`scripts/lint_data_ops.py`) also checks for four common pandas mistakes before the reviewers run: `.merge()` calls without `validate=`, `fillna` / `errors="coerce"` without a comment that justifies it, masks built by comparing against `""`, and `.columns` / `.index` assigned straight from a sample-id column. Its findings go through the same validation pass as everything else. A line can be exempted with a comment such as `# ANALYSIS_OK[join]: keys are validated upstream`.

## Requirements

- [Claude Code](https://claude.com/claude-code) (`claude` CLI) with access to the Opus, Sonnet and Haiku models. It is invoked three times per step, once per model.
- `python3`, only for the pandas checks on Python files. The script uses the standard library only.

## Install

Clone this repo directly into your personal skills directory:

```bash
git clone https://github.com/bw2/claude-code-review-skill.git ~/.claude/skills/review
```

Restart Claude Code (or start a new session) and the `/review` command is available in any project.

To update an existing install:

```bash
git -C ~/.claude/skills/review pull
```

### Optional: `/review-until-done`

The `review-until-done/` folder holds a second skill that runs `/review` in a loop: after each round it applies every fix the reviewers agreed on (`Unanimous` or `Agreed`), runs the project's tests if it can find them, and reviews again. It stops when the only findings left are `Controversial`, `Rejected` or `Inconclusive`, or real but rare edge cases, or after 8 rounds (change this with `--max-iterations N`). It stops right away if the tests fail. Fixes are left in your working tree and are never committed.

Claude Code only finds skills one folder below `~/.claude/skills/`, so link this one into place after cloning:

```bash
ln -s ~/.claude/skills/review/review-until-done ~/.claude/skills/review-until-done
```

Then use it the same way as `/review`:

```
/review-until-done path/to/file.py
```

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

1. **Setup**: creates a scratch `.code_review/` directory with one subdirectory per reviewer (`opus`, `sonnet`, `haiku`). Large targets (more than about 6,000 lines, 40 files or 300 KB) are split into 2 to 5 subsets that are reviewed separately and merged in the final report.
2. **Detect**: each reviewer reads the target from disk independently and reports findings as structured JSON (file, lines, severity, claim, evidence, suggested fix). For Python files, the pandas checks run here too.
3. **Consolidate**: duplicate findings across reviewers are merged; each unique finding gets a stable ID.
4. **Validate**: every reviewer re-reads every consolidated finding and votes `agree` / `disagree` / `unsure`, where `agree` means the finding is both accurate and worth fixing. The prompt hides which model originally flagged the finding and shuffles item order to defeat position bias.
5. **Synthesize**: findings are grouped by severity, each with a confidence label derived purely from the vote split (`Unanimous` / `Agreed` / `Controversial` / `Rejected` / `Inconclusive`), each model's vote, and a concrete fix.

Everything under `.code_review/` is scratch state for a single run — safe to delete between reviews.

## Permissions

The reviewers run with Claude Code's permission checks on; the skill does not use `--dangerously-skip-permissions`. Each reviewer is given access to the repository being reviewed (`--add-dir`) and is allowed four read-only git commands (`git ls-files`, `git diff`, `git log`, `git show`). It can read and search your files, and it is refused when it tries to write a file or run a command that needs approval.

## What changed in November 2026

- The optional third reviewer, `agy` (Google Antigravity), was removed: it was several times slower than the other reviewers and its findings duplicated theirs. Claude Haiku is now the third reviewer, so every run has three votes. The `--no-agy` option is no longer needed and is ignored.
- Reviewers now look for the six categories listed above, and validators judge whether a finding is worth fixing, not only whether it is accurate.
- Added the pandas checks for Python files.
- Reviewers are now given read access to the repository explicitly. In earlier versions, a reviewer could be refused reading the files under review, depending on your Claude Code settings.

## License

MIT
