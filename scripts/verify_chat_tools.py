"""Execute independent known-number analyst tests without optional pytest."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_chat_tools.py")
result = unittest.TextTestRunner(verbosity=2).run(suite)
report = {"framework": "unittest", "tests_run": result.testsRun,
          "failures": len(result.failures), "errors": len(result.errors), "passed": result.wasSuccessful(),
          "network_requested": False, "data": "explicit synthetic arrays with known exact answers; no ML fitting",
          "checks": ["3h window and exclusive end", "time zone conversion", "ramp arithmetic",
                     "partial calendar days", "first/second 24h blocks", "normalized fleet comparison",
                     "live and archive warning separation", "invalid modes", "answer mode propagation",
                     "live projection excludes unconfirmed physical units"]}
(ROOT / "reports/chat_tools_verification.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
sys.exit(0 if result.wasSuccessful() else 1)
