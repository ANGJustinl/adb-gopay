from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .adb_client import ADBClient
from .bluestacks_mim import resolve_active_clone_target
from .automation import AutomationEngine
from .config import (
    AppConfig,
    CredentialConfig,
    FlowConfig,
    NexSMSConfig,
    load_config,
    load_gopay_config,
)
from .gopay_flow import GoPayFlowConfig, GoPayRegistrationFlow
from .ocr import NullOCREngine, OCREngine, OCRUnavailableError, create_ocr_engine
from .tts import Speaker


@dataclass(slots=True)
class Runtime:
    config: AppConfig
    adb: ADBClient
    ocr: OCREngine
    speaker: Speaker
    engine: AutomationEngine


@dataclass(slots=True)
class GoPayRuntime:
    """Runtime for GoPay registration flow."""
    app_config: AppConfig
    nexsms_config: NexSMSConfig
    credential_config: CredentialConfig
    flow_config: FlowConfig
    adb: ADBClient
    ocr: OCREngine
    speaker: Speaker
    flow: GoPayRegistrationFlow


def create_runtime(
    config_path: str | Path | None = None,
    *,
    adb_path: str | None = None,
    device_serial: str | None = None,
    adb_port: str | None = None,
    target_package: str | None = None,
    launch_activity: str | None = None,
    tts_enabled: bool | None = None,
    log_callback=None,
) -> Runtime:
    config = load_config(config_path) if config_path else AppConfig()
    if adb_path:
        config.adb_path = adb_path
    if device_serial:
        config.device_serial = device_serial
    if adb_port:
        config.adb_port = adb_port
    if not device_serial and not adb_port:
        active_serial, active_port = resolve_active_clone_target(
            enabled=bool(config.bluestacks_use_temp_clone),
        )
        if active_serial:
            config.device_serial = active_serial
            config.adb_port = active_port
    if target_package:
        config.target_package = target_package
    if launch_activity:
        config.launch_activity = launch_activity
    if tts_enabled is not None:
        config.tts_enabled = tts_enabled

    adb = ADBClient(adb_path=config.adb_path, device_serial=config.device_serial, adb_port=config.adb_port)
    ocr = create_ocr_engine(config.ocr_backend)
    speaker = Speaker(enabled=config.tts_enabled, rate=config.voice_rate, voice_name=config.voice_name)
    engine = AutomationEngine(adb=adb, ocr=ocr, speaker=speaker, config=config, log_callback=log_callback)
    return Runtime(config=config, adb=adb, ocr=ocr, speaker=speaker, engine=engine)


def create_gopay_runtime(
    config_path: str | Path,
    *,
    adb_path: str | None = None,
    device_serial: str | None = None,
    adb_port: str | None = None,
    tts_enabled: bool | None = None,
    log_callback=None,
) -> GoPayRuntime:
    """Create a runtime for GoPay registration flow.

    Args:
        config_path: Path to GoPay config YAML file.
        adb_path: Override ADB path.
        device_serial: Override device serial.
        adb_port: Override ADB port.
        tts_enabled: Override TTS setting.
        log_callback: Callback for log messages.

    Returns:
        GoPayRuntime instance.
    """
    app_config, nexsms_config, cred_config, flow_config = load_gopay_config(config_path)

    # Apply overrides
    if adb_path:
        app_config.adb_path = adb_path
    if device_serial:
        app_config.device_serial = device_serial
    if adb_port:
        app_config.adb_port = adb_port
    if not device_serial and not adb_port:
        active_serial, active_port = resolve_active_clone_target(
            enabled=bool(app_config.bluestacks_use_temp_clone),
        )
        if active_serial:
            app_config.device_serial = active_serial
            app_config.adb_port = active_port
    if tts_enabled is not None:
        app_config.tts_enabled = tts_enabled

    # Create components
    adb = ADBClient(adb_path=app_config.adb_path, device_serial=app_config.device_serial, adb_port=app_config.adb_port)
    try:
        ocr = create_ocr_engine(app_config.ocr_backend)
    except OCRUnavailableError as exc:
        if log_callback:
            log_callback(f"OCR unavailable, continuing with UI dump only: {exc}")
        ocr = NullOCREngine()
    speaker = Speaker(
        enabled=app_config.tts_enabled,
        rate=app_config.voice_rate,
        voice_name=app_config.voice_name,
    )

    # Create GoPay flow config
    gopay_flow_config = GoPayFlowConfig(
        target_package=app_config.target_package,
        launch_activity=app_config.launch_activity,
        api_key=nexsms_config.api_key,
        nexsms_base_url=nexsms_config.base_url,
        nexsms_proxy=nexsms_config.proxy,
        activation_db_path=nexsms_config.activation_db_path,
        country_name=nexsms_config.country_name,
        country_order=nexsms_config.country_order,
        service_code=nexsms_config.service_code,
        default_price=nexsms_config.default_price,
        min_price=nexsms_config.min_price,
        max_price=nexsms_config.max_price,
        preferred_price=nexsms_config.preferred_price,
        acquire_priority=nexsms_config.acquire_priority,
        activation_retry_rounds=nexsms_config.activation_retry_rounds,
        activation_retry_delay_ms=nexsms_config.activation_retry_delay_ms,
        poll_interval=nexsms_config.poll_interval,
        poll_timeout=nexsms_config.poll_timeout,
        reuse_existing_number_min_remaining_minutes=nexsms_config.reuse_existing_number_min_remaining_minutes,
        activation_validity_minutes=nexsms_config.activation_validity_minutes,
        same_number_retry_limit=nexsms_config.same_number_retry_limit,
        same_number_expiry_guard_minutes=nexsms_config.same_number_expiry_guard_minutes,
        username_length=cred_config.username_length,
        pin_length=cred_config.pin_length,
        max_retries=flow_config.max_retries,
        step_delay=flow_config.step_delay,
        credentials_path=flow_config.credentials_path,
    )

    # Create flow
    flow = GoPayRegistrationFlow(
        adb=adb,
        ocr=ocr,
        speaker=speaker,
        config=gopay_flow_config,
        log_callback=log_callback,
    )

    return GoPayRuntime(
        app_config=app_config,
        nexsms_config=nexsms_config,
        credential_config=cred_config,
        flow_config=flow_config,
        adb=adb,
        ocr=ocr,
        speaker=speaker,
        flow=flow,
    )
