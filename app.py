"""Streamlit entry point for the Failure Synthesis System (FSS).

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

from query_app import main

if __name__ == "__main__":
    main()
