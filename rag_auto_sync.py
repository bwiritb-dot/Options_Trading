#!/usr/bin/env python3
"""
LightRAG Auto-Sync — File Watcher
===================================
Monitors project files for changes and automatically syncs them to LightRAG.
Usage:
    python rag_auto_sync.py                 # watch with defaults (check every 60s)
    python rag_auto_sync.py --interval 30   # check every 30 seconds
    python rag_auto_sync.py --debug         # show detailed logs
"""

import os
import sys
import argparse
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict
import json
import subprocess

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Files to watch (organized by category)
FILES_TO_WATCH = {
    "Documentation": [
        "IMPLEMENTATION_GUIDE.md",
        "Instructions_Ideas.txt",
        "MODIFICATION_LIST_V2.md",
        "RESEARCH_V2_DESIGN.md",
        "SYSTEM_PROMPT_V2.md",
        "SUMMARY_RESEARCH_COMPLETE.md",
    ],
    "Core Implementation": [
        "optimizer.py",
        "optimizer_step1.py",
        "optimizer_step2.py",
        "optimizer_step3.py",
        "optimizer_step4.py",
        "optimizer_step5.py",
    ],
    "Utilities": [
        "optimizer_chart.py",
    ],
}

# State file to track file modification times
STATE_FILE = ".claude/.rag_sync_state.json"

def get_file_hash(filepath: Path) -> str:
    """Get file modification time as a simple change detector."""
    try:
        return str(os.path.getmtime(filepath))
    except OSError:
        return ""

def load_state() -> Dict[str, str]:
    """Load previous file states from disk."""
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Could not load state file: {e}")
    return {}

def save_state(state: Dict[str, str]) -> None:
    """Save current file states to disk."""
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.error(f"Could not save state file: {e}")

def upload_file_to_lightrag(filepath: Path) -> bool:
    """Upload a file to LightRAG via HTTP API."""
    try:
        import requests

        # LightRAG upload endpoint
        url = "http://localhost:9621/v1/documents/upload"

        with open(filepath, 'rb') as f:
            files = {'file': f}
            response = requests.post(url, files=files, timeout=30)

        if response.status_code == 200:
            logger.info(f"✓ Synced: {filepath.name}")
            return True
        else:
            logger.error(f"✗ Failed to sync {filepath.name}: {response.status_code}")
            return False

    except ImportError:
        logger.error("requests library not installed. Install with: pip install requests")
        return False
    except Exception as e:
        logger.error(f"✗ Error syncing {filepath.name}: {e}")
        return False

def check_and_sync(verbose: bool = False) -> int:
    """Check for file changes and sync to LightRAG."""
    previous_state = load_state()
    current_state = {}
    changed_count = 0

    for category, files in FILES_TO_WATCH.items():
        for filename in files:
            filepath = Path(filename)

            if not filepath.exists():
                if verbose:
                    logger.debug(f"File not found: {filename}")
                continue

            current_hash = get_file_hash(filepath)
            current_state[filename] = current_hash

            previous_hash = previous_state.get(filename)

            if previous_hash != current_hash:
                if verbose:
                    logger.info(f"Change detected in {category}/{filename}")

                if upload_file_to_lightrag(filepath):
                    changed_count += 1
                else:
                    logger.warning(f"Failed to upload {filename}, will retry next cycle")

    # Update state file
    save_state(current_state)

    if changed_count > 0:
        logger.info(f"Synced {changed_count} file(s) to LightRAG")

    return changed_count

def main():
    parser = argparse.ArgumentParser(
        description="Auto-sync project files to LightRAG"
    )
    parser.add_argument(
        '--interval',
        type=int,
        default=60,
        help='Check interval in seconds (default: 60)'
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Show detailed debug logs'
    )
    parser.add_argument(
        '--once',
        action='store_true',
        help='Run once and exit (for cron jobs)'
    )

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    logger.info("LightRAG Auto-Sync started")
    logger.info(f"Check interval: {args.interval}s")
    logger.info(f"Watching {sum(len(f) for f in FILES_TO_WATCH.values())} files")

    if args.once:
        # Single run mode (for scheduled tasks)
        check_and_sync(verbose=args.debug)
        return 0

    # Continuous watch mode
    try:
        import time
        while True:
            check_and_sync(verbose=args.debug)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("Auto-sync stopped")
        return 0
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
