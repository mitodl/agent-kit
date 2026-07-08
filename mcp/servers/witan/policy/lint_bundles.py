#!/usr/bin/env python3
"""Structural linter for witan Cedar policy bundles.

omnigraph's `policy validate` only exercises the *per-graph* bundles (it loads
everything under the per-graph engine, so it cannot validate the server-scoped
`server.policy.yaml` — see policy/README.md). This linter is the CI gate for
*every* bundle, `server.policy.yaml` included: it catches the failures that
would otherwise ship silently — a group-name typo (`witan-user` vs
`witan-users`), an unknown action, a branch scope on an action that ignores it,
or a stray `deny` key (the model is allow-only).

It does NOT re-check decision semantics — that's `policy test`'s job. Run:

    uv run python policy/lint_bundles.py policy/*.policy.yaml
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

# Per-graph actions bind to Omnigraph::Graph::"<id>"; graph_list is server-scoped
# (Omnigraph::Server::"root"). Matches omnigraph-policy's PolicyAction.
BRANCH_SCOPE_ACTIONS = {"read", "export", "change"}
TARGET_BRANCH_SCOPE_ACTIONS = {
    "schema_apply",
    "branch_create",
    "branch_delete",
    "branch_merge",
}
UNSCOPED_GRAPH_ACTIONS = {"invoke_query", "admin"}
SERVER_ACTIONS = {"graph_list"}
GRAPH_ACTIONS = (
    BRANCH_SCOPE_ACTIONS | TARGET_BRANCH_SCOPE_ACTIONS | UNSCOPED_GRAPH_ACTIONS
)
ALL_ACTIONS = GRAPH_ACTIONS | SERVER_ACTIONS
SCOPE_VALUES = {"any", "protected", "unprotected"}


def lint_bundle(path: Path) -> list[str]:
    """Return a list of human-readable problems in one bundle file (empty = OK)."""
    errors: list[str] = []
    doc = yaml.safe_load(path.read_text())
    if not isinstance(doc, dict):
        return [f"{path.name}: top level must be a mapping"]

    if doc.get("version") != 1:
        errors.append(f"{path.name}: `version` must be 1")

    groups = doc.get("groups", {})
    if not isinstance(groups, dict) or not groups:
        errors.append(f"{path.name}: `groups` must be a non-empty mapping")
        groups = {}
    for name, members in groups.items():
        if not isinstance(members, list) or not all(
            isinstance(m, str) and m for m in members
        ):
            errors.append(f"{path.name}: group `{name}` must be a list of actor ids")

    protected = doc.get("protected_branches", [])
    if not isinstance(protected, list) or not all(
        isinstance(b, str) for b in protected
    ):
        errors.append(
            f"{path.name}: `protected_branches` must be a list of branch names"
        )

    rules = doc.get("rules", [])
    if not isinstance(rules, list) or not rules:
        errors.append(f"{path.name}: `rules` must be a non-empty list")
        rules = []

    seen_ids: set[str] = set()
    # A bundle is either a server bundle (graph_list) or a per-graph bundle; the
    # two action classes must not be mixed (omnigraph rejects that at load).
    saw_server_action = False
    saw_graph_action = False

    for i, rule in enumerate(rules):
        where = f"{path.name}: rule[{i}]"
        if not isinstance(rule, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        rid = rule.get("id")
        if not isinstance(rid, str) or not rid:
            errors.append(f"{where}: missing string `id`")
        else:
            where = f"{path.name}: rule `{rid}`"
            if rid in seen_ids:
                errors.append(f"{where}: duplicate rule id")
            seen_ids.add(rid)

        if "deny" in rule:
            errors.append(f"{where}: `deny` is not supported — the model is allow-only")
        allow = rule.get("allow")
        if not isinstance(allow, dict):
            errors.append(f"{where}: missing `allow` block")
            continue

        actors = allow.get("actors")
        if not isinstance(actors, dict) or "group" not in actors:
            errors.append(f"{where}: `allow.actors` must be `{{ group: <name> }}`")
        else:
            grp = actors["group"]
            if grp not in groups:
                errors.append(f"{where}: references undefined group `{grp}`")

        actions = allow.get("actions")
        if not isinstance(actions, list) or not actions:
            errors.append(f"{where}: `allow.actions` must be a non-empty list")
            actions = []
        for action in actions:
            if action not in ALL_ACTIONS:
                errors.append(f"{where}: unknown action `{action}`")
            if action in SERVER_ACTIONS:
                saw_server_action = True
            elif action in GRAPH_ACTIONS:
                saw_graph_action = True

        has_bs = "branch_scope" in allow
        has_tbs = "target_branch_scope" in allow
        if has_bs and has_tbs:
            errors.append(f"{where}: set branch_scope OR target_branch_scope, not both")
        for key in ("branch_scope", "target_branch_scope"):
            if key in allow and allow[key] not in SCOPE_VALUES:
                errors.append(f"{where}: `{key}` must be one of {sorted(SCOPE_VALUES)}")
        # An action must get the scope kind it actually reads (mirrors omnigraph).
        for action in actions:
            if has_bs and action not in BRANCH_SCOPE_ACTIONS:
                errors.append(f"{where}: action `{action}` does not take branch_scope")
            if has_tbs and action not in TARGET_BRANCH_SCOPE_ACTIONS:
                errors.append(
                    f"{where}: action `{action}` does not take target_branch_scope"
                )

    if saw_server_action and saw_graph_action:
        errors.append(
            f"{path.name}: mixes server-scoped (graph_list) and per-graph actions "
            "— omnigraph loads server and graph bundles under different engines"
        )
    return errors


def main(argv: list[str]) -> int:
    paths = [Path(p) for p in argv]
    if not paths:
        print("usage: lint_bundles.py <bundle.policy.yaml> ...", file=sys.stderr)
        return 2
    all_errors: list[str] = []
    for path in paths:
        all_errors.extend(lint_bundle(path))
    if all_errors:
        for err in all_errors:
            print(f"  ✗ {err}", file=sys.stderr)
        print(
            f"lint failed: {len(all_errors)} problem(s) in {len(paths)} bundle(s)",
            file=sys.stderr,
        )
        return 1
    print(f"lint OK: {len(paths)} bundle(s) structurally valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
