"""Select the introduction prompt for a discovery request drafter."""

import re
from pathlib import Path


_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def load_discovery_intro_prompt(product: str, court: str, venue: str) -> str:
    """Use a device-specific federal or California example when applicable."""
    if court.casefold() == "federal":
        prompt_dir, prompt_name = product, "DRAFT_INTRO_FED"
    elif re.search(r"\bCalifornia\b", venue, flags=re.IGNORECASE):
        prompt_dir, prompt_name = product, "DRAFT_INTRO_CA"
    else:
        prompt_dir, prompt_name = "rog", "DRAFT_INTRO_STATE"

    return (_PROMPTS_DIR / prompt_dir / prompt_name).read_text()
