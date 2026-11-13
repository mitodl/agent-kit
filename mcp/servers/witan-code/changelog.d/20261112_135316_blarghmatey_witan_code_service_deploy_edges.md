### Fixed

- `code_repo_dependencies` and the bridge explorer draw the "ol-infrastructure
  deploys repo X" edge again. The extractor has written a repo service anchor
  as the bare repo URI with `sub_kind="repo"` since #7, but the graph builder
  still looked for a `repo:` key prefix and the `all_bindings` read did not
  return `sub_kind`, so no deployed graph ever had a service edge. Image and
  app-name anchors still draw no edge: nothing in the indexed repos yet says
  which repo builds an image.
