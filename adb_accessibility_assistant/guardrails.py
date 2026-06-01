from __future__ import annotations

from .config import RuleConfig
from .models import OCRTextBlock, ScreenSnapshot


def normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


def detect_sensitive_keywords(texts: list[OCRTextBlock], keywords: list[str]) -> list[str]:
    haystack = " ".join(normalize_text(block.text) for block in texts)
    matches = [keyword for keyword in keywords if normalize_text(keyword) and normalize_text(keyword) in haystack]
    return matches


def match_rule(snapshot: ScreenSnapshot, rule: RuleConfig) -> OCRTextBlock | None:
    ordered = snapshot.ordered_texts()
    normalized_lines = [(block, normalize_text(block.text)) for block in ordered]
    page_text = " ".join(line for _, line in normalized_lines)

    if any(normalize_text(required) not in page_text for required in rule.all_of):
        return None

    for anchor in rule.any_of:
        normalized_anchor = normalize_text(anchor)
        for block, normalized_line in normalized_lines:
            if normalized_anchor and normalized_anchor in normalized_line:
                return block
    return None
