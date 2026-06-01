from __future__ import annotations

import io
import time
from collections.abc import Callable

from PIL import Image

from .adb_client import ADBClient
from .config import AppConfig, RuleConfig
from .guardrails import detect_sensitive_keywords, match_rule, normalize_text
from .models import AutoStepResult, OCRTextBlock, ScreenSnapshot
from .ocr import OCREngine
from .tts import Speaker


class AutomationEngine:
    def __init__(
        self,
        adb: ADBClient,
        ocr: OCREngine,
        speaker: Speaker,
        config: AppConfig,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.adb = adb
        self.ocr = ocr
        self.speaker = speaker
        self.config = config
        self.log_callback = log_callback
        self.last_snapshot: ScreenSnapshot | None = None
        self._rule_attempts: dict[str, int] = {}
        self._screen_fingerprint: str | None = None

    def log(self, message: str) -> None:
        if self.log_callback:
            self.log_callback(message)

    def start_app(self) -> None:
        self.adb.wait_for_device()
        if self.config.reset_app_on_start and self.config.target_package:
            self.log(f"Clearing package before start: {self.config.target_package}")
            self.adb.clear_app_data(self.config.target_package)
        self.adb.start_app(self.config.target_package, self.config.launch_activity)
        self.log(f"Started package: {self.config.target_package}")
        self.speaker.say("应用已启动。")

    def capture_snapshot(self) -> ScreenSnapshot:
        png = self.adb.screencap_png()
        width, height = Image.open(io.BytesIO(png)).size
        blocks = [block for block in self.ocr.recognize(png) if block.confidence >= self.config.ocr_confidence_threshold]
        snapshot = ScreenSnapshot(width=width, height=height, texts=blocks)
        self.last_snapshot = snapshot
        self._reset_attempts_if_screen_changed(snapshot)
        return snapshot

    def describe_snapshot(self, snapshot: ScreenSnapshot) -> str:
        ordered = snapshot.ordered_texts()
        if not ordered:
            return "没有识别到可用文本。"
        preview = ordered[: self.config.speech_preview_limit]
        numbered = [f"{index + 1}. {block.text}" for index, block in enumerate(preview)]
        return "当前页面识别到这些文本。 " + "； ".join(numbered)

    def scan_and_describe(self) -> tuple[ScreenSnapshot, str]:
        snapshot = self.capture_snapshot()
        description = self.describe_snapshot(snapshot)
        self.log(description)
        self.speaker.say(description)
        return snapshot, description

    def list_snapshot_lines(self, snapshot: ScreenSnapshot | None = None) -> list[str]:
        target = snapshot or self.last_snapshot
        if target is None:
            return []
        lines: list[str] = []
        for index, block in enumerate(target.ordered_texts(), start=1):
            x, y = block.center()
            lines.append(f"{index}. {block.text} (conf={block.confidence:.2f}, x={x}, y={y})")
        return lines

    def tap_text(self, target_text: str) -> AutoStepResult:
        snapshot = self.capture_snapshot()
        sensitive = detect_sensitive_keywords(snapshot.texts, self.config.sensitive_keywords)
        if sensitive:
            message = f"检测到敏感页面关键词: {', '.join(sensitive)}，已暂停点击。"
            self.log(message)
            self.speaker.say(message)
            return AutoStepResult(status="paused_sensitive", message=message, snapshot=snapshot)

        block = self._find_text(snapshot, target_text)
        if block is None:
            message = f"未找到文本: {target_text}"
            self.log(message)
            self.speaker.say(message)
            return AutoStepResult(status="not_found", message=message, snapshot=snapshot)

        x, y = block.center()
        self.adb.tap(x, y)
        message = f"已点击文本 {block.text}，坐标 ({x}, {y})"
        self.log(message)
        self.speaker.say(message)
        return AutoStepResult(status="acted", message=message, snapshot=snapshot, matched_text=block.text)

    def auto_step(self) -> AutoStepResult:
        snapshot = self.capture_snapshot()
        sensitive = detect_sensitive_keywords(snapshot.texts, self.config.sensitive_keywords)
        if sensitive:
            message = f"检测到敏感页面关键词: {', '.join(sensitive)}，自动化已暂停。"
            self.log(message)
            self.speaker.say(message)
            return AutoStepResult(status="paused_sensitive", message=message, snapshot=snapshot)

        for rule in self.config.rules:
            matched = match_rule(snapshot, rule)
            if matched is None:
                continue
            attempts = self._rule_attempts.get(rule.name, 0)
            if attempts >= rule.max_retries:
                continue
            self._rule_attempts[rule.name] = attempts + 1
            return self._perform_rule(rule, matched, snapshot)

        message = "当前页面没有匹配到自动化规则。"
        self.log(message)
        self.speaker.say(message)
        return AutoStepResult(status="no_rule", message=message, snapshot=snapshot)

    def auto_loop(self, max_steps: int = 20) -> list[AutoStepResult]:
        results: list[AutoStepResult] = []
        for _ in range(max_steps):
            result = self.auto_step()
            results.append(result)
            if result.status != "acted":
                break
            time.sleep(self.config.polling_interval)
        return results

    def _perform_rule(self, rule: RuleConfig, matched: OCRTextBlock, snapshot: ScreenSnapshot) -> AutoStepResult:
        if rule.speak_before:
            self.speaker.say(rule.speak_before)

        if rule.action == "tap_first":
            x, y = matched.center()
            self.adb.tap(x, y)
            action_text = f"点击 {matched.text} ({x}, {y})"
        elif rule.action == "input_text":
            if rule.input_text is None:
                raise ValueError(f"Rule {rule.name} uses input_text but no input_text value was configured")
            self.adb.input_text(rule.input_text)
            action_text = f"输入文本 for rule {rule.name}"
        elif rule.action == "keyevent":
            if rule.keyevent is None:
                raise ValueError(f"Rule {rule.name} uses keyevent but no keyevent value was configured")
            self.adb.keyevent(rule.keyevent)
            action_text = f"发送按键 {rule.keyevent}"
        else:
            raise ValueError(f"Unsupported rule action: {rule.action}")

        if rule.delay_after > 0:
            time.sleep(rule.delay_after)

        if rule.speak_after:
            self.speaker.say(rule.speak_after)

        message = f"规则 {rule.name} 已执行: {action_text}"
        self.log(message)
        return AutoStepResult(
            status="acted",
            message=message,
            snapshot=snapshot,
            matched_rule=rule.name,
            matched_text=matched.text,
        )

    def _find_text(self, snapshot: ScreenSnapshot, target_text: str) -> OCRTextBlock | None:
        needle = normalize_text(target_text)
        for block in snapshot.ordered_texts():
            if needle and needle in normalize_text(block.text):
                return block
        return None

    def _reset_attempts_if_screen_changed(self, snapshot: ScreenSnapshot) -> None:
        fingerprint = "|".join(normalize_text(block.text) for block in snapshot.ordered_texts()[:8])
        if fingerprint != self._screen_fingerprint:
            self._rule_attempts.clear()
            self._screen_fingerprint = fingerprint
