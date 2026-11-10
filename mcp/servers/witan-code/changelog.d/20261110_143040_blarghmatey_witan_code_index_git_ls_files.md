### Fixed

- Indexing inside a git work tree collects git's listing (`git ls-files
  --cached --others --exclude-standard`) instead of walking the disk, so
  files git ignores no longer reach the code graph. The fixed skip list
  only caught a virtualenv named `.venv` or `venv`: a dry estimate of
  open-edx-plugins with `uv sync` into `env/` came to 703,408 records in
  108 2 MiB loads, against 14,661 records in 5 loads now. The skip list,
  nested-checkout exclusion and extension filter still apply on top. The
  PostToolUse hook holds the files it names to the same rule with one
  `git check-ignore` per batch, and the repo-level bridge extractors read
  the same listing. Outside a work tree, or without a `git` binary, the
  disk is walked as before. A full-repo index purges rows already stored
  for files that are now ignored, wherever the run is allowed to purge.
