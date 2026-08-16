"""Import shim: pulls config and db from Block 1, and the universe from Block 2.

Block 2's own `_shared` is shadowed by this one (same module name, and this
folder comes first on `sys.path`), so `config` and `db` are bound here *before*
`universe` is imported anywhere. Import `universe` from the caller, not from
this file, or that ordering becomes circular.
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
import links  # noqa: E402

__all__ = ["config", "db", "links"]
