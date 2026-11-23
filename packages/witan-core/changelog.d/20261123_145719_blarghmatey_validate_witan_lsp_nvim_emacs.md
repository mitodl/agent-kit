### Fixed

- lsp-mode got each CodeLens three times: as a lens, in the hover on its line,
  and as a code action. It renders lenses but advertises only
  `workspace.codeLens`, never `textDocument.codeLens`, so the language server
  took it for a client without CodeLens. Either capability now counts.
