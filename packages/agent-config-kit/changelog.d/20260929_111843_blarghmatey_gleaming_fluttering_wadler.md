### Fixed

- Applying a plugin hook file, or `install_files` output, over a symlink no longer crashes with `FileNotFoundError` when the symlink is dangling, or writes through a live one into its target. It now raises a `ConflictingPathError` naming `--force`, and with `--force` replaces the symlink with the copied file.
