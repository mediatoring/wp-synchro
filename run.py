#!/usr/bin/env python3
"""
WP Synchro — entry point.

Usage:
    python run.py --config configs/mysite.yaml [--port 8765]
"""
import argparse
import os
import sys
from pathlib import Path

# Make `src` importable
sys.path.insert(0, str(Path(__file__).parent))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="WP Synchro — local WordPress-to-WordPress incremental sync tool"
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to YAML config file (see configs/example.yaml)",
    )
    parser.add_argument(
        "--port", type=int, default=8765, help="Web UI port (default: 8765)"
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--reload", action="store_true", help="Enable auto-reload (dev mode)"
    )
    args = parser.parse_args()

    cfg = args.config or os.environ.get("WP_SYNCHRO_CONFIG", "configs/example.yaml")
    config_path = str(Path(cfg).resolve())
    if not Path(config_path).exists():
        print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
        print("       Copy configs/example.yaml to configs/mysite.yaml and fill in your values.", file=sys.stderr)
        sys.exit(1)

    os.environ["WP_SYNCHRO_CONFIG"] = config_path

    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        import uvicorn
    except ImportError:
        print("ERROR: uvicorn not installed. Run:  pip install -r requirements.txt", file=sys.stderr)
        sys.exit(1)

    print()
    print("=" * 60)
    print("  WP Synchro")
    print(f"  Config : {config_path}")
    print(f"  UI     : http://{args.host}:{args.port}")
    print("  NOTE   : VPN must be active for old-server access")
    print("=" * 60)
    print()

    uvicorn.run(
        "src.web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
