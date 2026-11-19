"""AST-based lint for common pandas missing-data/join anti-patterns.

Ported from scilintr's rule design (unchecked-merge, unannotated-missingness,
sentinel-mask-assignment, positional-sample-alignment), reimplemented directly
rather than depending on the upstream package.

Usage:
    python3 lint_data_ops.py <path>... [--rules unchecked-merge,...] [--json] [--summary]

A finding on line L is suppressed by a waiver comment on any line in [L-4, L]:
    df.merge(other, on="id")  # ANALYSIS_OK[join]: keys are pre-validated upstream
"""
import argparse
import ast
import json
import re
import sys


WAIVER_RE = re.compile(r'#\s*ANALYSIS_OK\[(?P<category>[\w-]+)\]:\s*(?P<explanation>\S.*?)\s*$')
WAIVER_WINDOW = 4

SAMPLE_ID_KEYS = {
    "sample_id", "barcode", "donor_id", "patient_id", "id",
    "subject_id", "participant_id", "case_id", "specimen_id",
    "individual_id", "sample", "sample_name",
}


def _source_line(source_lines, lineno):
    return source_lines[lineno - 1].strip() if 1 <= lineno <= len(source_lines) else ""


def _make_finding(rule, filename, source_lines, lineno):
    return {"rule": rule, "file": filename, "line": lineno, "snippet": _source_line(source_lines, lineno)}


def check_unchecked_merge(tree, source_lines, filename):
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        is_merge_call = (
            (isinstance(node.func, ast.Attribute) and node.func.attr == "merge")
            or (isinstance(node.func, ast.Name) and node.func.id == "merge")
        )
        if is_merge_call and not any(kw.arg == "validate" for kw in node.keywords):
            findings.append(_make_finding("unchecked-merge", filename, source_lines, node.lineno))
    return findings


def check_unannotated_missingness(tree, source_lines, filename):
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        if attr in ("fillna", "nan_to_num"):
            findings.append(_make_finding("unannotated-missingness", filename, source_lines, node.lineno))
        elif attr == "to_numeric" and any(
            kw.arg == "errors" and isinstance(kw.value, ast.Constant) and kw.value.value == "coerce"
            for kw in node.keywords
        ):
            findings.append(_make_finding("unannotated-missingness", filename, source_lines, node.lineno))
    return findings


def _contains_empty_string_compare(node):
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.Eq, ast.NotEq)):
        operands = [node.left] + node.comparators
        if any(isinstance(o, ast.Constant) and o.value == "" for o in operands):
            return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.BitAnd, ast.BitOr)):
        return _contains_empty_string_compare(node.left) or _contains_empty_string_compare(node.right)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Invert):
        return _contains_empty_string_compare(node.operand)
    return False


def check_sentinel_mask_assignment(tree, source_lines, filename):
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and _contains_empty_string_compare(node.value):
            findings.append(_make_finding("sentinel-mask-assignment", filename, source_lines, node.lineno))
    return findings


def check_positional_sample_alignment(tree, source_lines, filename):
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not isinstance(value, ast.Subscript) or not isinstance(value.value, ast.Name):
            continue
        key_node = value.slice
        if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
            continue
        if key_node.value.lower() not in SAMPLE_ID_KEYS:
            continue
        for target in node.targets:
            if isinstance(target, ast.Attribute) and target.attr in ("columns", "index"):
                findings.append(_make_finding("positional-sample-alignment", filename, source_lines, node.lineno))
    return findings


RULES = {
    "unchecked-merge": check_unchecked_merge,
    "unannotated-missingness": check_unannotated_missingness,
    "sentinel-mask-assignment": check_sentinel_mask_assignment,
    "positional-sample-alignment": check_positional_sample_alignment,
}

RULE_WAIVER_CATEGORIES = {
    "unchecked-merge": "join",
    "unannotated-missingness": "imputation",
    "sentinel-mask-assignment": "missingness",
    "positional-sample-alignment": "alignment",
}


def find_waivers(source_lines):
    waivers = []
    for i, line in enumerate(source_lines, start=1):
        match = WAIVER_RE.search(line)
        if match:
            waivers.append((i, match.group("category")))
    return waivers


def is_waived(lineno, waivers, category):
    return any(w_category == category and w_line <= lineno <= w_line + WAIVER_WINDOW for w_line, w_category in waivers)


def lint_source(source, filename, rule_names):
    source_lines = source.splitlines()
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as e:
        print(f"{filename}: syntax error, skipping ({e})", file=sys.stderr)
        return []
    waivers = find_waivers(source_lines)
    findings = []
    for name in rule_names:
        category = RULE_WAIVER_CATEGORIES[name]
        for finding in RULES[name](tree, source_lines, filename):
            if not is_waived(finding["line"], waivers, category):
                findings.append(finding)
    return findings


def lint_file(path, rule_names):
    with open(path) as f:
        source = f.read()
    return lint_source(source, path, rule_names)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", help="Python files to lint")
    parser.add_argument("--rules", help="Comma-separated rule names to run (default: all)")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of human-readable text")
    parser.add_argument("--summary", action="store_true", help="Print only a per-rule count summary")
    args = parser.parse_args()

    rule_names = args.rules.split(",") if args.rules else list(RULES.keys())
    for name in rule_names:
        if name not in RULES:
            parser.error(f"unknown rule: {name} (choices: {', '.join(RULES)})")

    all_findings = []
    for path in args.paths:
        all_findings.extend(lint_file(path, rule_names))

    if args.json:
        print(json.dumps({"findings": all_findings}))
        return

    if args.summary:
        counts = {}
        for finding in all_findings:
            counts[finding["rule"]] = counts.get(finding["rule"], 0) + 1
        for rule, count in counts.items():
            print(f"{rule}: {count}")
    else:
        for finding in all_findings:
            print(f"{finding['file']}:{finding['line']}: [{finding['rule']}] {finding['snippet']}")

    sys.exit(1 if all_findings else 0)


if __name__ == "__main__":
    main()
