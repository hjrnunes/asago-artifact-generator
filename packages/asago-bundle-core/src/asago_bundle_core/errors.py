"""The error base every adapter's own errors extend."""

from __future__ import annotations


class BundleError(ValueError):
    """An adapter step cannot use its input."""
