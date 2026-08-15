"""Import shim: pulls config and db from Block 1.

Each block is its own folder, but the tunables and the schema are shared.
Use `from _shared import config, db` rather than repeating the path dance.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "block-1-setup"))

import config  # noqa: E402
import db  # noqa: E402

__all__ = ["config", "db"]
