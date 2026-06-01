from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


DEFAULT_SENSITIVE_KEYWORDS = [
    "otp",
    "verification code",
    "kode verifikasi",
    "pin",
    "passcode",
    "password",
]


@dataclass(slots=True)
class NexSMSConfig:
    """NexSMS API configuration."""
    api_key: str = ""
    base_url: str = "https://api.nexsms.net"
    proxy: str = ""
    activation_db_path: str = "artifacts/nexsms_activations.sqlite3"
    country_name: str = "Indonesia"
    country_order: list[str] = field(default_factory=list)
    service_code: str = "gopay"
    default_price: float = 0.27
    min_price: float | None = None
    max_price: float | None = None
    preferred_price: float | None = None
    acquire_priority: str = "country"
    activation_retry_rounds: int = 3
    activation_retry_delay_ms: int = 2000
    poll_interval: float = 5.0
    poll_timeout: float = 120.0
    reuse_existing_number_min_remaining_minutes: float = 15.0
    activation_validity_minutes: float = 20.0
    same_number_retry_limit: int = 3
    same_number_expiry_guard_minutes: float = 8.0


@dataclass(slots=True)
class CredentialConfig:
    """Credential generation configuration."""
    username_length: int = 8
    username_charset: str = "abcdefghijklmnopqrstuvwxyz"
    pin_length: int = 6


@dataclass(slots=True)
class FlowConfig:
    """GoPay flow configuration."""
    max_retries: int = 3
    step_delay: float = 2.0
    credentials_path: str = "credentials.json"


@dataclass(slots=True)
class RuleConfig:
    name: str
    any_of: list[str]
    all_of: list[str] = field(default_factory=list)
    action: str = "tap_first"
    input_text: str | None = None
    keyevent: str | None = None
    delay_after: float = 1.2
    max_retries: int = 2
    speak_before: str | None = None
    speak_after: str | None = None


@dataclass(slots=True)
class AppConfig:
    adb_path: str = "adb"
    device_serial: str | None = None
    target_package: str = "com.example.app"
    launch_activity: str | None = None
    reset_app_on_start: bool = False
    polling_interval: float = 1.5
    ocr_backend: str = "rapidocr"
    ocr_confidence_threshold: float = 0.45
    tts_enabled: bool = False
    voice_rate: int = 175
    voice_name: str | None = None
    speech_preview_limit: int = 8
    sensitive_keywords: list[str] = field(default_factory=lambda: list(DEFAULT_SENSITIVE_KEYWORDS))
    rules: list[RuleConfig] = field(default_factory=list)


def _to_string_list(value: object, field_name: str) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise ValueError(f"{field_name} must be a string or a list of strings")


def _to_optional_float(value: object, field_name: str) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number") from exc


def _load_rule(index: int, payload: dict[str, object]) -> RuleConfig:
    name = str(payload.get("name") or f"rule_{index}")
    any_of = _to_string_list(payload.get("any_of"), f"rules[{index}].any_of")
    if not any_of:
        raise ValueError(f"rules[{index}].any_of must contain at least one anchor")
    return RuleConfig(
        name=name,
        any_of=any_of,
        all_of=_to_string_list(payload.get("all_of"), f"rules[{index}].all_of"),
        action=str(payload.get("action") or "tap_first"),
        input_text=str(payload["input_text"]) if payload.get("input_text") is not None else None,
        keyevent=str(payload["keyevent"]) if payload.get("keyevent") is not None else None,
        delay_after=float(payload.get("delay_after", 1.2)),
        max_retries=int(payload.get("max_retries", 2)),
        speak_before=str(payload["speak_before"]) if payload.get("speak_before") is not None else None,
        speak_after=str(payload["speak_after"]) if payload.get("speak_after") is not None else None,
    )


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("Top-level config must be a mapping")

    rule_payloads = raw.get("rules") or []
    if not isinstance(rule_payloads, list):
        raise ValueError("rules must be a list")

    rules = [_load_rule(index, payload) for index, payload in enumerate(rule_payloads) if isinstance(payload, dict)]
    if len(rules) != len(rule_payloads):
        raise ValueError("Each rules entry must be a mapping")

    return AppConfig(
        adb_path=str(raw.get("adb_path") or "adb"),
        device_serial=str(raw["device_serial"]).strip() if raw.get("device_serial") else None,
        target_package=str(raw.get("target_package") or "com.example.app"),
        launch_activity=str(raw["launch_activity"]).strip() if raw.get("launch_activity") else None,
        reset_app_on_start=bool(raw.get("reset_app_on_start", False)),
        polling_interval=float(raw.get("polling_interval", 1.5)),
        ocr_backend=str(raw.get("ocr_backend") or "rapidocr"),
        ocr_confidence_threshold=float(raw.get("ocr_confidence_threshold", 0.45)),
        tts_enabled=bool(raw.get("tts_enabled", False)),
        voice_rate=int(raw.get("voice_rate", 175)),
        voice_name=str(raw["voice_name"]).strip() if raw.get("voice_name") else None,
        speech_preview_limit=int(raw.get("speech_preview_limit", 8)),
        sensitive_keywords=_to_string_list(raw.get("sensitive_keywords"), "sensitive_keywords")
        or list(DEFAULT_SENSITIVE_KEYWORDS),
        rules=rules,
    )


def load_gopay_config(path: str | Path) -> tuple[AppConfig, NexSMSConfig, CredentialConfig, FlowConfig]:
    """Load GoPay-specific configuration.

    Returns:
        Tuple of (AppConfig, NexSMSConfig, CredentialConfig, FlowConfig).
    """
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("Top-level config must be a mapping")

    # Load base AppConfig
    app_config = AppConfig(
        adb_path=str(raw.get("adb_path") or "adb"),
        device_serial=str(raw["device_serial"]).strip() if raw.get("device_serial") else None,
        target_package=str(raw.get("target_package") or "com.gojek.gopay"),
        launch_activity=str(raw["launch_activity"]).strip() if raw.get("launch_activity") else None,
        reset_app_on_start=bool(raw.get("reset_app_on_start", True)),
        ocr_backend=str(raw.get("ocr_backend") or "rapidocr"),
        ocr_confidence_threshold=float(raw.get("ocr_confidence_threshold", 0.45)),
        tts_enabled=bool(raw.get("tts_enabled", False)),
        voice_rate=int(raw.get("voice_rate", 175)),
        voice_name=str(raw["voice_name"]).strip() if raw.get("voice_name") else None,
        sensitive_keywords=_to_string_list(raw.get("sensitive_keywords"), "sensitive_keywords")
        or list(DEFAULT_SENSITIVE_KEYWORDS),
    )

    # Load NexSMS config
    nexsms_raw = raw.get("nexsms") or {}
    nexsms_config = NexSMSConfig(
        api_key=str(nexsms_raw.get("api_key") or ""),
        base_url=str(nexsms_raw.get("base_url") or "https://api.nexsms.net"),
        proxy=str(nexsms_raw.get("proxy") or "").strip(),
        activation_db_path=str(
            nexsms_raw.get("activation_db_path") or "artifacts/nexsms_activations.sqlite3"
        ).strip(),
        country_name=str(nexsms_raw.get("country_name") or "Indonesia"),
        country_order=_to_string_list(nexsms_raw.get("country_order"), "nexsms.country_order"),
        service_code=str(nexsms_raw.get("service_code") or "gopay"),
        default_price=float(nexsms_raw.get("default_price", 0.27)),
        min_price=_to_optional_float(nexsms_raw.get("min_price"), "nexsms.min_price"),
        max_price=_to_optional_float(nexsms_raw.get("max_price"), "nexsms.max_price"),
        preferred_price=_to_optional_float(nexsms_raw.get("preferred_price"), "nexsms.preferred_price"),
        acquire_priority=str(nexsms_raw.get("acquire_priority") or "country"),
        activation_retry_rounds=int(nexsms_raw.get("activation_retry_rounds", 3)),
        activation_retry_delay_ms=int(nexsms_raw.get("activation_retry_delay_ms", 2000)),
        poll_interval=float(nexsms_raw.get("poll_interval", 5.0)),
        poll_timeout=float(nexsms_raw.get("poll_timeout", 120.0)),
        reuse_existing_number_min_remaining_minutes=float(
            nexsms_raw.get("reuse_existing_number_min_remaining_minutes", 15.0)
        ),
        activation_validity_minutes=float(nexsms_raw.get("activation_validity_minutes", 20.0)),
        same_number_retry_limit=int(nexsms_raw.get("same_number_retry_limit", 3)),
        same_number_expiry_guard_minutes=float(nexsms_raw.get("same_number_expiry_guard_minutes", 8.0)),
    )

    # Load credential config
    cred_raw = raw.get("credential") or {}
    cred_config = CredentialConfig(
        username_length=int(cred_raw.get("username_length", 8)),
        username_charset=str(cred_raw.get("username_charset") or "abcdefghijklmnopqrstuvwxyz"),
        pin_length=int(cred_raw.get("pin_length", 6)),
    )

    # Load flow config
    flow_raw = raw.get("flow") or {}
    flow_config = FlowConfig(
        max_retries=int(flow_raw.get("max_retries", 3)),
        step_delay=float(flow_raw.get("step_delay", 2.0)),
        credentials_path=str(flow_raw.get("credentials_path") or "credentials.json"),
    )

    return app_config, nexsms_config, cred_config, flow_config
