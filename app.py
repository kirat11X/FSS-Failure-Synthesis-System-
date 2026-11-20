"""Streamlit entry point for the Failure Synthesis System (FSS).

Modernized to use the Java BlockStore and Python Qwen RAG pipeline.
Delegates to the complete 8-part FSS investigation dashboard.

Usage:
    streamlit run app.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure project root is in sys.path
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Ensure local proxy compatibility: set NO_PROXY for local communication
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

# Protobuf compatibility with Python 3.14 (bypass C extension tp_new incompatibility)
sys.modules.setdefault("google._upb._message", None)
sys.modules.setdefault("google.protobuf.pyext._message", None)

from streamlit_app import main

if __name__ == "__main__":
    main()
