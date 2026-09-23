"""The bundled skills must work on agents without Claude's AskUserQuestion.

``witan setup --agent pi`` installs these same SKILL.md files into Pi, whose
core has no ``AskUserQuestion`` tool (and no "Other" free-text escape hatch). A
skill that says "build an AskUserQuestion call" unconditionally leaves a Pi
agent with an instruction it cannot follow — and the tempting improvisation is
to pick an answer itself, which for ``witan-task`` means claiming a task the
user never chose. So the tool may be *named* only inside a capability-scoped
"Asking the user" section that also gives the plain-message fallback.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parents[1]
_SKILLS = sorted((_PKG / "witan" / "skills").glob("*/SKILL.md"))
# witan-code's bundled skill, when run from the workspace checkout.
_WITAN_CODE_SKILL = (
    _PKG.parent / "witan-code" / "witan_code" / "skills" / "witan-code" / "SKILL.md"
)
_ALL = _SKILLS + ([_WITAN_CODE_SKILL] if _WITAN_CODE_SKILL.exists() else [])

_ASKING_HEADING = "## Asking the user"


def _sections(text: str) -> list[tuple[str, str]]:
    """``(heading, body)`` for each ``## `` section (preamble heading is "")."""
    parts = re.split(r"(?m)^(## .*)$", text)
    out = [("", parts[0])]
    out += [(parts[i].strip(), parts[i + 1]) for i in range(1, len(parts), 2)]
    return out


def test_the_bundled_skills_were_found():
    names = {p.parent.name for p in _SKILLS}
    assert {"witan-workflow", "witan-task"} <= names


@pytest.mark.parametrize("skill", _ALL, ids=lambda p: p.parent.name)
def test_ask_user_question_is_only_named_conditionally(skill):
    for heading, body in _sections(skill.read_text()):
        if "AskUserQuestion" not in body:
            continue
        assert heading == _ASKING_HEADING, (
            f"{skill.parent.name}: AskUserQuestion named outside the "
            f"capability-scoped '{_ASKING_HEADING}' section (in {heading!r})"
        )
        assert "actually available" in body
        assert "Otherwise" in body and "wait" in body


@pytest.mark.parametrize("skill", _ALL, ids=lambda p: p.parent.name)
def test_no_fabricated_other_option(skill):
    """Claude's AskUserQuestion adds "Other" itself; telling the agent to use
    (or add) an "Other" option is Claude-only and misleads everywhere else."""
    assert not re.search(r"""use ["“]Other["”]""", skill.read_text()), skill


@pytest.mark.parametrize("name", ["witan-workflow", "witan-task"])
def test_interactive_skills_keep_their_confirmation_gates(name):
    text = (_PKG / "witan" / "skills" / name / "SKILL.md").read_text()
    assert _ASKING_HEADING in text
    # The "never act before the user answers" rule is stated once for the skill…
    assert "happens until the user has answered" in text
    # …and at the step where acting early would do the damage.
    gate = {
        "witan-workflow": "do not start a session or\ncreate a project on a guess",
        "witan-task": "Do not claim anything until the user has picked a\n  task",
    }[name]
    assert gate in text
