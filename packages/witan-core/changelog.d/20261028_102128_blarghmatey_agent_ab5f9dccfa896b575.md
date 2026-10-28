### Changed

- `OmnigraphClient.load_batch` and `export_to` now reach a deployed
  omnigraph-server over the pooled HTTP transport instead of the `omnigraph`
  CLI. A load posts its NDJSON to `POST /graphs/<id>/load/ndjson` and an export
  streams `POST /graphs/<id>/export` straight to the output file, under the same
  retry policy and write gate as before. These are the routes the 0.13 CLI
  itself calls for a remote store, and both exist unchanged in 0.14. The 0.14
  CLI refuses a plain-http server target, which is what the deployed data tier
  is, so the subprocess path would have stopped working at that pin bump.
  Local and `s3://` stores keep the CLI, as does a remote store with
  `WITAN_OMNIGRAPH_HTTP=0`.
- A branched client's `load` (witan-code's `--branch <view> --from main`) goes
  over HTTP as the route's `branch` and `from` query parameters. A subclass
  that injects any other flag into `load` is refused with an error rather than
  having the flag dropped.
- A remote load's return value is now the server's `GraphBatchLoadOutput` JSON
  rather than the CLI's printed summary.
