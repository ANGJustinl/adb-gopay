"""GoPay registration flow state machine."""

from __future__ import annotations

import io
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .adb_client import ADBClient
from .automation import AutomationEngine
from .config import AppConfig
from .credential_generator import generate_pin, generate_username, save_credentials
from .gopay_pages import detect_gopay_page
from .models import ScreenSnapshot
from .nexsms_client import NexSMSClient, NexSMSError, is_phone_code_timeout_error
from .ocr import OCREngine
from .tts import Speaker
from .ui_dump import UINode, dump_ui_nodes, find_first_node, parse_bounds, tap_node


class FlowState(str, Enum):
    """Registration flow states."""
    INIT = "init"
    WAITING_LOCATION_PERMISSION = "waiting_location_permission"
    WAITING_LANDING_INTRO = "waiting_landing_intro"
    WAITING_PHONE_INPUT = "waiting_phone_input"
    PHONE_ENTERED = "phone_entered"
    WAITING_SIGNUP_TERMS = "waiting_signup_terms"
    WAITING_VERIFICATION_METHOD = "waiting_verification_method"
    WAITING_OTP_METHOD_SWITCH = "waiting_otp_method_switch"
    WAITING_OTP = "waiting_otp"
    OTP_ENTERED = "otp_entered"
    WAITING_POST_OTP_PAGE = "waiting_post_otp_page"
    WAITING_USERNAME = "waiting_username"
    USERNAME_SET = "username_set"
    WAITING_HOME = "waiting_home"
    WAITING_PROFILE_DASHBOARD = "waiting_profile_dashboard"
    WAITING_PROTECTION_OVERVIEW = "waiting_protection_overview"
    WAITING_PIN = "waiting_pin"
    PIN_SET = "pin_set"
    WAITING_PIN_CONFIRM = "waiting_pin_confirm"
    PIN_CONFIRMED = "pin_confirmed"
    REGISTRATION_COMPLETE = "registration_complete"
    ERROR = "error"
    MANUAL = "manual"


@dataclass
class FlowContext:
    """Context data for the registration flow."""
    phone_number: str = ""
    phone_acquired_at_epoch: float = 0.0
    phone_expiry_epoch: float = 0.0
    phone_retry_count: int = 0
    otp_code: str = ""
    otp_resend_count: int = 0
    last_otp_code: str = ""
    otp_status_baseline: str = ""
    otp_phase: str = "initial"
    username: str = ""
    pin: str = ""
    current_state: FlowState = FlowState.INIT
    pin_flow_source: str = ""
    error_message: str = ""
    retry_count: int = 0
    max_retries: int = 3


# OCR anchor patterns for page detection (Indonesian)
PAGE_ANCHORS: dict[FlowState, list[str]] = {
    FlowState.WAITING_LOCATION_PERMISSION: [
        "Izinkan akses lokasi",
        "Perlindungan dari penipuan",
        "Promo sekitarmu",
        "Data lokasi aman",
        "Nanti aja",
    ],
    FlowState.WAITING_LANDING_INTRO: [
        "Masukkan nomor HP-mu",
        "Bahasa Indonesia",
        "Help",
    ],
    FlowState.WAITING_PHONE_INPUT: [
        "Nomor HP",
        "Masukkan nomor",
        "nomor handphone",
        "Phone number",
        "Enter phone",
    ],
    FlowState.WAITING_SIGNUP_TERMS: [
        "Penting sebelum kamu lanjut",
        "Ketentuan Layanan",
        "Pemberitahuan Privasi",
    ],
    FlowState.WAITING_VERIFICATION_METHOD: [
        "Pilih metode verifikasi",
        "OTP via WhatsApp",
        "OTP via SMS",
    ],
    FlowState.WAITING_OTP_METHOD_SWITCH: [
        "Cek WhatsApp",
        "Buka WhatsApp",
        "Coba Metode Lainnya",
    ],
    FlowState.WAITING_OTP: [
        "Kode verifikasi",
        "OTP",
        "kode OTP",
        "Masukkan kode",
        "Verification code",
    ],
    FlowState.WAITING_USERNAME: [
        "Isi data diri dulu",
        "Masukkan namamu",
        "Buat akun",
        "Nama",
        "Username",
        "Buat username",
        "nama pengguna",
        "Create username",
        "Enter username",
    ],
    FlowState.WAITING_HOME: [
        "Eksplor fitur GoPay",
        "Spesial cuma buat kamu",
        "Transfer & Terima",
        "Top Up Games",
    ],
    FlowState.WAITING_PROFILE_DASHBOARD: [
        "Pengaturan & keamanan",
        "Perlindungan akun",
        "Pengaturan akun & aplikasi",
        "Bantuan",
    ],
    FlowState.WAITING_PROTECTION_OVERVIEW: [
        "0/4 langkah tuntas",
        "Izin lokasi",
        "Pasang PIN",
        "Verifikasi Email",
        "Upgrade ke GoPay Plus",
    ],
    FlowState.WAITING_PIN: [
        "Pasang PIN",
        "Tips bikin PIN yang aman",
        "PIN GoPay bikin bayar-bayar",
        "Buat PIN",
        "Masukkan PIN",
        "Create PIN",
        "Enter PIN",
        "6 digit PIN",
    ],
    FlowState.WAITING_PIN_CONFIRM: [
        "Konfirmasi PIN",
        "Confirm PIN",
        "Masukkan ulang PIN",
        "Ulangi PIN",
    ],
    FlowState.REGISTRATION_COMPLETE: [
        "PIN confirmed",
    ],
}

UI_PAGE_STATE_MAP: dict[str, FlowState] = {
    "location_permission_intro": FlowState.WAITING_LOCATION_PERMISSION,
    "landing_intro": FlowState.WAITING_LANDING_INTRO,
    "phone_input": FlowState.WAITING_PHONE_INPUT,
    "signup_terms": FlowState.WAITING_SIGNUP_TERMS,
    "verification_method": FlowState.WAITING_VERIFICATION_METHOD,
    "otp_input_whatsapp": FlowState.WAITING_OTP_METHOD_SWITCH,
    "otp_input": FlowState.WAITING_OTP,
    "profile_name_input": FlowState.WAITING_USERNAME,
    "home_dashboard": FlowState.WAITING_HOME,
    "profile_dashboard": FlowState.WAITING_PROFILE_DASHBOARD,
    "protection_overview": FlowState.WAITING_PROTECTION_OVERVIEW,
    "username_input": FlowState.WAITING_USERNAME,
    "pin_input": FlowState.WAITING_PIN,
    "pin_confirm": FlowState.WAITING_PIN_CONFIRM,
}


def detect_page_state(snapshot: ScreenSnapshot) -> FlowState | None:
    """Detect current page state from OCR text.

    Args:
        snapshot: Screen snapshot with OCR results.

    Returns:
        Detected FlowState or None if no match.
    """
    all_text = " ".join(block.text.lower() for block in snapshot.texts)

    for state, anchors in PAGE_ANCHORS.items():
        for anchor in anchors:
            if anchor.lower() in all_text:
                return state
    return None


def has_text(snapshot: ScreenSnapshot, text: str) -> bool:
    """Check if specific text exists in snapshot."""
    text_lower = text.lower()
    return any(text_lower in block.text.lower() for block in snapshot.texts)


def tap_text(snapshot: ScreenSnapshot, adb: ADBClient, text: str) -> bool:
    """Tap on text found in snapshot.

    Returns:
        True if text was found and tapped.
    """
    text_lower = text.lower()
    for block in snapshot.texts:
        if text_lower in block.text.lower():
            x, y = block.center()
            adb.tap(x, y)
            return True
    return False


@dataclass
class GoPayFlowConfig:
    """Configuration for GoPay registration flow."""
    target_package: str = "com.gojek.gopay"
    launch_activity: str | None = None
    api_key: str = ""
    nexsms_base_url: str = "https://api.nexsms.net"
    nexsms_proxy: str = ""
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
    otp_resend_limit: int = 1
    same_number_retry_limit: int = 3
    same_number_expiry_guard_minutes: float = 8.0
    username_length: int = 8
    pin_length: int = 6
    max_retries: int = 3
    step_delay: float = 2.0
    credentials_path: str = "credentials.json"


class GoPayRegistrationFlow:
    """Manages the GoPay registration flow."""

    def __init__(
        self,
        adb: ADBClient,
        ocr: OCREngine,
        speaker: Speaker,
        config: GoPayFlowConfig,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.adb = adb
        self.ocr = ocr
        self.speaker = speaker
        self.config = config
        self.log = log_callback or (lambda msg: None)
        self.ctx = FlowContext(max_retries=config.max_retries)
        self._nexsms: NexSMSClient | None = None
        self._paused = False
        self._stopped = False
        self._last_ui_nodes: list[UINode] = []
        self._last_page_id: str | None = None

    @property
    def nexsms(self) -> NexSMSClient:
        """Lazy initialize NexSMS client."""
        if self._nexsms is None:
            self._nexsms = NexSMSClient(
                api_key=self.config.api_key,
                base_url=self.config.nexsms_base_url,
                proxy=self.config.nexsms_proxy,
                activation_db_path=self.config.activation_db_path,
                reuse_existing_number_min_remaining_minutes=self.config.reuse_existing_number_min_remaining_minutes,
                activation_validity_minutes=self.config.activation_validity_minutes,
            )
        return self._nexsms

    @property
    def state(self) -> FlowState:
        return self.ctx.current_state

    @property
    def is_complete(self) -> bool:
        return self.ctx.current_state == FlowState.REGISTRATION_COMPLETE

    @property
    def is_error(self) -> bool:
        return self.ctx.current_state == FlowState.ERROR

    def pause(self) -> None:
        """Pause the flow."""
        self._paused = True
        self.log("Flow paused.")

    def resume(self) -> None:
        """Resume the flow."""
        self._paused = False
        self.log("Flow resumed.")

    def stop(self) -> None:
        """Stop the flow."""
        self._stopped = True
        self.log("Flow stopped.")

    def reset(self) -> None:
        """Reset flow to initial state."""
        self.ctx = FlowContext(max_retries=self.config.max_retries)
        self._paused = False
        self._stopped = False
        self.log("Flow reset.")

    def _wait_if_paused(self) -> None:
        """Wait while flow is paused."""
        while self._paused and not self._stopped:
            time.sleep(0.5)

    def _set_state(self, state: FlowState) -> None:
        """Transition to a new state."""
        self.ctx.current_state = state
        self.ctx.retry_count = 0
        self.log(f"State: {state.value}")

    def _start_clean_session(self) -> None:
        """Clear app data and relaunch GoPay from a clean state."""
        self.adb.wait_for_device()
        self.log(f"Clearing app data: {self.config.target_package}")
        self.adb.clear_app_data(self.config.target_package)
        time.sleep(1.0)
        self.log(f"Starting app: {self.config.target_package}")
        self.adb.start_app(self.config.target_package, self.config.launch_activity)
        self._last_page_id = None
        self._last_ui_nodes = []
        self.ctx.pin_flow_source = ""
        self.ctx.otp_code = ""
        self.ctx.otp_resend_count = 0
        time.sleep(self.config.step_delay)

    def _capture_snapshot(self) -> ScreenSnapshot:
        """Capture and OCR the current screen."""
        png = self.adb.screencap_png()
        from PIL import Image
        width, height = Image.open(io.BytesIO(png)).size
        blocks = [
            block for block in self.ocr.recognize(png)
            if block.confidence >= 0.45
        ]
        return ScreenSnapshot(width=width, height=height, texts=blocks)

    def _capture_ui_nodes(self) -> list[UINode]:
        _, nodes = dump_ui_nodes(self.adb)
        self._last_ui_nodes = nodes
        return nodes

    def _detect_and_update_state(self) -> FlowState | None:
        """Detect current page and update state if needed."""
        try:
            nodes = self._capture_ui_nodes()
            page_match = detect_gopay_page(nodes)
            if page_match:
                self._last_page_id = page_match.spec.page_id
                detected = UI_PAGE_STATE_MAP.get(page_match.spec.page_id)
                if detected and detected != self.ctx.current_state:
                    self.log(f"Detected page via UI dump: {page_match.spec.page_id}")
                    self._set_state(detected)
                return detected
        except Exception as exc:
            self.log(f"UI dump detection fallback to OCR: {exc}")

        snapshot = self._capture_snapshot()
        detected = detect_page_state(snapshot)
        if detected and detected != self.ctx.current_state:
            self.log(f"Detected page via OCR: {detected.value}")
            self._set_state(detected)

        return detected

    def _get_phone_number(self) -> str:
        """Get a phone number from NexSMS platform."""
        self.log("Getting phone number from NexSMS...")

        # Get service code - use 'ni' for GoPay/Gojek
        service_code = self.config.service_code
        if not service_code:
            services = self.nexsms.get_services()
            from .nexsms_client import find_service_code
            service_code = find_service_code(services, "gojek")
        if not service_code:
            service_code = "ni"  # Default for GoPay

        order_result = self.nexsms.acquire_number(
            service_code=service_code,
            country_name=self.config.country_name,
            country_order=self.config.country_order,
            default_price=self.config.default_price,
            min_price=self.config.min_price,
            max_price=self.config.max_price,
            preferred_price=self.config.preferred_price,
            acquire_priority=self.config.acquire_priority,
            retry_rounds=self.config.activation_retry_rounds,
            retry_delay_ms=self.config.activation_retry_delay_ms,
            log_callback=self.log,
        )
        phone = order_result["phone_number"]
        self.ctx.phone_acquired_at_epoch = float(order_result.get("acquired_at_epoch") or time.time())
        self.ctx.phone_expiry_epoch = float(order_result.get("expiry_epoch") or 0.0)
        self.ctx.phone_retry_count = 0
        self.log(
            "Got phone number: "
            f"{phone} (country={order_result['country_name']}, price={order_result['price']})"
        )
        if self.ctx.phone_expiry_epoch > 0:
            remaining_minutes = max(0.0, (self.ctx.phone_expiry_epoch - time.time()) / 60.0)
            expiry_source = str(order_result.get("expiry_source") or "unknown")
            self.log(
                f"Phone validity remaining: {remaining_minutes:.1f} minutes "
                f"(source={expiry_source})"
            )
        return phone

    def _wait_for_otp(self) -> str:
        """Wait for OTP code from NexSMS."""
        self.log(f"Waiting for {self.ctx.otp_phase} OTP on {self.ctx.phone_number}...")
        ignore_codes = {self.ctx.last_otp_code} if self.ctx.last_otp_code else None
        code = self.nexsms.wait_for_sms(
            self.ctx.phone_number,
            poll_interval=self.config.poll_interval,
            timeout=self.config.poll_timeout,
            ignore_codes=ignore_codes,
            baseline_status_text=self.ctx.otp_status_baseline or None,
        )
        self.ctx.last_otp_code = code
        self.ctx.otp_status_baseline = ""
        self.log(f"Got OTP: {code}")
        return code

    def phone_expiry_remaining_seconds(self) -> float | None:
        """Return the remaining lifetime for the current phone number, if known."""
        if self.ctx.phone_expiry_epoch <= 0:
            return None
        return self.ctx.phone_expiry_epoch - time.time()

    def _input_text(self, text: str) -> None:
        """Input text via ADB."""
        self.adb.input_text(text)
        time.sleep(0.5)

    def _phone_number_for_input(self, phone_number: str) -> str:
        """Convert a full Indonesian phone number to the local part expected by GoPay."""
        digits = "".join(ch for ch in str(phone_number or "") if ch.isdigit())
        if digits.startswith("62"):
            digits = digits[2:]
        if digits.startswith("0"):
            digits = digits[1:]
        return digits

    def _tap_confirm(self, preferred_terms: list[str] | None = None) -> None:
        """Tap a preferred confirm button, then fall back to common confirm buttons."""
        button_terms = list(preferred_terms or [])
        for term in ["Lanjut", "Buat akun", "Next", "Submit", "Konfirmasi", "Confirm", "OK", "Oke"]:
            if term not in button_terms:
                button_terms.append(term)

        try:
            nodes = self._capture_ui_nodes()
            for button_text in button_terms:
                node = find_first_node(nodes, terms=[button_text], clickable=True, enabled=True)
                if node:
                    x, y = tap_node(self.adb, node)
                    self.log(f"Tapped via UI dump: {button_text} at ({x}, {y})")
                    time.sleep(self.config.step_delay)
                    return
        except Exception as exc:
            self.log(f"UI dump confirm fallback to OCR: {exc}")

        snapshot = self._capture_snapshot()
        for button_text in button_terms:
            if tap_text(snapshot, self.adb, button_text):
                self.log(f"Tapped via OCR: {button_text}")
                time.sleep(self.config.step_delay)
                return
        # If no button found, try pressing Enter
        self.adb.keyevent("KEYCODE_ENTER")
        time.sleep(self.config.step_delay)

    def _tap_bounds_center(self, bounds: tuple[int, int, int, int], reason: str) -> bool:
        left, top, right, bottom = bounds
        if right <= left or bottom <= top:
            return False
        x = (left + right) // 2
        y = (top + bottom) // 2
        self.adb.tap(x, y)
        self.log(f"Focused {reason} via bounds heuristic at ({x}, {y})")
        time.sleep(0.5)
        return True

    def _tap_profile_tab(self) -> None:
        """Open the Profil tab from the GoPay home dashboard."""
        try:
            nodes = self._capture_ui_nodes()
            profile_node = find_first_node(nodes, terms=["Profil"], clickable=True, enabled=True)
            if profile_node:
                x, y = tap_node(self.adb, profile_node)
                self.log(f"Tapped Profil tab via UI dump at ({x}, {y})")
                time.sleep(self.config.step_delay)
                return
        except Exception as exc:
            self.log(f"Home navigation UI dump fallback: {exc}")

        snapshot = self._capture_snapshot()
        if tap_text(snapshot, self.adb, "Profil"):
            self.log("Tapped Profil tab via OCR")
            time.sleep(self.config.step_delay)
            return

        x = int(snapshot.width * 0.9)
        y = int(snapshot.height * 0.965)
        self.adb.tap(x, y)
        self.log(f"Tapped Profil tab via coordinate fallback at ({x}, {y})")
        time.sleep(self.config.step_delay)

    def _tap_landing_intro_cta(self) -> None:
        """Tap the first CTA on the GoPay landing intro screen."""
        try:
            nodes = self._capture_ui_nodes()
            target = find_first_node(
                nodes,
                terms=["Masukkan nomor HP-mu"],
                clickable=True,
                enabled=True,
            )
            if target:
                x, y = tap_node(self.adb, target)
                self.log(f"Tapped landing CTA via UI dump at ({x}, {y})")
                time.sleep(self.config.step_delay)
                return
        except Exception as exc:
            self.log(f"Landing CTA UI dump fallback: {exc}")

        snapshot = self._capture_snapshot()
        if tap_text(snapshot, self.adb, "Masukkan nomor HP-mu"):
            self.log("Tapped landing CTA via OCR")
            time.sleep(self.config.step_delay)
            return

        x = int(snapshot.width * 0.5)
        y = int(snapshot.height * 0.82)
        self.adb.tap(x, y)
        self.log(f"Tapped landing CTA via coordinate fallback at ({x}, {y})")
        time.sleep(self.config.step_delay)

    def _reject_location_permission_intro(self) -> None:
        """Dismiss the first-launch location permission intro."""
        try:
            nodes = self._capture_ui_nodes()
            reject_node = find_first_node(
                nodes,
                terms=["Nanti aja", "Nanti", "Skip", "Lewati"],
                clickable=True,
                enabled=True,
            )
            if reject_node:
                x, y = tap_node(self.adb, reject_node)
                self.log(f"Rejected location intro via UI dump at ({x}, {y})")
                time.sleep(self.config.step_delay)
                return
        except Exception as exc:
            self.log(f"Location intro UI dump fallback: {exc}")

        snapshot = self._capture_snapshot()
        for text in ["Nanti aja", "Nanti", "Skip", "Lewati"]:
            if tap_text(snapshot, self.adb, text):
                self.log(f"Rejected location intro via OCR: {text}")
                time.sleep(self.config.step_delay)
                return

        x = int(snapshot.width * 0.5)
        y = int(snapshot.height * 0.955)
        self.adb.tap(x, y)
        self.log(f"Rejected location intro via coordinate fallback at ({x}, {y})")
        time.sleep(self.config.step_delay)

    def _tap_protection_section(self) -> None:
        """Open the Perlindungan akun checklist from the profile dashboard."""
        try:
            nodes = self._capture_ui_nodes()
            target = find_first_node(
                nodes,
                terms=["Perkuat perlindunganmu di sini", "Perlindungan akun", "0%"],
                clickable=True,
                enabled=True,
            )
            if target:
                x, y = tap_node(self.adb, target)
                self.log(f"Tapped protection section via UI dump at ({x}, {y})")
                time.sleep(self.config.step_delay)
                return
        except Exception as exc:
            self.log(f"Profile protection UI dump fallback: {exc}")

        snapshot = self._capture_snapshot()
        for text in ["Perkuat perlindunganmu di sini", "Perlindungan akun", "0%"]:
            if tap_text(snapshot, self.adb, text):
                self.log(f"Tapped protection section via OCR: {text}")
                time.sleep(self.config.step_delay)
                return

        x = int(snapshot.width * 0.5)
        y = int(snapshot.height * 0.28)
        self.adb.tap(x, y)
        self.log(f"Tapped protection section via coordinate fallback at ({x}, {y})")
        time.sleep(self.config.step_delay)

    def _tap_protection_pin(self) -> None:
        """Tap the Pasang PIN item inside the protection checklist."""
        try:
            nodes = self._capture_ui_nodes()
            target = find_first_node(
                nodes,
                terms=["Buat mastiin cuma kamu yang bisa transaksi", "Pasang PIN"],
                clickable=True,
                enabled=True,
            )
            if target:
                x, y = tap_node(self.adb, target)
                self.log(f"Tapped Pasang PIN via UI dump at ({x}, {y})")
                time.sleep(self.config.step_delay)
                self.ctx.pin_flow_source = "protection_overview"
                return
        except Exception as exc:
            self.log(f"Protection PIN UI dump fallback: {exc}")

        snapshot = self._capture_snapshot()
        for text in ["Buat mastiin cuma kamu yang bisa transaksi", "Pasang PIN"]:
            if tap_text(snapshot, self.adb, text):
                self.log(f"Tapped Pasang PIN via OCR: {text}")
                time.sleep(self.config.step_delay)
                self.ctx.pin_flow_source = "protection_overview"
                return

        x = int(snapshot.width * 0.34)
        y = int(snapshot.height * 0.49)
        self.adb.tap(x, y)
        self.log(f"Tapped Pasang PIN via coordinate fallback at ({x}, {y})")
        time.sleep(self.config.step_delay)
        self.ctx.pin_flow_source = "protection_overview"

    def _tap_otp_resend(self) -> bool:
        """Tap the Kirim Ulang button on the OTP page."""
        try:
            nodes = self._capture_ui_nodes()
            resend_node = find_first_node(
                nodes,
                terms=["Kirim Ulang", "Kirim ulang", "Resend"],
                clickable=True,
                enabled=True,
            )
            if resend_node:
                x, y = tap_node(self.adb, resend_node)
                self.log(f"Tapped OTP resend via UI dump at ({x}, {y})")
                time.sleep(self.config.step_delay)
                return True
        except Exception as exc:
            self.log(f"OTP resend UI dump fallback: {exc}")

        snapshot = self._capture_snapshot()
        for text in ["Kirim Ulang", "Kirim ulang", "Resend"]:
            if tap_text(snapshot, self.adb, text):
                self.log(f"Tapped OTP resend via OCR: {text}")
                time.sleep(self.config.step_delay)
                return True

        return False

    def _tap_other_otp_method(self) -> bool:
        """Tap the control that opens alternate OTP methods."""
        try:
            nodes = self._capture_ui_nodes()
            switch_node = find_first_node(
                nodes,
                terms=["Coba Metode Lainnya", "Metode Lainnya", "Try another method"],
                clickable=True,
                enabled=True,
            )
            if switch_node:
                x, y = tap_node(self.adb, switch_node)
                self.log(f"Tapped alternate OTP method via UI dump at ({x}, {y})")
                time.sleep(self.config.step_delay)
                return True
        except Exception as exc:
            self.log(f"OTP method-switch UI dump fallback: {exc}")

        snapshot = self._capture_snapshot()
        for text in ["Coba Metode Lainnya", "Metode Lainnya", "Try another method"]:
            if tap_text(snapshot, self.adb, text):
                self.log(f"Tapped alternate OTP method via OCR: {text}")
                time.sleep(self.config.step_delay)
                return True

        return False

    def _input_pin_digits(self, pin: str) -> None:
        """Enter a PIN using the on-screen keypad when possible."""
        for digit in pin:
            try:
                nodes = self._capture_ui_nodes()
                key_node = find_first_node(
                    nodes,
                    terms=[digit],
                    clickable=True,
                    enabled=True,
                )
                if key_node:
                    x, y = tap_node(self.adb, key_node)
                    self.log(f"Tapped PIN digit {digit} via UI dump at ({x}, {y})")
                    time.sleep(0.2)
                    continue
            except Exception as exc:
                self.log(f"PIN keypad UI dump fallback for {digit}: {exc}")

            self.adb.keyevent(f"KEYCODE_{digit}")
            self.log(f"Entered PIN digit {digit} via keyevent")
            time.sleep(0.2)

    def _focus_phone_input_via_layout(self, nodes: list[UINode]) -> bool:
        country_node = find_first_node(nodes, terms=["Country code", "+62"], enabled=True)
        row_node = find_first_node(nodes, terms=["Nomor HP", "Bidang Masukan"], enabled=True)
        if not country_node or not row_node:
            return False

        row_left, row_top, row_right, row_bottom = parse_bounds(row_node.bounds)
        _, country_top, country_right, country_bottom = parse_bounds(country_node.bounds)

        field_left = max(country_right + 24, row_left + 138)
        field_top = max(country_top - 1, row_top + 72)
        field_right = row_right - 54
        field_bottom = min(country_bottom - 7, row_bottom - 8)
        return self._tap_bounds_center(
            (field_left, field_top, field_right, field_bottom),
            "phone input",
        )

    def _focus_otp_input_via_layout(self, nodes: list[UINode]) -> bool:
        row_node = find_first_node(nodes, terms=["OTP, wajib Bidang Masukan", "Masukkan OTP"], enabled=True)
        resend_node = find_first_node(nodes, terms=["Kirim Ulang"], enabled=True)
        if not row_node:
            return False

        row_left, row_top, row_right, row_bottom = parse_bounds(row_node.bounds)
        field_left = row_left + 24
        field_top = row_top + 72
        field_right = row_right - 217
        field_bottom = row_bottom - 8

        if resend_node:
            resend_left, _, _, resend_bottom = parse_bounds(resend_node.bounds)
            field_right = min(field_right, resend_left - 24)
            field_bottom = min(field_bottom, resend_bottom)

        return self._tap_bounds_center(
            (field_left, field_top, field_right, field_bottom),
            "otp input",
        )

    def _focus_profile_name_input_via_layout(self, nodes: list[UINode]) -> bool:
        placeholder_node = find_first_node(nodes, terms=["Masukkan namamu"], enabled=True)
        if placeholder_node:
            left, top, right, bottom = parse_bounds(placeholder_node.bounds)
            return self._tap_bounds_center(
                (max(24, left - 24), max(0, top - 12), right + 96, bottom + 20),
                "profile-name input",
            )

        label_node = find_first_node(nodes, terms=["Nama"], enabled=True)
        if not label_node:
            return False

        screen_right = max(parse_bounds(node.bounds)[2] for node in nodes) if nodes else 1080
        _, _, _, label_bottom = parse_bounds(label_node.bounds)
        return self._tap_bounds_center(
            (24, label_bottom + 8, max(200, screen_right - 24), label_bottom + 84),
            "profile-name input",
        )

    def _ui_contains_text(self, nodes: list[UINode], text: str) -> bool:
        needle = text.casefold().strip()
        if not needle:
            return False
        for node in nodes:
            if needle in (node.text or "").casefold():
                return True
            if needle in (node.content_desc or "").casefold():
                return True
        return False

    def _focus_first_input(
        self,
        nodes: list[UINode],
        *,
        fallback_terms: list[str] | None = None,
        page_id: str | None = None,
    ) -> bool:
        edit_node = find_first_node(
            nodes,
            class_names=["android.widget.EditText"],
            enabled=True,
        )
        if edit_node:
            x, y = tap_node(self.adb, edit_node)
            self.log(f"Focused EditText via UI dump at ({x}, {y})")
            time.sleep(0.5)
            return True

        if page_id == "phone_input" and self._focus_phone_input_via_layout(nodes):
            return True

        if page_id == "otp_input" and self._focus_otp_input_via_layout(nodes):
            return True

        if page_id == "profile_name_input" and self._focus_profile_name_input_via_layout(nodes):
            return True

        if fallback_terms:
            target = find_first_node(nodes, terms=fallback_terms, enabled=True)
            if target:
                x, y = target.center()
                self.adb.tap(x, y + 50)
                self.log(f"Focused fallback field via UI dump at ({x}, {y + 50})")
                time.sleep(0.5)
                return True
        return False

    def _handle_phone_input(self) -> None:
        """Handle phone number input state."""
        if not self.ctx.phone_number:
            try:
                self.ctx.phone_number = self._get_phone_number()
            except NexSMSError as exc:
                self.ctx.error_message = str(exc)
                self._set_state(FlowState.ERROR)
                return

        try:
            nodes = self._capture_ui_nodes()
            self._focus_first_input(
                nodes,
                fallback_terms=["Nomor HP", "Country code", "Masuk atau daftar"],
                page_id=self._last_page_id,
            )
        except Exception as exc:
            self.log(f"UI dump phone-input fallback to OCR: {exc}")
            snapshot = self._capture_snapshot()
            for block in snapshot.texts:
                text_lower = block.text.lower()
                if any(kw in text_lower for kw in ["nomor", "phone", "masukkan"]):
                    x, y = block.center()
                    self.adb.tap(x, y + 50)
                    time.sleep(0.5)
                    break

        # GoPay already shows +62 in the country-code field, so only input the local part.
        phone_input = self._phone_number_for_input(self.ctx.phone_number)
        self.log(f"Inputting local phone number: {phone_input}")
        self._input_text(phone_input)
        self.ctx.otp_code = ""
        self.ctx.otp_resend_count = 0
        self.ctx.last_otp_code = ""
        self.ctx.otp_status_baseline = ""
        self.ctx.otp_phase = "initial"
        self._tap_confirm()
        self._set_state(FlowState.PHONE_ENTERED)

    def _handle_otp_input(self) -> None:
        """Handle OTP input state."""
        if not self.ctx.otp_code:
            try:
                self.ctx.otp_code = self._wait_for_otp()
            except NexSMSError as exc:
                if (
                    is_phone_code_timeout_error(str(exc))
                    and self.ctx.otp_resend_count < self.config.otp_resend_limit
                    and self._tap_otp_resend()
                ):
                    self.ctx.otp_resend_count += 1
                    self.ctx.error_message = (
                        f"OTP timeout. Resend tapped "
                        f"({self.ctx.otp_resend_count}/{self.config.otp_resend_limit})."
                    )
                    self.log(self.ctx.error_message)
                    return
                self.ctx.error_message = str(exc)
                self._set_state(FlowState.ERROR)
                return

        try:
            nodes = self._capture_ui_nodes()
            self._focus_first_input(
                nodes,
                fallback_terms=["OTP", "Masukkan OTP", "Kode"],
                page_id=self._last_page_id,
            )
        except Exception as exc:
            self.log(f"UI dump otp-input fallback to OCR: {exc}")
            snapshot = self._capture_snapshot()
            for block in snapshot.texts:
                text_lower = block.text.lower()
                if any(kw in text_lower for kw in ["kode", "otp", "verifikasi"]):
                    x, y = block.center()
                    self.adb.tap(x, y + 50)
                    time.sleep(0.5)
                    break

        self._input_text(self.ctx.otp_code)
        self._tap_confirm()
        self._set_state(FlowState.OTP_ENTERED)

    def _handle_whatsapp_otp_method_switch(self) -> None:
        """Switch a WhatsApp OTP page to the SMS verification flow using the same phone number."""
        if self.ctx.phone_number and not self.ctx.otp_status_baseline:
            try:
                _, status_text = self.nexsms.get_sms_status(
                    self.ctx.phone_number,
                    format="json_latest",
                )
                self.ctx.otp_status_baseline = status_text or ""
                if self.ctx.otp_status_baseline:
                    self.log("Captured current SMS baseline before switching OTP method.")
            except NexSMSError as exc:
                self.log(f"Unable to capture SMS baseline before method switch: {exc}")

        self.ctx.otp_code = ""
        self.ctx.otp_resend_count = 0
        self.ctx.otp_phase = "post_pin"
        if self._tap_other_otp_method():
            self._set_state(FlowState.WAITING_VERIFICATION_METHOD)
            return
        self.log("Alternate OTP method button not found yet.")

    def _handle_location_permission(self) -> None:
        """Handle the first-launch location permission intro by rejecting it."""
        self._reject_location_permission_intro()

    def _handle_landing_intro(self) -> None:
        """Handle the landing intro before phone-number entry."""
        self._tap_landing_intro_cta()

    def _handle_signup_terms(self) -> None:
        """Handle post-phone terms acceptance page."""
        self._tap_confirm()
        self._set_state(FlowState.WAITING_VERIFICATION_METHOD)

    def _handle_verification_method(self) -> None:
        """Select the delivery method for OTP."""
        try:
            nodes = self._capture_ui_nodes()
            target = find_first_node(nodes, terms=["OTP via SMS"], clickable=True, enabled=True)
            if target is None:
                target = find_first_node(nodes, terms=["OTP via WhatsApp"], clickable=True, enabled=True)
            if target:
                x, y = tap_node(self.adb, target)
                self.log(f"Selected verification method at ({x}, {y}): {target.label}")
                time.sleep(self.config.step_delay)
                self._set_state(FlowState.WAITING_OTP)
                return
        except Exception as exc:
            self.log(f"Verification-method UI dump fallback: {exc}")

        snapshot = self._capture_snapshot()
        for button_text in ["OTP via SMS", "OTP via WhatsApp"]:
            if tap_text(snapshot, self.adb, button_text):
                self.log(f"Selected verification method via OCR: {button_text}")
                time.sleep(self.config.step_delay)
                self._set_state(FlowState.WAITING_OTP)
                return

        self.log("Verification method option not found yet.")

    def _handle_username_input(self) -> None:
        """Handle account-name or username input state."""
        if not self.ctx.username:
            self.ctx.username = generate_username(self.config.username_length)
            if self._last_page_id == "profile_name_input":
                self.ctx.username = self.ctx.username.capitalize()
                self.log(f"Generated account name: {self.ctx.username}")
            else:
                self.log(f"Generated username: {self.ctx.username}")

        try:
            nodes = self._capture_ui_nodes()
            self._focus_first_input(
                nodes,
                fallback_terms=["Masukkan namamu", "Nama", "Username", "nama pengguna"],
                page_id=self._last_page_id,
            )
        except Exception as exc:
            self.log(f"UI dump username-input fallback to OCR: {exc}")
            snapshot = self._capture_snapshot()
            for block in snapshot.texts:
                text_lower = block.text.lower()
                if any(kw in text_lower for kw in ["masukkan namamu", "nama", "username", "nama pengguna"]):
                    x, y = block.center()
                    self.adb.tap(x, y + 50)
                    time.sleep(0.5)
                    break

        self.log(f"Inputting account name/username: {self.ctx.username}")
        self._input_text(self.ctx.username)

        if self._last_page_id == "profile_name_input":
            time.sleep(0.5)
            try:
                nodes = self._capture_ui_nodes()
                if not self._ui_contains_text(nodes, self.ctx.username):
                    self.log("Name not visible after first input; retrying focused input.")
                    self._focus_profile_name_input_via_layout(nodes)
                    self._input_text(self.ctx.username)
            except Exception as exc:
                self.log(f"Profile-name verification fallback skipped: {exc}")

        self._tap_confirm()
        self._set_state(FlowState.USERNAME_SET)

    def _handle_pin_input(self) -> None:
        """Handle PIN input state."""
        if not self.ctx.pin:
            self.ctx.pin = generate_pin(self.config.pin_length)
            self.log(f"Generated PIN: {self.ctx.pin}")

        self._input_pin_digits(self.ctx.pin)
        self._tap_confirm(preferred_terms=["Lanjut"])
        self._set_state(FlowState.PIN_SET)

    def _handle_pin_confirm(self) -> None:
        """Handle PIN confirmation state."""
        self._input_pin_digits(self.ctx.pin)
        self._tap_confirm(preferred_terms=["Konfirmasi PIN", "Confirm PIN", "Konfirmasi", "Confirm"])
        self.ctx.otp_code = ""
        self.ctx.otp_resend_count = 0
        self.ctx.otp_phase = "post_pin"
        self._set_state(FlowState.PIN_CONFIRMED)

    def _handle_home(self) -> None:
        """Handle the home dashboard after account creation."""
        self._tap_profile_tab()

    def _handle_profile_dashboard(self) -> None:
        """Handle the profile dashboard before opening the protection checklist."""
        self._tap_protection_section()

    def _handle_protection_overview(self) -> None:
        """Handle the protection overview by opening the PIN setup flow."""
        self._tap_protection_pin()

    def _handle_completion(self) -> None:
        """Handle registration completion."""
        self.log("Registration complete!")
        self.speaker.say("注册完成！")

        # Save credentials
        save_credentials(
            username=self.ctx.username,
            pin=self.ctx.pin,
            phone=self.ctx.phone_number,
            path=self.config.credentials_path,
        )
        self.log(f"Credentials saved to {self.config.credentials_path}")
        if self.ctx.phone_number:
            try:
                self.nexsms.mark_number_consumed(
                    self.ctx.phone_number,
                    reason="registration_complete",
                )
                self.log(f"Marked phone as consumed in local pool: {self.ctx.phone_number}")
            except Exception as exc:
                self.log(f"Warning: failed to mark phone as consumed: {exc}")

    def prepare_phone_input(self, max_steps: int = 10) -> FlowState:
        """Drive the app to the phone input page and stop there."""
        for _ in range(max_steps):
            if self._stopped:
                break

            self._wait_if_paused()
            if self.ctx.current_state == FlowState.INIT:
                self._start_clean_session()
            self._detect_and_update_state()

            if self.ctx.current_state == FlowState.WAITING_PHONE_INPUT:
                return self.ctx.current_state

            match self.ctx.current_state:
                case FlowState.WAITING_LOCATION_PERMISSION:
                    self._handle_location_permission()
                case FlowState.WAITING_LANDING_INTRO:
                    self._handle_landing_intro()
                case _:
                    break

            time.sleep(0.5)

        return self.ctx.current_state

    def step(self) -> FlowState:
        """Execute one step of the registration flow.

        Returns:
            Current state after the step.
        """
        if self._stopped:
            return self.ctx.current_state

        self._wait_if_paused()
        if self.ctx.current_state == FlowState.INIT:
            self._start_clean_session()

        # Detect current page state
        self._detect_and_update_state()

        # Handle current state
        match self.ctx.current_state:
            case FlowState.WAITING_LOCATION_PERMISSION:
                self._handle_location_permission()

            case FlowState.WAITING_LANDING_INTRO:
                self._handle_landing_intro()

            case FlowState.WAITING_PHONE_INPUT:
                self._handle_phone_input()

            case FlowState.PHONE_ENTERED:
                # Wait for OTP page to appear
                time.sleep(self.config.step_delay)
                self._detect_and_update_state()
                if self.ctx.current_state == FlowState.PHONE_ENTERED:
                    self._set_state(FlowState.WAITING_SIGNUP_TERMS)

            case FlowState.WAITING_SIGNUP_TERMS:
                self._handle_signup_terms()

            case FlowState.WAITING_VERIFICATION_METHOD:
                self._handle_verification_method()

            case FlowState.WAITING_OTP_METHOD_SWITCH:
                self._handle_whatsapp_otp_method_switch()

            case FlowState.WAITING_OTP:
                self._handle_otp_input()

            case FlowState.OTP_ENTERED:
                # Wait for next page
                time.sleep(self.config.step_delay)
                self._detect_and_update_state()
                if self.ctx.current_state == FlowState.OTP_ENTERED:
                    self._set_state(FlowState.WAITING_POST_OTP_PAGE)

            case FlowState.WAITING_POST_OTP_PAGE:
                time.sleep(self.config.step_delay)
                self._detect_and_update_state()
                if self.ctx.current_state == FlowState.WAITING_POST_OTP_PAGE:
                    self.log("Waiting for a recognizable page after OTP confirmation...")

            case FlowState.WAITING_USERNAME:
                self._handle_username_input()

            case FlowState.USERNAME_SET:
                time.sleep(self.config.step_delay)
                self._detect_and_update_state()
                if self.ctx.current_state == FlowState.USERNAME_SET:
                    if self._last_page_id == "profile_name_input":
                        self._set_state(FlowState.WAITING_HOME)
                    else:
                        self._set_state(FlowState.WAITING_PIN)

            case FlowState.WAITING_HOME:
                if self.ctx.otp_phase == "post_pin":
                    self._set_state(FlowState.REGISTRATION_COMPLETE)
                else:
                    self._handle_home()

            case FlowState.WAITING_PROFILE_DASHBOARD:
                if self.ctx.otp_phase == "post_pin":
                    self._set_state(FlowState.REGISTRATION_COMPLETE)
                else:
                    self._handle_profile_dashboard()

            case FlowState.WAITING_PROTECTION_OVERVIEW:
                if self.ctx.otp_phase == "post_pin":
                    self._set_state(FlowState.REGISTRATION_COMPLETE)
                else:
                    self._handle_protection_overview()

            case FlowState.WAITING_PIN:
                self._handle_pin_input()

            case FlowState.PIN_SET:
                time.sleep(self.config.step_delay)
                self._detect_and_update_state()
                if self.ctx.current_state == FlowState.PIN_SET:
                    self._set_state(FlowState.WAITING_PIN_CONFIRM)

            case FlowState.WAITING_PIN_CONFIRM:
                self._handle_pin_confirm()

            case FlowState.PIN_CONFIRMED:
                time.sleep(self.config.step_delay)
                self._detect_and_update_state()
                if self.ctx.current_state == FlowState.PIN_CONFIRMED:
                    self._set_state(FlowState.WAITING_HOME)

            case FlowState.REGISTRATION_COMPLETE:
                self._handle_completion()

            case FlowState.ERROR:
                if self.ctx.retry_count < self.ctx.max_retries:
                    self.ctx.retry_count += 1
                    self.log(f"Retrying... ({self.ctx.retry_count}/{self.ctx.max_retries})")
                    # Go back to previous state
                    self._set_state(FlowState.INIT)
                else:
                    self.log(f"Max retries reached. Error: {self.ctx.error_message}")
                    self.speaker.say("注册失败，需要人工处理。")

            case FlowState.MANUAL:
                # Manual mode - do nothing
                pass

        return self.ctx.current_state

    def run(self, max_steps: int = 50) -> FlowState:
        """Run the complete registration flow.

        Args:
            max_steps: Maximum number of steps to execute.

        Returns:
            Final state.
        """
        for i in range(max_steps):
            if self._stopped:
                break

            state = self.step()

            if state == FlowState.REGISTRATION_COMPLETE:
                self._handle_completion()
                break

            if state in (
                FlowState.ERROR,
                FlowState.MANUAL,
            ):
                break

            time.sleep(0.5)

        return self.ctx.current_state

    def get_status(self) -> dict[str, Any]:
        """Get current flow status."""
        return {
            "state": self.ctx.current_state.value,
            "phone": self.ctx.phone_number,
            "phone_retry_count": self.ctx.phone_retry_count,
            "otp_phase": self.ctx.otp_phase,
            "username": self.ctx.username,
            "has_pin": bool(self.ctx.pin),
            "error": self.ctx.error_message,
            "retry_count": self.ctx.retry_count,
            "paused": self._paused,
        }
