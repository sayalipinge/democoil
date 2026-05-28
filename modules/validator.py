"""
Format validation: reduce OCR output to digits and check against the two
coil-ID regex patterns (HSM-2, CSP).
"""

import re
from config import HSM2_PATTERN, CSP_PATTERN


def clean_text(text: str) -> str:
    """Drop anything that isn't a digit."""
    return re.sub(r"[^0-9]", "", text)


def validate_format(coil_id: str):
    """Return the matching pattern name, or None if no match."""
    if HSM2_PATTERN.match(coil_id):
        return "HSM2"
    if CSP_PATTERN.match(coil_id):
        return "CSP"
    return None
