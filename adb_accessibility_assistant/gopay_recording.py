from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .gopay_pages import GoPayPageMatch
from .ui_dump import UINode


@dataclass(slots=True)
class GoPayPageRecord:
    recorded_at: str
    detected_page_id: str | None
    detected_page_title: str | None
    detection_score: int | None
    matched_terms: list[str]
    notes: str
    next_candidate: str
    ocr_lines: list[str]
    actionable: list[dict[str, object]]
    nodes: list[dict[str, object]]


def build_page_record(
    *,
    page_match: GoPayPageMatch | None,
    ocr_lines: list[str],
    nodes: list[UINode],
    actionable_nodes: list[UINode],
) -> GoPayPageRecord:
    return GoPayPageRecord(
        recorded_at=datetime.now(timezone.utc).isoformat(),
        detected_page_id=page_match.spec.page_id if page_match else None,
        detected_page_title=page_match.spec.title if page_match else None,
        detection_score=page_match.score if page_match else None,
        matched_terms=page_match.matched_terms if page_match else [],
        notes=page_match.spec.notes if page_match else "",
        next_candidate=page_match.spec.next_candidate if page_match else "",
        ocr_lines=ocr_lines,
        actionable=[asdict(node) for node in actionable_nodes],
        nodes=[asdict(node) for node in nodes],
    )


def save_page_record(record: GoPayPageRecord, output_dir: str | Path, stem: str | None = None) -> Path:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    base_name = stem or record.detected_page_id or "unknown_page"
    file_path = directory / f"{base_name}.json"
    file_path.write_text(json.dumps(asdict(record), ensure_ascii=False, indent=2), encoding="utf-8")
    return file_path
