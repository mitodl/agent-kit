### Fixed

- The edit and SessionStart hooks skip a repo the cluster does not serve instead of indexing it. Each attempt was refused with `ClusterGraphMissing`, which the hook discarded, but the server logged it and counted it as a failed tool call against its error-ratio alert. The drainer asks the cluster for its served graphs once per process (`code_store_graphs` through the MCP tier, `GET /graphs` direct) and indexes as before when that listing fails or comes back empty. An explicit `witan-code index` still reports the refusal.
