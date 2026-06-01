from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class OCRTextBlock:
    text: str
    confidence: float
    box: list[tuple[int, int]]

    def center(self) -> tuple[int, int]:
        xs = [point[0] for point in self.box]
        ys = [point[1] for point in self.box]
        return (sum(xs) // len(xs), sum(ys) // len(ys))


@dataclass(slots=True)
class ScreenSnapshot:
    width: int
    height: int
    texts: list[OCRTextBlock]
    captured_at: datetime = field(default_factory=datetime.utcnow)

    def ordered_texts(self) -> list[OCRTextBlock]:
        return sorted(self.texts, key=lambda block: (block.center()[1], block.center()[0]))


@dataclass(slots=True)
class AutoStepResult:
    status: str
    message: str
    snapshot: ScreenSnapshot | None = None
    matched_rule: str | None = None
    matched_text: str | None = None
