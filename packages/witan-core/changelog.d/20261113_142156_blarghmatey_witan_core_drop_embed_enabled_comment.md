### Fixed

- A comment in `omnigraph_install.py` said witan defaults `WITAN_EMBED_ENABLED`
  off. Nothing reads that variable, so it now says witan has no embeddings and
  no schema declares a `Vector` column. Comment only; no behavior change.
