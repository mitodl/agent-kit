### Changed

- Raised the `witan-core` floor to `>=0.50`, which pins omnigraph 0.14
  (storage format 16) and sends `Omnigraph-Http-Api: 0.14`. A 0.14 data tier
  refuses an older client's requests.
