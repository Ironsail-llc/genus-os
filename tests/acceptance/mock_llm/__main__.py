"""Serve the mock model server: ``python -m tests.acceptance.mock_llm --port 11434``."""

from __future__ import annotations

import argparse


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11434)
    args = parser.parse_args()

    uvicorn.run(
        "tests.acceptance.mock_llm.app:app", host=args.host, port=args.port, log_level="warning"
    )


if __name__ == "__main__":
    main()
