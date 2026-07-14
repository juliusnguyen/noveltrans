"""Frozen-app entry point (PyInstaller needs a script, not a module)."""

from noveltrans.app import main

raise SystemExit(main())
