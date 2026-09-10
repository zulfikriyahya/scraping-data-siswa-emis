#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate
python3 scraper.py
read -p "Selesai. Tekan ENTER untuk menutup..."