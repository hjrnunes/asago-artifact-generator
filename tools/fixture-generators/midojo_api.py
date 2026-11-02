"""Resolve MiDojo's spec and evaluation-context classes at the revision a fixture names.

Each generator runs with the MiDojo revision its ``recording.json`` names, and those
revisions predate the injection-spec rename. The spec class and the evaluation context
keep one name per revision: ``midojo.injection_spec.InjectionSpec`` and
``midojo.evaluation_context.EvaluationContext`` from 8f8976f, ``midojo.episode.Episode``
and ``midojo.episode_context.EpisodeContext`` before it. MiDojo's ``open_evaluation``
and its pre-rename spelling are passthrough factories for the context class, so
``open_context`` constructs the class directly and runs at either revision.
"""

from __future__ import annotations

from typing import Any

try:  # MiDojo 8f8976f and later
    from midojo.evaluation_context import EvaluationContext as _Context
    from midojo.injection_spec import InjectionSpec, thaw
except ModuleNotFoundError as error:  # the revision a recorded fixture names
    if error.name not in {"midojo.evaluation_context", "midojo.injection_spec"}:
        raise
    from midojo.episode import Episode as InjectionSpec
    from midojo.episode import thaw
    from midojo.episode_context import EpisodeContext as _Context

__all__ = ["InjectionSpec", "open_context", "thaw"]


def open_context(spec: InjectionSpec, **kwargs: Any) -> Any:
    """A context that runs ``spec`` once, as ``midojo.open_evaluation`` returns one."""
    return _Context(spec, **kwargs)
