; YAML symbol extraction. Each mapping key becomes a Symbol; nested keys get a
; dotted qualified path (e.g. jobs.build.steps) so large configs are navigable.

(block_mapping_pair
  key: (flow_node) @symbol.key)
