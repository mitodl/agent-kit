### Added

- `OmnigraphClient.profile`, a frozen `SubstrateProfile` declaring the store's backend (`local`, `s3`, `az`, `server`, or `mcp` for witan-code's MCP-tier client) and one predicate per capability that used to be read off `is_remote`: `shared`, `maintenance` (`client` or `server`), `asserts_actor`, `http_transport` and `admission_gated`. `http_contract` carries the HTTP contract the transport negotiated on `HEAD /healthz`, and reading it never probes.
- `OmnigraphClient.require(capability)`, which raises `CapabilityUnsupported` (a `RuntimeError` refusal with `backend` and `capability` fields) and first logs a `witan.capability.gap` record keyed `<backend>:<capability>`.

### Changed

- `optimize()` and `cleanup()` on a deployed-server store raise `CapabilityUnsupported` before running the CLI, instead of failing on omnigraph's own refusal of `--server`.
- The client's actor assertion, HTTP transport choice, write admission gate, call budget and unavailable-server wait now read the profile instead of `is_remote`. Behaviour is unchanged. `is_remote` stays for witan and witan-code callers until they move to the specific predicate.
