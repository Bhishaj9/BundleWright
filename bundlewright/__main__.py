"""Allow `python -m bundlewright ...` with no installation."""

from .cli import main

raise SystemExit(main())
