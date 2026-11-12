### Fixed

- The web UI's Bridge tab finds the binding behind a service (deploy) edge.
  It asked `code_interface_providers` for the key `repo:<deployed uri>`,
  which witan-code stopped writing in #7; the binding is keyed by the bare
  URI with `sub_kind` "repo". It had nothing to find until witan-code drew
  deploy edges again in the same change.
