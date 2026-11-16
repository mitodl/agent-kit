"""Prompt-injection screening of record text (tk-screen-untrusted-content-returned-by-witan-tools-043ff5).

Screening runs server-side at write time, as a guard at ADR 0001's
``change()`` choke point, behind a classifier slot a deployment fills with a
provider and model (:mod:`.classifier`). The verdict and the classifier version
are stored on the record, and reads attach them (:mod:`.label`). witan ships no
classifier, so until one is configured every record reads as unscreened.
"""

from .classifier import ENTRY_POINT_GROUP, Classifier, load_classifier
from .guard import (
    SCREEN_FIELDS,
    SCREENED_QUERIES,
    UNSCREENED,
    Screener,
    compose_guards,
    payloads,
    screener_from_config,
    text_digest,
)
from .label import UNSCREENED_NOTICE, attach, screening
from .prompt import PROMPT_VERSION, SYSTEM_PROMPT
from .verdict import Verdict, parse_verdict

__all__ = [
    "ENTRY_POINT_GROUP",
    "PROMPT_VERSION",
    "SCREENED_QUERIES",
    "SCREEN_FIELDS",
    "SYSTEM_PROMPT",
    "UNSCREENED",
    "UNSCREENED_NOTICE",
    "Classifier",
    "Screener",
    "Verdict",
    "attach",
    "compose_guards",
    "load_classifier",
    "parse_verdict",
    "payloads",
    "screener_from_config",
    "screening",
    "text_digest",
]
