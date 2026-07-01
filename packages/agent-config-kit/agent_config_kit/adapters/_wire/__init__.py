"""Vendored, codegen'd wire-format models — adapter-internal, not public API.

Generated with ``datamodel-code-generator`` (per spec D6) from real published
JSON Schemas (Claude Code's ``settings.json`` hooks section, OpenCode's
``config.json``) or, where no upstream schema exists (Pi, GitHub Copilot/VS
Code), a minimal hand-authored one. Regenerate on demand when upstream
schemas change — deliberately not enforced by CI, since roughly half the
v1 platforms have no live schema to diff against automatically.

Not imported by ``agent_config_kit``'s adapters at runtime; used only to
validate adapter output against real schema shape (see
``tests/test_wire_validation.py``) and as a reference for future adapter
work.
"""
