#!/usr/bin/env python3
"""SmartChargeTesla — start the dispatch control service.

Usage:
  python run.py                          # reads config.yaml in current directory
  python run.py --config /path/to/config.yaml
"""

import asyncio
import argparse
import logging
import sys
from pathlib import Path

import yaml


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        print(f"ERROR: config file not found: {path}")
        print("Copy config.example.yaml to config.yaml and fill in your credentials.")
        sys.exit(1)
    with open(p) as f:
        return yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser(description="SmartChargeTesla dispatch controller")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    args = parser.parse_args()

    setup_logging()
    log = logging.getLogger("smartcharge")
    log.info("SmartChargeTesla starting")

    cfg = load_config(args.config)

    from smartcharge.service import run
    asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
