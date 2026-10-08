"""Garak adapter for authored artifact packages.

The adapter turns a package into a Garak bundle and Garak's report into an
``execution-receipt-v1``. It imports no Garak code: Garak runs in its own
interpreter from the bundle's entrypoint.
"""
