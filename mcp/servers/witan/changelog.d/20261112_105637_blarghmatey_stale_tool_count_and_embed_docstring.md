### Fixed

- `recall`'s description said embeddings were deferred behind
  `WITAN_EMBED_ENABLED`. Nothing reads that variable, so it now says embeddings
  are not implemented and `recall` seeds from BM25 only.
