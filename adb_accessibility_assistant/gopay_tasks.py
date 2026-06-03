from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from .adb_client import AndroidDeviceError
from .bluestacks_mim import (
    BlueStacksTempCloneManager,
    load_active_clone_session,
    resolve_bluestacks_source_instance,
)
from .config import load_gopay_config
from .gopay_flow import FlowState
from .gopay_pages import actionable_nodes, detect_gopay_page
from .gopay_recording import build_page_record, save_page_record
from .nexsms_client import (
    DEVICE_COOLDOWN_ERROR_PREFIX,
    PHONE_NUMBER_REJECTED_ERROR_PREFIX,
    NexSMSError,
    is_device_cooldown_error,
    is_phone_code_timeout_error,
    is_phone_number_rejected_error,
)
from .ocr import OCRUnavailableError
from .runtime import create_gopay_runtime
from .ui_dump import dump_ui_nodes

LogCallback = Callable[[str], None] | None
StopCallbackRegistrar = Callable[[Callable[[], None]], None] | None


def _register_stop_callback(register_stop: StopCallbackRegistrar, stop_fn: Callable[[], None]) -> None:
    if register_stop:
        register_stop(stop_fn)


def _build_clone_manager(
    *,
    config_path: str | Path,
    adb_path: str | None,
    device_serial: str | None,
    app_config,
    log_callback: LogCallback = None,
) -> BlueStacksTempCloneManager | None:
    if not app_config.bluestacks_use_temp_clone:
        return None
    base_app_config, _, _, _ = load_gopay_config(config_path)
    source_instance_name = resolve_bluestacks_source_instance(
        conf_path=None,
        source_instance_name=app_config.bluestacks_master_instance or base_app_config.bluestacks_master_instance,
        adb_port=base_app_config.adb_port,
        device_serial=device_serial or base_app_config.device_serial,
        window_title=base_app_config.bluestacks_window_title,
    )
    return BlueStacksTempCloneManager(
        adb_path=adb_path or app_config.adb_path,
        source_instance_name=source_instance_name,
        mim_window_title=app_config.bluestacks_mim_window_title,
        log_callback=log_callback,
    )


def cleanup_active_gopay_clone(
    *,
    config_path: str | Path,
    adb_path: str | None = None,
    log_callback: LogCallback = None,
) -> bool:
    session = load_active_clone_session()
    if session is None:
        return False
    app_config, _, _, _ = load_gopay_config(config_path)
    manager = BlueStacksTempCloneManager(
        adb_path=adb_path or app_config.adb_path,
        source_instance_name=session.source_instance_name,
        mim_window_title=app_config.bluestacks_mim_window_title,
        log_callback=log_callback,
    )
    manager.current_session = session
    manager.dispose_current()
    return True


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
    runtime = None
    base_app_config, _, _, _ = load_gopay_config(config_path)
    if adb_path:
        base_app_config.adb_path = adb_path
    if device_serial:
        base_app_config.device_serial = device_serial
    clone_manager = _build_clone_manager(
        config_path=config_path,
        adb_path=adb_path,
        device_serial=device_serial,
        app_config=base_app_config,
        log_callback=log_callback,
    )

    if clone_manager is not None and device_serial is None:
        clone_session = clone_manager.provision()
        runtime = create_gopay_runtime(
            config_path=config_path,
            adb_path=adb_path,
            device_serial=clone_session.device_serial,
            tts_enabled=not mute,
            log_callback=log_callback,
        )
    else:
        runtime = create_gopay_runtime(
            config_path=config_path,
            adb_path=adb_path,
            device_serial=device_serial,
            tts_enabled=not mute,
            log_callback=log_callback,
        )

    def stop_current_flow() -> None:
        runtime.flow.stop()

    _register_stop_callback(register_stop, stop_current_flow)
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
    defer_clone_cleanup: bool = False,
    log_callback: LogCallback = None,
    register_stop: StopCallbackRegistrar = None,
) -> dict[str, Any]:
    base_app_config, _, _, _ = load_gopay_config(config_path)
    if adb_path:
        base_app_config.adb_path = adb_path
    if device_serial:
        base_app_config.device_serial = device_serial
    clone_manager = _build_clone_manager(
        config_path=config_path,
        adb_path=adb_path,
        device_serial=device_serial,
        app_config=base_app_config,
        log_callback=log_callback,
    )

    def rebuild_runtime(target_serial: str | None = None) -> None:
        nonlocal runtime
        runtime = create_gopay_runtime(
            config_path=config_path,
            adb_path=adb_path,
            device_serial=target_serial,
            adb_port=None,
            tts_enabled=not mute,
            log_callback=log_callback,
        )
        if poll_timeout is not None:
            runtime.flow.config.poll_timeout = poll_timeout
        if step_delay is not None:
            runtime.flow.config.step_delay = step_delay
        if phone:
            runtime.flow.ctx.phone_number = phone

    if clone_manager is not None and device_serial is None:
        clone_session = clone_manager.provision()
        rebuild_runtime(clone_session.device_serial)
    else:
        runtime = create_gopay_runtime(
            config_path=config_path,
            adb_path=adb_path,
            device_serial=device_serial,
            tts_enabled=not mute,
            log_callback=log_callback,
        )
        if poll_timeout is not None:
            runtime.flow.config.poll_timeout = poll_timeout
        if step_delay is not None:
            runtime.flow.config.step_delay = step_delay
        if phone:
            runtime.flow.ctx.phone_number = phone

    def finalize_result(payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data")
        if not isinstance(data, dict):
            data = {}
            payload["data"] = data
        clone_cleanup_pending = bool(
            clone_manager is not None
            and runtime.app_config.bluestacks_cleanup_clone_on_exit
            and defer_clone_cleanup
            and load_active_clone_session() is not None
        )
        data["clone_cleanup_pending"] = clone_cleanup_pending
        if clone_cleanup_pending:
            session = load_active_clone_session()
            if session is not None:
                data["clone_instance"] = session.instance_name
                data["clone_serial"] = session.device_serial
        return payload

    def stop_current_flow() -> None:
        runtime.flow.stop()

    _register_stop_callback(register_stop, stop_current_flow)

    if poll_timeout is not None:
        runtime.flow.config.poll_timeout = poll_timeout
    if step_delay is not None:
        runtime.flow.config.step_delay = step_delay
    if phone:
        runtime.flow.ctx.phone_number = phone
        if log_callback:
            log_callback(f"Reusing phone: {phone}")

    max_phone_cycles = max(
        3 if retry_on_otp_timeout else 1,
        max(1, int(runtime.flow.config.max_retries)),
    )
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

    def handle_device_cooldown(reason: str, *, error_state: FlowState) -> dict[str, Any] | None:
        nonlocal pending_phone_meta, phone_cycle, attempt
        phone_meta = snapshot_phone_meta()
        phone_number = str(phone_meta.get("phone_number") or "")
        pending_phone_meta = phone_meta if phone_number else {}

        if clone_manager is None:
            return finalize_result({
                "ok": False,
                "status": "error",
                "state": error_state.value,
                "message": (
                    "GoPay device cooldown detected. The phone number was not marked invalid. "
                    "Non-clone mode cannot recover automatically; clone or replace the BlueStacks device, "
                    "or enable bluestacks_use_temp_clone: true, then retry."
                ),
                "data": {
                    "flow_status": runtime.flow.get_status(),
                    "device_cooldown": True,
                    "phone": phone_number,
                    "reason": reason,
                },
            })

        if phone_cycle >= max_phone_cycles:
            return finalize_result({
                "ok": False,
                "status": "error",
                "state": error_state.value,
                "message": (
                    "GoPay device cooldown repeated. The phone number was not marked invalid; "
                    "stop and use a fresh BlueStacks clone/device before retrying."
                ),
                "data": {
                    "flow_status": runtime.flow.get_status(),
                    "device_cooldown": True,
                    "phone": phone_number,
                    "reason": reason,
                },
            })

        try:
            if log_callback:
                log_callback(
                    "OTP cooldown detected. Rotating to a fresh BlueStacks clone; "
                    "keeping the same phone number."
                )
            clone_session = clone_manager.rotate()
            rebuild_runtime(clone_session.device_serial)
        except Exception as exc:
            return finalize_result({
                "ok": False,
                "status": "error",
                "state": error_state.value,
                "message": (
                    "GoPay device cooldown detected, but clone rotation failed. "
                    "The phone number was not marked invalid; manually clone/replace the device and retry. "
                    f"Rotation error: {exc}"
                ),
                "data": {
                    "flow_status": runtime.flow.get_status(),
                    "device_cooldown": True,
                    "phone": phone_number,
                    "reason": reason,
                },
            })

        phone_cycle += 1
        attempt += 1
        if log_callback:
            if phone_number:
                log_callback(f"Will retry with the same phone number after clone rotation: {phone_number}")
            else:
                log_callback("Will retry after clone rotation.")
        return {"retry": True}

    def retry_with_new_phone(reason: str, *, error_state: FlowState) -> dict[str, Any] | None:
        nonlocal pending_phone_meta, phone_cycle, attempt
        if reason.startswith("otp_cooldown:"):
            return handle_device_cooldown(reason, error_state=error_state)
        if user_supplied_phone:
            return finalize_result({
                "ok": False,
                "status": "error",
                "state": error_state.value,
                "message": f"User-supplied phone was rejected: {reason}",
                "data": {"flow_status": runtime.flow.get_status()},
            })
        invalidate_current_phone(reason)
        pending_phone_meta = {}
        phone_cycle += 1
        attempt += 1
        if phone_cycle > max_phone_cycles:
            return None
        if log_callback:
            log_callback("Will retry with a new phone number...")
        return {"retry": True}

    pending_phone_meta = snapshot_phone_meta()
    attempt = 1
    phone_cycle = 1
    try:
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
                    return finalize_result({
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
                    })

                if final_state in (FlowState.ERROR, FlowState.WAITING_OTP):
                    err = runtime.flow.ctx.error_message or final_state.value
                    if is_device_cooldown_error(err):
                        reason = err[len(DEVICE_COOLDOWN_ERROR_PREFIX):].strip() or "device_cooldown"
                        cooldown_result = handle_device_cooldown(reason, error_state=final_state)
                        if cooldown_result and cooldown_result.get("retry"):
                            continue
                        if cooldown_result:
                            return cooldown_result
                        break
                    if is_phone_number_rejected_error(err):
                        reason = err[len(PHONE_NUMBER_REJECTED_ERROR_PREFIX):].strip() or "phone_rejected"
                        retry_result = retry_with_new_phone(reason, error_state=final_state)
                        if retry_result and retry_result.get("retry"):
                            continue
                        if retry_result:
                            return retry_result
                        break
                    if retry_on_otp_timeout and is_phone_code_timeout_error(err):
                        phone_meta = snapshot_phone_meta()
                        invalidate, reason = should_invalidate_phone(phone_meta)
                        if invalidate:
                            if user_supplied_phone:
                                return finalize_result({
                                    "ok": False,
                                    "status": "error",
                                    "state": final_state.value,
                                    "message": f"User-supplied phone exceeded retry policy: {reason}",
                                    "data": {"flow_status": runtime.flow.get_status()},
                                })
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

                    return finalize_result({
                        "ok": False,
                        "status": "error",
                        "state": final_state.value,
                        "message": err,
                        "data": {"flow_status": runtime.flow.get_status()},
                    })

                if final_state == FlowState.MANUAL:
                    return finalize_result({
                        "ok": False,
                        "status": "manual",
                        "state": final_state.value,
                        "message": "Flow requires manual intervention.",
                        "data": {"flow_status": runtime.flow.get_status()},
                    })

                return finalize_result({
                    "ok": False,
                    "status": "error",
                    "state": final_state.value,
                    "message": f"Flow ended with state: {final_state.value}",
                    "data": {"flow_status": runtime.flow.get_status()},
                })

            except (AndroidDeviceError, NexSMSError, RuntimeError, ValueError, OCRUnavailableError) as exc:
                error_text = str(exc)
                if is_device_cooldown_error(error_text):
                    reason = error_text[len(DEVICE_COOLDOWN_ERROR_PREFIX):].strip() or "device_cooldown"
                    cooldown_result = handle_device_cooldown(reason, error_state=runtime.flow.state)
                    if cooldown_result and cooldown_result.get("retry"):
                        continue
                    if cooldown_result:
                        return cooldown_result
                    break
                if is_phone_number_rejected_error(error_text):
                    reason = error_text[len(PHONE_NUMBER_REJECTED_ERROR_PREFIX):].strip() or "phone_rejected"
                    retry_result = retry_with_new_phone(reason, error_state=runtime.flow.state)
                    if retry_result and retry_result.get("retry"):
                        continue
                    if retry_result:
                        return retry_result
                    break
                if retry_on_otp_timeout and is_phone_code_timeout_error(error_text):
                    phone_meta = snapshot_phone_meta()
                    invalidate, reason = should_invalidate_phone(phone_meta)
                    if invalidate:
                        if user_supplied_phone:
                            return finalize_result({
                                "ok": False,
                                "status": "error",
                                "state": runtime.flow.state.value,
                                "message": f"User-supplied phone exceeded retry policy: {reason}",
                                "data": {"flow_status": runtime.flow.get_status()},
                            })
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

                return finalize_result({
                    "ok": False,
                    "status": "error",
                    "state": runtime.flow.state.value,
                    "message": error_text,
                    "data": {"flow_status": runtime.flow.get_status()},
                })

        return finalize_result({
            "ok": False,
            "status": "error",
            "state": runtime.flow.state.value,
            "message": "All attempts exhausted.",
            "data": {"flow_status": runtime.flow.get_status()},
        })
    finally:
        if (
            clone_manager is not None
            and runtime.app_config.bluestacks_cleanup_clone_on_exit
            and not defer_clone_cleanup
        ):
            try:
                clone_manager.dispose_current()
            except Exception as exc:
                if log_callback:
                    log_callback(f"Warning: failed to clean up BlueStacks clone: {exc}")


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
