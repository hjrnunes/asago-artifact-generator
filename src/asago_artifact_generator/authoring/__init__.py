"""Target-free two-call authoring orchestration.

The model owns experiment meaning and artifact content.  This module owns the
small mechanical surface around that response: deterministic prompt views,
closed structural checks, request accounting, and immutable package assembly.
It never contacts a target, setup transport, discovery service, or judge.
"""
