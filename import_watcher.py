#!/usr/bin/env python3
"""
SETUP INSTRUCTIONS:

1. Ensure convert_sources_to_master.py exists in this directory

2. Run this script:
   python3 import_watcher.py

3. Export from EasyBoss as usual — files land directly in ~/Downloads.
   Only files matching "Export Order Package*.xlsx" are processed;
   everything else in Downloads is ignored. If more than one matching
   file is present, the newest (by modified time) is used.

This script polls ~/Downloads every 1 second (no filesystem event
watching — just a plain loop), so it doesn't depend on how a given OS
or browser reports file creation/rename events. After a successful
import, the processed file is moved into
~/Downloads/Processed Exports/. Press Ctrl+C to stop.
"""

import os
import glob
import shutil
import subprocess
import time

FILENAME_PATTERN = "Export Order Package*.xlsx"
PROCESSED_SUBFOLDER = "Processed Exports"
POLL_INTERVAL_SECONDS = 1


def find_latest_matching_file(watch_path):
    """Return the path of the newest file in watch_path matching
    FILENAME_PATTERN, or None if no match is found."""
    pattern_path = os.path.join(watch_path, FILENAME_PATTERN)
    candidates = [f for f in glob.glob(pattern_path) if os.path.isfile(f)]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def process_file(processed_subfolder, latest_file):
    """Copy latest_file to data/eb.xlsx, run the conversion pipeline, and
    move it to Processed Exports on success. Leaves it in place on failure."""
    filename = os.path.basename(latest_file)

    try:
        print(f"\n📥 New EasyBoss export detected")
        print(f"📋 File: {filename}")

        os.makedirs('data', exist_ok=True)

        destination = 'data/eb.xlsx'
        shutil.copy2(latest_file, destination)
        print(f"✅ Copied to {destination}")

        print(f"🔄 Running conversion...")
        print("-" * 60)

        result = subprocess.run(
            ['python3', 'convert_sources_to_master.py'],
            timeout=300  # 5 minute timeout
        )

        print("-" * 60)

        if result.returncode == 0:
            print(f"✅ Conversion successful - orders_master.csv updated")
            os.makedirs(processed_subfolder, exist_ok=True)
            moved_to = os.path.join(processed_subfolder, filename)
            shutil.move(latest_file, moved_to)
            print(f"📦 Moved processed file to {moved_to}")
        else:
            print(f"❌ Conversion failed (exit code: {result.returncode})")
            print(f"⚠️  File left in place at {latest_file} for review — not moved.")

    except subprocess.TimeoutExpired:
        print(f"❌ Conversion script timed out (5 minutes)")
        print(f"⚠️  File left in place at {latest_file} for review — not moved.")
    except Exception as e:
        print(f"❌ Error processing file: {str(e)}")
        print(f"⚠️  File left in place at {latest_file} for review — not moved.")

    print()  # Blank line for readability


def main():
    """Main function - poll ~/Downloads every second"""
    watch_path = os.path.expanduser('~/Downloads')
    processed_subfolder = os.path.join(watch_path, PROCESSED_SUBFOLDER)

    print("=" * 60)
    print("📦 Packing System - Import Watcher (polling mode)")
    print("=" * 60)
    print(f"🔍 Monitoring:     {watch_path}")
    print(f"🔎 Pattern:        {FILENAME_PATTERN}")
    print(f"⏱️  Poll interval:  {POLL_INTERVAL_SECONDS}s")
    print(f"📁 Output folder:  data/eb.xlsx")
    print(f"🐍 Conversion:     convert_sources_to_master.py")
    print(f"📦 Processed to:   {processed_subfolder}")
    print("\n⏳ Polling for new export files... (Press Ctrl+C to stop)\n")

    # Track files already imported, keyed by (filename, mtime) so a later
    # file that reuses the same name (e.g. re-exported) is still picked up,
    # but the same untouched file is never processed twice.
    processed_keys = set()

    try:
        while True:
            latest_file = find_latest_matching_file(watch_path)

            if latest_file is not None:
                filename = os.path.basename(latest_file)
                mtime = os.path.getmtime(latest_file)
                key = (filename, mtime)

                if key not in processed_keys:
                    process_file(processed_subfolder, latest_file)
                    processed_keys.add(key)

            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\n\n⏹️  Stopping watcher...")
        print("✅ Watcher stopped\n")


if __name__ == '__main__':
    main()