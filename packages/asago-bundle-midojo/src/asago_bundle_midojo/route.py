"""Routes: the client that drives one run of an episode.

An episode is the same whatever drives it; the route is a choice per run, and the
receipt records it (``episode.route``). MiDojo's own route, its attacker loop over
``midojo.open_episode``, is the one that exists. A PyRIT target or a Garak generator
over the same episode API would each be one more name here, recorded the same way.
"""

from __future__ import annotations

MIDOJO = "midojo"
ROUTES = (MIDOJO,)
DEFAULT_ROUTE = MIDOJO
