### Changed

- The remote merge (`witan migrate merge` against a deployment) exports its
  source store through `OmnigraphClient.export_to` instead of its own
  `omnigraph export` subprocess. A source on a deployed omnigraph-server now
  goes over the pooled HTTP transport, which the 0.14 CLI's refusal of
  plain-http server targets would otherwise have broken, and every source rides
  the shared retry policy.
