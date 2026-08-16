"""Import shim for setup, universe and fetch blocks."""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent

for _folder in ("block-4-fetch", "block-2-universe", "block-1-setup"):
    _path = str(_ROOT / _folder)
    if _path not in sys.path:
        sys.path.insert(0, _path)

import config  # noqa: E402
import db  # noqa: E402
import links  # noqa: E402

__all__ = ["config", "db", "links"]
