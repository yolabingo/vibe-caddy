"""The ``vibe-caddy`` command line.

The CLI is stateless: it reads and writes ``$XDG_DATA_HOME/vibe-caddy/registry.json``,
regenerates the Caddyfile, and talks to launchd and to Caddy's admin API. Nothing
here stays resident, so there is no daemon of our own to crash, restart or
version-skew against the registry on disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import __version__, doctor, paths, provision, service
from .errors import VibeError
from .models import Route
from .progress import Step

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    name="vibe-caddy",
    help="Friendly https://<name>.localhost names for local dev servers.",
    add_completion=True,
    rich_markup_mode="rich",
)
# Help panels. Twenty commands in one flat list is unreadable, so `--help`
# groups them by the question the user is asking.
PANEL_APPS = "Running apps"
PANEL_ROUTES = "Managing routes"
PANEL_INSPECT = "Inspecting"
PANEL_PROXY = "Proxy and dashboard"
PANEL_SYSTEM = "Installation and diagnostics"

caddy_app = typer.Typer(help="Control the Caddy LaunchDaemon.", no_args_is_help=True)
app.add_typer(caddy_app, name="caddy", rich_help_panel=PANEL_PROXY)


def _print_error(exc: VibeError) -> None:
    """Render a VibeError the way the user should read it."""
    err_console.print(f"[bold red]error[/] {exc}")
    if exc.hint:
        err_console.print(f"[dim]hint:[/] {exc.hint}")


def fail(exc: VibeError) -> None:
    """Print a VibeError and exit non-zero from inside a command."""
    _print_error(exc)
    raise typer.Exit(1)


def run() -> None:
    """Entry point: run the app, and never let a VibeError reach the terminal raw.

    Commands that can fail catch their own errors so they can add context, but a
    `VibeError` escaping any of them is still a message meant for the user, not a
    traceback. This is the backstop -- `vibe-caddy caddy restart` as a non-root
    user used to print a full traceback where every other command printed one
    clean line.
    """
    try:
        app()
    except VibeError as exc:
        _print_error(exc)
        raise SystemExit(1) from None


STATE_STYLE = {
    "ready": "green",
    "up": "green",
    "starting": "yellow",
    "crashed": "red",
    "stopped": "dim",
    "down": "red",
}


# How each progress step reads on screen. The column widths line the details up; they
# differ per kind because the labels do.
STEP_FORMATS = {
    "detected": "detected  [bold]{}[/]",
    "scaffolded": "wrote     {}",
    "wrote": "wrote    {}",
    "note": "[yellow]note[/]      {}",
    "migrated": "migrated {}",
    "loaded": "loaded   {}",
    "serving": "serving  {}",
    "trusted": "trusted  {}",
    "removed": "removed  {}",
    "untrusted": "untrusted {}",
    "opening": "{}",
}


def show_step(step: Step) -> None:
    """Print one progress step as it completes."""
    console.print(STEP_FORMATS[step.kind].format(step.detail))


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[
        bool, typer.Option("--version", "-V", help="Show the version and exit.")
    ] = False,
) -> None:
    if version:
        console.print(f"vibe-caddy {__version__}")
        raise typer.Exit
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit


# ------------------------------------------------------------------ listing


@app.command("list", rich_help_panel=PANEL_INSPECT)
def list_routes() -> None:
    """List every registered route and its live state."""
    statuses = service.statuses()
    if not statuses:
        console.print("[dim]no routes registered.[/] Create one with: vibe-caddy init")
        return

    table = Table(box=None, pad_edge=False, header_style="bold")
    for column in ("NAME", "URL", "PORT", "TYPE", "STATE", "PID"):
        table.add_column(column, justify="right" if column in ("PORT", "PID") else "left")

    for status in statuses:
        route = status.route
        label = f"  └ {route.name}" if route.parent else route.name
        table.add_row(
            label,
            route.href,
            str(route.port) if route.port else "-",
            route.type.value,
            Text(status.state, style=STATE_STYLE.get(status.state, "")),
            str(status.pid) if status.pid else "-",
        )
    console.print(table)


@app.command(rich_help_panel=PANEL_INSPECT)
def status() -> None:
    """Show whether Caddy is serving, and a one-line route summary."""
    report = service.overview()
    if report.foreign:
        mark = f"[red]not ours[/] ({paths.CADDY_ADMIN} held by {report.foreign})"
    else:
        mark = "[green]running[/]" if report.running else "[red]not running[/]"
    console.print(f"caddy        {mark}  ({report.caddy_version or 'not installed'})")
    console.print(f"caddyfile    {paths.caddyfile()}")
    console.print(f"registry     {paths.registry_file()}")
    console.print(f"state        {paths.state_dir()}")
    console.print(f"routes       {len(report.routes)} registered, {report.serving} serving")
    if not report.running:
        raise typer.Exit(1)


@app.command("open", rich_help_panel=PANEL_INSPECT)
def open_route(
    name: Annotated[
        str | None, typer.Argument(help="Route to open. Defaults to the dashboard.")
    ] = None,
) -> None:
    """Open a route in the default browser."""
    try:
        service.open_route(name, observe=show_step)
    except VibeError as exc:
        fail(exc)


@app.command(rich_help_panel=PANEL_INSPECT)
def logs(
    name: Annotated[str, typer.Argument(help="Route whose log to show.")],
    lines: Annotated[int, typer.Option("--lines", "-n", help="How many lines to show.")] = 50,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Stream new output.")] = False,
) -> None:
    """Show a managed route's log."""
    result = service.read_log(name, lines, follow=follow)
    if not result.found:
        err_console.print(f"[yellow]no log yet:[/] {result.path}")
        raise typer.Exit(1)
    if result.text is not None:
        console.print(result.text)


# ------------------------------------------------------------- registration


@app.command(rich_help_panel=PANEL_APPS)
def init(
    name: Annotated[
        str | None, typer.Option("--name", help="Route name. Defaults to the directory name.")
    ] = None,
    framework: Annotated[
        str | None,
        typer.Option(
            "--framework",
            "-w",
            metavar="NAME",
            help="Use a framework preset, then start the app. "
            "'auto' detects it; 'list' prints every preset.",
        ),
    ] = None,
    cmd: Annotated[
        str | None, typer.Option("--cmd", help="Command that starts the dev server.")
    ] = None,
    directory: Annotated[
        Path | None,
        typer.Option(
            "--directory",
            "-d",
            metavar="PATH",
            help="Project to set up. Defaults to the current directory.",
        ),
    ] = None,
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Replace an existing vibe-caddy.toml.")
    ] = False,
) -> None:
    """Write a vibe-caddy.toml, and with --framework also start the app.

    Without --framework the project is still inspected so the generated command
    is a working one; it is written but not started, leaving the file open to
    edit. With --framework the app is registered and started immediately.
    """
    if framework == "list":
        _print_frameworks()
        return

    try:
        result = service.init_project(
            directory,
            name=name,
            framework=framework,
            cmd=cmd,
            overwrite=overwrite,
            observe=show_step,
        )
    except VibeError as exc:
        fail(exc)
    else:
        if result.route is None:
            console.print("\nreview [bold]cmd[/], then run: [bold]vibe-caddy start[/]")
            console.print(f"it will be served at [bold]{paths.url(result.name)}[/]")
        else:
            console.print(f"started   [bold]{result.route.href}[/] (port {result.route.port})")


def _print_frameworks() -> None:
    """Print every preset and the command it generates."""
    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("NAME")
    table.add_column("FRAMEWORK")
    table.add_column("COMMAND")
    for row in service.framework_rows():
        table.add_row(row.name, row.label, row.command)
    console.print(table)


@app.command(rich_help_panel=PANEL_ROUTES)
def register(
    name: Annotated[str, typer.Argument(help="Hostname label: <name>.localhost")],
    port: Annotated[int | None, typer.Argument(help="Port the app already listens on.")] = None,
    url: Annotated[
        str | None, typer.Option("--url", help="Make this a bookmark to an external URL.")
    ] = None,
    proxy: Annotated[
        bool, typer.Option("--proxy", help="Reverse-proxy the bookmark instead of redirecting.")
    ] = False,
    insecure: Annotated[
        bool, typer.Option("--insecure", help="Accept a self-signed upstream certificate.")
    ] = False,
    icon: Annotated[
        str | None, typer.Option("--icon", help="Emoji or image URL for the dashboard.")
    ] = None,
) -> None:
    """Point a name at a port you start yourself, or at an external URL."""
    if port is None and url is None:
        fail(VibeError("give a port, or --url for a bookmark"))
    try:
        route = service.register(
            name, port=port, url=url, proxy=proxy, insecure_skip_verify=insecure, icon=icon
        )
    except VibeError as exc:
        fail(exc)
    else:
        console.print(f"registered [bold]{route.name}[/] -> {route.href}")


@app.command(rich_help_panel=PANEL_ROUTES)
def deregister(
    name: Annotated[str | None, typer.Argument(help="Route to remove.")] = None,
    all_routes: Annotated[bool, typer.Option("--all", help="Remove every registered app.")] = False,
    include_dashboard: Annotated[
        bool,
        typer.Option("--include-dashboard", help="With --all, also remove vibe-caddy's dashboard."),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Remove a route, or every route with --all, stopping managed ones first."""
    if all_routes == (name is not None):
        fail(
            VibeError(
                "give a route name, or --all",
                hint="vibe-caddy deregister <name>   |   vibe-caddy deregister --all",
            )
        )

    targets: list[Route] = []
    try:
        targets = service.removal_targets(name, include_dashboard=include_dashboard)
    except VibeError as exc:
        fail(exc)

    if not targets:
        console.print("[dim]nothing to remove.[/]")
        return

    if all_routes and not yes and not _confirm_removal(targets):
        console.print("[dim]cancelled; nothing was removed.[/]")
        raise typer.Exit(1)

    try:
        removed = service.deregister_many([route.name for route in targets])
    except VibeError as exc:
        fail(exc)
    else:
        for route in removed:
            console.print(f"removed [bold]{route.name}[/]")
        if all_routes:
            console.print(f"\n{len(removed)} route{'s' if len(removed) != 1 else ''} removed.")


def _confirm_removal(targets: list[Route]) -> bool:
    """Show exactly what will go, then ask.

    Removing every route stops running apps and deletes their launchd jobs, so
    the list is printed first rather than trusting the user to remember what is
    registered.
    """
    console.print("This will remove:")
    for route in targets:
        suffix = " [dim](running)[/]" if service.status_of(route).state == "ready" else ""
        console.print(f"  {route.name}  [dim]{route.href}[/]{suffix}")
    console.print("[dim]Project files are left alone; re-register with vibe-caddy start.[/]")
    return typer.confirm("Remove these routes?")


# ------------------------------------------------------------------ lifecycle


@app.command(rich_help_panel=PANEL_APPS)
def start(
    name: Annotated[
        str | None, typer.Argument(help="Registered route. Omit to read ./vibe-caddy.toml.")
    ] = None,
    as_slug: Annotated[
        str | None, typer.Option("--as", help="Override the worktree label.")
    ] = None,
) -> None:
    """Start a managed app, registering it from vibe-caddy.toml if needed."""
    try:
        route = service.start_project(as_slug=as_slug) if name is None else service.start(name)
    except VibeError as exc:
        fail(exc)
    else:
        console.print(f"started [bold]{route.name}[/] -> {route.href} (port {route.port})")


@app.command(rich_help_panel=PANEL_APPS)
def stop(name: Annotated[str, typer.Argument(help="Route to stop.")]) -> None:
    """Stop a managed app. The route stays registered."""
    try:
        route = service.stop(name)
    except VibeError as exc:
        fail(exc)
    else:
        console.print(f"stopped [bold]{route.name}[/]")


@app.command(rich_help_panel=PANEL_APPS)
def restart(name: Annotated[str, typer.Argument(help="Route to restart.")]) -> None:
    """Restart a managed app, picking up edits to its vibe-caddy.toml."""
    try:
        route = service.restart(name)
    except VibeError as exc:
        fail(exc)
    else:
        console.print(f"restarted [bold]{route.name}[/] -> {route.href}")


@app.command(rich_help_panel=PANEL_ROUTES)
def update(
    name: Annotated[str, typer.Argument(help="Route to change.")],
    port: Annotated[
        int | None, typer.Option("--port", help="New port. 0 assigns a free one.")
    ] = None,
    cmd: Annotated[str | None, typer.Option("--cmd", help="New start command.")] = None,
    icon: Annotated[str | None, typer.Option("--icon", help="Emoji or image URL.")] = None,
    autostart: Annotated[
        bool | None, typer.Option("--autostart/--no-autostart", help="Start at login.")
    ] = None,
) -> None:
    """Change a registered route in place."""
    fields = {
        key: value
        for key, value in (
            ("port", port),
            ("cmd", cmd),
            ("icon", icon),
            ("autostart", autostart),
        )
        if value is not None
    }
    if not fields:
        fail(VibeError("nothing to change", hint="pass at least one of --port/--cmd/--icon"))
    try:
        route = service.update(name, **fields)
    except VibeError as exc:
        fail(exc)
    except ValueError as exc:
        fail(VibeError(str(exc)))
    else:
        console.print(f"updated [bold]{route.name}[/] -> {route.href} (port {route.port})")


@app.command(rich_help_panel=PANEL_ROUTES)
def prune() -> None:
    """Remove worktree routes whose git checkout has been deleted."""
    removed = service.prune_worktrees()
    if not removed:
        console.print("[dim]nothing to prune.[/]")
        return
    for name in removed:
        console.print(f"pruned [bold]{name}[/]")


# ------------------------------------------------------------------- caddy


@app.command(rich_help_panel=PANEL_PROXY)
def reload() -> None:
    """Regenerate the Caddyfile from the registry and reload Caddy."""
    try:
        service.reload_caddy()
    except VibeError as exc:
        fail(exc)
    else:
        console.print(f"reloaded {paths.caddyfile()}")


@app.command(rich_help_panel=PANEL_PROXY)
def caddyfile(
    validate: Annotated[
        bool, typer.Option("--validate", help="Validate instead of printing.")
    ] = False,
) -> None:
    """Print the Caddyfile vibe-caddy would generate from the current registry."""
    try:
        result = service.write_caddyfile(validate=validate)
    except VibeError as exc:
        fail(exc)
    else:
        if not validate:
            console.print(result.content, markup=False, highlight=False)
        else:
            console.print(f"[green]valid[/] {paths.caddyfile()}")


@caddy_app.command("start")
def caddy_start() -> None:
    """Load the Caddy LaunchDaemon. Needs sudo."""
    try:
        up = provision.start_daemon()
    except VibeError as exc:
        fail(exc)
    else:
        console.print("caddy daemon loaded" if up else "caddy did not come up")


@caddy_app.command("stop")
def caddy_stop() -> None:
    """Unload the Caddy LaunchDaemon. Needs sudo."""
    try:
        provision.stop_daemon()
    except VibeError as exc:
        fail(exc)
    else:
        console.print("caddy daemon unloaded")


@caddy_app.command("restart")
def caddy_restart() -> None:
    """Restart the Caddy LaunchDaemon in place. Needs sudo."""
    up = provision.restart_daemon()
    console.print("caddy daemon restarted" if up else "caddy did not come up")


# ------------------------------------------------------------- setup/doctor


@app.command(rich_help_panel=PANEL_SYSTEM)
def setup(
    skip_trust: Annotated[
        bool, typer.Option("--no-trust", help="Do not touch the System keychain.")
    ] = False,
) -> None:
    """Install the Caddy LaunchDaemon and trust its CA. Needs sudo."""
    try:
        result = provision.setup(skip_trust=skip_trust, observe=show_step)
    except VibeError as exc:
        fail(exc)
    if result.refusal is not None:
        _print_refusal(result.refusal)
        raise typer.Exit(1)

    if result.dashboard_error:
        err_console.print(
            f"[yellow]warning[/] the dashboard did not start: {result.dashboard_error}"
        )
        err_console.print("[dim]hint:[/] retry it with: vibe-caddy dashboard install")
    else:
        console.print(f"dashboard {paths.url(paths.DASHBOARD_ROUTE)}")

    console.print("\n[green]ready.[/] Create an app with: [bold]vibe-caddy init[/]")


def _print_refusal(refusal: provision.Refusal) -> None:
    """Say why setup changed nothing."""
    if isinstance(refusal, provision.PortsHeld):
        err_console.print("[bold red]error[/] vibe-caddy needs ports 80 and 443, but:")
        for conflict in refusal.conflicts:
            err_console.print(f"  {conflict}")
        if refusal.hint:
            err_console.print(f"[dim]hint:[/] {refusal.hint}")
        err_console.print("[dim]nothing was changed.[/]")
    else:
        err_console.print(
            f"[bold red]error[/] {paths.CADDY_ADMIN} is held by {refusal.holder}, "
            "which vibe-caddy did not start."
        )
        err_console.print("[dim]hint:[/] stop it, then re-run setup. Nothing was changed.")


@app.command(rich_help_panel=PANEL_SYSTEM)
def uninstall() -> None:
    """Remove the Caddy LaunchDaemon and untrust its CA. Needs sudo."""
    try:
        provision.uninstall(observe=show_step)
    except VibeError as exc:
        fail(exc)
    else:
        console.print(
            f"\n[dim]left in place:[/] {paths.data_dir()} (registry, certificate authority)"
        )
        console.print(f"[dim]left in place:[/] {paths.state_dir()} (generated config, logs)")


@app.command("doctor", rich_help_panel=PANEL_SYSTEM)
def doctor_command() -> None:
    """Diagnose DNS, listeners, the daemon, certificates and every route."""
    checks = doctor.run()
    marks = {
        doctor.Level.OK: "[green]ok  [/]",
        doctor.Level.WARN: "[yellow]warn[/]",
        doctor.Level.FAIL: "[red]fail[/]",
    }
    for check in checks:
        console.print(f"{marks[check.level]} {check.name:<22} {check.detail}")
        if check.fix:
            console.print(f"     [dim]fix:[/] {check.fix}")
    if not all(check.healthy for check in checks):
        raise typer.Exit(1)


# ---------------------------------------------------------------- dashboard


dashboard_app = typer.Typer(help="The web dashboard.", no_args_is_help=True)
app.add_typer(dashboard_app, name="dashboard", rich_help_panel=PANEL_PROXY)


@dashboard_app.command("serve")
def dashboard_serve(
    port: Annotated[int, typer.Option("--port", help="Port to listen on.")] = 7999,
    host: Annotated[str, typer.Option("--host", help="Address to bind.")] = "127.0.0.1",
) -> None:
    """Run the dashboard in the foreground. Used by the launchd job."""
    from .dashboard import serve

    serve(host=host, port=port)


@dashboard_app.command("install")
def dashboard_install() -> None:
    """Register the dashboard as a managed route at https://vibe.localhost."""
    try:
        provision.install_dashboard()
    except VibeError as exc:
        fail(exc)
    else:
        console.print(f"dashboard at [bold]{paths.url(paths.DASHBOARD_ROUTE)}[/]")


if __name__ == "__main__":
    app()
