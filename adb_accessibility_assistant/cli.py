from __future__ import annotations

import argparse
import time
from pathlib import Path

from .adb_client import ADBClient, AndroidDeviceError
from .api_server import serve_api
from .bluestacks_config import (
    DEFAULT_BLUESTACKS_CONF_PATH,
    backup_bluestacks_conf,
    get_instance_profile,
    list_instances,
    list_running_bluestacks_processes,
    load_bluestacks_conf,
    save_bluestacks_conf,
    stop_bluestacks_processes,
    update_instance_profile,
)
from .bluestacks_player_ui import BlueStacksSettingsController
from .config import AppConfig, load_config
from .gopay_flow import FlowState
from .gopay_pages import actionable_nodes, detect_gopay_page
from .gopay_recording import build_page_record, save_page_record
from .gopay_tasks import prepare_phone_input_task, run_gopay_full_task
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

    api_server = subparsers.add_parser(
        "api-server",
        parents=[common],
        help="Start HTTP API server for background GoPay tasks",
    )
    api_server.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    api_server.add_argument("--port", type=int, default=8787, help="Bind port (default: 8787)")
    api_server.add_argument(
        "--callback-timeout",
        type=float,
        default=10.0,
        help="HTTP callback timeout in seconds (default: 10)",
    )

    bluestacks_show = subparsers.add_parser(
        "bluestacks-device-show",
        help="Show BlueStacks instance device profile fields from bluestacks.conf",
    )
    bluestacks_show.add_argument(
        "--conf-path",
        type=Path,
        default=DEFAULT_BLUESTACKS_CONF_PATH,
        help="Path to BlueStacks bluestacks.conf",
    )
    bluestacks_show.add_argument(
        "--instance",
        default=None,
        help="Specific instance name, e.g. Rvc64. Omit to list all instances.",
    )

    bluestacks_set = subparsers.add_parser(
        "bluestacks-device-set",
        help="Update BlueStacks instance device profile fields in bluestacks.conf",
    )
    bluestacks_set.add_argument(
        "--conf-path",
        type=Path,
        default=DEFAULT_BLUESTACKS_CONF_PATH,
        help="Path to BlueStacks bluestacks.conf",
    )
    bluestacks_set.add_argument(
        "--instance",
        required=True,
        help="Instance name, e.g. Rvc64",
    )
    bluestacks_set.add_argument("--profile-code", default=None, help="BlueStacks device_profile_code value")
    bluestacks_set.add_argument("--brand", default=None, help="device_custom_brand value")
    bluestacks_set.add_argument("--manufacturer", default=None, help="device_custom_manufacturer value")
    bluestacks_set.add_argument("--model", default=None, help="device_custom_model value")
    bluestacks_set.add_argument(
        "--stop-if-running",
        action="store_true",
        help="Force-stop BlueStacks processes before writing config",
    )
    bluestacks_set.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing the config file",
    )

    bluestacks_switch = subparsers.add_parser(
        "bluestacks-preset-switch",
        help="Switch the current BlueStacks instance device preset through the player Settings > Phone UI",
    )
    bluestacks_switch.add_argument(
        "--conf-path",
        type=Path,
        default=DEFAULT_BLUESTACKS_CONF_PATH,
        help="Path to BlueStacks bluestacks.conf",
    )
    bluestacks_switch.add_argument(
        "--instance",
        required=True,
        help="Instance name, e.g. Rvc64",
    )
    bluestacks_switch.add_argument(
        "--window-title",
        default=None,
        help="Override player window title, e.g. 'BlueStacks App Player 1'",
    )
    bluestacks_switch.add_argument(
        "--preset",
        default=None,
        help="Target preset display name, e.g. 'Samsung Galaxy S20+'",
    )
    bluestacks_switch.add_argument(
        "--steps",
        type=int,
        default=1,
        help="Cycle this many preset steps when --preset is omitted (default: 1)",
    )
    bluestacks_switch.add_argument(
        "--max-cycles",
        type=int,
        default=30,
        help="Maximum forward cycles when searching for --preset (default: 30)",
    )
    bluestacks_switch.add_argument(
        "--save-wait",
        type=float,
        default=3.0,
        help="Seconds to wait after saving before verification (default: 3.0)",
    )
    bluestacks_switch.add_argument(
        "--adb-path",
        default=None,
        help="Override adb executable path for runtime verification",
    )
    bluestacks_switch.add_argument(
        "--device",
        default=None,
        help="Override adb serial for runtime verification",
    )
    bluestacks_switch.add_argument(
        "--no-verify-adb",
        action="store_true",
        help="Skip adb getprop verification after switching",
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
    if args.command == "api-server":
        return run_api_server(args)
    if args.command == "bluestacks-device-show":
        return run_bluestacks_device_show(args)
    if args.command == "bluestacks-device-set":
        return run_bluestacks_device_set(args)
    if args.command == "bluestacks-preset-switch":
        return run_bluestacks_preset_switch(args)

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
        result = prepare_phone_input_task(
            config_path=args.config,
            adb_path=args.adb_path,
            device_serial=args.device,
            mute=args.mute,
            max_steps=args.max_steps,
            log_callback=print,
        )
    except (OCRUnavailableError, RuntimeError, ValueError) as exc:
        print(f"Initialization failed: {exc}")
        return 2

    print(result["message"])
    return 0 if result.get("ok") else 1


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
    if not args.config:
        print("Error: --config is required for run-gopay")
        return 1

    print("=" * 60)
    print("GoPay Registration - Full Run")
    print("=" * 60)
    print(f"  Config:          {args.config}")
    print(f"  Max steps:       {args.max_steps}")
    print(f"  OTP retry:       {'on' if args.retry_on_otp_timeout else 'off'}")
    if args.phone:
        print(f"  Reuse phone:     {args.phone}")
    print("=" * 60)

    try:
        result = run_gopay_full_task(
            config_path=args.config,
            adb_path=args.adb_path,
            device_serial=args.device,
            mute=args.mute,
            max_steps=args.max_steps,
            poll_timeout=args.poll_timeout,
            step_delay=args.step_delay,
            retry_on_otp_timeout=args.retry_on_otp_timeout,
            phone=args.phone,
            log_callback=print,
        )
    except (OCRUnavailableError, RuntimeError, ValueError) as exc:
        print(f"Initialization failed: {exc}")
        return 2
    if result.get("ok"):
        data = result.get("data") or {}
        print(f"\n{'='*60}")
        print("REGISTRATION COMPLETE!")
        print(f"  Username: {data.get('username')}")
        print(f"  Phone:    {data.get('phone')}")
        print(f"{'='*60}")
        return 0

    print(f"\nFlow ended: {result.get('state')} ({result.get('message')})")
    return 1


def run_api_server(args: argparse.Namespace) -> int:
    try:
        serve_api(
            host=args.host,
            port=args.port,
            default_config_path=args.config,
            mute=args.mute,
            callback_timeout=args.callback_timeout,
            log_callback=print,
        )
        return 0
    except KeyboardInterrupt:
        print("\nAPI server stopped.")
        return 0
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"API server failed: {exc}")
        return 1


def run_bluestacks_device_show(args: argparse.Namespace) -> int:
    try:
        conf = load_bluestacks_conf(args.conf_path)
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"Failed to load BlueStacks config: {exc}")
        return 1

    try:
        if args.instance:
            profile = get_instance_profile(conf, args.instance)
            print(f"Config:         {conf.path}")
            print(f"Instance:       {profile.instance_name}")
            print(f"Display name:   {profile.display_name}")
            print(f"ADB port:       {profile.adb_port or 'unknown'}")
            print(f"Profile code:   {profile.profile_code or '(empty)'}")
            print(f"Custom brand:   {profile.custom_brand or '(empty)'}")
            print(f"Manufacturer:   {profile.custom_manufacturer or '(empty)'}")
            print(f"Custom model:   {profile.custom_model or '(empty)'}")
            return 0

        print(f"Config: {conf.path}")
        for instance_name in list_instances(conf):
            profile = get_instance_profile(conf, instance_name)
            print("-" * 48)
            print(f"Instance:       {profile.instance_name}")
            print(f"Display name:   {profile.display_name}")
            print(f"ADB port:       {profile.adb_port or 'unknown'}")
            print(f"Profile code:   {profile.profile_code or '(empty)'}")
            print(f"Custom brand:   {profile.custom_brand or '(empty)'}")
            print(f"Manufacturer:   {profile.custom_manufacturer or '(empty)'}")
            print(f"Custom model:   {profile.custom_model or '(empty)'}")
        return 0
    except ValueError as exc:
        print(f"Failed to read instance profile: {exc}")
        return 1


def run_bluestacks_device_set(args: argparse.Namespace) -> int:
    if (
        args.profile_code is None
        and args.brand is None
        and args.manufacturer is None
        and args.model is None
    ):
        print("Error: specify at least one of --profile-code, --brand, --manufacturer, or --model")
        return 1

    running = list_running_bluestacks_processes()
    if running and not args.stop_if_running and not args.dry_run:
        print("BlueStacks is currently running. Stop it before editing device fields, or rerun with --stop-if-running.")
        print(f"Running processes: {', '.join(running)}")
        return 1
    if running and args.dry_run and not args.stop_if_running:
        print(f"BlueStacks is currently running: {', '.join(running)}")
        print("Continuing because this is a dry-run only.")
    if running and args.stop_if_running:
        stopped = stop_bluestacks_processes()
        print(f"Stopped processes: {', '.join(stopped) if stopped else 'none'}")

    try:
        conf = load_bluestacks_conf(args.conf_path)
        updated_conf, changes = update_instance_profile(
            conf,
            instance_name=args.instance,
            profile_code=args.profile_code,
            custom_brand=args.brand,
            custom_manufacturer=args.manufacturer,
            custom_model=args.model,
        )
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"Failed to prepare BlueStacks config update: {exc}")
        return 1

    if not changes:
        print("No changes detected.")
        return 0

    print(f"Config:    {conf.path}")
    print(f"Instance:  {args.instance}")
    for key, (old_value, new_value) in changes.items():
        print(f"{key}: '{old_value}' -> '{new_value}'")

    if args.dry_run:
        print("Dry-run only. Config file was not modified.")
        return 0

    try:
        backup_path = backup_bluestacks_conf(conf.path)
        save_bluestacks_conf(updated_conf)
        print(f"Backup:    {backup_path}")
        print("BlueStacks config updated. Restart the instance to apply the new device fields.")
        return 0
    except OSError as exc:
        print(f"Failed to write BlueStacks config: {exc}")
        return 1


def run_bluestacks_preset_switch(args: argparse.Namespace) -> int:
    try:
        conf_before = load_bluestacks_conf(args.conf_path)
        profile_before = get_instance_profile(conf_before, args.instance)
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"Failed to load BlueStacks config: {exc}")
        return 1

    window_title = args.window_title or profile_before.display_name
    if not window_title:
        print("Failed to resolve BlueStacks window title. Pass --window-title explicitly.")
        return 1

    if args.preset:
        print(f"Switching instance {args.instance} to preset: {args.preset}")
    else:
        print(f"Cycling instance {args.instance} preset by {args.steps} step(s)")
    print(f"Window title: {window_title}")

    controller = BlueStacksSettingsController(window_title=window_title)
    try:
        result = controller.switch_preset(
            target_preset=args.preset,
            steps=args.steps,
            save=True,
            save_wait_seconds=args.save_wait,
            max_cycles=args.max_cycles,
        )
    except RuntimeError as exc:
        print(f"Preset switch failed: {exc}")
        return 1

    print(f"Preset before: {result.before_preset}")
    print(f"Preset after:  {result.after_preset}")
    print(f"Changed:       {'yes' if result.changed else 'no'}")

    try:
        conf_after = load_bluestacks_conf(args.conf_path)
        profile_after = get_instance_profile(conf_after, args.instance)
        print(f"Profile code before: {profile_before.profile_code or '(empty)'}")
        print(f"Profile code after:  {profile_after.profile_code or '(empty)'}")
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"Warning: failed to reload BlueStacks config after switch: {exc}")
        profile_after = None

    if args.no_verify_adb:
        return 0

    adb_serial = args.device
    if not adb_serial and profile_after and profile_after.adb_port:
        adb_serial = f"127.0.0.1:{profile_after.adb_port}"

    if not adb_serial:
        print("ADB verification skipped: no device serial override and no adb_port found for the instance.")
        return 0

    adb = ADBClient(adb_path=args.adb_path or "adb", device_serial=adb_serial)
    try:
        adb.assert_available()
        model_result = adb.run("shell", "getprop", "ro.product.model")
        manufacturer_result = adb.run("shell", "getprop", "ro.product.manufacturer")
        brand_result = adb.run("shell", "getprop", "ro.product.brand")
        assert isinstance(model_result.stdout, str)
        assert isinstance(manufacturer_result.stdout, str)
        assert isinstance(brand_result.stdout, str)
        model = model_result.stdout.strip()
        manufacturer = manufacturer_result.stdout.strip()
        brand = brand_result.stdout.strip()
        print(f"ADB serial:         {adb_serial}")
        print(f"ro.product.model:   {model}")
        print(f"ro.product.brand:   {brand}")
        print(f"ro.product.manufacturer: {manufacturer}")
        return 0
    except (AndroidDeviceError, RuntimeError, ValueError) as exc:
        print(f"ADB verification failed: {exc}")
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
