import os
import sys
from pathlib import Path

SOLUTION = Path(__file__).resolve().parents[1]
REPO = SOLUTION.parent
sys.path.insert(0, str(SOLUTION))
os.environ.setdefault("LEDGER_PII_KEY", "test-only-key")
