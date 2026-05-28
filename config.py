"""
JSW Coil OCR - Central Configuration
Paths, thresholds, and regex patterns used across the demo pipeline.
"""

import re
from pathlib import Path

# ─── Project paths ──────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
DEMO_IMAGES_DIR = DATA_DIR / "demo_images"
RESULTS_DIR = DATA_DIR / "results"
GROUND_TRUTH_CSV = DATA_DIR / "ground_truth.csv"
MODELS_DIR = PROJECT_ROOT / "models"
YOLO_MODEL_PATH = MODELS_DIR / "yolo_coil_detect.pt"

# ─── OCR thresholds ─────────────────────────────────────────────────────
CONFIDENCE_THRESHOLD = 0.85
YOLO_CONFIDENCE = 0.25
YOLO_PAD_RATIO = 0.10

# ─── Coil ID regex patterns ─────────────────────────────────────────────
# Structural break-down (see modules/crnn_reader._PATTERN_FORCED):
#
# HSM-2: "0 2 2 Y N N N N N N"
#         pos0=0 pos1=2 pos2=2 pos3=Y(any year digit)  pos4-9 = serial
# CSP:   "2 Y S 0 H H H H C C"
#         pos0=2 pos1=Y(any year digit) pos2=S pos3=0  pos4-9 = HHHHCC
#
# Year digit is NOT restricted — plant has coils from any year (2025, 2026,
# 2027, 2028 ...). Only structural fixed digits enforced.
HSM2_PATTERN = re.compile(r"^022\d{7}$")
CSP_PATTERN  = re.compile(r"^2\d{2}0\d{6}$")
