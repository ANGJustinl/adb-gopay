from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from .adb_client import AndroidDeviceError
from .config import load_gopay_config
from .gopay_flow import FlowState
from .gopay_pages import actionable_nodes, detect_gopay_page
from .gopay_recording import build_page_record, save_page_record
from .nexsms_client import NexSMSError, is_phone_code_timeout_error
from .ocr import OCRUnavailableError
from .runtime import create_gopay_runtime
from .ui_dump import dump_ui_nodes

LogCallback = Callable[[str], None] | None
StopCallbackRegistrar = Callable[[Callable[[], None]], None] | None


def _register_stop_callback(register_stop: StopCallbackRegistrar, stop_fn: Callable[[], None]) -> None:
    if register_stop:
        register_stop(stop_fn)


def prepare_phone_input_task(
    *,
    config_path: str | Path,
    adb_path: str | None = None,
    device_serial: str | None = None,
    mute: bool = True,
    max_steps: int = 10,
    log_callback: LogCallback = None,
    register_stop: StopCallbackRegistrar = None,
) -> dict[str, Any]:
    runtime = create_gopay_runtime(
        config_path=config_path,
        adb_path=adb_path,
        device_serial=device_serial,
        tts_enabled=not mute,
        log_callback=log_callback,
    )
    _register_stop_callback(register_stop, runtime.flow.stop)
    if log_callback:
        log_callback("Preparing GoPay phone input page from a clean app state...")
    final_state = runtime.flow.prepare_phone_input(max_steps=max_steps)
    status = runtime.flow.get_status()
    if final_state == FlowState.WAITING_PHONE_INPUT:
        return {
            "ok": True,
            "status": "success",
            "state": final_state.value,
            "message": "GoPay is waiting for phone number input.",
            "data": status,
        }
    return {
        "ok": False,
        "status": "error",
        "state": final_state.value,
        "message": f"Flow stopped before phone input: {final_state.value}",
        "data": status,
    }


def inspect_gopay_page_task(
    *,
    config_path: str | Path,
    adb_path: str | None = None,
    device_serial: str | None = None,
    mute: bool = True,
    save_dir: str | Path = "artifacts\\gopay",
    log_callback: LogCallback = None,
    register_stop: StopCallbackRegistrar = None,
) -> dict[str, Any]:
    runtime = create_gopay_runtime(
        config_path=config_path,
        adb_path=adb_path,
        device_serial=device_serial,
        tts_enabled=not mute,
        log_callback=log_callback,
    )
    _register_stop_callback(register_stop, runtime.flow.stop)

    nodes = dump_ui_nodes(runtime.adb)[1]
    ocr_lines: list[str] = []
    try:
        png = runtime.adb.screencap_png()
        ocr_lines = [
            block.text
            for block in runtime.ocr.recognize(png)
            if block.confidence >= runtime.app_config.ocr_confidence_threshold
        ]
    except Exception as exc:
        if log_callback:
            log_callback(f"OCR capture warning: {exc}")

    page_match = detect_gopay_page(nodes, ocr_lines)
    action_nodes = actionable_nodes(nodes)
    record = build_page_record(
        page_match=page_match,
        ocr_lines=ocr_lines,
        nodes=nodes,
        actionable_nodes=action_nodes,
    )
    saved_path = save_page_record(record, save_dir)
    return {
        "ok": True,
        "status": "success",
        "state": runtime.flow.state.value,
        "message": "GoPay page inspected.",
        "data": {
            "detected_page_id": record.detected_page_id,
            "detected_page_title": record.detected_page_title,
            "matched_terms": record.matched_terms,
            "notes": record.notes,
            "next_candidate": record.next_candidate,
            "actionable_count": len(record.actionable),
            "saved_path": str(saved_path),
        },
    }


def run_gopay_full_task(
    *,
    config_path: str | Path,
    adb_path: str | None = None,
    device_serial: str | None = None,
    mute: bool = True,
    max_steps: int = 200,
    poll_timeout: int | None = None,
    step_delay: float | None = None,
    retry_on_otp_timeout: bool = False,
    phone: str | None = None,
    log_callback: LogCallback = None,
    register_stop: StopCallbackRegistrar = None,
) -> dict[str, Any]:
    runtime = create_gopay_runtime(
        config_path=config_path,
        adb_path=adb_path,
        device_serial=device_serial,
        tts_enabled=not mute,
        log_callback=log_callback,
    )
    _register_stop_callback(register_stop, runtime.flow.stop)

    if poll_timeout is not None:
        runtime.flow.config.poll_timeout = poll_timeout
    if step_delay is not None:
        runtime.flow.config.step_delay = step_delay
    if phone:
        runtime.flow.ctx.phone_number = phone
        if log_callback:
            log_callback(f"Reusing phone: {phone}")

    max_phone_cycles = 3 if retry_on_otp_timeout else 1
    user_supplied_phone = bool(phone)

    def snapshot_phone_meta() -> dict[str, float | int | str]:
        return {
            "phone_number": runtime.flow.ctx.phone_number,
            "phone_acquired_at_epoch": runtime.flow.ctx.phone_acquired_at_epoch,
            "phone_expiry_epoch": runtime.flow.ctx.phone_expiry_epoch,
            "phone_retry_count": runtime.flow.ctx.phone_retry_count,
        }

    def restore_phone_meta(meta: dict[str, float | int | str]) -> None:
        runtime.flow.ctx.phone_number = str(meta.get("phone_number") or "")
        runtime.flow.ctx.phone_acquired_at_epoch = float(meta.get("phone_acquired_at_epoch") or 0.0)
        runtime.flow.ctx.phone_expiry_epoch = float(meta.get("phone_expiry_epoch") or 0.0)
        runtime.flow.ctx.phone_retry_count = int(meta.get("phone_retry_count") or 0)

    def current_phone_remaining_minutes(meta: dict[str, float | int | str]) -> float | None:
        expiry_epoch = float(meta.get("phone_expiry_epoch") or 0.0)
        if expiry_epoch <= 0:
            return None
        return max(0.0, (expiry_epoch - time.time()) / 60.0)

    def should_invalidate_phone(meta: dict[str, float | int | str]) -> tuple[bool, str]:
        retry_limit = max(0, int(runtime.flow.config.same_number_retry_limit))
        retry_count = int(meta.get("phone_retry_count") or 0)
        if retry_count >= retry_limit:
            return True, f"same-number retry limit reached ({retry_count}/{retry_limit})"

        remaining_minutes = current_phone_remaining_minutes(meta)
        guard_minutes = max(0.0, float(runtime.flow.config.same_number_expiry_guard_minutes))
        if remaining_minutes is not None and remaining_minutes <= guard_minutes:
            return True, (
                f"sms validity remaining {remaining_minutes:.1f}m "
                f"<= guard {guard_minutes:.1f}m"
            )
        return False, ""

    def invalidate_current_phone(reason: str) -> None:
        phone_number = runtime.flow.ctx.phone_number
        if user_supplied_phone or not phone_number:
            return
        runtime.flow.nexsms.mark_number_invalid(phone_number, reason=reason)
        try:
            result = runtime.flow.nexsms.close_activation(phone_number)
            if log_callback:
                log_callback(f"Marked phone number invalid: {phone_number}")
                log_callback(f"Reason: {reason}")
                if result:
                    log_callback(f"NexSMS: {result}")
        except NexSMSError as exc:
            if log_callback:
                log_callback(f"Warning: failed to invalidate phone number {phone_number}: {exc}")

    pending_phone_meta = snapshot_phone_meta()
    attempt = 1
    phone_cycle = 1
    while phone_cycle <= max_phone_cycles:
        if attempt > 1:
            if log_callback:
                log_callback(f"Retry attempt {attempt} (phone cycle {phone_cycle}/{max_phone_cycles})")
            runtime.flow.reset()
            if pending_phone_meta.get("phone_number"):
                restore_phone_meta(pending_phone_meta)
            if user_supplied_phone and runtime.flow.ctx.phone_number:
                runtime.flow.ctx.phone_number = phone or ""
                if log_callback:
                    log_callback(f"Reusing existing phone number: {runtime.flow.ctx.phone_number}")
            elif runtime.flow.ctx.phone_number:
                same_retry_count = int(runtime.flow.ctx.phone_retry_count or 0)
                remaining_minutes = current_phone_remaining_minutes(snapshot_phone_meta())
                if same_retry_count > 0 and log_callback:
                    if remaining_minutes is None:
                        log_callback(
                            "Reusing existing phone number: "
                            f"{runtime.flow.ctx.phone_number} (same-number retry {same_retry_count})"
                        )
                    else:
                        log_callback(
                            "Reusing existing phone number: "
                            f"{runtime.flow.ctx.phone_number} "
                            f"(same-number retry {same_retry_count}, remaining {remaining_minutes:.1f}m)"
                        )

        try:
            if log_callback:
                log_callback(f"[Attempt {attempt}] Starting registration flow...")
            final_state = runtime.flow.run(max_steps=max_steps)

            if final_state == FlowState.REGISTRATION_COMPLETE:
                return {
                    "ok": True,
                    "status": "success",
                    "state": final_state.value,
                    "message": "Registration completed successfully.",
                    "data": {
                        "username": runtime.flow.ctx.username,
                        "phone": runtime.flow.ctx.phone_number,
                        "credentials_path": runtime.flow.config.credentials_path,
                        "flow_status": runtime.flow.get_status(),
                    },
                }

            if final_state in (FlowState.ERROR, FlowState.WAITING_OTP):
                err = runtime.flow.ctx.error_message or final_state.value
                if retry_on_otp_timeout and is_phone_code_timeout_error(err):
                    phone_meta = snapshot_phone_meta()
                    invalidate, reason = should_invalidate_phone(phone_meta)
                    if invalidate:
                        if user_supplied_phone:
                            return {
                                "ok": False,
                                "status": "error",
                                "state": final_state.value,
                                "message": f"User-supplied phone exceeded retry policy: {reason}",
                                "data": {"flow_status": runtime.flow.get_status()},
                            }
                        invalidate_current_phone(reason)
                        pending_phone_meta = {}
                        phone_cycle += 1
                        attempt += 1
                        if phone_cycle > max_phone_cycles:
                            break
                        if log_callback:
                            log_callback("Will retry with a new phone number...")
                        continue

                    phone_meta["phone_retry_count"] = int(phone_meta.get("phone_retry_count") or 0) + 1
                    pending_phone_meta = phone_meta
                    remaining_minutes = current_phone_remaining_minutes(phone_meta)
                    if log_callback:
                        if user_supplied_phone:
                            log_callback("OTP polling timed out. Will retry with the same user-supplied phone number...")
                        elif remaining_minutes is None:
                            log_callback(
                                "OTP polling timed out. Will retry with the same phone number "
                                f"({phone_meta['phone_retry_count']}/{runtime.flow.config.same_number_retry_limit})..."
                            )
                        else:
                            log_callback(
                                "OTP polling timed out. Will retry with the same phone number "
                                f"({phone_meta['phone_retry_count']}/{runtime.flow.config.same_number_retry_limit}, "
                                f"remaining {remaining_minutes:.1f}m)..."
                            )
                    attempt += 1
                    continue

                return {
                    "ok": False,
                    "status": "error",
                    "state": final_state.value,
                    "message": err,
                    "data": {"flow_status": runtime.flow.get_status()},
                }

            if final_state == FlowState.MANUAL:
                return {
                    "ok": False,
                    "status": "manual",
                    "state": final_state.value,
                    "message": "Flow requires manual intervention.",
                    "data": {"flow_status": runtime.flow.get_status()},
                }

            return {
                "ok": False,
                "status": "error",
                "state": final_state.value,
                "message": f"Flow ended with state: {final_state.value}",
                "data": {"flow_status": runtime.flow.get_status()},
            }

        except (AndroidDeviceError, NexSMSError, RuntimeError, ValueError, OCRUnavailableError) as exc:
            error_text = str(exc)
            if retry_on_otp_timeout and is_phone_code_timeout_error(error_text):
                phone_meta = snapshot_phone_meta()
                invalidate, reason = should_invalidate_phone(phone_meta)
                if invalidate:
                    if user_supplied_phone:
                        return {
                            "ok": False,
                            "status": "error",
                            "state": runtime.flow.state.value,
                            "message": f"User-supplied phone exceeded retry policy: {reason}",
                            "data": {"flow_status": runtime.flow.get_status()},
                        }
                    invalidate_current_phone(reason)
                    pending_phone_meta = {}
                    phone_cycle += 1
                    attempt += 1
                    if phone_cycle > max_phone_cycles:
                        break
                    if log_callback:
                        log_callback("Will retry with a new phone number...")
                    continue

                phone_meta["phone_retry_count"] = int(phone_meta.get("phone_retry_count") or 0) + 1
                pending_phone_meta = phone_meta
                remaining_minutes = current_phone_remaining_minutes(phone_meta)
                if log_callback:
                    if user_supplied_phone:
                        log_callback("OTP polling timed out. Retrying with the same user-supplied phone number...")
                    elif remaining_minutes is None:
                        log_callback(
                            "OTP polling timed out. Retrying with the same phone number "
                            f"({phone_meta['phone_retry_count']}/{runtime.flow.config.same_number_retry_limit})..."
                        )
                    else:
                        log_callback(
                            "OTP polling timed out. Retrying with the same phone number "
                            f"({phone_meta['phone_retry_count']}/{runtime.flow.config.same_number_retry_limit}, "
                            f"remaining {remaining_minutes:.1f}m)..."
                        )
                attempt += 1
                continue

            return {
                "ok": False,
                "status": "error",
                "state": runtime.flow.state.value,
                "message": error_text,
                "data": {"flow_status": runtime.flow.get_status()},
            }

    return {
        "ok": False,
        "status": "error",
        "state": runtime.flow.state.value,
        "message": "All attempts exhausted.",
        "data": {"flow_status": runtime.flow.get_status()},
    }


def resolve_gopay_task_config_path(
    config_path: str | Path | None,
    default_config_path: str | Path | None,
) -> str | Path:
    if config_path:
        return config_path
    if default_config_path:
        return default_config_path
    raise ValueError("config_path is required for GoPay API tasks")


def validate_gopay_config_file(config_path: str | Path) -> None:
    load_gopay_config(config_path)
