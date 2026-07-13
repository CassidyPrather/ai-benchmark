"""Allow running ai-benchmark as a module: python -m ai_benchmark."""

import sys

from ai_benchmark.cli import main

if __name__ == "__main__":
    sys.exit(main())
