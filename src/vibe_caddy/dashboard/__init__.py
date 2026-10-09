"""The vibe-caddy web dashboard, served at https://vibe.vc.localhost."""

from __future__ import annotations

from .app import create_app, serve

__all__ = ["create_app", "serve"]
