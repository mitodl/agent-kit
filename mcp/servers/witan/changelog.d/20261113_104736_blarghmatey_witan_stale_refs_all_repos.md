### Fixed

- `witan memory stale-refs` failed with `AttributeError: 'dict' object has no attribute 'strip'`, with or without `--all-repos`, because `memory_stale_refs` landed without the `all_repos` change the other repo-taking tools got. It now takes `all_repos=True` to check every repo's memories, and refuses `repo=""` like the rest.
