from __future__ import annotations

import json
import re
from typing import Any


def strip_markdown_code_fences(text: str) -> str:
    """
    Remove optional ```json ... ``` fences around model output.
    """

    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text)
    return text


def extract_json_object(text: str) -> dict[str, Any]:
    """
    Extract a JSON object from a model response and parse it.
    """

    clean = strip_markdown_code_fences(text)
    if not (clean.startswith("{") and clean.endswith("}")):
        match = re.search(r"\{.*\}", clean, flags=re.DOTALL)
        if match:
            clean = match.group(0)
    return json.loads(clean)
