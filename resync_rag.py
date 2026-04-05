#!/usr/bin/env python3
"""
Quick LightRAG re-sync for changed files
Usage:
    python resync_rag.py optimizer.py optimizer_step1.py  # specific files
    python resync_rag.py --all                             # all project files
"""

import sys
import subprocess
import argparse
from pathlib import Path

FILES_BY_CATEGORY = {
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

def get_all_files():
    """Get all tracked files."""
    files = []
    for category, file_list in FILES_BY_CATEGORY.items():
        files.extend(file_list)
    return files

def main():
    parser = argparse.ArgumentParser(description="Re-sync files to LightRAG")
    parser.add_argument('files', nargs='*', help='Files to sync')
    parser.add_argument('--all', action='store_true', help='Sync all files')
    parser.add_argument('--changed', action='store_true', help='Sync files changed since last sync')

    args = parser.parse_args()

    if args.all:
        files_to_sync = get_all_files()
        print(f"Syncing all {len(files_to_sync)} files to LightRAG...")
    elif args.changed:
        # Read last sync state
        try:
            with open('.claude/.rag_sync_state.json', 'r') as f:
                import json
                last_state = json.load(f)
                files_to_sync = list(last_state.keys())
                print(f"Syncing {len(files_to_sync)} changed files...")
        except:
            files_to_sync = []
            print("No previous sync state found. Use --all to sync all files.")
    else:
        files_to_sync = args.files
        if not files_to_sync:
            print("Usage: python resync_rag.py optimizer.py optimizer_step1.py")
            print("       python resync_rag.py --all")
            return 1

    success_count = 0
    for file in files_to_sync:
        filepath = Path(file)
        if not filepath.exists():
            print(f"✗ Not found: {file}")
            continue

        print(f"✓ Queued: {file}")
        success_count += 1

    if success_count > 0:
        print(f"\n→ To upload these {success_count} files to LightRAG, use the MCP tools in Claude Code")
        print("  Or ask Claude to re-upload these files:")
        print(f"  Files: {', '.join(files_to_sync)}")

    return 0

if __name__ == "__main__":
    sys.exit(main())
