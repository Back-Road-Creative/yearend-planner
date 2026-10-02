"""``planner <command>``. Every page action gets a CLI twin here."""

from __future__ import annotations

import sys

import typer

from planner import __version__
from planner.paths import CloudSyncedPathError, layout

app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.command()
def version() -> None:
    """Print the planner version."""
    typer.echo(f"planner {__version__}")


@app.command()
def paths() -> None:
    """Show where this planner keeps its folders, creating data/ and out/."""
    lay = layout()
    try:
        lay.ensure()
    except CloudSyncedPathError as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"root   {lay.root}")
    typer.echo(f"data   {lay.data}")
    typer.echo(f"out    {lay.out}")
    typer.echo(f"config {lay.config}")


@app.command()
def selfcheck() -> None:
    """Run one real federal calculation through the tax engine and print it."""
    from planner.engine.selfcheck import run

    result = run()
    typer.echo(
        f"policyengine-us {result.engine_version}: single, NC, "
        f"{result.year} wages ${result.wages:,} -> federal income tax "
        f"${result.income_tax:,.2f}"
    )
    if not result.income_tax > 0:
        typer.echo("selfcheck failed: tax is not positive", err=True)
        raise typer.Exit(code=1)


@app.command()
def check_config() -> None:
    """Load config/ and report; exit non-zero on a malformed file."""
    from planner.config import load_assumptions, load_capabilities, load_thresholds

    lay = layout()
    a = load_assumptions(lay.config / "assumptions.example.yaml")
    t = load_thresholds(lay.config / "thresholds.yaml")
    c = load_capabilities(lay.config / "capabilities.yaml")
    typer.echo(
        f"assumptions: {len(a)} fields; thresholds: years {sorted(t)}; "
        f"capabilities: {len(c)}"
    )


def main() -> int:
    app()
    return 0


if __name__ == "__main__":
    sys.exit(main())
