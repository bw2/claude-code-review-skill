---
name: review
description: >
  Performs code review by launching Claude three times under three different
  models (Opus, Sonnet and Haiku), all in parallel. Then, it merges their
  findings into a deduplicated list and launches a validation pass, asking
  each reviewer to agree or disagree with every finding under a neutral
  prompt that hides authorship. Then, it gives you a final list of findings
  with confidence labels and proposed fixes so that you can choose which ones
  to fix. Python targets also get a deterministic AST pre-pass for pandas
  missing-data/join anti-patterns (unchecked merges, unannotated fillna,
  empty-string sentinel masks, positional sample-id alignment), fed into the
  same pipeline.
---

# Review Skill

Three independent reviewers (Claude running as Opus, as Sonnet, and as Haiku) detect issues in parallel by reading the local files directly — there is **no shared context bundle**. Every consolidated finding is then scored by all three validators (each running locally with disk access) using a deliberately neutral prompt — no reviewer attribution, no meta-framing about peer review. The prompt removes explicit authorship signals, though residual stylistic fingerprints in the verbatim `claim` text may still leak self-recognition. Findings are presented with confidence labels derived purely from validator votes (Unanimous / Agreed / Controversial / Rejected / Inconclusive).

All three reviewers (opus, sonnet, haiku) can read disk, so there is no shared bundle of file contents: each agent runs in its own working directory and reads the target files from the repo on its own.

The third slot used to be Google's `agy` CLI, as an optional reviewer. It was dropped in Nov 2026: on a 600-line review agy took ~17-23 minutes per step at high and at medium effort (vs ~2-6 for the other reviewers in that run), and all its findings duplicated theirs. Claude Haiku now fills that slot, so the skill needs only the `claude` CLI.

## Requirements

- `claude` CLI (this skill runs inside it, so it's already present), with access to the Opus, Sonnet and Haiku models. It is invoked three times per step, once each with `--model opus`, `--model sonnet` and `--model haiku`.
- `python3`, only for the pandas lint pre-pass on Python targets (Step 2). The lint script uses the standard library only.

## Workflow

> **Working-directory hygiene.** Every shell command in this skill assumes the project root as cwd. Because the harness's bash cwd can drift across tool calls (a prior `cd subdir` from earlier in the session can persist), each command below anchors itself with `cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" && …` (or its parametrized form). Do not strip those anchors — they're what make the skill robust to a drifted cwd. The expression is `git rev-parse --show-toplevel` for git repos (finds the project root regardless of harness cwd), falling back to `pwd` for non-git projects. **Caveat for non-git projects:** the `pwd` fallback only works if the harness cwd happens to be the project root — if it has drifted, you must `cd` back to the project root yourself first, or replace the whole `$(…)` expression with a hardcoded absolute project-root path inside each command before running it.

### Step 1: Setup

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" && mkdir -p .code_review/opus .code_review/sonnet .code_review/haiku
```

Generate a unique run id `XX` as the current timestamp in the format `YYYYMMDD_HHMMSS`:

```bash
XX=$(date +%Y%m%d_%H%M%S)
```

Use this same `XX` value as the prefix for every file the skill writes under `.code_review/` during this run (prompts, the completion marker, etc.) — capture it once and reuse it so all artifacts from a single run share a stable prefix.

### Step 1b: Decide whether to split into independent subsets

Large projects can exceed what the detection models can usefully attend to in a single review, even when each agent reads the files from disk itself — review quality degrades long before the context-window hard limit, and a single agent juggling 50 files reasons more shallowly than three agents handling ~15 each. When the target is large, the review is split into multiple subsets that are reviewed in parallel as independent pipelines, then merged at synthesis (Step 6).

**Resolve the file list and measure the target.** First resolve the user's argument the same way Step 2 will (explicit paths, `git diff main...HEAD`, or all files under a directory via `git ls-files`). Then compute:
- Total source lines: `wc -l <files>` (sum the `total` row).
- Total file count.
- Approximate total byte size: `wc -c <files>` summed, or `du -bc <files> | tail -1`.

**Threshold for splitting (moderate):**
- ≤ ~6,000 source lines **AND** ≤ ~40 files **AND** ≤ ~300 KB → **single subset called `all`**, proceed to Step 2 unchanged.
- Otherwise → **plan a split** before launching reviewers.

**Plan the split.** The goal is 2–5 subsets where each subset (a) fits comfortably under the threshold above and (b) is internally cohesive and externally as independent as possible from the other subsets. Apply these heuristics in order of preference:

1. **By top-level subdirectory** (`src/`, `scripts/`, `tests/`, language-specific dirs, etc.) — usually the cleanest natural boundary.
2. **By language / runtime** (e.g. Python build scripts vs. browser JS/HTML/CSS) — often correlates with module boundaries.
3. **By named feature module** when a feature has its own subdir and few cross-imports.
4. **By coarse import/require analysis** — quickly grep for cross-file references; group strongly-connected files together.

Sanity-check the split: each subset should be reviewable without constantly cross-referencing other subsets. If two candidate subsets are deeply interdependent (one imports many symbols from the other, shared types, etc.), merge them. When a cross-subset reference is unavoidable, you may include the referenced file in BOTH subsets if it is small (under a few hundred lines) and the dependency is non-trivial; otherwise note the dependency in a one-line `> Out-of-subset dependency: <file>` header inside the dependent subset's prompt so reviewers know what's intentionally out of scope.

**Oversize fallback.** If a single file or directory is itself larger than the threshold and cannot be cleanly split along module boundaries, review it as its own subset anyway. Append a one-line `> Note: this subset exceeds the recommended review size; reviewer attention may be degraded.` header to that subset's prompt so the user sees the caveat in the synthesis output. Do NOT mechanically chunk a file mid-way.

**Output of this step:** a list of subsets, each with:
- A short kebab-case name (e.g. `web-ui`, `build-scripts`, `audio-pipeline`).
- The file list.
- A one-sentence rationale for the boundary.

Print the planned splits to the user as a brief preview before launching reviewers (subset name → file count, short rationale) so they can interrupt if the boundaries look wrong.

**Threading subsets through Steps 2–5.** When more than one subset is planned, run Steps 2–5 **once per subset**, with these naming conventions:
- The Step 2 `<label>` becomes `<base-label>__<subset-name>` (e.g. `swedish_flashcards__web-ui`). All existing `XX_PROMPT__<label>__…` and `XX_PROMPT__<label>_…_validate.md` paths in Steps 2–5 keep their exact shape; only the `<label>` value carries the subset.
- The reviewer/validator subdirectories (`.code_review/opus`, `.code_review/sonnet`, `.code_review/haiku`) are shared across subsets — each subset's CLI invocations use a distinct prompt-file path, so parallel runs from the same subdirectory don't collide as long as we rely on captured stdout (we do; no CLI writes files we depend on later).

**Concurrency cap.** Launching every (subset × reviewer) at once can trip rate limits and overload the machine. **Cap at ~6 concurrent CLI invocations.** Batch the work: e.g. with 3 subsets × 3 detection agents = 9 runs, launch 6 first and queue the remaining 3 to start as earlier ones finish. The same cap applies to the Step 5 validation pass.

**Waiting for background runs.** The simplest wait is no wait: background tasks launched with `run_in_background: true` auto-notify on completion and re-invoke you, so you can just let a batch finish and read its output files when the notifications arrive — do NOT schedule short-interval poll wakeups for harness-tracked tasks. If you need an explicit blocking wait (e.g. to gate the next batch), either chain sequentially after the first batch returns, or issue a single foreground `Bash` call containing an `until <cond>; do sleep N; done` loop that blocks until the tasks' output files are complete (e.g. each contains its closing ```` ``` ```` fence). **Do NOT call the `Monitor` tool with a list/array of task IDs — it has no `tasks` parameter and the call will fail input validation (`An unexpected parameter 'tasks' was provided`).** `Monitor` is a deferred tool, so its schema is not loaded by default: you must first call `ToolSearch` with query `select:Monitor`, and then invoke it with its actual shape `{description, timeout_ms, persistent, command}` — where `command` is a shell command whose stdout lines are treated as events and whose exit ends the watch. To wait on these background CLI runs with `Monitor` you would pass a polling `command` (the same `until`-loop), never task IDs; in practice the auto-notification or the plain `until`-loop `Bash` call is simpler and preferred.

If only a single subset is planned (project is below threshold), the rest of the skill behaves exactly as it did before splitting was introduced.

### Step 2: Resolve the target and write the detection prompt

> **Per subset.** When Step 1b planned multiple subsets, perform this step once per subset using that subset's file list as the resolved `{TARGET}`. Express `{TARGET}` as an explicit file list (e.g. "the files `path/to/foo.py` and `path/to/bar.py` (paths are relative to the repo root)") so the agent reviews only the subset's files. Use `<label>` = `<base-label>__<subset-name>` in the prompt-file paths. Write all subsets' prompts before moving on to Step 3.

**Data-ops AST pre-pass (Python targets only).** Before writing the detection prompts, if the subset's resolved file list includes any `.py` files, run the deterministic pandas missing-data/join lint directly — no agent spawn, this is a plain script call by the orchestrator:
```bash
python3 ~/.claude/skills/review/scripts/lint_data_ops.py --json <the .py files in this subset>
```
That is the install path from the README. If the skill is installed somewhere else, use the `scripts/lint_data_ops.py` that sits next to this `SKILL.md`.

A `.py` file with none of the 4 syntactic patterns below just yields zero findings at near-zero parse cost, so there's no gating on "does this look like data analysis" — always run it when `.py` files are in scope. Map each JSON finding (`{rule, file, line, snippet}`) into the standard finding schema (`lines = str(line)`) using this fixed per-rule template (the `snippet` field is reused verbatim as `evidence` for every rule):

| rule | severity | title | claim | suggested_fix |
| :--- | :--- | :--- | :--- | :--- |
| `unchecked-merge` | high | Unchecked merge cardinality | "`.merge()` call at `<file>:<line>` has no `validate=` argument — silent row drop/duplication possible on key mismatch" | "Add `validate=` with the expected cardinality (`one_to_one`/`one_to_many`/`many_to_one`/`many_to_many`), or a `# ANALYSIS_OK[join]: <reason>` waiver if intentional." |
| `unannotated-missingness` | medium | Unannotated missing-value handling | "`<file>:<line>` fills/coerces missing values without an `# ANALYSIS_OK[imputation]: <reason>` waiver justifying the choice" | "Add a `# ANALYSIS_OK[imputation]: <reason>` waiver justifying the fill method." |
| `sentinel-mask-assignment` | high | Empty-string used as missingness sentinel | "`<file>:<line>` builds a boolean mask by comparing against `\"\"` instead of using `.notna()`/`.isna()`" | "Replace with `.notna()`/`.isna()`, or a `# ANALYSIS_OK[missingness]: <reason>` waiver if `\"\"` is a genuine non-null value here." |
| `positional-sample-alignment` | high | Columns/index assigned from a sample-id-like key without an explicit join | "`<file>:<line>` assigns `.columns`/`.index` directly from a sample-id-like key, assuming row order already matches — no key-based alignment" | "Align explicitly via `.set_index()`/`.reindex()`/a merge on the key, or a `# ANALYSIS_OK[alignment]: <reason>` waiver if order is guaranteed elsewhere." |

Assign these findings X-ids in the same sequence as the detection agents' findings at Step 4 below, and carry a provenance tag (e.g. `AST rule: unchecked-merge`) through Step 5 (as the `[AST rule: <name>]` prefix on the rendered `finding` text) and Step 6 (as the `[AST rule: <name>]` synthesis label) so they can be identified at each stage. This sub-step is additive-only — it changes nothing for targets with no `.py` files.

Parse the user's argument to `/review`:
- If it names files/directories or a git ref (e.g. `--diff main...HEAD`), use that.
- Otherwise default to `git diff main...HEAD` if there's a non-empty diff, else "the code in the current directory".

If the argument contains `--no-agy` (an option from earlier versions of this skill, when agy was an optional reviewer), strip it and ignore it, so it isn't mistaken for a path.

Resolve `{TARGET}` to a concrete description the agent can act on, e.g.:
- "the files `path/to/foo.py` and `path/to/bar.py` (paths are relative to the repo root)"
- "the changes in `git diff main...HEAD` (run the diff yourself to see them, then read the changed files in full)"
- "all source files under `./analysis/` (use `git ls-files analysis/` to enumerate, then read each)"

Write three per-agent detection prompts — `.code_review/XX_PROMPT__<label>__opus.md`, `.code_review/XX_PROMPT__<label>__sonnet.md`, and `.code_review/XX_PROMPT__<label>__haiku.md` — using the template below with `<agent>` substituted to the agent's actual subdirectory name (`opus`, `sonnet`, or `haiku`) in each file. The three prompts are identical apart from the substituted subdirectory name. (A single shared file would leave the literal `<agent>` placeholder in the prompt, which is confusing to the model even if path resolution via `../..` still works.)

````
Do a thorough code review of {TARGET}.

**Read the files from disk yourself.** You are running from a subdirectory two levels below the repo root; the repo root is `../..` (your cwd is `.code_review/<agent>/` under the repo root). Use `../..` to construct all file paths. Start by listing what's in scope (e.g. `git -C ../.. ls-files` or the explicit paths above, prefixed with `../../`), then read each file in full before forming opinions.

**What to look for.** Focus on these categories:

1. **Security holes.** Treat the machine the code runs on, its files and environment, and everyone who can log into it as trusted. Report only holes that a party without that access can exploit, e.g. other hosts on the network reaching a server that listens on a non-localhost address, a malicious web page in the user's browser sending requests to a server on localhost, content fetched from a remote URL that gets executed or rendered unescaped, downloads over plain HTTP or without an integrity check, secrets committed to the repository. Do not report holes that need a local user, local process, or local file to be malicious (e.g. injection through command-line arguments, config files, or input data files on disk).
2. **Logic errors and bugs**: wrong output, data loss, crashes, conditions that test the wrong thing, off-by-one errors, mishandled missing values, etc.
3. **Duplicated code**: the same non-trivial logic written out in two or more places, where one shared function or loop would replace the copies. Cite every copy.
4. **Unnecessarily complicated code**: code that a clearly simpler version with the same behavior would replace (e.g. an abstraction with a single user, a hand-written version of a standard-library function, convoluted control flow). Report it only when you can describe the simpler version concretely in `suggested_fix`; differences of taste do not count.
5. **Dead code**: functions, classes, branches, parameters, variables, imports, CSS rules, or template blocks that nothing uses. Search the whole repository for users (including tests, templates, and lookups by string name) before reporting.
6. **Notes about how the code was written**: comments, docstrings, and names (file, test, function, or variable names) that refer to things a reader of the published repository cannot see or follow: the developer's machine (absolute paths such as `/Users/...`, home-directory paths, hostnames), plans, blueprints, specs, or design notes used while writing the code, Claude Code or other AI assistants, past sessions, or code-review rounds and finding ids (e.g. "fixed in review round 3", "X12", "issues found in the 2026-06-22 code review"). A reference to a document that is part of the repository (e.g. `README.md`, `docs/...`) is fine. In `suggested_fix`: for a comment that is nothing but such a reference, delete it; for a comment that also explains the code, delete only the reference and keep the explanation; for a name, give a new name that says what the thing does or tests.

Also report other real problems you find (e.g. documentation that contradicts the implementation in a misleading way, or a severe performance problem), but the categories above come first.

For each candidate issue:

1. Pinpoint the exact file and line(s).
2. Re-open the file and confirm the cited lines actually say what you claim.
3. For security holes and bugs, confirm the failure mode is reachable: trace at least one caller / test / config that exercises this code, and confirm the bad input can occur in practice. If you cannot, do not report the finding. Speculative "this would crash if X were None" concerns when every caller statically prevents X from being None are the dominant false-positive class — drop them. **Do not invent hypothetical inputs to argue reachability** — phrases like "if the test used X" or "if a caller passed Y" mean you have not found a real trigger. Cite a concrete input an existing caller / test / config actually produces, or drop the finding. A hypothetical that would-trigger-the-bug-if-it-existed is not evidence of reachability; it is a confession that you couldn't find one.
4. For the other categories, `evidence` is the proof instead: every copy's file:line for duplicated code, the search you ran that found no users for dead code, why the simpler version behaves the same for complicated code, and the quoted text for notes about how the code was written.

**Severity bar — report only:**
- `critical` — wrong output, data loss, a security hole (as defined above), or crash on a realistic input.
- `high` — likely bug or logic error under normal use, OR significant maintainability hazard.
- `medium` — verifiable quality issue: dead code, duplicated code, unnecessarily complicated code, notes about how the code was written, or documentation that contradicts the implementation in a misleading way.

Do NOT report `low` / style / nits.

**Specifically DO NOT flag:**
- Theoretical edge cases that are extremely unlikely to occur in practice: a malformed input no upstream step produces, a race that needs an interleaving the code cannot reach, an overflow at a scale the data never approaches. Judge how likely the trigger really is, not just whether it is logically possible.
- Missing type hints, formatting.
- Missing defensive checks for inputs that cannot occur given the call sites.
- "Consider extracting a helper" / suggested abstractions where existing code is clear and used in one place.
- Pre-existing issues outside the target.
- Anything a linter would catch, except dead code (unused imports, variables, functions), which is in scope.

**Output format — STRICT.** Before the JSON, you may include a brief `<scratch>…</scratch>` block listing the files you inspected and any candidates you ruled out — use this to free up exploration, not as part of the answer. The final answer must be a single JSON object inside one ```json fenced code block, emitted as the LAST ```json block in your response. **If multiple ```json blocks appear in your response (e.g. you echoed the example schema), only the LAST block is parsed.** Anything outside the parsed block (including the scratch) is discarded.

```json
{
  "summary": {"critical": 0, "high": 2, "medium": 1},
  "findings": [
    {
      "id": "F1",
      "file": "path/to/file.py",
      "lines": "42-45",
      "severity": "high",
      "title": "Short one-line title (≤ 100 chars)",
      "claim": "Self-contained 1-2 sentence statement of what is wrong, grounded in the code. Another reader who has not seen your other findings must be able to judge it from this field alone. Cite line content where useful.",
      "evidence": "Concrete reachability: which caller/test/config triggers it, and the input that produces the bad outcome. Cite file:line.",
      "suggested_fix": "Brief concrete fix, or empty string if not obvious"
    }
  ]
}
```

Field rules:
- `id`: `F1`, `F2`, … in listed order.
- `lines`: `42`, `42-45`, or `42,50,73`.
- `severity`: exactly one of `critical`, `high`, `medium`.
- `claim` and `evidence` will be shown verbatim to peer validators — keep them self-contained and grounded.
- If you find no issues, return `{"summary": {"critical": 0, "high": 0, "medium": 0}, "findings": []}`.
````

If the user passed extra instructions beyond a target, append them after the template (do not modify the output schema). **Do not** append a project file tree or file contents — each agent reads the disk itself.

### Step 3: Launch the detection agents in parallel

All detection runs use `run_in_background: true`. Each agent runs in its own subdirectory (`.code_review/opus`, `.code_review/sonnet`, `.code_review/haiku`) two levels below the repo root, so it reads the repo from `../..`. **When multiple subsets exist, launch each subset's three agents the same way, but obey the Step 1b concurrency cap (~6 concurrent CLI invocations across all subsets × agents) — batch the remainder and start queued runs as earlier ones finish.**

**Reviewer permissions.** Every `claude -p` call in this skill runs with Claude Code's permission checks on. `--add-dir ../..` gives the reviewer access to the repo root: its own cwd is `.code_review/<agent>/`, and without this option it is refused even reading the files under review. `--allowedTools` allows the four read-only git commands in the `git -C ../..` form that the prompts suggest. With both, a reviewer can read and search the repo and run `git ls-files`, `git diff`, `git log` and `git show`, and it is refused when it tries to write a file or run a command that needs approval (for example `git commit`). Do not replace these options with `--dangerously-skip-permissions`: the code under review is untrusted input to the reviewer.

**Opus (`--model opus`, an alias that always points at the newest Opus):**
```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.code_review/opus" && cat ../XX_PROMPT__<label>__opus.md | claude -p --model opus --add-dir ../.. --allowedTools "Bash(git -C ../.. ls-files *)" "Bash(git -C ../.. diff *)" "Bash(git -C ../.. log *)" "Bash(git -C ../.. show *)" --output-format text
```

**Sonnet (`--model sonnet`, an alias that always points at the newest Sonnet):**
```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.code_review/sonnet" && cat ../XX_PROMPT__<label>__sonnet.md | claude -p --model sonnet --add-dir ../.. --allowedTools "Bash(git -C ../.. ls-files *)" "Bash(git -C ../.. diff *)" "Bash(git -C ../.. log *)" "Bash(git -C ../.. show *)" --output-format text
```

**Haiku (`--model haiku`, an alias that always points at the newest Haiku):**
```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.code_review/haiku" && cat ../XX_PROMPT__<label>__haiku.md | claude -p --model haiku --add-dir ../.. --allowedTools "Bash(git -C ../.. ls-files *)" "Bash(git -C ../.. diff *)" "Bash(git -C ../.. log *)" "Bash(git -C ../.. show *)" --output-format text
```

If a run fails (non-zero exit or empty output), proceed without it and say so near the top of the Step 6 report. There is no stand-in for a failed slot, and its output is never made up.

### Step 4: Consolidate findings

> **Per subset.** When Step 1b planned multiple subsets, perform consolidation independently per subset (each subset has its own set of detection agent JSON outputs). Use subset-local finding ids like `<subset-name>:X1`, `<subset-name>:X2`, … so the subset is obvious downstream. Cross-subset deduplication is unnecessary because subsets contain disjoint files; if a finding happens to span subsets (e.g. an out-of-subset dependency you exposed via the `> Out-of-subset dependency:` header), assign it to whichever subset's reviewers raised it and leave the other subset alone.

After all three detection runs return, parse each agent's JSON. **If the response contains multiple ```json fenced blocks (e.g. the model echoed the example schema), use the LAST block as the answer** — the templates instruct the model to emit its answer last, and any earlier block is an echo. If an agent returned prose, salvage what you can and flag the run as less reliable. If an agent failed, proceed with the survivors.

Deduplicate across agents (same file, overlapping lines, same root cause → one merged finding). Be conservative — only merge when clearly the same issue. When merging, take the higher severity.

For each merged finding, assign a stable cross-val id `X1`, `X2`, … and record `file`, `lines`, `severity`, `title`, `claim`, `evidence`, `suggested_fix` (most self-contained variant when multiple agents flagged it). **`suggested_fix` must end up non-empty for every finding** — if every agent that flagged it left `suggested_fix` empty, write a concrete one-line fix yourself from the `claim`/`evidence` before moving on. Step 6 always prints a planned fix per finding, so there must be one to print.

If the Step 2 data-ops AST pre-pass produced any findings for this subset, fold them into this same consolidated list now, using the fixed rule→severity/title/claim/evidence/suggested_fix mapping from Step 2 (no further dedup needed against LLM findings — the AST rule names are specific enough that overlap is unlikely, but merge normally if a detection agent independently flagged the exact same line).

The set of unique findings — regardless of who flagged each one — is the input to Step 5. **Every validator validates every finding** (including findings it originally flagged); the prompt removes explicit authorship signals (no reviewer name, no ordering hint, no "you said" framing). Note that the verbatim `claim` text retains its original stylistic fingerprint, so this is a soft (not absolute) protection against self-recognition bias.

For AST pre-pass findings specifically, Step 5 validators are judging something different from an LLM-detected finding: the syntactic pattern match itself is already exact (that's not what's in question), what's genuinely uncertain is whether the matched code is real-world pandas usage — e.g. an unrelated `.merge()` method on a non-DataFrame object would be a false positive the AST rule can't rule out on its own. See the added instruction line in the Step 5 template below.

### Step 5: Peer validation pass

> **Per subset.** When Step 1b planned multiple subsets, run a separate validation pass per subset, scoring only that subset's consolidated findings. Validator prompts use `<label>` = `<base-label>__<subset-name>`. Across all subsets the same ~6-concurrent-CLI cap from Step 1b applies; batch validators if running many subsets at once. Findings from different subsets are never mixed in a single validator prompt — that would force a validator to chase paths outside the subset it was scoped to.

Every validator (Opus, Sonnet, Haiku) runs against **every** consolidated finding — including findings it originally flagged. The prompt is deliberately neutral and hides any framing about who proposed the issue. **All validators read from disk — no file contents are inlined**, since all three roster slots have disk access.

Write `.code_review/XX_PROMPT__<label>_<agent>_validate.md`:

````
For each item below: read the cited source code and decide whether the finding should be acted on. Answer two questions, and answer `agree` only when both hold:

1. **Accurate.** Does the code at the cited lines actually do what the finding says? Check it yourself in the files on disk; do not take the item's text on trust.
2. **Worth fixing.** Under the rules below (the categories, the severity bar and the "not worth fixing" list), is it a problem worth changing the code for? A finding that describes the code correctly but falls under that list, or below the severity bar, gets `disagree`.

Evaluate every finding as if safety depended on it. You will be punished or held legally liable if you agree with a finding that turns out to be wrong or blatantly not worth fixing, and equally if you disagree with a finding that turns out to be correct and important to fix. Neither `agree` nor `disagree` is a safe default; only a checked answer is.

Provide a reason grounded in the actual code. A `disagree` must say which question failed: not accurate (what the code does instead), or not worth fixing (which rule applies).

Your cwd is `.code_review/<agent>/`. The source lives at `../..`. Read each cited file from disk before answering, prefixing paths with `../../`. Read freely within the target project directory and its subdirectories. Consult files outside the project only reluctantly, when a finding genuinely cannot be settled from inside the project.

Output: a single JSON array in one ```json fenced code block, in the same order as the items below. No preamble. If multiple ```json blocks appear in your response (e.g. you echoed the example), only the LAST block is parsed. Each entry: `{"issue_id": "...", "verdict": "agree" | "disagree" | "unsure", "reason": "<cite specific code>"}`. Use `unsure` only when the cited code does not let you reach a confident answer.

Judge security findings under this trust model: the machine the code runs on, its files and environment, and everyone who can log into it are trusted. A finding that needs a local user, local process, or local file to be malicious is not a real security hole; disagree with it.

For items whose `finding` text is prefixed with `[AST rule: <name>]` (`unchecked-merge`, `unannotated-missingness`, `sentinel-mask-assignment`, `positional-sample-alignment`), judge whether the code is genuinely operating on a pandas DataFrame/Series in context — do not re-derive whether the syntactic pattern matched, that part is already exact.

**What counts as a problem worth fixing.** The categories: (1) security holes, under the trust model above; (2) logic errors and bugs: wrong output, data loss, crashes, conditions that test the wrong thing, off-by-one errors, mishandled missing values; (3) duplicated code: the same non-trivial logic written out in two or more places, where one shared function or loop would replace the copies; (4) unnecessarily complicated code, only when a clearly simpler version with the same behavior can be described concretely (differences of taste do not count); (5) dead code that nothing in the repository uses; (6) notes about how the code was written: comments, docstrings and names that refer to things a reader of the published repository cannot see or follow (the developer's machine, plans or design notes used while writing, AI assistants, past sessions, code-review rounds or finding ids; a reference to a document that is part of the repository is fine); and other real problems, such as documentation that contradicts the implementation in a misleading way, or a severe performance problem.

**Severity bar.** `critical`: wrong output, data loss, a security hole, or a crash on a realistic input. `high`: a likely bug or logic error under normal use, or a significant maintainability hazard. `medium`: a verifiable quality issue of categories 3 to 6, or misleading documentation. Anything below that (style, nits, `low`) is not worth fixing.

**Not worth fixing; answer `disagree`:**
- Theoretical edge cases that are extremely unlikely to occur in practice: a malformed input no upstream step produces, a race that needs an interleaving the code cannot reach, an overflow at a scale the data never approaches. Judge how likely the trigger really is, not just whether it is logically possible.
- Missing type hints, formatting.
- Missing defensive checks for inputs that cannot occur given the call sites.
- "Consider extracting a helper" and suggested abstractions where the existing code is clear and used in one place.
- Pre-existing issues outside the files the items cite.
- Anything a linter would catch, except dead code (unused imports, variables, functions), which is in scope.

```json
[
  {"issue_id": "X1", "verdict": "agree",    "reason": "Accurate: foo.py:42 calls bar() before bar is defined; bar is only set inside the `if cfg:` branch on line 47. Falsy cfg → NameError. Worth fixing: a crash, and main.py:12 passes cfg=None."},
  {"issue_id": "X2", "verdict": "disagree", "reason": "Not accurate: line 98 has `if x is None: return`, so x is provably non-None at line 100."},
  {"issue_id": "X3", "verdict": "unsure",   "reason": "The cited code does not show enough of the surrounding control flow to decide."},
  {"issue_id": "X4", "verdict": "disagree", "reason": "Accurate: lines 12-14 repeat the check at 30-32. Not worth fixing: two clear lines used in one place each; the 'consider extracting a helper' rule."}
]
```

Items:

<for each consolidated finding, render in shuffled order, using ONLY the fields below — no reviewer names, no ordering hints, no framing about where the finding came from. For AST pre-pass findings, prefix the finding field with `[AST rule: <name>]` so the instruction above can identify them>
### X<N>
- file: <file>
- lines: <lines>
- finding: <claim> (AST pre-pass findings: `[AST rule: <name>] <claim>`)
- reachability: <evidence>
````

Per-agent prompt rules:
- **Shuffle the order of items independently for each validating agent.** This defeats position bias.
- Every validator gets the same full list of consolidated findings (including ones it originally flagged at detection).
- **No reviewer attribution and no meta-framing.** Do not name any original finder; do not say "you wrote this" or "you flagged this"; do not reveal that this prompt follows an earlier *detection* pass or that other agents produced these claims. The agree/disagree question itself IS the task — `do you agree with the finding?` is fine and intentional; what's forbidden is exposing the multi-agent / peer-review workflow context.
- Include `file`, `lines`, `finding` (the `claim` text), and `reachability` (the `evidence` text) per item — the detection prompt's field rules promise validators will see both. Severity and any other Step-4 metadata are still omitted to avoid anchoring the verdict, except the `[AST rule: <name>]` prefix on AST pre-pass findings' `finding` text, which is needed to trigger the instruction above.
- Do NOT inline file contents — every validator reads from disk.

**Launch the three validators in parallel.** Each uses the same model as in its own detection run: opus stays opus, sonnet stays sonnet, haiku stays haiku.

**Opus validator:**
```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.code_review/opus" && cat ../XX_PROMPT__<label>_opus_validate.md | claude -p --model opus --add-dir ../.. --allowedTools "Bash(git -C ../.. ls-files *)" "Bash(git -C ../.. diff *)" "Bash(git -C ../.. log *)" "Bash(git -C ../.. show *)" --output-format text
```

**Sonnet validator:**
```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.code_review/sonnet" && cat ../XX_PROMPT__<label>_sonnet_validate.md | claude -p --model sonnet --add-dir ../.. --allowedTools "Bash(git -C ../.. ls-files *)" "Bash(git -C ../.. diff *)" "Bash(git -C ../.. log *)" "Bash(git -C ../.. show *)" --output-format text
```

**Haiku validator:**
```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.code_review/haiku" && cat ../XX_PROMPT__<label>_haiku_validate.md | claude -p --model haiku --add-dir ../.. --allowedTools "Bash(git -C ../.. ls-files *)" "Bash(git -C ../.. diff *)" "Bash(git -C ../.. log *)" "Bash(git -C ../.. show *)" --output-format text
```

If a validator fails (non-zero exit or empty output), drop that slot for this run and proceed with the survivors — do NOT manufacture a verdict for the missing slot. Validation is a separate CLI call from detection, so a model that succeeded at detection can still fail here, and the other way round.

Parse each validator's JSON into per-issue verdicts. You now have, for each finding, up to 3 verdicts (one per validator).

### Step 6: Synthesis

> **Multiple subsets.** When Step 1b planned multiple subsets, the synthesis report combines all subsets into a single output. Re-number findings into a global `X1`, `X2`, … sequence and append `(subset: <subset-name>)` after each X-id label so the reader can see where each finding came from. Order severity tiers globally (all Critical findings first across all subsets, then High across all subsets, etc.). If any subset had its `> Note: this subset exceeds the recommended review size` header, surface that caveat once near the top of the report. If a subset returned zero findings at any tier, you do not need a per-subset placeholder — the tier-level `None.` rule still applies globally.

For each finding, compute a consensus label **purely from validator verdicts**. Who originally flagged the issue at detection is not used at synthesis — every validator scored every finding.

Each validator returns one of `agree`, `disagree`, `unsure` per finding (those are the only valid verdicts — "unverified" is not a verdict). If a validator's CLI itself failed, that validator simply doesn't have a verdict on this run — drop the slot and label among the survivors. Note the dropped validator inline (e.g. "Haiku unavailable") so the reader sees there are fewer than 3 votes.

Apply rules in order, top-down — first match wins:

1. **Rejected** — 2 or more `disagree`, no `agree`. Remaining slots may be `unsure` or dropped (note inline). No validator agrees; the finding is treated as a false positive at this confidence (symmetric with rule 4 Agreed).
2. **Unanimous** — all 3 validator slots ran (none dropped) and every one returned `agree`.
3. **Controversial** — at least one `agree` AND at least one `disagree` (a genuine split among voters). Show every dissenting reason inline.
4. **Agreed** — 2 or more `agree`, no `disagree`. Remaining slots may be `unsure` or dropped (note inline).
5. **Inconclusive** — none of the above. Covers: fewer than 2 `agree` with no opposing `disagree` (e.g. 1 agree + 2 unsure, or 0 agree + ≤1 disagree + unsure/dropped). Weak/insufficient-signal cases.

**Do NOT re-judge severity at synthesis.** The reviewers/validators saw the code; you're working from JSON. Your job is narrow: deduplicate, label confidence, present. Use the merged severity from Step 4. If you think a verdict is weak, surface that as a user-facing note next to the finding — do not silently downgrade or drop it.

**Present** grouped by severity tier. For each tier, list findings or write `None.` explicitly. Every bullet starts with the X-id in bold. The label lists the three validator verdicts in a fixed order (Opus, Sonnet, Haiku) so the reader can see at a glance how each model voted — but do NOT mention who originally flagged the issue.

**Every finding gets a `Fix:` line — no exceptions.** Print it as a second line under the bullet, using the finding's (possibly synthesizer-authored, per Step 4) `suggested_fix`. This lets the reader decide apply/skip from the report alone, without re-opening each agent's raw JSON. For a `Rejected` finding, replace the fix text with a one-line note that no fix is planned since validators treat it as a false positive — do not invent a code fix for something the review concluded isn't real.

```
## Critical (must fix immediately)
- **X1** [Unanimous: Opus agree, Sonnet agree, Haiku agree] Description… (file:line)
  Fix: <concrete one-line fix>

## High (likely bugs / significant hazards)
- **X2** [Agreed: Opus agree, Sonnet unsure, Haiku agree] Description… (file:line)
  Fix: <concrete one-line fix>
- **X2b** [AST rule: unchecked-merge] [Unanimous: Opus agree, Sonnet agree, Haiku agree] Description… (file:line)
  Fix: <concrete one-line fix>
- **X3** [Controversial: Opus agree, Sonnet disagree — "<reason>", Haiku agree] Description… (file:line)
  Fix: <concrete one-line fix>
- **X4** [Inconclusive: Opus unsure — "<reason>", Sonnet unsure — "<reason>", Haiku agree] Description… (file:line)
  Fix: <concrete one-line fix>

## Medium (quality issues)
- **X5** [Agreed: Opus agree, Sonnet agree, Haiku unsure] Description… (file:line)
  Fix: <concrete one-line fix>
- **X6** [Rejected: Opus disagree — "<reason>", Sonnet disagree — "<reason>", Haiku disagree — "<reason>"] Description… (file:line) — included for transparency; no validator agrees, treat as a likely false positive.
  Fix: None planned — treated as a false positive.
- **X7** [Agreed: Opus agree, Sonnet agree, Haiku unavailable] Description… (file:line) — the Haiku validator failed; proceeding with 2 of 3 validators.
  Fix: <concrete one-line fix>
```

For Controversial / Inconclusive items, always include each non-`agree` validator's short reason in quotes.

**Offer to fix.** Ask which findings to apply, by X-id (e.g. "apply X1 and X3").

### Step 7: Mark the review complete

Once the user has triaged the findings — either by responding to all or most of them (apply / dismiss / "skip the rest") or explicitly signalling they're done — write an empty marker file so future runs (and the user) can tell that this review was closed out:

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" && touch .code_review/XX__CODE_REVIEW_COMPLETED
```

`XX` is the same run id chosen in Step 1, so the marker sits alongside that run's prompts under `.code_review/`. Do NOT write this marker preemptively after just presenting the synthesis — it means the user has actually engaged with the findings, not merely seen them.

## Notes

- There is **no shared context bundle**: every agent reads the local repo from `../..` (two levels up from its run subdirectory `.code_review/<agent>/`) on its own.
- Large projects are split into independent subsets at Step 1b (~6k lines / ~40 files / ~300 KB threshold), with Steps 2–5 running per subset in parallel under a ~6-concurrent-CLI cap, and Step 6 merging all subsets into a single global report.
- The three slots use `--model opus`, `--model sonnet` and `--model haiku` in both detection (Step 3) and validation (Step 5). Each is an alias for the newest model of its tier, so the roster always has three different models and does not depend on the CLI's default model. Validators judge two things, whether the finding is accurate and whether it is worth fixing under the review's own rules, which the validation prompt spells out.
- **Every validator validates every finding** (including ones it originally flagged at detection). The prompt removes explicit origin signals — no reviewer name, no ordering hint, no "you said" framing. Self-recognition via stylistic fingerprints in the verbatim `claim` text is a known residual risk (frontier evaluators recognize their own outputs above chance — Panickssery et al. 2024), so treat this as a soft, not absolute, protection.
- Claim order is shuffled independently per validator to defeat position bias.
- Synthesis labels are derived **purely from validator verdicts**, not from who originally flagged the finding. Detection only determines what gets into the candidate list; validation determines confidence.
- Validators are told to read freely inside the target project directory; consulting files outside the project is allowed only reluctantly when a claim genuinely cannot be settled from in-project code.
- If a slot fails at detection or at validation, drop it for that step and proceed with the survivors. There are no stand-ins, and a verdict is never invented. Detection and validation are decided independently.
- Reviewers and validators run with permission checks on (see "Reviewer permissions" in Step 3): `--add-dir ../..` lets them read the repo, `--allowedTools` covers four read-only git commands, and nothing lets them write files.
- If a review is requested multiple times, write each prompt as if it's the first review — do not mention prior reviews.
- **Data-ops AST pre-pass** (Step 2): when the target includes `.py` files, `lint_data_ops.py` runs as a deterministic pre-pass alongside the LLM detection agents, feeding findings into the same Step 4 consolidation → Step 5 validation → Step 6 synthesis pipeline, tagged with an `[AST rule: <name>]` provenance label. This exists because the 4 patterns it checks (unchecked pandas merges, unannotated missingness, empty-string sentinel masks, positional sample-id alignment) are exact syntactic matches an LLM reviewer could miss or false-positive on — but whether a match is real pandas usage in context is still a judgment call, so it still goes through peer validation like everything else, not a hard-coded pass/fail.
