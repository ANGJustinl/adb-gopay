from __future__ import annotations

import argparse
import time
from pathlib import Path

from .adb_client import ADBClient, AndroidDeviceError
from .config import AppConfig, load_config
from .gopay_flow import FlowState
from .gopay_pages import actionable_nodes, detect_gopay_page
from .gopay_recording import build_page_record, save_page_record
from .ocr import OCRUnavailableError, available_backends, create_ocr_engine
from .runtime import create_gopay_runtime, create_runtime
from .ui_dump import dump_ui_nodes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ADB accessibility assistant")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=None, help="Path to YAML config")
    common.add_argument("--adb-path", default=None, help="Override adb executable path")
    common.add_argument("--device", default=None, help="Override adb device serial")
    common.add_argument("--package", default=None, help="Override target package name")
    common.add_argument("--activity", default=None, help="Override target launch activity")
    common.add_argument("--mute", action="store_true", help="Disable speech output")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", parents=[common], help="Check adb, OCR, TTS, and connected devices")
    subparsers.add_parser("start-app", parents=[common], help="Launch the configured app")
    subparsers.add_parser("scan", parents=[common], help="Capture screen, OCR it, and speak a summary")
    subparsers.add_parser("dump-ui", parents=[common], help="Dump Android UI hierarchy and print actionable nodes")

    tap_text = subparsers.add_parser("tap-text", parents=[common], help="Tap the first OCR line that matches a string")
    tap_text.add_argument("text", help="Text fragment to tap")

    subparsers.add_parser("auto-step", parents=[common], help="Run one rules-based automation step")

    auto_loop = subparsers.add_parser("auto-loop", parents=[common], help="Run repeated automation steps")
    auto_loop.add_argument("--max-steps", type=int, default=20, help="Maximum loop steps")

    subparsers.add_parser("assist", parents=[common], help="Interactive keyboard-driven assist mode")
    subparsers.add_parser("gui", parents=[common], help="Open the simple desktop control panel")

    # GoPay commands
    gopay_register = subparsers.add_parser("gopay-register", parents=[common], help="Run GoPay registration flow")
    gopay_register.add_argument("--max-steps", type=int, default=50, help="Maximum flow steps")
    gopay_to_phone = subparsers.add_parser(
        "gopay-to-phone-input",
        parents=[common],
        help="Clean-start GoPay and stop at the phone number input page",
    )
    gopay_to_phone.add_argument("--max-steps", type=int, default=10, help="Maximum flow steps")

    subparsers.add_parser("gopay-gui", parents=[common], help="Open GoPay registration GUI")
    gopay_inspect = subparsers.add_parser("gopay-inspect", parents=[common], help="Inspect current GoPay page and save a flow snapshot")
    gopay_inspect.add_argument("--save-dir", default="artifacts\\gopay", help="Directory to store page snapshots")

    gopay_tap = subparsers.add_parser("gopay-tap", parents=[common], help="Tap a node by content-desc text on GoPay page")
    gopay_tap.add_argument("desc_text", help="Content-desc text to find and tap")

    gopay_next = subparsers.add_parser("gopay-next", parents=[common], help="Tap the next candidate CTA on current GoPay page")
    gopay_next.add_argument("--save-dir", default="artifacts\\gopay", help="Directory to store page snapshots")

    gopay_input = subparsers.add_parser("gopay-input", parents=[common], help="Input phone number on GoPay page")
    gopay_input.add_argument("phone_number", help="Phone number to input (without country code)")
    gopay_input.add_argument("--save-dir", default="artifacts\\gopay", help="Directory to store page snapshots")

    # Full end-to-end GoPay registration run
    run_gopay = subparsers.add_parser(
        "run-gopay",
        parents=[common],
        help="Run full GoPay registration flow end-to-end",
    )
    run_gopay.add_argument(
        "--max-steps", type=int, default=200,
        help="Maximum flow steps before giving up (default: 200)",
    )
    run_gopay.add_argument(
        "--poll-timeout", type=int, default=None,
        help="Override NexSMS OTP poll timeout in seconds (default: from config, 600)",
    )
    run_gopay.add_argument(
        "--step-delay", type=float, default=None,
        help="Override delay between flow steps in seconds (default: from config, 0.4)",
    )
    run_gopay.add_argument(
        "--retry-on-otp-timeout", action="store_true", default=False,
        help="Re-order phone number and retry if OTP poll times out",
    )
    run_gopay.add_argument(
        "--phone", type=str, default=None,
        help="Reuse an existing phone number (with country code, e.g. +62857xxxxxxxx) instead of ordering new",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        return run_doctor(args)
    if args.command == "gui":
        from .gui import launch_gui

        launch_gui(
            config_path=args.config,
            adb_path=args.adb_path,
            device_serial=args.device,
            target_package=args.package,
            launch_activity=args.activity,
            tts_enabled=not args.mute,
        )
        return 0

    if args.command == "gopay-gui":
        from .gopay_gui import launch_gopay_gui

        if not args.config:
            print("Error: --config is required for gopay-gui")
            return 1

        launch_gopay_gui(
            config_path=args.config,
            adb_path=args.adb_path,
            device_serial=args.device,
            tts_enabled=not args.mute,
        )
        return 0

    if args.command == "gopay-register":
        return run_gopay_register(args)
    if args.command == "gopay-to-phone-input":
        return run_gopay_to_phone_input(args)
    if args.command == "gopay-inspect":
        return run_gopay_inspect(args)
    if args.command == "gopay-tap":
        return run_gopay_tap(args)
    if args.command == "gopay-next":
        return run_gopay_next(args)
    if args.command == "gopay-input":
        return run_gopay_input(args)
    if args.command == "run-gopay":
        return run_gopay_full(args)

    try:
        runtime = create_runtime(
            config_path=args.config,
            adb_path=args.adb_path,
            device_serial=args.device,
            target_package=args.package,
            launch_activity=args.activity,
            tts_enabled=not args.mute,
            log_callback=print,
        )
    except (OCRUnavailableError, RuntimeError, ValueError) as exc:
        print(f"Initialization failed: {exc}")
        return 2

    try:
        if args.command == "start-app":
            runtime.engine.start_app()
            return 0

        if args.command == "scan":
            snapshot, description = runtime.engine.scan_and_describe()
            print(f"Screen size: {snapshot.width}x{snapshot.height}")
            print(description)
            for line in runtime.engine.list_snapshot_lines(snapshot):
                print(line)
            return 0

        if args.command == "dump-ui":
            _, nodes = dump_ui_nodes(runtime.adb)
            if not nodes:
                print("No actionable UI nodes found.")
                return 1
            for node in nodes:
                print(node.summary())
            return 0

        if args.command == "tap-text":
            result = runtime.engine.tap_text(args.text)
            print(result.message)
            return 0 if result.status == "acted" else 1

        if args.command == "auto-step":
            result = runtime.engine.auto_step()
            print(result.message)
            return 0 if result.status == "acted" else 1

        if args.command == "auto-loop":
            results = runtime.engine.auto_loop(max_steps=args.max_steps)
            for result in results:
                print(result.message)
            return 0 if results and results[-1].status == "acted" else 1

        if args.command == "assist":
            run_assist_shell(runtime.engine)
            return 0
    except (AndroidDeviceError, OCRUnavailableError, RuntimeError, ValueError) as exc:
        print(f"Command failed: {exc}")
        return 1

    parser.print_help()
    return 1


def _resolve_app_config(args: argparse.Namespace, default_package: str = "com.gojek.gopay") -> AppConfig:
    if args.config:
        config = load_config(args.config)
    else:
        config = AppConfig(target_package=default_package)

    if args.adb_path:
        config.adb_path = args.adb_path
    if args.device:
        config.device_serial = args.device
    if args.package:
        config.target_package = args.package
    elif not config.target_package or config.target_package == "com.example.app":
        config.target_package = default_package
    if args.activity:
        config.launch_activity = args.activity
    return config


def _create_gopay_adb(args: argparse.Namespace) -> tuple[AppConfig, ADBClient]:
    config = _resolve_app_config(args)
    return config, ADBClient(adb_path=config.adb_path, device_serial=config.device_serial)


def _capture_optional_ocr_lines(config: AppConfig, adb: ADBClient) -> tuple[list[str], str | None]:
    try:
        ocr = create_ocr_engine(config.ocr_backend)
    except OCRUnavailableError as exc:
        return [], f"OCR unavailable, using UI dump only: {exc}"
    except Exception as exc:
        return [], f"OCR initialization warning: {exc}"

    try:
        png = adb.screencap_png()
        blocks = [
            block.text
            for block in ocr.recognize(png)
            if block.confidence >= config.ocr_confidence_threshold
        ]
        return blocks, None
    except Exception as exc:
        return [], f"OCR capture warning: {exc}"


def run_doctor(args: argparse.Namespace) -> int:
    backends = available_backends()
    print("OCR backends:")
    for name, enabled in backends.items():
        print(f"  {name}: {'ok' if enabled else 'missing'}")

    try:
        runtime = create_runtime(
            config_path=args.config,
            adb_path=args.adb_path,
            device_serial=args.device,
            target_package=args.package,
            launch_activity=args.activity,
            tts_enabled=not args.mute,
            log_callback=print,
        )
        runtime.adb.assert_available()
        devices = runtime.adb.list_devices()
        print("ADB: ok")
        print(f"Devices: {', '.join(devices) if devices else 'none'}")
        print(f"TTS: {'enabled' if runtime.config.tts_enabled else 'disabled'}")
        print(f"Target package: {runtime.config.target_package}")
        return 0
    except (AndroidDeviceError, OCRUnavailableError, RuntimeError, ValueError) as exc:
        print(f"Doctor failed: {exc}")
        return 1


def run_gopay_register(args: argparse.Namespace) -> int:
    """Run GoPay registration flow from CLI."""
    if not args.config:
        print("Error: --config is required for gopay-register")
        return 1

    try:
        runtime = create_gopay_runtime(
            config_path=args.config,
            adb_path=args.adb_path,
            device_serial=args.device,
            tts_enabled=not args.mute,
            log_callback=print,
        )
    except (OCRUnavailableError, RuntimeError, ValueError) as exc:
        print(f"Initialization failed: {exc}")
        return 2

    try:
        print("Starting registration flow from a clean app state...")
        final_state = runtime.flow.run(max_steps=args.max_steps)

        if final_state == FlowState.REGISTRATION_COMPLETE:
            print("Registration completed successfully!")
            return 0
        elif final_state == FlowState.ERROR:
            print("Registration failed with error.")
            return 1
        else:
            print(f"Flow ended with state: {final_state.value}")
            return 1
    except (AndroidDeviceError, OCRUnavailableError, RuntimeError, ValueError) as exc:
        print(f"Command failed: {exc}")
        return 1


def run_gopay_to_phone_input(args: argparse.Namespace) -> int:
    """Drive GoPay to the phone input page and stop there."""
    if not args.config:
        print("Error: --config is required for gopay-to-phone-input")
        return 1

    try:
        runtime = create_gopay_runtime(
            config_path=args.config,
            adb_path=args.adb_path,
            device_serial=args.device,
            tts_enabled=not args.mute,
            log_callback=print,
        )
    except (OCRUnavailableError, RuntimeError, ValueError) as exc:
        print(f"Initialization failed: {exc}")
        return 2

    try:
        print("Preparing GoPay phone input page from a clean app state...")
        final_state = runtime.flow.prepare_phone_input(max_steps=args.max_steps)

        if final_state == FlowState.WAITING_PHONE_INPUT:
            print("Ready: GoPay is waiting for phone number input.")
            return 0

        print(f"Flow stopped before phone input. Current state: {final_state.value}")
        return 1
    except (AndroidDeviceError, OCRUnavailableError, RuntimeError, ValueError) as exc:
        print(f"Command failed: {exc}")
        return 1


def run_gopay_inspect(args: argparse.Namespace) -> int:
    try:
        config, adb = _create_gopay_adb(args)
    except (RuntimeError, ValueError) as exc:
        print(f"Initialization failed: {exc}")
        return 2

    try:
        _, nodes = dump_ui_nodes(adb)
        ocr_lines, warning = _capture_optional_ocr_lines(config, adb)
        if warning:
            print(warning)

        page_match = detect_gopay_page(nodes, ocr_lines)
        action_nodes = actionable_nodes(nodes)
        record = build_page_record(
            page_match=page_match,
            ocr_lines=ocr_lines,
            nodes=nodes,
            actionable_nodes=action_nodes,
        )
        saved_path = save_page_record(record, args.save_dir)

        if page_match:
            print(f"Detected page: {page_match.spec.page_id} ({page_match.spec.title})")
            print(f"Matched terms: {', '.join(page_match.matched_terms)}")
            if page_match.spec.notes:
                print(f"Notes: {page_match.spec.notes}")
            if page_match.spec.next_candidate:
                print(f"Next candidate: {page_match.spec.next_candidate}")
        else:
            print("Detected page: unknown")

        print("Actionable nodes:")
        if not action_nodes:
            print("  none")
        else:
            for node in action_nodes:
                print(f"  {node.summary()}")

        print(f"Saved snapshot: {saved_path}")
        return 0
    except (AndroidDeviceError, RuntimeError, ValueError) as exc:
        print(f"Command failed: {exc}")
        return 1


def run_gopay_tap(args: argparse.Namespace) -> int:
    """Tap a node by content-desc text."""
    try:
        _, adb = _create_gopay_adb(args)
    except (RuntimeError, ValueError) as exc:
        print(f"Initialization failed: {exc}")
        return 2

    try:
        _, nodes = dump_ui_nodes(adb)
        desc_lower = args.desc_text.lower()

        # Find node by content-desc
        target_node = None
        for node in nodes:
            if node.content_desc and desc_lower in node.content_desc.lower():
                target_node = node
                break

        if not target_node:
            print(f"Node not found with content-desc containing: {args.desc_text}")
            print("Available nodes with content-desc:")
            for node in nodes:
                if node.content_desc:
                    print(f"  - {node.content_desc}")
            return 1

        # Calculate center and tap
        bounds = target_node.bounds
        # Parse bounds like [24,1830][1056,1896]
        parts = bounds.replace("][", ",").strip("[]").split(",")
        x1, y1, x2, y2 = map(int, parts)
        center_x = (x1 + x2) // 2
        center_y = (y1 + y2) // 2

        print(f"Tapping: {target_node.content_desc}")
        print(f"Bounds: {bounds}")
        print(f"Center: ({center_x}, {center_y})")

        adb.tap(center_x, center_y)
        print("Tap sent.")
        return 0
    except (AndroidDeviceError, RuntimeError, ValueError) as exc:
        print(f"Command failed: {exc}")
        return 1


def run_gopay_next(args: argparse.Namespace) -> int:
    """Tap the next candidate CTA on current GoPay page."""
    try:
        config, adb = _create_gopay_adb(args)
    except (RuntimeError, ValueError) as exc:
        print(f"Initialization failed: {exc}")
        return 2

    try:
        _, nodes = dump_ui_nodes(adb)
        ocr_lines, warning = _capture_optional_ocr_lines(config, adb)
        if warning:
            print(warning)

        page_match = detect_gopay_page(nodes, ocr_lines)
        if not page_match:
            print("Could not detect current page.")
            return 1

        print(f"Current page: {page_match.spec.page_id} ({page_match.spec.title})")
        print(f"Next candidate: {page_match.spec.next_candidate}")

        # Try to find and tap the primary CTA
        cta_by_page = {
            "landing_intro": "Masukkan nomor HP-mu",
            "signup_terms": "Lanjut",
            "verification_method": "OTP via SMS",
        }
        cta_desc = cta_by_page.get(page_match.spec.page_id)
        if cta_desc is None:
            print(f"No automatic CTA defined for page: {page_match.spec.page_id}")
            print("Use 'gopay-tap <desc_text>' to tap a specific element.")
            return 1

        # Find and tap the CTA
        cta_lower = cta_desc.lower()
        target_node = None
        for node in nodes:
            if node.content_desc and cta_lower in node.content_desc.lower():
                target_node = node
                break

        if not target_node:
            print(f"CTA node not found: {cta_desc}")
            return 1

        # Calculate center and tap
        bounds = target_node.bounds
        parts = bounds.replace("][", ",").strip("[]").split(",")
        x1, y1, x2, y2 = map(int, parts)
        center_x = (x1 + x2) // 2
        center_y = (y1 + y2) // 2

        print(f"Tapping CTA: {target_node.content_desc}")
        print(f"Bounds: {bounds}")
        print(f"Center: ({center_x}, {center_y})")

        adb.tap(center_x, center_y)
        print("Tap sent.")

        # Wait a moment and inspect the new page
        import time
        time.sleep(2)
        print("\nInspecting new page...")
        return run_gopay_inspect(args)
    except (AndroidDeviceError, RuntimeError, ValueError) as exc:
        print(f"Command failed: {exc}")
        return 1


def run_gopay_input(args: argparse.Namespace) -> int:
    """Input phone number on GoPay phone input page."""
    try:
        _, adb = _create_gopay_adb(args)
    except (RuntimeError, ValueError) as exc:
        print(f"Initialization failed: {exc}")
        return 2

    try:
        _, nodes = dump_ui_nodes(adb)

        # Find phone input field - look for "Nomor HP" content-desc
        input_node = None
        for node in nodes:
            if node.content_desc and "nomor hp" in node.content_desc.lower():
                input_node = node
                break

        if not input_node:
            print("Phone input field not found.")
            print("Looking for input-related nodes:")
            for node in nodes:
                if node.content_desc and ("nomor" in node.content_desc.lower() or "phone" in node.content_desc.lower()):
                    print(f"  - {node.content_desc}")
            return 1

        # Tap on input field to focus it
        bounds = input_node.bounds
        parts = bounds.replace("][", ",").strip("[]").split(",")
        x1, y1, x2, y2 = map(int, parts)
        center_x = (x1 + x2) // 2
        center_y = (y1 + y2) // 2

        print(f"Tapping input field: {input_node.content_desc}")
        print(f"Bounds: {bounds}")
        print(f"Center: ({center_x}, {center_y})")
        adb.tap(center_x, center_y)

        import time
        time.sleep(0.5)

        # Input phone number
        phone = args.phone_number
        print(f"Inputting phone number: {phone}")
        adb.input_text(phone)

        time.sleep(0.5)

        # Now tap "Lanjut" button
        print("Looking for 'Lanjut' button...")
        lanjut_node = None
        for node in nodes:
            if node.content_desc and "lanjut" in node.content_desc.lower():
                # Make sure it's not "Lanjut dengan Google"
                if "google" not in node.content_desc.lower():
                    lanjut_node = node
                    break

        if lanjut_node:
            bounds = lanjut_node.bounds
            parts = bounds.replace("][", ",").strip("[]").split(",")
            x1, y1, x2, y2 = map(int, parts)
            center_x = (x1 + x2) // 2
            center_y = (y1 + y2) // 2

            print(f"Tapping 'Lanjut' button")
            print(f"Bounds: {bounds}")
            adb.tap(center_x, center_y)

            # Wait and inspect new page
            time.sleep(2)
            print("\nInspecting new page...")
            return run_gopay_inspect(args)
        else:
            print("'Lanjut' button not found. You may need to tap it manually.")
            return 0
    except (AndroidDeviceError, RuntimeError, ValueError) as exc:
        print(f"Command failed: {exc}")
        return 1


def run_gopay_full(args: argparse.Namespace) -> int:
    """Run full GoPay registration flow end-to-end from CLI."""
    from .gopay_flow import FlowState
    from .nexsms_client import NexSMSError, is_phone_code_timeout_error

    try:
        runtime = create_gopay_runtime(
            config_path=args.config,
            adb_path=args.adb_path,
            device_serial=args.device,
            tts_enabled=not args.mute,
            log_callback=print,
        )
    except (OCRUnavailableError, RuntimeError, ValueError) as exc:
        print(f"Initialization failed: {exc}")
        return 2

    # Apply CLI overrides
    if args.poll_timeout is not None:
        runtime.flow.config.poll_timeout = args.poll_timeout
    if args.step_delay is not None:
        runtime.flow.config.step_delay = args.step_delay
    if args.phone:
        runtime.flow.ctx.phone_number = args.phone
        print(f"  Reusing phone:   {args.phone}")

    print("=" * 60)
    print("GoPay Registration - Full Run")
    print("=" * 60)
    print(f"  Config:          {args.config}")
    print(f"  Max steps:       {args.max_steps}")
    print(f"  Poll timeout:    {runtime.flow.config.poll_timeout}s")
    print(f"  Step delay:      {runtime.flow.config.step_delay}s")
    print(f"  OTP retry:       {'on' if args.retry_on_otp_timeout else 'off'}")
    print(f"  Activation DB:   {runtime.flow.config.activation_db_path or 'disabled'}")
    print(
        "  Reuse existing: "
        f">{runtime.flow.config.reuse_existing_number_min_remaining_minutes}m remaining"
    )
    print(f"  Activation TTL:  {runtime.flow.config.activation_validity_minutes}m")
    print(f"  Same-number retry limit: {runtime.flow.config.same_number_retry_limit}")
    print(f"  Expiry guard:    {runtime.flow.config.same_number_expiry_guard_minutes}m")
    print(f"  Country:         {runtime.flow.config.country_name}")
    print(f"  Service:         {runtime.flow.config.service_code}")
    print("=" * 60)

    max_phone_cycles = 3 if args.retry_on_otp_timeout else 1
    user_supplied_phone = bool(args.phone)

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
            print(f"Marked phone number invalid: {phone_number}")
            print(f"  Reason: {reason}")
            if result:
                print(f"  NexSMS: {result}")
        except NexSMSError as exc:
            print(f"Warning: failed to invalidate phone number {phone_number}: {exc}")

    pending_phone_meta = snapshot_phone_meta()
    attempt = 1
    phone_cycle = 1
    while phone_cycle <= max_phone_cycles:
        if attempt > 1:
            print(f"\n{'='*60}")
            print(f"RETRY attempt {attempt} (phone cycle {phone_cycle}/{max_phone_cycles})")
            print(f"{'='*60}")
            runtime.flow.reset()
            if pending_phone_meta.get("phone_number"):
                restore_phone_meta(pending_phone_meta)
            if user_supplied_phone and runtime.flow.ctx.phone_number:
                runtime.flow.ctx.phone_number = args.phone
                print(f"Reusing existing phone number: {runtime.flow.ctx.phone_number}")
            elif runtime.flow.ctx.phone_number:
                same_retry_count = int(runtime.flow.ctx.phone_retry_count or 0)
                remaining_minutes = current_phone_remaining_minutes(snapshot_phone_meta())
                if same_retry_count > 0:
                    if remaining_minutes is None:
                        print(
                            "Reusing existing phone number: "
                            f"{runtime.flow.ctx.phone_number} (same-number retry {same_retry_count})"
                        )
                    else:
                        print(
                            "Reusing existing phone number: "
                            f"{runtime.flow.ctx.phone_number} "
                            f"(same-number retry {same_retry_count}, remaining {remaining_minutes:.1f}m)"
                        )

        try:
            print(f"\n[Attempt {attempt}] Starting registration flow...")
            final_state = runtime.flow.run(max_steps=args.max_steps)

            if final_state == FlowState.REGISTRATION_COMPLETE:
                print(f"\n{'='*60}")
                print("REGISTRATION COMPLETE!")
                print(f"  Username: {runtime.flow.ctx.username}")
                print(f"  Phone:    {runtime.flow.ctx.phone_number}")
                print(f"{'='*60}")
                return 0

            # OTP timeout or generic error -> retry if enabled
            if final_state in (FlowState.ERROR, FlowState.WAITING_OTP):
                err = runtime.flow.ctx.error_message or final_state.value
                print(f"\nFlow ended: {final_state.value} ({err})")
                if (
                    args.retry_on_otp_timeout
                    and is_phone_code_timeout_error(err)
                ):
                    phone_meta = snapshot_phone_meta()
                    invalidate, reason = should_invalidate_phone(phone_meta)
                    if invalidate:
                        if user_supplied_phone:
                            print(f"User-supplied phone exceeded retry policy: {reason}")
                            return 1
                        invalidate_current_phone(reason)
                        pending_phone_meta = {}
                        phone_cycle += 1
                        attempt += 1
                        if phone_cycle > max_phone_cycles:
                            break
                        print("Will retry with a new phone number...")
                        continue

                    phone_meta["phone_retry_count"] = int(phone_meta.get("phone_retry_count") or 0) + 1
                    pending_phone_meta = phone_meta
                    remaining_minutes = current_phone_remaining_minutes(phone_meta)
                    if user_supplied_phone:
                        print(
                            "OTP polling timed out. Will retry with the same user-supplied phone number..."
                        )
                    else:
                        if remaining_minutes is None:
                            print(
                                "OTP polling timed out. Will retry with the same phone number "
                                f"({phone_meta['phone_retry_count']}/{runtime.flow.config.same_number_retry_limit})..."
                            )
                        else:
                            print(
                                "OTP polling timed out. Will retry with the same phone number "
                                f"({phone_meta['phone_retry_count']}/{runtime.flow.config.same_number_retry_limit}, "
                                f"remaining {remaining_minutes:.1f}m)..."
                            )
                    attempt += 1
                    continue
                return 1

            if final_state == FlowState.MANUAL:
                print(f"\nFlow requires manual intervention (state: {final_state.value}).")
                return 1

            print(f"\nFlow ended with state: {final_state.value}")
            return 1

        except KeyboardInterrupt:
            print("\n\nInterrupted by user.")
            print(f"Current state: {runtime.flow.ctx.current_state.value}")
            if runtime.flow.ctx.phone_number:
                print(f"Phone: {runtime.flow.ctx.phone_number}")
            return 130
        except (AndroidDeviceError, NexSMSError, RuntimeError, ValueError) as exc:
            print(f"\nFlow error: {exc}")
            if (
                args.retry_on_otp_timeout
                and is_phone_code_timeout_error(str(exc))
            ):
                phone_meta = snapshot_phone_meta()
                invalidate, reason = should_invalidate_phone(phone_meta)
                if invalidate:
                    if user_supplied_phone:
                        print(f"User-supplied phone exceeded retry policy: {reason}")
                        return 1
                    invalidate_current_phone(reason)
                    pending_phone_meta = {}
                    phone_cycle += 1
                    attempt += 1
                    if phone_cycle > max_phone_cycles:
                        break
                    print("Will retry with a new phone number...")
                    continue

                phone_meta["phone_retry_count"] = int(phone_meta.get("phone_retry_count") or 0) + 1
                pending_phone_meta = phone_meta
                remaining_minutes = current_phone_remaining_minutes(phone_meta)
                if user_supplied_phone:
                    print("OTP polling timed out. Retrying with the same user-supplied phone number...")
                else:
                    if remaining_minutes is None:
                        print(
                            "OTP polling timed out. Retrying with the same phone number "
                            f"({phone_meta['phone_retry_count']}/{runtime.flow.config.same_number_retry_limit})..."
                        )
                    else:
                        print(
                            "OTP polling timed out. Retrying with the same phone number "
                            f"({phone_meta['phone_retry_count']}/{runtime.flow.config.same_number_retry_limit}, "
                            f"remaining {remaining_minutes:.1f}m)..."
                        )
                attempt += 1
                continue
            return 1

    print("All attempts exhausted.")
    return 1


def run_assist_shell(engine) -> None:
    print("Assist mode commands: start-app, scan, repeat, tap <index>, tap-text <text>, auto-step, back, quit")
    last_snapshot = None

    while True:
        raw = input("assist> ").strip()
        if not raw:
            continue
        if raw == "quit":
            break
        if raw == "start-app":
            engine.start_app()
            continue
        if raw == "scan":
            last_snapshot, _ = engine.scan_and_describe()
            for line in engine.list_snapshot_lines(last_snapshot):
                print(line)
            continue
        if raw == "repeat":
            if last_snapshot is None:
                print("No prior scan available.")
            else:
                description = engine.describe_snapshot(last_snapshot)
                print(description)
                engine.speaker.say(description)
            continue
        if raw.startswith("tap-text "):
            target_text = raw[len("tap-text ") :].strip()
            result = engine.tap_text(target_text)
            print(result.message)
            continue
        if raw.startswith("tap "):
            if last_snapshot is None:
                print("Scan first.")
                continue
            index_text = raw[len("tap ") :].strip()
            try:
                index = int(index_text) - 1
                block = last_snapshot.ordered_texts()[index]
            except (ValueError, IndexError):
                print("Invalid OCR index.")
                continue
            x, y = block.center()
            engine.adb.tap(x, y)
            message = f"Tapped OCR line {index + 1}: {block.text}"
            print(message)
            engine.speaker.say(message)
            continue
        if raw == "auto-step":
            result = engine.auto_step()
            print(result.message)
            continue
        if raw == "back":
            engine.adb.keyevent("KEYCODE_BACK")
            print("Sent BACK")
            engine.speaker.say("已返回。")
            continue
        print("Unknown command.")
