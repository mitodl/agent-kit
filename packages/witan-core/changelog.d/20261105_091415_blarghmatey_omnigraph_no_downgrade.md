### Fixed

- The omnigraph installer (`witan setup`, `witan-code setup`) no longer
  replaces a binary newer than its pin. A newer binary may already have
  stamped local stores with a storage format the pin refuses (0.14 writes 16,
  which 0.13 won't open), so it is left in place with a message saying why. A
  strict install (`install_omnigraph()`'s default, which CI uses) raises
  `OmnigraphInstallFailed` instead. Delete the binary and re-run to install
  the pin anyway. Releases before this one still downgrade.
