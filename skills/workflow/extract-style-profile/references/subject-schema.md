# `subject.json` schema

Every script reads `<workdir>/subject.json`. Write it before running anything.

| Field | Required | Meaning |
|---|---|---|
| `mode` | yes | `person` (one human), `team` (several humans, output is shared conventions), or `repo` (conventions of specific repositories regardless of author) |
| `name` | yes | Display name used in the report title |
| `members` | person/team (optional in repo mode) | `[{"login": "<github login>", "author_patterns": ["<regex>", ...]}]`. Patterns match `Name <email>` in git history. Run `discover.py` first and copy the anchored regexes from `activity/identities.md`. Anchor with `^` but don't end with `$`: git matches `--author` against a line that continues past the email. In repo mode, members are only used to merge one person's aliases in the stats |
| `orgs` | no | Restrict to repos/activity owned by these GitHub orgs/users. Empty means everything the members touched |
| `repos` | repo mode | `["owner/name", ...]`. In person/team mode, extra repos to include beyond discovery |
| `exclude_repo_patterns` | no | Regexes on `owner/name`. Default `["-ghsa-"]` (private security-advisory forks duplicate their parent) |
| `local_roots` | no | Directories to scan for existing checkouts, e.g. `["~/code"]`. Found checkouts are symlinked, never cloned or modified |
| `since` / `until` | yes | ISO dates. `until` is exclusive. Nothing on or after `until` is collected |
| `ai_cutoff` | yes | ISO date. Data before it is **baseline**; data on or after it is **comparison only**. See "Choosing ai_cutoff" |
| `eras` | yes | `[{"name": "E2_2016-2019", "start": "2016-01-01", "end": "2020-01-01"}, ...]`, contiguous, covering `since`..`until`. Put `ai_cutoff` on an era boundary |
| `min_commits` | no | Skip repos with fewer discovered commits (default 3) |
| `full_clone_min_commits` | no | Repos below this are cloned blobless: messages only, no diffs (default 20) |

## Examples

Person:

```json
{
  "mode": "person",
  "name": "Jane Doe",
  "members": [{"login": "janedoe", "author_patterns": ["^Jane\\ Doe\\ <jane@example\\.com>", "^Jane\\ Doe\\ <jdoe@oldjob\\.com>"]}],
  "orgs": [],
  "local_roots": ["~/code"],
  "since": "2014-01-01",
  "until": "2026-01-01",
  "ai_cutoff": "2025-01-01",
  "eras": [
    {"name": "E1_2014-2017", "start": "2014-01-01", "end": "2018-01-01"},
    {"name": "E2_2018-2021", "start": "2018-01-01", "end": "2022-01-01"},
    {"name": "E3_2022-2024", "start": "2022-01-01", "end": "2025-01-01"},
    {"name": "E4_2025", "start": "2025-01-01", "end": "2026-01-01"}
  ]
}
```

Team (conventions shared by a group; output is aggregate, not per-person):

```json
{
  "mode": "team",
  "name": "Platform team",
  "members": [
    {"login": "alice", "author_patterns": ["^Alice\\ Smith\\ <"]},
    {"login": "bob", "author_patterns": ["^Bob\\ Jones\\ <", "^[^<]*<bob@corp\\.com>"]}
  ],
  "orgs": ["acme"],
  "since": "2021-01-01",
  "until": "2026-01-01",
  "ai_cutoff": "2025-01-01",
  "eras": [
    {"name": "E1_2021-2022", "start": "2021-01-01", "end": "2023-01-01"},
    {"name": "E2_2023-2024", "start": "2023-01-01", "end": "2025-01-01"},
    {"name": "E3_2025", "start": "2025-01-01", "end": "2026-01-01"}
  ]
}
```

Repo (a codebase's conventions, all human authors):

```json
{
  "mode": "repo",
  "name": "acme/payments",
  "repos": ["acme/payments", "acme/payments-sdk"],
  "since": "2022-01-01",
  "until": "2026-01-01",
  "ai_cutoff": "2025-01-01",
  "eras": [
    {"name": "E1_2022-2024", "start": "2022-01-01", "end": "2025-01-01"},
    {"name": "E2_2025", "start": "2025-01-01", "end": "2026-01-01"}
  ]
}
```

## Choosing eras

Eras exist so that changes over time are visible, not averaged away. Pick
boundaries from evidence rather than calendar convenience:

1. Look at `activity/yearly.md` and `activity/repos.tsv` (person/team). A shift
   in which repos dominate (for example, config management giving way to IaC)
   usually marks a role or stack change.
2. After `profile_stats.py`, look at the per-year commit table. A step change in
   tense, conventional-commit share, or body rate is a boundary candidate.
3. Keep each era big enough to measure (roughly 300+ commits or 100+ prose
   items). Merge thin early years into one era.
4. It's fine to re-cut eras after the first stats pass and re-run
   `extract_commits.py` / `build_corpora.py`. They are cheap.

## Choosing `ai_cutoff`

The point of the cutoff is to keep AI-assisted output from being learned as the
subject's natural style. Start from a conservative guess (the first month any AI
tooling was used, if known), run the pipeline through `profile_stats.py`, then
read `stats/ai_markers.md`:

- Choose the first month where markers become **sustained relative to activity**,
  not the first isolated hit. Upstream templates and bots produce stray emoji and
  headers years earlier.
- When unsure, choose the earlier date. Losing a few months of baseline costs less
  than learning generated prose as "their voice".
- If the subject says they never used AI tools, set `ai_cutoff` equal to `until`
  (no comparison era) but still skim `ai_markers.md` for surprises.
- Ask the subject when the markers are ambiguous. It is their history.
