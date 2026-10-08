### Fixed

- The commit id parser refuses a history-block slot of 16384 or more, which
  omnigraph cannot emit. It was accepted and ordered as a real commit.
