"""Allow ``python -m robothor.cli`` to invoke the public CLI."""

from robothor.cli import main

raise SystemExit(main())
