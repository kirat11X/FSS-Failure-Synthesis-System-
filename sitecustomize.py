import os
import sys
from pathlib import Path

# Fix for protobuf C extension tp_new incompatibility on Python 3.14
sys.modules.setdefault("google._upb._message", None)
sys.modules.setdefault("google.protobuf.pyext._message", None)

# Add uv cache paths at the beginning of sys.path, excluding broken C-extension archives
uv_archives = Path("/home/kirat/.cache/uv/archive-v0")
if uv_archives.is_dir():
    for d in sorted(uv_archives.iterdir(), reverse=True):
        if d.is_dir() and str(d) not in sys.path:
            # Skip incompatible pre-compiled numpy/pyarrow archives
            has_incompatible = any(
                f.name.startswith("numpy") or f.name.startswith("pyarrow")
                for f in d.iterdir()
            )
            if not has_incompatible:
                sys.path.insert(0, str(d))
