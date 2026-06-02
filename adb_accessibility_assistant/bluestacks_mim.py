from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import ImageGrab

from .adb_client import ADBClient, AndroidDeviceError
from .bluestacks_config import (
    DEFAULT_BLUESTACKS_CONF_PATH,
    BlueStacksConf,
    BlueStacksInstanceProfile,
    get_instance_profile,
    list_instances,
    load_bluestacks_conf,
    save_bluestacks_conf,
)
from .bluestacks_player_ui import (
    DEFAULT_MIN_CONFIDENCE,
    OCRWindowBlock,
    WindowRect,
    _list_visible_windows,
    click_screen,
    close_window,
    focus_window,
    grab_window_image,
    kill_window_process,
)
from .ocr import OCRUnavailableError, create_ocr_engine


DEFAULT_MIM_EXE_PATH = Path(
    os.environ.get("PROGRAMFILES", r"C:\Program Files")
) / "BlueStacks_nxt" / "HD-MultiInstanceManager.exe"
DEFAULT_ACTIVE_CLONE_PATH = Path("artifacts") / "bluestacks_active_clone.json"
MIM_TITLE_KEYWORDS = ("Multi Instance Manager", "多开管理器")
ROW_BUTTON_TERMS_START = ("启动", "啟動", "start", "launch", "open")
CLONE_DIALOG_MARKER_TERMS = (
    "复制多开引擎",
    "複製多開引擎",
    "复制多开",
    "複製多開",
    "多开数量",
    "多開數量",
)
CLONE_DIALOG_CONFIRM_TERMS = ("新增", "创建", "建立", "add", "create")
ROW_ACTION_OFFSET_CLONE = 104
ROW_ANCHOR_Y_TOLERANCE = 34


@dataclass(slots=True)
class BlueStacksCloneSession:
    source_instance_name: str
    instance_name: str
    display_name: str
    adb_port: str
    device_serial: str


def load_active_clone_session(path: str | Path = DEFAULT_ACTIVE_CLONE_PATH) -> BlueStacksCloneSession | None:
    state_path = Path(path)
    if not state_path.exists():
        return None
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return None
    try:
        return BlueStacksCloneSession(
            source_instance_name=str(payload.get("source_instance_name") or ""),
            instance_name=str(payload.get("instance_name") or ""),
            display_name=str(payload.get("display_name") or ""),
            adb_port=str(payload.get("adb_port") or ""),
            device_serial=str(payload.get("device_serial") or ""),
        )
    except Exception:
        return None


def resolve_active_clone_target(
    *,
    enabled: bool,
    state_path: str | Path = DEFAULT_ACTIVE_CLONE_PATH,
) -> tuple[str | None, str | None]:
    if not enabled:
        return None, None
    session = load_active_clone_session(state_path)
    if session is None or not session.device_serial:
        return None, None
    return session.device_serial, None


def find_bluestacks_mim_window(window_title: str | None = None) -> WindowRect:
    windows = [
        window
        for window in _list_visible_windows()
        if "BlueStacks" in window.title and any(keyword in window.title for keyword in MIM_TITLE_KEYWORDS)
    ]
    if not windows:
        raise RuntimeError("BlueStacks Multi Instance Manager window not found.")
    if window_title:
        for window in windows:
            if window.title == window_title:
                return window
        raise RuntimeError(f"BlueStacks Multi Instance Manager window not found: {window_title}")
    windows.sort(key=lambda window: (window.left, window.top, window.title))
    return windows[0]


def _normalize_text(value: str) -> str:
    return "".join(character.casefold() for character in str(value or "") if character.isalnum())


def _wait_for_port(port: int, timeout_seconds: float = 120.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            try:
                sock.connect(("127.0.0.1", port))
                return
            except OSError:
                time.sleep(1.5)
    raise RuntimeError(f"BlueStacks ADB port did not start listening: {port}")


def _is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.8)
        try:
            sock.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _derive_image_name(instance_name: str) -> str:
    token = str(instance_name or "").strip()
    if not token:
        raise ValueError("instance_name is required")
    return token.split("_", 1)[0]


def _remove_instance_from_manager_registry(instance_name: str, manager_path: Path) -> None:
    if not manager_path.exists():
        return
    tree = ET.parse(manager_path)
    root = tree.getroot()
    namespace_uri = ""
    if root.tag.startswith("{"):
        namespace_uri = root.tag[1:].split("}", 1)[0]
    ns = {"vbox": namespace_uri} if namespace_uri else {}
    machine_registry = root.find(".//vbox:MachineRegistry", ns) if namespace_uri else root.find(".//MachineRegistry")
    if machine_registry is None:
        return
    target_suffix = f"\\{instance_name}\\{instance_name}.bstk".casefold()
    removed = False
    for entry in list(machine_registry):
        src = str(entry.attrib.get("src") or "")
        if src.casefold().endswith(target_suffix):
            machine_registry.remove(entry)
            removed = True
    if removed:
        tree.write(manager_path, encoding="utf-8", xml_declaration=True)


def _remove_instance_from_conf(conf: BlueStacksConf, instance_name: str) -> BlueStacksConf:
    prefix = f"bst.instance.{instance_name}."
    new_lines = [line for line in conf.lines if not line.strip().startswith(prefix)]
    new_values = {key: value for key, value in conf.values.items() if not key.startswith(prefix)}
    return BlueStacksConf(path=conf.path, lines=new_lines, values=new_values)


class BlueStacksMultiInstanceController:
    def __init__(
        self,
        *,
        mim_window_title: str | None = None,
        mim_exe_path: str | Path | None = None,
        conf_path: str | Path | None = None,
    ) -> None:
        self.mim_window_title = mim_window_title
        self.mim_exe_path = Path(mim_exe_path or DEFAULT_MIM_EXE_PATH)
        self.conf_path = Path(conf_path or DEFAULT_BLUESTACKS_CONF_PATH)
        try:
            self._ocr = create_ocr_engine("rapidocr")
        except OCRUnavailableError as exc:
            raise RuntimeError(f"BlueStacks multi-instance automation requires OCR: {exc}") from exc

    def open(self, *, timeout_seconds: float = 12.0) -> WindowRect:
        if not self.mim_exe_path.exists():
            raise FileNotFoundError(f"BlueStacks Multi Instance Manager not found: {self.mim_exe_path}")
        try:
            window = find_bluestacks_mim_window(self.mim_window_title)
            focus_window(window)
            return window
        except RuntimeError:
            subprocess.Popen([str(self.mim_exe_path)], close_fds=True)
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                window = find_bluestacks_mim_window(self.mim_window_title)
                focus_window(window)
                return window
            except RuntimeError:
                time.sleep(0.5)
        raise RuntimeError("BlueStacks Multi Instance Manager did not open.")

    def select_instance(self, display_name: str, *, timeout_seconds: float = 12.0) -> None:
        target_text = _normalize_text(display_name)
        if not target_text:
            raise ValueError("display_name is required")
        deadline = time.time() + timeout_seconds
        last_seen: list[str] = []
        while time.time() < deadline:
            window = self.open()
            blocks = self._capture_blocks(window)
            last_seen = [block.text for block in blocks]
            candidates = [
                block
                for block in blocks
                if target_text in _normalize_text(block.text)
            ]
            if candidates:
                candidates.sort(key=lambda block: (block.top, block.left))
                block = candidates[0]
                focus_window(window)
                click_screen(max(window.left + 24, block.center_screen()[0]), block.center_screen()[1])
                time.sleep(0.4)
                return
            time.sleep(0.5)
        raise RuntimeError(
            f"BlueStacks instance row not found in Multi Instance Manager: {display_name}. "
            f"OCR seen: {', '.join(last_seen[:12])}"
        )

    def _find_row_anchor(
        self,
        display_name: str,
        *,
        timeout_seconds: float = 12.0,
    ) -> tuple[WindowRect, OCRWindowBlock, list[OCRWindowBlock]]:
        target_text = _normalize_text(display_name)
        if not target_text:
            raise ValueError("display_name is required")
        deadline = time.time() + timeout_seconds
        last_seen: list[str] = []
        while time.time() < deadline:
            window = self.open()
            blocks = self._capture_blocks(window)
            last_seen = [block.text for block in blocks]
            anchors = [block for block in blocks if target_text in _normalize_text(block.text)]
            if anchors:
                anchors.sort(key=lambda block: (block.top, block.left))
                return window, anchors[0], blocks
            time.sleep(0.5)
        raise RuntimeError(
            f"BlueStacks instance row not found in Multi Instance Manager: {display_name}. "
            f"OCR seen: {', '.join(last_seen[:12])}"
        )

    def _find_row_button_block(
        self,
        *,
        row_anchor: OCRWindowBlock,
        blocks: list[OCRWindowBlock],
        terms: tuple[str, ...],
    ) -> OCRWindowBlock | None:
        row_y = row_anchor.center_screen()[1]
        candidates = [
            block
            for block in blocks
            if any(term in _normalize_text(block.text) for term in terms)
            and abs(block.center_screen()[1] - row_y) <= ROW_ANCHOR_Y_TOLERANCE
            and block.center_screen()[0] > row_anchor.center_screen()[0]
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda block: (abs(block.center_screen()[1] - row_y), block.center_screen()[0]))
        return candidates[0]

    def click_clone_action_for_instance(self, display_name: str, *, timeout_seconds: float = 12.0) -> None:
        window, row_anchor, _ = self._find_row_anchor(display_name, timeout_seconds=timeout_seconds)
        row_y = row_anchor.center_screen()[1]
        focus_window(window)
        click_screen(window.right - ROW_ACTION_OFFSET_CLONE, row_y)
        time.sleep(0.6)

    def start_instance_via_ui(self, display_name: str, *, timeout_seconds: float = 12.0) -> None:
        window, row_anchor, blocks = self._find_row_anchor(display_name, timeout_seconds=timeout_seconds)
        button_block = self._find_row_button_block(
            row_anchor=row_anchor,
            blocks=blocks,
            terms=tuple(_normalize_text(term) for term in ROW_BUTTON_TERMS_START),
        )
        focus_window(window)
        if button_block is not None:
            click_screen(*button_block.center_screen())
        else:
            click_screen(window.right - 260, row_anchor.center_screen()[1])
        time.sleep(0.8)

    def confirm_clone_dialog(self, *, timeout_seconds: float = 15.0) -> None:
        marker_terms = tuple(_normalize_text(term) for term in CLONE_DIALOG_MARKER_TERMS)
        confirm_terms = tuple(_normalize_text(term) for term in CLONE_DIALOG_CONFIRM_TERMS)
        deadline = time.time() + timeout_seconds
        last_seen: list[str] = []
        while time.time() < deadline:
            window = self.open()
            blocks = self._capture_blocks(window)
            last_seen = [block.text for block in blocks]
            dialog_markers = [
                block for block in blocks
                if any(term in _normalize_text(block.text) for term in marker_terms)
            ]
            confirm_candidates = [
                block
                for block in blocks
                if any(term in _normalize_text(block.text) for term in confirm_terms)
            ]
            if confirm_candidates:
                confirm_candidates.sort(key=lambda block: (block.center_local()[1], block.center_local()[0]))
                focus_window(window)
                click_screen(*confirm_candidates[-1].center_screen())
                time.sleep(0.8)
                return
            if dialog_markers:
                focus_window(window)
                click_screen(window.right - 48, window.bottom - 28)
                time.sleep(0.8)
                return
            time.sleep(0.4)
        raise RuntimeError(
            "BlueStacks clone dialog confirm button not found. "
            f"OCR seen: {', '.join(last_seen[:20])}"
        )

    def invoke(self, *args: str) -> None:
        command = [str(self.mim_exe_path), *args]
        subprocess.Popen(command, close_fds=True)

    def clone_selected_instance(
        self,
        *,
        source_instance_name: str,
        source_display_name: str,
        timeout_seconds: float = 240.0,
    ) -> BlueStacksInstanceProfile:
        conf_before = load_bluestacks_conf(self.conf_path)
        instances_before = set(list_instances(conf_before))
        self.click_clone_action_for_instance(source_display_name)
        dialog_warning = ""
        try:
            self.confirm_clone_dialog(timeout_seconds=6.0)
        except RuntimeError as exc:
            dialog_warning = str(exc)
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            conf_after = load_bluestacks_conf(self.conf_path)
            new_instances = [name for name in list_instances(conf_after) if name not in instances_before]
            if new_instances:
                preferred = [name for name in new_instances if name.startswith(f"{source_instance_name}_")]
                chosen = sorted(preferred or new_instances)[-1]
                return get_instance_profile(conf_after, chosen)
            time.sleep(1.5)
        suffix = f" {dialog_warning}" if dialog_warning else ""
        raise RuntimeError(f"Timed out waiting for BlueStacks clone of {source_instance_name}.{suffix}".strip())

    def start_selected_instance(self) -> None:
        self.invoke("--cmd", "startSelectedInstance")

    def stop_selected_instance(self) -> None:
        self.invoke("--cmd", "stopSelectedInstance")

    def delete_selected_instance(self) -> None:
        self.invoke("--cmd", "deleteSelectedInstance")

    def _capture_blocks(self, window: WindowRect) -> list[OCRWindowBlock]:
        focus_window(window)
        try:
            image = grab_window_image(window)
        except Exception:
            bbox = (window.left, window.top, window.right, window.bottom)
            try:
                image = ImageGrab.grab(bbox=bbox, all_screens=True)
            except TypeError:
                image = ImageGrab.grab(bbox=bbox)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        blocks = self._ocr.recognize(buffer.getvalue())
        converted: list[OCRWindowBlock] = []
        for block in blocks:
            if block.confidence < DEFAULT_MIN_CONFIDENCE:
                continue
            local_box = [(int(x), int(y)) for x, y in block.box]
            screen_box = [(window.left + x, window.top + y) for x, y in local_box]
            converted.append(
                OCRWindowBlock(
                    text=block.text,
                    confidence=block.confidence,
                    local_box=local_box,
                    screen_box=screen_box,
                )
            )
        return converted


class BlueStacksTempCloneManager:
    def __init__(
        self,
        *,
        adb_path: str = "adb",
        source_instance_name: str,
        mim_window_title: str | None = None,
        conf_path: str | Path | None = None,
        state_path: str | Path = DEFAULT_ACTIVE_CLONE_PATH,
        log_callback=None,
    ) -> None:
        self.adb_path = adb_path
        self.source_instance_name = source_instance_name
        self.conf_path = Path(conf_path or DEFAULT_BLUESTACKS_CONF_PATH)
        self.state_path = Path(state_path)
        self.log = log_callback or (lambda _message: None)
        self.controller = BlueStacksMultiInstanceController(
            mim_window_title=mim_window_title,
            conf_path=self.conf_path,
        )
        self.current_session: BlueStacksCloneSession | None = load_active_clone_session(self.state_path)
        if self.current_session and self.current_session.source_instance_name != self.source_instance_name:
            self.current_session = None

    def provision(self) -> BlueStacksCloneSession:
        conf = load_bluestacks_conf(self.conf_path)
        source_profile = get_instance_profile(conf, self.source_instance_name)
        if not source_profile.display_name:
            raise RuntimeError(f"BlueStacks source instance has no display name: {self.source_instance_name}")
        self.log(f"Opening BlueStacks Multi Instance Manager for source {source_profile.display_name}...")
        clone_profile = self.controller.clone_selected_instance(
            source_instance_name=self.source_instance_name,
            source_display_name=source_profile.display_name,
        )
        if not clone_profile.display_name or not clone_profile.adb_port:
            raise RuntimeError(f"BlueStacks clone missing display name or adb_port: {clone_profile.instance_name}")
        self.log(
            "Created BlueStacks clone: "
            f"{clone_profile.instance_name} ({clone_profile.display_name}, adb_port={clone_profile.adb_port})"
        )
        self.log(f"Starting BlueStacks clone via official UI: {clone_profile.display_name}")
        self.controller.start_instance_via_ui(clone_profile.display_name)
        adb_port = int(clone_profile.adb_port)
        self.log(f"Waiting for BlueStacks clone ADB port: {adb_port}")
        serial = f"127.0.0.1:{clone_profile.adb_port}"
        adb = ADBClient(adb_path=self.adb_path)
        target_adb = ADBClient(adb_path=self.adb_path, device_serial=serial)
        deadline = time.time() + 120.0
        last_error = ""
        while time.time() < deadline:
            try:
                _wait_for_port(adb_port, timeout_seconds=8.0)
                result = adb.run("connect", serial, check=False)
                output = " ".join(
                    part.strip()
                    for part in (str(result.stdout or "").strip(), str(result.stderr or "").strip())
                    if part and part.strip()
                )
                if output:
                    self.log(f"ADB connect: {output}")
                target_adb.wait_for_device(timeout=20.0)
                session = BlueStacksCloneSession(
                    source_instance_name=self.source_instance_name,
                    instance_name=clone_profile.instance_name,
                    display_name=clone_profile.display_name,
                    adb_port=clone_profile.adb_port,
                    device_serial=serial,
                )
                self.current_session = session
                self._save_active_session(session)
                return session
            except (AndroidDeviceError, RuntimeError) as exc:
                last_error = str(exc)
                time.sleep(2.0)
        raise RuntimeError(
            f"BlueStacks clone ADB connection did not become ready: {serial}. {last_error}".strip()
        )

    def rotate(self) -> BlueStacksCloneSession:
        self.dispose_current()
        return self.provision()

    def dispose_current(self) -> None:
        session = self.current_session
        if session is None:
            self._clear_active_session()
            return
        self.log(f"Cleaning up BlueStacks clone: {session.instance_name}")
        adb_port = int(session.adb_port) if str(session.adb_port or "").isdigit() else None
        try:
            if session.device_serial:
                ADBClient(adb_path=self.adb_path, device_serial=session.device_serial).run("emu", "kill", check=False)
            time.sleep(2.0)
        except Exception as exc:
            self.log(f"Warning: failed to stop clone via adb emu kill: {exc}")
        try:
            player_window = None
            for window in _list_visible_windows():
                if window.title == session.display_name:
                    player_window = window
                    break
            if player_window is not None:
                close_window(player_window)
                deadline = time.time() + 8.0
                while time.time() < deadline:
                    if not any(window.title == session.display_name for window in _list_visible_windows()):
                        break
                    time.sleep(0.5)
                if any(window.title == session.display_name for window in _list_visible_windows()):
                    self.log(f"Force-killing BlueStacks clone window: {session.display_name}")
                    kill_window_process(player_window)
        except Exception as exc:
            self.log(f"Warning: failed to close clone window directly: {exc}")
        if adb_port is not None:
            deadline = time.time() + 15.0
            while time.time() < deadline and _is_port_open(adb_port):
                time.sleep(0.5)
        try:
            self.controller.select_instance(session.display_name, timeout_seconds=6.0)
            self.controller.delete_selected_instance()
            time.sleep(2.0)
        except Exception as exc:
            self.log(f"Warning: failed to delete clone via Multi Instance Manager command: {exc}")
        self._manual_cleanup(session.instance_name)
        self.current_session = None
        self._clear_active_session()

    def _save_active_session(self, session: BlueStacksCloneSession) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(asdict(session), ensure_ascii=True, indent=2), encoding="utf-8")

    def _clear_active_session(self) -> None:
        self.state_path.unlink(missing_ok=True)

    def _manual_cleanup(self, instance_name: str) -> None:
        current_name = self.current_session.instance_name if self.current_session else ""
        if "_gopay_" not in instance_name and current_name != instance_name:
            raise RuntimeError(f"Refusing to manually delete non-temp BlueStacks instance: {instance_name}")

        conf = load_bluestacks_conf(self.conf_path)
        if instance_name in list_instances(conf):
            updated_conf = _remove_instance_from_conf(conf, instance_name)
            save_bluestacks_conf(updated_conf)

        engine_dir = self.conf_path.parent / "Engine" / instance_name
        shutil.rmtree(engine_dir, ignore_errors=True)
        manager_xml = self.conf_path.parent / "Engine" / "Manager" / "BstkGlobal.xml"
        _remove_instance_from_manager_registry(instance_name, manager_xml)


def resolve_bluestacks_source_instance(
    *,
    conf_path: str | Path | None = None,
    source_instance_name: str | None = None,
    adb_port: str | None = None,
    device_serial: str | None = None,
    window_title: str | None = None,
) -> str:
    conf = load_bluestacks_conf(conf_path)
    if source_instance_name:
        get_instance_profile(conf, source_instance_name)
        return source_instance_name

    serial = str(device_serial or "").strip()
    port_hint = str(adb_port or "").strip()
    if not port_hint and serial.startswith("127.0.0.1:"):
        port_hint = serial.split(":", 1)[1]

    for instance_name in list_instances(conf):
        profile = get_instance_profile(conf, instance_name)
        if port_hint and profile.adb_port == port_hint:
            base_name = _derive_image_name(instance_name)
            if base_name != instance_name:
                try:
                    get_instance_profile(conf, base_name)
                    return base_name
                except ValueError:
                    pass
            return instance_name
        if window_title and profile.display_name == window_title:
            base_name = _derive_image_name(instance_name)
            if base_name != instance_name:
                try:
                    get_instance_profile(conf, base_name)
                    return base_name
                except ValueError:
                    pass
            return instance_name

    raise RuntimeError(
        "Unable to resolve BlueStacks source instance from config. "
        "Set bluestacks_master_instance explicitly."
    )
