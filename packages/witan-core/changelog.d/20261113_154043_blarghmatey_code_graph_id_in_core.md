### Added

- `witan_core.graph_ids` holds the cluster code-graph id: `code_graph_id(repo)`, `CODE_GRAPH_PREFIX`, `BRIDGE_GRAPH_ID` and `GRAPH_ID_RE`. It moved from `witan_code.config.graph_id`, whose algorithm ol-infrastructure kept a byte-for-byte copy of to declare the same graphs in `cluster.yaml`. The module is stdlib-only, so that stack can import it instead, and `tests/test_graph_ids.py` pins its output. `witan_core.omnigraph` now validates graph ids against the same `GRAPH_ID_RE`.
