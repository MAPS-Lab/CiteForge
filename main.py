"""Compatibility launcher for the installed CiteForge command."""

from __future__ import annotations

from citeforge.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
