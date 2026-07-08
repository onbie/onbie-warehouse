#!/bin/bash

cd "$(dirname "$0")"

echo "🚀 Starting Import Watcher..."
python3 import_watcher.py &

sleep 2

echo "🚀 Starting Packing System..."
python3 -m streamlit run app.py