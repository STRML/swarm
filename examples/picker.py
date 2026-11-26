#!/usr/bin/env python3
"""Example quota picker for `swarm spawn`.

`swarm spawn` runs the `picker:` command from ~/.config/swarm/config.yaml and parses the last
line of stdout as a JSON object mapping seat names to headroom scores:

  - float / int: higher score wins inside the role's intelligence band
  - "throttled": seat is skipped
  - null:        seat falls back to `unknown_score` in config.yaml

Replace `read_utilization()` below with calls to your own plan/usage tracker.
"""
import json
import os
import sys


def read_utilization():
    """Return estimated % remaining before reset for each configured seat."""
    # Example static/env-driven scores; swap in real quota API or cache reads.
    return {
        "claude": float(os.environ.get("PICKER_CLAUDE_SCORE", "15.0")),
        "codex": float(os.environ.get("PICKER_CODEX_SCORE", "8.5")),
        "omp": float(os.environ.get("PICKER_OMP_SCORE", "5.0")),
        "agy": None,
    }


def main():
    scores = read_utilization()
    print(json.dumps(scores))
    return 0


if __name__ == "__main__":
    sys.exit(main())
