#!/usr/bin/env python3
"""Write a verified, timestamped snapshot of the Corpus SQLite database."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from corpus.backup import main

raise SystemExit(main())
