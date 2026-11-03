# Render the plan comment body from the work graph.
#
# A pure function of its input, deliberately: every fact here comes from witan
# and the linked PRs, so the body is reproducible rather than authored. That is
# what makes regenerate-and-overwrite safe, and it is why upsert's --expect-sha
# does not apply to a rendered comment -- there is no hand-written prose for a
# second session to clobber.
#
# Input (one object on stdin):
#   { spec:          string|null,   # URL of the spec or design doc
#     decisions:     string|null,   # markdown links to the ADRs, pre-formatted
#     note:          string|null,   # the ONE authored line, <=280 chars
#     live_pr:       bool,          # false when WITAN_LIVE_PR=0 suppressed it
#     generated_on:  string,        # UTC date, from `date -u`, never guessed
#     chunks: [ { slug, title, status, external_uri, created_at,
#                 blocked_by: [slug], live_pr: {...}|null } ] }
#
# Output: the comment body on stdout. The marker and digest lines are NOT added
# here; upsert-plan-comment.sh owns both halves of that contract.

# Chunk order is creation order. `plan-technical-approach` creates the chunk
# tasks in plan order, so created_at carries it without a second field to keep
# in sync. A topological sort over blocked_by was the alternative and is worse:
# independent chunks have no edges between them, so it would have to fall back
# to this anyway, and it would renumber the table every time a dependency moved.
def ordered: .chunks | sort_by(.created_at // "");

# `blocked_by` arrives in two shapes and the renderer must not care which.
# The server returns a list; `witan tasks` projects it through `", ".join(...)`
# for its table, so the CLI hands over a comma-joined STRING. Normalise once
# here rather than making every call site test the type.
def blockers_of:
  (.blocked_by // [])
  | if type == "string"
    then (split(", ") | map(select(. != "")))
    else . end;

# `live_pr` arrives in three shapes, for the same reason `blocked_by` arrives in
# two. The server omits the key when no lookup ran; `witan tasks` projects rows
# through `render_table`, which normalises a missing value to "" because TOML
# has no null. Only an OBJECT is a lookup result -- a missing key, a null and an
# empty string all mean the same thing, which is that nobody asked.
#
# Testing `!= null` and then indexing is what made the first end-to-end render
# die with "Cannot index string with error", on the commonest input there is:
# chunk tasks that exist but have no PR yet.
def live_record:
  (.live_pr // null) | if type == "object" then . else null end;

# How many checks are failing, pending, passing -- absent on a PR with no CI.
def check_summary:
  (.checks // {}) as $c
  | if ($c.fail // 0) > 0 then "checks failing"
    elif ($c.pending // 0) > 0 then "checks pending"
    else null end;

def review_summary:
  (.review_decision // "") as $r
  | if   $r == "APPROVED"           then "approved"
    elif $r == "CHANGES_REQUESTED"  then "changes requested"
    elif $r == "REVIEW_REQUIRED"    then "review required"
    else null end;

# The PR cell. A task with no external_uri has no PR yet; that is a normal
# state for an unstarted chunk, not a missing value.
def pr_cell:
  . as $x
  | ($x | live_record) as $pr
  | if ($x.external_uri // "") | test("/pull/[0-9]+$") then
      ($pr.ref // ("#" + ($x.external_uri | capture("/(?<n>[0-9]+)$").n)))
      | "[\(.)](\($x.external_uri))"
    else "—" end;

# The state cell, in priority order: what the PR says, then what the task says.
#
# `live_pr` absent is NOT the same as "no PR". It also means the lookup was
# disabled (WITAN_LIVE_PR=0) or failed, and rendering those as a blank cell
# reads as "nothing opened yet" -- the one misreading that would have someone
# re-do work that is already in review. Say unavailable instead.
def state_cell($live_enabled):
  live_record as $pr
  | (.status // "") as $st
  | if $pr != null and ($pr.error | not) then
      ([ $pr.state
       , (if $pr.draft and $pr.state == "open" then "draft" else null end)
       , ($pr | review_summary)
       , ($pr | check_summary)
       ] | map(select(. != null)) | join(", "))
    elif ((.external_uri // "") | test("/pull/[0-9]+$")) then
      if $live_enabled then "PR state unavailable" else "PR state not fetched" end
    elif $st == "blocked" then
      "blocked" + (if (blockers_of | length) > 0
                   then " on " + (blockers_of | join(", "))
                   else "" end)
    elif $st == "closed"      then "done, no PR"
    elif $st == "in_progress" then "in progress"
    else "not started" end;

# A chunk title is a task title, written by whoever planned the work, and a
# pipe in it would end the cell early and shift every column after it. Escape
# it rather than trusting the author to avoid one.
def cell: gsub("\\|"; "\\|");

# Takes a to_entries pair so the row number and the chunk travel together;
# piping `.value` first would put `.key` out of reach.
def row($i; $live_enabled):
  .value
  | "| \($i) | \(.title // "" | cell) | \(pr_cell) | \(state_cell($live_enabled)) |";

# The header's blocked-on line comes from the graph, not from parsing the spec
# for `(open)` markers: a blocker a reader cares about is one that is actually
# holding a chunk, and that is exactly what a `blocks` edge records.
def blockers:
  [ ordered[] | select((.status // "") == "blocked") | blockers_of[] ]
  | unique;

# Only a real boolean answers "was the lookup enabled". Absent, null, or
# anything else means the caller did not say, and the safe default is "it ran"
# -- which renders an unresolved PR as `unavailable` rather than claiming it was
# never fetched.
(.live_pr | if type == "boolean" then . else true end) as $live_enabled
| [ ([ (if (.spec // "") != "" then "**Plan:** \(.spec)" else null end)
     , (if (.decisions // "") != "" then "**Decisions:** \(.decisions)" else null end)
     ] | map(select(. != null))
       | if length == 0 then null else join(" · ") end)
  , (if (blockers | length) > 0
     then "**Blocked on:** " + (blockers | join(", "))
     else null end)
  , (if (.spec // "") != "" or (.decisions // "") != "" or ((blockers | length) > 0)
      then "" else null end)
  , "| # | Chunk | PR | State |"
  , "|---|-------|----|-------|"
  , ( [ ordered | to_entries[] | row(.key + 1; $live_enabled) ]
      | if length == 0 then ["| — | _no chunk tasks found_ | — | — |"] else . end
      | .[] )
  , (if (.note // "") != "" then "" else null end)
  , (if (.note // "") != "" then "**Note:** \(.note)" else null end)
  , ""
  , "<!-- regenerated \(.generated_on) -->"
  ]
| map(select(. != null))
| join("\n")
