#!/bin/bash
cd /opt/croton-news
source venv/bin/activate
python3 run_scrapers.py 2>&1 | grep -v "Failed to fetch"
