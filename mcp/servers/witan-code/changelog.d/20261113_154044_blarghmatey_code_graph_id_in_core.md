### Changed

- The cluster graph-id function moved to witan-core as `witan_core.graph_ids.code_graph_id`, along with `CODE_GRAPH_PREFIX`, `BRIDGE_GRAPH_ID` and `GRAPH_ID_RE`. `witan_code.config` no longer defines them. The ids it returns are unchanged. Requires witan-core 0.57.0.
