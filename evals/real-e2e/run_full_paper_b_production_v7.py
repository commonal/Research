from __future__ import annotations

"""Fresh target replay after the generic PaperModel-driven prompt audit.

The production runner is reused unchanged; only the durable output directory
is redirected so v5 and v6 remain historical evidence.
"""

from pathlib import Path
import sys

import run_full_paper_b_production_v1 as base


OUTPUT = Path(__file__).resolve().parent / "full-paper-b-production-v7"


def main() -> int:
    base.OUTPUT = OUTPUT
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())
