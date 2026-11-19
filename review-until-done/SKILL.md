---
name: review-until-done
description: >
  Iteratively run /review and auto-apply every non-controversial
  finding (Unanimous/Agreed confidence labels, any severity), re-reviewing after
  each round, until the only findings left are Controversial, Rejected,
  Inconclusive, or real-but-rare edge cases. Operates as a goal loop: it adopts a
  fixed completion condition and keeps working across turns until that condition
  is met. Fixes are applied to the working tree only — never committed. Use when
  the user says "/review-until-done" optionally followed by the same
  instructions you would pass to /review (a target path, "the diff",
  a directory, etc.).
---

# Review Until Done (meta-skill)

A thin driver on top of `/review`. It runs a **review → auto-fix → re-review**
loop and stops only when every remaining finding is one the loop is not supposed to
touch. Nothing here re-implements the review itself — each round delegates the actual
detection/validation/synthesis to `/review` and consumes its synthesis report.

Requires the `/review` skill from this repository to be installed (see the README).

## Relationship to `/goal`

This behaves like Claude Code's built-in `/goal`: a fixed completion condition is set once
and Claude keeps working across turns until it is met. A skill *is* model instructions, so
this skill self-drives the loop rather than invoking the `/goal` command (the harness invokes
skills, not built-in slash commands). If the user wants the live `/goal` overlay
(elapsed/turns/tokens), they can type `/goal` themselves with the completion condition in the
[Goal](#the-goal-completion-condition) section below; it is not required for this skill to work.

## Arguments

Everything after `/review-until-done` is forwarded **verbatim** to each
`/review` invocation as its target/instructions — except one optional flag this
skill consumes itself:

- `--max-iterations N` — hard cap on review rounds (default **8**). Strip it from the args
  before forwarding the remainder to `/review`.

If no target is given, `/review` resolves it the same way it always does
(the diff vs. `main`, a directory, etc.) — do not second-guess that here.

## The goal (completion condition)

**Done when** the most recent `/review` synthesis report contains **no finding**
that is *all* of:

1. labeled **Unanimous** or **Agreed**, AND
2. not a rare edge case (see [Definitions](#definitions)), AND
3. not a fix already attempted in a previous round that failed to clear the finding.

Equivalently: keep looping while at least one *actionable* finding exists (Unanimous/Agreed,
not rare, not already-attempted-and-stuck). Stop when the only things left are **Controversial**,
**Rejected**, **Inconclusive**, rare edge cases, or fixes that were tried and didn't stick.

The loop also stops early on any [hard stop](#safety-and-stop-conditions).

## Definitions

- **Non-controversial / actionable** — confidence label **Unanimous** or **Agreed** (validators
  reached consensus with no dissent). These get fixed, at **any severity** (Critical, High,
  Medium).
- **Controversial** — the `Controversial` label (≥1 `agree` and ≥1 `disagree`). Never auto-fixed;
  a human should adjudicate the split.
- **Rejected / Inconclusive** — treated as not-actionable. `Rejected` has nothing to fix (false
  positive); `Inconclusive` is weak signal — leave it for a human.
- **Rare edge case** — a finding that is real (often Unanimous/Agreed) but only triggers under
  conditions the finding text itself characterizes as unlikely/uncommon for this codebase's actual
  usage (e.g. requires malformed input ruled out upstream, or an extreme value never seen in real
  data). Judge from the finding text plus a quick look at the code. **When genuinely uncertain
  whether something is rare, treat it as actionable and fix it** — a consensus finding is a real
  bug, and fixing it is harmless; stopping early would leave a real bug in place. Findings about
  duplicated code, unnecessarily complicated code, dead code, or notes about how the code was
  written (comments, docstrings, or names that refer to the developer's machine, plans, Claude Code,
  past sessions, or review rounds) are never rare edge cases.

## The loop

Track state across rounds in a running tally (kept in your working memory / summary, not a file):
`fixed[]`, `deferred_controversial[]`, `deferred_inconclusive_or_rejected[]`, `deferred_rare[]`,
`stuck[]` (attempted but reappeared). Key each finding by `(file, short-description)` so the same
issue can be recognized across rounds.

For each round (up to `--max-iterations`, default 8):

1. **Review.** Invoke `/review <forwarded-args>` via the Skill tool. Let it run its
   full pipeline through the Step 6 synthesis report. When it reaches the "offer to fix" question at the
   end of its Step 6, **do not wait for the user** — this skill is the triager.

2. **Classify** every finding in the synthesis by its confidence label and the rare-edge-case test.
   Compute the set of **actionable** findings (Unanimous/Agreed, not rare, not already in `stuck[]`).

3. **Check the goal.** If the actionable set is empty → the goal is met; go to
   [Finish](#finishing). Otherwise continue.

4. **Apply fixes.** For each actionable finding, apply its `Fix:` line: read the cited file, make
   the edit with the Edit tool. For duplicated code, replace every cited copy, not just the first.
   To rename a file, use plain `mv` (not `git mv`, which stages the change) and update every
   reference to the old name. Apply all of the round's actionable fixes before re-reviewing.
   Record each in `fixed[]` (with X-id, file:line, one-line description). Record every non-actionable
   finding in the matching `deferred_*` list (dedup across rounds — a finding already deferred in an
   earlier round stays counted once).

5. **Verify (regression guard).** If the project has an obvious test command (e.g. `pytest`,
   `python3 -m unittest`, or a `test`/`lint` script in `package.json`, `Makefile`, etc.), run it.
   **If tests fail, HARD STOP** — do not pile more fixes onto a broken tree; report which round's
   fixes broke it and leave the working tree as-is for the user to inspect. If no test command is
   detectable, skip verification and note that in the summary.

6. **Close out the review run.** The delegated `/review` writes its own
   `XX__CODE_REVIEW_COMPLETED` marker at its Step 7 once triage has happened — since this skill just
   triaged (applied/deferred every finding), let it write that marker. Do not fabricate it yourself.

7. **Loop-guard, then re-review.** Before starting the next round, compare this round's actionable
   findings against the *previous* round's `fixed[]`: any finding that was fixed last round and has
   reappeared this round means the fix didn't take (or regressed). Move it to `stuck[]` and do **not**
   re-attempt the same fix — otherwise the loop can oscillate forever. Then return to step 1.

## Safety and stop conditions

Stop the loop (and go to [Finish](#finishing)) on the **first** of:

- **Goal met** — a round finds no actionable findings.
- **Max iterations reached** — `--max-iterations` rounds completed. Report that the cap was hit and
  that actionable findings may remain.
- **Test failure** — the regression guard (step 5) failed; leave the tree as-is.
- **No forward progress** — a full round applied zero new fixes yet the actionable set is non-empty
  because everything left is in `stuck[]`. Report the stuck findings for manual attention.

Never commit. Never touch git staging/branches. All fixes stay in the working tree for the user to
review and commit. Never delete or overwrite the delegated review's `.code_review/` artifacts.

## Finishing

Print a compact final summary:

- **Why it stopped** — goal met / max iterations / test failure / no progress.
- **Rounds run** — N.
- **Fixed** — count + a bullet per fix (`X-id`, `file:line`, one-line description).
- **Left for a human**, grouped: Controversial, Inconclusive/Rejected, Rare edge cases, Stuck
  (attempted but reappeared) — with the one-line reason for each.
- **Verification** — test command run + pass/fail, or "skipped (no test command found)".
- A reminder that nothing was committed — the user should review the working-tree diff before committing.
