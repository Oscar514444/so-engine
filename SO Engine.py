"""Backward-compatible launcher for the packaged SO Engine application."""

from so_engine.app import main


if __name__ == "__main__":
    raise SystemExit(main())
