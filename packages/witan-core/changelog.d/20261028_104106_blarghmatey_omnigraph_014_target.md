### Added

- CLI addressing follows the installed omnigraph binary. `cli_speaks_targets`
  runs `omnigraph version --json` once per binary file: 0.14 reports
  `protocols.target: 2` and gets `--target`, while 0.13, which exits 2 on
  `--json`, keeps `--store`/`--server`/`--graph`. Any other protocol raises.
  `store_cli_args` now takes `binary=` (required) and `token=`. On 0.14 a
  local store is `--target <absolute path>`, and a server is a `witan` server
  target in an operator config that witan generates and passes with
  `--operator-config`. That config names `WITAN_OMNIGRAPH_TOKEN` as the
  target's token source, so the file holds no secret and each subprocess
  carries its own actor's token. Local calls pass a generated config too,
  because 0.14 refuses a whole command when `~/.omnigraph/config.yaml` holds
  an entry it cannot parse (e.g. a 0.13-format `profiles:` block).
- `store_address(uri, graph_id)`, the binary-independent identity of a store,
  for callers that compare two spellings of one store.
- `init_cli_args(binary)`: the generated empty operator config for
  `omnigraph init` on 0.14, which keeps its positional URI but still reads
  `~/.omnigraph/config.yaml`.
- `write_quiet_args(binary)`: `--quiet` on writes for a pre-0.14 CLI only,
  since 0.14 removed the flag and exits 64 on it.

### Changed

- `store_subprocess_env` sets a remote store's token as both
  `OMNIGRAPH_BEARER_TOKEN` (read by a pre-0.14 CLI) and
  `WITAN_OMNIGRAPH_TOKEN` (read by 0.14 through the generated config), and
  strips both for a local or `s3://` store. The ambient
  `OMNIGRAPH_BEARER_TOKEN` is still the fallback for a remote store with no
  configured token, over the CLI and the HTTP transport alike.
- `schema_apply` addresses its store with `store_cli_args`, because 0.14
  dropped `schema apply`'s positional URI.
- On a 0.14 binary, `store_cli_args` raises `RuntimeError` for what that CLI
  cannot reach: a direct `s3://` or `az://` store, and a plain `http://`
  server other than an anonymous loopback one (0.14 sends a token only over
  HTTPS). Reads and writes that go over the pooled HTTP transport are not
  affected.
