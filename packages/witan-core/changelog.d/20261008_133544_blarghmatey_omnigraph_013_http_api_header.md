### Added

- The pooled HTTP transport sends `Omnigraph-Http-Api: 0.13` on every request.
  omnigraph-server 0.13 answers `400 api_contract_mismatch` without it; 0.11
  ignores it.
- The transport checks the same header on responses before decoding the body.
  A response stamped with another contract is terminal. Once the pinned
  storage format reaches 14, a response with no stamp is too (a successful
  one, or any refusal of a write), because without the server's stamp a
  429/503/409 no longer proves nothing was applied. A read that failed
  unstamped keeps its ordinary classification, since repeating a read is safe.
