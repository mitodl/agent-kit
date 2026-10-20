# Agent instructions

## Tooling

- Prefer `uv` over `pip` for anything Python.
- Run `just test` before pushing.

## Delegation

<!-- portability: ok -->
<!-- Eval fixture: the input the agent under test reads, not an instruction
agent-kit ships to any platform. -->
Do not call the Agent tool or spawn subagents unless the user requested it.
Do not use workflows or deep research unless the user requested it.
