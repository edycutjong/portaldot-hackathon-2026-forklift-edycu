"""Command-line entry point."""

import click

from forklift import __version__


@click.group()
@click.version_option(__version__, prog_name="forklift")
def main() -> None:
    """Lift Portaldot mainnet state onto a local fork of the official node."""


if __name__ == "__main__":
    main()
