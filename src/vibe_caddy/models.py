"""Typed shapes for the registry and for project config files."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from . import paths

Port = Annotated[int, Field(ge=1, le=65535)]
#: A port in a project config, where 0 is the documented way to ask for one to be
#: assigned. The registry only ever stores a resolved :data:`Port`.
PortOrAuto = Annotated[int, Field(ge=0, le=65535)]


class RouteType(StrEnum):
    #: vibe owns the process via launchd.
    MANAGED = "managed"
    #: A managed route for a linked git worktree of another managed route.
    WORKTREE = "worktree"
    #: A name pointed at a port someone else is responsible for starting.
    STATIC = "static"
    #: A name pointed at an external URL, by redirect or by proxy.
    BOOKMARK = "bookmark"


class Route(BaseModel):
    """One entry in the registry; one site block in the generated Caddyfile."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: RouteType
    port: Port | None = None
    cmd: str | None = None
    dir: str | None = None
    parent: str | None = None
    url: HttpUrl | None = None
    proxy: bool = False
    insecure_skip_verify: bool = False
    icon: str | None = None
    #: Framework preset this route was created from, if any. Only used to pick
    #: the logo shown on the dashboard.
    framework: str | None = None
    reserve_ports: dict[str, Port] = Field(default_factory=dict)
    #: Rewrite ``Origin`` on WebSocket upgrades to the upstream's own origin.
    #: Dev servers with cross-origin guards (Next.js HMR, Vite) refuse the socket
    #: otherwise and fall into a reload loop.
    ws_origin_rewrite: bool = True
    #: Symlink the plist into ``~/Library/LaunchAgents`` so the app starts at login.
    autostart: bool = False
    created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))

    @model_validator(mode="after")
    def _check_shape(self) -> Route:
        if self.type is RouteType.BOOKMARK:
            if self.url is None:
                raise ValueError("bookmark routes require a url")
        elif self.port is None:
            raise ValueError(f"{self.type} routes require a port")

        if self.type in (RouteType.MANAGED, RouteType.WORKTREE) and not self.cmd:
            raise ValueError(f"{self.type} routes require a cmd")
        # Without a directory the route persists fine and then cannot be started:
        # build_plist has nowhere to run the command.
        if self.type in (RouteType.MANAGED, RouteType.WORKTREE) and not self.dir:
            raise ValueError(f"{self.type} routes require a dir")
        if self.proxy and self.url is None:
            raise ValueError("proxy requires a url")
        return self

    @property
    def managed(self) -> bool:
        """True when launchd supervises a process for this route."""
        return self.type in (RouteType.MANAGED, RouteType.WORKTREE)

    @property
    def hostname(self) -> str:
        return paths.hostname(self.name)

    @property
    def href(self) -> str:
        """Where the dashboard should link. Bookmarks keep their own URL visible
        only when they redirect; proxied bookmarks stay on the vibe hostname."""
        if self.type is RouteType.BOOKMARK and not self.proxy:
            return str(self.url)
        return paths.url(self.name)

    def all_ports(self) -> set[int]:
        """Every port this route lays claim to, for collision checks."""
        claimed = set(self.reserve_ports.values())
        if self.port is not None:
            claimed.add(self.port)
        return claimed


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    view: Literal["list", "grid"] = "grid"


class RegistryData(BaseModel):
    """On-disk shape of ``$XDG_DATA_HOME/vibe-caddy/registry.json``."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    routes: dict[str, Route] = Field(default_factory=dict)
    preferences: Preferences = Field(default_factory=Preferences)


class ProjectConfig(BaseModel):
    """Parsed ``vibe-caddy.toml`` from a project directory."""

    model_config = ConfigDict(extra="forbid")

    name: str
    cmd: str
    port: Port | None = None
    icon: str | None = None
    framework: str | None = None
    autostart: bool = False
    ws_origin_rewrite: bool = True
    #: Extra ports the command binds, exported as ``$PORT_<UPPERCASE_KEY>``.
    #: ``0`` or an omitted value means "assign one for me".
    reserve_ports: dict[str, PortOrAuto | None] = Field(default_factory=dict)
