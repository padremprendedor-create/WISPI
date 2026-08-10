"""Entrypoint: uv run python -m wispi [--console] [--log-level DEBUG]"""
from .app import main

raise SystemExit(main())
