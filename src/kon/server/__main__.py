"""Entry point for ``python -m kon.server`` and ``kon-serve`` command."""

from __future__ import annotations

import argparse
import logging
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Kon fleet-compatible server")
    parser.add_argument("--port", "-p", type=int, default=int(os.environ.get("KON_PORT", "8080")))
    parser.add_argument("--host", type=str, default=os.environ.get("KON_HOST", "0.0.0.0"))
    parser.add_argument("--workspace", "-w", type=str, default=None,
                        help="Working directory for the agent (default: cwd)")
    parser.add_argument("--model", "-m", type=str, default=None,
                        help="Model to use (e.g. anthropic/claude-sonnet-4-6)")
    parser.add_argument("--system-prompt", type=str, default=None,
                        help="Path to system prompt file")
    args = parser.parse_args()

    # Set env vars before importing the app (which creates ServerState)
    if args.workspace:
        os.environ["KON_WORKSPACE"] = args.workspace
    if args.model:
        if "/" in args.model:
            parts = args.model.split("/", 1)
            os.environ["KON_PROVIDER"] = parts[0]
            os.environ["KON_MODEL"] = parts[1]
        else:
            os.environ["KON_MODEL"] = args.model
    if args.system_prompt:
        from pathlib import Path
        prompt_path = Path(args.system_prompt)
        if prompt_path.exists():
            os.environ["KON_SYSTEM_PROMPT"] = prompt_path.read_text()
        else:
            print(f"Warning: system prompt file not found: {args.system_prompt}", file=sys.stderr)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    import uvicorn
    uvicorn.run(
        "kon.server.app:app",
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
