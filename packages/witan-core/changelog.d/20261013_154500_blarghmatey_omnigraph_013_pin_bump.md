### Changed

- The pinned omnigraph binary moves from 0.11.0 to 0.13.0, and the declared
  storage format from 9 to 14. 0.13 reads only format 14, so a local store
  written by 0.11 has to go through `witan migrate storage` after
  `witan setup` installs the new binary, and every deployed graph is rebuilt
  in the cutover.
- With the format at 14, `omnigraph_http.RESPONSE_CONTRACT_REQUIRED` is on: a
  response without `Omnigraph-Http-Api: 0.13` is a contract violation. A
  client on this release pointed at a 0.11 server has its writes applied and
  reported as indeterminate, so the server moves first.
