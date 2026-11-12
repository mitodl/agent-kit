### Changed

- `RemoteMCPProxy` no longer fills in the detected `repo` on a call that passes `all_repos`, in `_map_args` and in the journal's client context. witan-council now spells "every repo" `all_repos=True` and refuses a `repo` beside it. An explicit `repo=""` is still sent as given, and a current server refuses it.
