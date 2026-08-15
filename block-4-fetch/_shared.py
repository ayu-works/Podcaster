"""Import shim: Block 1's config and db, plus Block 2's API client.

Block 2's own `_shared` is shadowed by this one (same module name, this folder
first on `sys.path`), so `config` is bound here before `podcastindex` imports
it. Import `podcastindex` from the caller, not from this file.
"""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent

for _folder in ("block-2-universe", "block-1-setup"):
    _path = str(_ROOT / _folder)
    if _path not in sys.path:
        sys.path.insert(0, _path)

import config  # noqa: E402
import db  # noqa: E402

__all__ = ["config", "db"]
