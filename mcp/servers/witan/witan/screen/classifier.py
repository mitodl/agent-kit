"""The pluggable classifier slot a deployment fills with a provider and model.

witan ships no implementation. A provider package registers a factory under
the :data:`ENTRY_POINT_GROUP` entry point (or a deployment names one by dotted
path), and ``[screen]`` in the server's config names the provider and the model
it should call. The factory is called once, at server import, as
``factory(model=<model>)``.

An implementation returns the model's RAW text. It does not parse it: witan
parses every output with :func:`~.verdict.parse_verdict`, so no provider can
return a permissive verdict except by the model actually writing
``{"decision":"clean"}``.
"""

from __future__ import annotations

import pkgutil
from importlib import metadata
from typing import Protocol, runtime_checkable

from ..config import ScreenConfig

ENTRY_POINT_GROUP = "witan.classifiers"


@runtime_checkable
class Classifier(Protocol):
    """A prompt-injection classifier backed by some provider's model."""

    version: str
    """Names what produced a verdict, e.g. ``"<provider>/<model>"``. Stored
    with every verdict, so it must change whenever the verdicts could: a new
    model, a new provider, or a provider-side setting that changes output."""

    def classify(self, system_prompt: str, payload: str) -> str:
        """Run the model and return its raw output.

        :param system_prompt: :data:`~.prompt.SYSTEM_PROMPT`.
        :param payload: The JSON parts to classify, at most
            :data:`~.guard.MAX_PAYLOAD_CHARS` long.
        :returns: The model's text, unparsed.
        :rtype: str
        """
        ...


def load_classifier(config: ScreenConfig) -> Classifier | None:
    """Build the configured classifier, or ``None`` when none is configured.

    A configured provider that cannot be loaded raises at startup: a
    deployment that asked for screening must not start without it and mark
    every write unscreened in silence.
    """
    if config.provider is None:
        return None
    factory = _provider_factory(config.provider)
    classifier = factory(model=config.model)
    if not isinstance(classifier, Classifier):
        raise TypeError(
            f"Screening provider {config.provider!r} did not return a Classifier "
            "(needs a `version` and a `classify(system_prompt, payload)`)."
        )
    return classifier


def _provider_factory(provider: str):
    if ":" in provider:
        return pkgutil.resolve_name(provider)
    for ep in metadata.entry_points(group=ENTRY_POINT_GROUP, name=provider):
        return ep.load()
    raise ValueError(
        f"No screening provider named {provider!r}: install a package that "
        f"registers it under the {ENTRY_POINT_GROUP!r} entry point, or give a "
        "'module.path:Attr' dotted path."
    )
