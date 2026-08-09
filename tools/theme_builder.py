"""Launcher for the theme builder, so it runs from a checkout with nothing installed.

    python tools/theme_builder.py

The app itself lives in agent/deckbuilder because it is built on deckhost's config, protocol
and asset code and imports them as a package. This file only puts agent/ on the path first, the
same way make_assets.py and protocol_test.py do.

Deliberately not named deckbuilder.py: tools/ goes on sys.path too, and a module with the same
name as the package shadows it — importing `deckbuilder.app` then fails with "not a package",
from a file whose only job was to import it.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "agent"))

from deckbuilder.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
