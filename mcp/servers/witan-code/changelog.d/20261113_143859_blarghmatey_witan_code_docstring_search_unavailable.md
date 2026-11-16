### Changed

- `code_search_symbol` returns `{"symbols": [...]}` instead of a bare list, so the result has room for the `note` below. A caller that read the list now reads `result["symbols"]`.

### Fixed

- `code_search_symbol` says when it skipped the docstring search. A store whose schema has no `Symbol.docstring` index, or a deployed witan-code tier older than the docstring query, answers with name matches only, and the only sign was a warning in the server's log, which a client of a deployed store never sees. The result now carries a `note` starting `docstring search unavailable` that gives the reason and, in a cross-repo fan-out, how many of the stores searched skipped it. The tool docs also say that only the first 500 characters of a docstring are indexed.
