# Agent instructions

## Tooling

- Prefer `uv` over `pip` for anything Python.
- Run `just test` before pushing.

## Delegation

Do not call the Agent tool or spawn subagents unless the user requested it.
Do not use workflows or deep research unless the user requested it.
