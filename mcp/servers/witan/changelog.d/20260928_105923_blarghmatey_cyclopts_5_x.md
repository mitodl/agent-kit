### Changed

- Allow cyclopts 5 (`cyclopts>=4,<6`). Under cyclopts 5, `--help` shows a
  value placeholder after each option (e.g. `--target STR`), and a usage error
  such as an unknown option exits with code 2 instead of 1.
