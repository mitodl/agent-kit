#!/usr/bin/env bash
# UserPromptSubmit hook: inject active project + ready-task context before each prompt.
# Delegates entirely to `witan inject-context` — no checkout or QUERIES_DIR dependency.
witan inject-context 2>/dev/null || true
