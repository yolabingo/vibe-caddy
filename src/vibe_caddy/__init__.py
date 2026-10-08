"""vibe-caddy: friendly ``https://<name>.localhost`` names for local dev servers.

Routing and TLS are delegated to Caddy, process supervision to launchd, and name
resolution to the operating system's built-in handling of the ``.localhost`` TLD.
This package contributes only the registry and the config generators that wire
those three together.
"""

__version__ = "0.1.0"
