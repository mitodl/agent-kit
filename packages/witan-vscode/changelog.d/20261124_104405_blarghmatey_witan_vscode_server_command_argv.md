### Fixed

- `witan.server.command` is documented as one argument per item, in the setting and the README, which had suggested adding `--target <name>` as written. When the server does not start, the error shows the command as the JSON array it is written as, and names any item that folds an option and its value together (e.g. `"--target production"`), instead of a space-joined line that hid the mistake.
