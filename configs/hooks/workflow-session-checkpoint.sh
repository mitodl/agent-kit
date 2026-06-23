#!/usr/bin/env bash
# Stop hook: auto-close the active WorkflowSession when the agent stops.
# Delegates entirely to `witan session-checkpoint` — no checkout or QUERIES_DIR dependency.
witan session-checkpoint 2>/dev/null || true
