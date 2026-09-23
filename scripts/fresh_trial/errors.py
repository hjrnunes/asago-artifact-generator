"""Errors raised while loading frozen authoring trial inputs."""


class TrialInputError(ValueError):
    """A frozen input, control, or rendering does not match its recorded pin."""
