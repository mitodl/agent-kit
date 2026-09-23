### Added

- **The UI's Bridge tab: the cross-repo dependency graph, drillable to the
  bindings behind each edge.** witan-code's `code_repo_dependencies` is drawn
  as a repo graph with an edge table beside it; an edge lists the contracts
  behind it, and a contract opens the `InterfaceBinding` rows in the edge's two
  repos through `code_interface_providers` and `_consumers`. Filters for
  contract kind, a confidence floor, stoplisted generic keys (DEBUG, PORT) and
  Stage-2 precise edges. The tab appears only when the server has witan-code
  mounted, and a server without it no longer fails the page's tool check. ADR
  0011 is amended to bind the three tools as optional, and never the
  `code_*` tools that elicit.
