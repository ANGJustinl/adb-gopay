from __future__ import annotations

import ctypes
import io
import subprocess
import time
from dataclasses import dataclass

from PIL import Image, ImageGrab

from .models import OCRTextBlock
from .ocr import OCRUnavailableError, create_ocr_engine


VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_I = 0x49
VK_S = 0x53
VK_DOWN = 0x28
VK_UP = 0x26
VK_RETURN = 0x0D
KEYEVENTF_KEYUP = 0x0002
SW_RESTORE = 9
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
WM_CLOSE = 0x0010
PW_RENDERFULLCONTENT = 0x00000002
BI_RGB = 0

SETTINGS_TITLE_CANDIDATES = ("設定", "设置", "Settings")
PHONE_TAB_CANDIDATES = ("手機", "手机", "Phone")
DEVICE_LABEL_CANDIDATES = (
    "選擇預設的手機裝置",
    "选择预设的手机装置",
    "預設的手機裝置",
    "预设的手机装置",
    "predefined device",
    "device profile",
)
NETWORK_PROVIDER_CANDIDATES = ("網路電信商", "网络电信商", "Network provider")
ROOT_ACCESS_CANDIDATES = ("Root 存取權限", "Root access", "Root")
SAVE_BUTTON_CANDIDATES = ("儲存變更", "储存变更", "Save changes", "Save Changes")
BLUESTACKS_WINDOW_PREFIX = "BlueStacks App Player"
DEFAULT_MIN_CONFIDENCE = 0.30
MENU_SETTINGS_SEARCH_MARGIN = 260


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.c_ushort),
        ("biBitCount", ctypes.c_ushort),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class _RGBQUAD(ctypes.Structure):
    _fields_ = [
        ("rgbBlue", ctypes.c_ubyte),
        ("rgbGreen", ctypes.c_ubyte),
        ("rgbRed", ctypes.c_ubyte),
        ("rgbReserved", ctypes.c_ubyte),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", _BITMAPINFOHEADER),
        ("bmiColors", _RGBQUAD * 1),
    ]


@dataclass(slots=True)
class WindowRect:
    hwnd: int
    title: str
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(slots=True)
class DevicePresetSwitchResult:
    window_title: str
    before_preset: str
    after_preset: str
    changed: bool


@dataclass(slots=True)
class OCRWindowBlock:
    text: str
    confidence: float
    local_box: list[tuple[int, int]]
    screen_box: list[tuple[int, int]]

    @property
    def left(self) -> int:
        return min(point[0] for point in self.local_box)

    @property
    def top(self) -> int:
        return min(point[1] for point in self.local_box)

    @property
    def right(self) -> int:
        return max(point[0] for point in self.local_box)

    @property
    def bottom(self) -> int:
        return max(point[1] for point in self.local_box)

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def center_local(self) -> tuple[int, int]:
        xs = [point[0] for point in self.local_box]
        ys = [point[1] for point in self.local_box]
        return (sum(xs) // len(xs), sum(ys) // len(ys))

    def center_screen(self) -> tuple[int, int]:
        xs = [point[0] for point in self.screen_box]
        ys = [point[1] for point in self.screen_box]
        return (sum(xs) // len(xs), sum(ys) // len(ys))


user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


def _list_visible_windows() -> list[WindowRect]:
    windows: list[WindowRect] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value
        if not title:
            return True
        rect = _RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        windows.append(
            WindowRect(
                hwnd=hwnd,
                title=title,
                left=rect.left,
                top=rect.top,
                right=rect.right,
                bottom=rect.bottom,
            )
        )
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return windows


def list_bluestacks_player_windows() -> list[WindowRect]:
    return [window for window in _list_visible_windows() if window.title.startswith(BLUESTACKS_WINDOW_PREFIX)]


def find_bluestacks_player_window(window_title: str | None = None) -> WindowRect:
    windows = list_bluestacks_player_windows()
    if not windows:
        raise RuntimeError("No visible BlueStacks player windows found.")

    if window_title:
        for window in windows:
            if window.title == window_title:
                return window
        raise RuntimeError(f"BlueStacks player window not found: {window_title}")

    if len(windows) > 1:
        titles = ", ".join(window.title for window in windows)
        raise RuntimeError(f"Multiple BlueStacks windows found. Specify --window-title. Found: {titles}")
    return windows[0]


def focus_window(window: WindowRect) -> None:
    title_for_ps = window.title.replace("'", "''")
    user32.ShowWindow(window.hwnd, SW_RESTORE)
    user32.SetWindowPos(window.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    user32.BringWindowToTop(window.hwnd)
    user32.SetForegroundWindow(window.hwnd)
    user32.SetActiveWindow(window.hwnd)
    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "$ws = New-Object -ComObject WScript.Shell; "
                    f"$null = $ws.AppActivate('{title_for_ps}');"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
    except OSError:
        pass
    time.sleep(0.35)


def click_screen(x: int, y: int) -> None:
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.08)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.04)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def get_window_process_id(window: WindowRect) -> int:
    process_id = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(window.hwnd, ctypes.byref(process_id))
    return int(process_id.value)


def close_window(window: WindowRect) -> None:
    user32.PostMessageW(window.hwnd, WM_CLOSE, 0, 0)
    time.sleep(0.5)


def kill_window_process(window: WindowRect) -> None:
    process_id = get_window_process_id(window)
    if process_id <= 0:
        raise RuntimeError(f"Failed to resolve process id for window: {window.title}")
    subprocess.run(
        ["taskkill", "/PID", str(process_id), "/F"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    time.sleep(0.8)


def grab_window_image(window: WindowRect):
    width = max(1, int(window.width))
    height = max(1, int(window.height))
    hwnd = int(window.hwnd)
    hwnd_dc = user32.GetWindowDC(hwnd)
    if not hwnd_dc:
        raise RuntimeError(f"GetWindowDC failed for window: {window.title}")
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    if not mem_dc:
        user32.ReleaseDC(hwnd, hwnd_dc)
        raise RuntimeError(f"CreateCompatibleDC failed for window: {window.title}")
    bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
    if not bitmap:
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)
        raise RuntimeError(f"CreateCompatibleBitmap failed for window: {window.title}")
    old_bitmap = gdi32.SelectObject(mem_dc, bitmap)
    try:
        ok = user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT)
        if ok != 1:
            raise RuntimeError(f"PrintWindow failed for window: {window.title}")
        bitmap_info = _BITMAPINFO()
        bitmap_info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bitmap_info.bmiHeader.biWidth = width
        bitmap_info.bmiHeader.biHeight = -height
        bitmap_info.bmiHeader.biPlanes = 1
        bitmap_info.bmiHeader.biBitCount = 32
        bitmap_info.bmiHeader.biCompression = BI_RGB
        buffer = ctypes.create_string_buffer(width * height * 4)
        rows = gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(bitmap_info), 0)
        if rows != height:
            raise RuntimeError(f"GetDIBits failed for window: {window.title}")
        image = Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1)
        return image.copy()
    finally:
        if old_bitmap:
            gdi32.SelectObject(mem_dc, old_bitmap)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)


def tap_virtual_key(key_code: int) -> None:
    user32.keybd_event(key_code, 0, 0, 0)
    time.sleep(0.04)
    user32.keybd_event(key_code, 0, KEYEVENTF_KEYUP, 0)


def tap_hotkey(*key_codes: int) -> None:
    for key_code in key_codes:
        user32.keybd_event(key_code, 0, 0, 0)
        time.sleep(0.03)
    for key_code in reversed(key_codes):
        user32.keybd_event(key_code, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.03)


def tap_ctrl_shortcut(key_code: int) -> None:
    tap_hotkey(VK_CONTROL, key_code)


def _normalize_text(value: str) -> str:
    return "".join(character.casefold() for character in value if character.isalnum())


def _matches_any(text: str, terms: tuple[str, ...] | list[str]) -> bool:
    normalized_text = _normalize_text(text)
    return any(_normalize_text(term) in normalized_text for term in terms)


def _normalize_preset_name(value: str) -> str:
    return " ".join(value.casefold().split())


class BlueStacksSettingsController:
    def __init__(self, *, window_title: str) -> None:
        self.window_title = window_title
        try:
            self._ocr = create_ocr_engine("rapidocr")
        except OCRUnavailableError as exc:
            raise RuntimeError(f"BlueStacks preset switching requires OCR: {exc}") from exc

    def _window(self) -> WindowRect:
        return find_bluestacks_player_window(self.window_title)

    def ensure_phone_settings_open(self, *, timeout_seconds: float = 10.0) -> None:
        if self._phone_settings_ready():
            return

        if not self._settings_shell_visible():
            self._open_settings_from_toolbar()
            self._wait_for(self._settings_shell_visible, timeout_seconds=timeout_seconds / 2.0, description="BlueStacks Settings")

        self._click_phone_tab()
        self._wait_for(self._phone_settings_ready, timeout_seconds=timeout_seconds, description="BlueStacks Phone settings")

    def current_preset(self) -> str:
        self.ensure_phone_settings_open()
        block = self._require_current_preset_block()
        return block.text.strip()

    def cycle_once(self, *, direction: str = "next") -> str:
        self.ensure_phone_settings_open()
        before_block = self._require_current_preset_block()
        before = before_block.text.strip()
        self._click_block(before_block)
        time.sleep(0.20)
        tap_virtual_key(VK_UP if direction == "previous" else VK_DOWN)
        time.sleep(0.12)
        tap_virtual_key(VK_RETURN)
        time.sleep(0.55)
        return self._wait_for_changed_preset(before, timeout_seconds=5.0)

    def select_preset(self, target_preset: str, *, max_cycles: int = 30) -> str:
        target_normalized = _normalize_preset_name(target_preset)
        current = self.current_preset()
        if _normalize_preset_name(current) == target_normalized:
            return current

        seen = {_normalize_preset_name(current)}
        for _ in range(max_cycles):
            current = self.cycle_once(direction="next")
            normalized = _normalize_preset_name(current)
            if normalized == target_normalized:
                return current
            if normalized in seen:
                break
            seen.add(normalized)
        raise RuntimeError(f"Preset not found by cycling forward: {target_preset}")

    def save(self) -> None:
        self.ensure_phone_settings_open()
        if self._try_click_save_button():
            return
        focus_window(self._window())
        tap_ctrl_shortcut(VK_S)

    def switch_preset(
        self,
        *,
        target_preset: str | None = None,
        steps: int = 1,
        save: bool = True,
        save_wait_seconds: float = 3.0,
        max_cycles: int = 30,
    ) -> DevicePresetSwitchResult:
        self.ensure_phone_settings_open()
        before = self.current_preset()
        after = before

        if target_preset:
            after = self.select_preset(target_preset, max_cycles=max_cycles)
        else:
            direction = "previous" if steps < 0 else "next"
            for _ in range(abs(steps or 1)):
                after = self.cycle_once(direction=direction)

        changed = _normalize_preset_name(before) != _normalize_preset_name(after)
        if changed and save:
            self.save()
            time.sleep(save_wait_seconds)

        return DevicePresetSwitchResult(
            window_title=self.window_title,
            before_preset=before,
            after_preset=after,
            changed=changed,
        )

    def _settings_shell_visible(self) -> bool:
        window = self._window()
        blocks = self._capture_blocks(window)
        if not blocks:
            return False
        title = self._find_best_match(
            blocks,
            SETTINGS_TITLE_CANDIDATES,
            x_range=(0.00, 0.30),
            y_range=(0.05, 0.35),
        )
        return title is not None

    def _phone_settings_ready(self) -> bool:
        window = self._window()
        blocks = self._capture_blocks(window)
        if not blocks:
            return False
        return self._find_current_preset_block(blocks, window=window) is not None

    def _open_settings_with_shortcut(self) -> None:
        focus_window(self._window())
        tap_hotkey(VK_CONTROL, VK_SHIFT, VK_I)
        time.sleep(1.0)

    def _open_settings_from_toolbar(self) -> None:
        # BlueStacks host buttons are exposed to UIAutomation even when their labels are blank.
        # We probe the right-side candidates first, then use OCR to click the visible "Settings" menu item.
        for rank_from_right in (1, 2, 3, 4):
            if not self._invoke_toolbar_button(rank_from_right):
                continue
            time.sleep(0.50)
            if self._settings_shell_visible():
                return
            if self._click_settings_menu_item():
                time.sleep(0.90)
                if self._settings_shell_visible():
                    return
        self._open_settings_with_shortcut()

    def _click_phone_tab(self) -> None:
        window = self._window()
        focus_window(window)
        blocks = self._capture_blocks(window)
        target = self._find_best_match(
            blocks,
            PHONE_TAB_CANDIDATES,
            x_range=(0.00, 0.25),
            y_range=(0.30, 0.85),
        )
        if target is None:
            raise RuntimeError("BlueStacks Phone tab not found in Settings.")
        self._click_block(target)
        time.sleep(0.45)

    def _require_current_preset_block(self) -> OCRWindowBlock:
        window = self._window()
        blocks = self._capture_blocks(window)
        block = self._find_current_preset_block(blocks, window=window)
        if block is None:
            raise RuntimeError("Current BlueStacks device preset is not visible on the Phone settings page.")
        return block

    def _find_current_preset_block(
        self,
        blocks: list[OCRWindowBlock],
        *,
        window: WindowRect,
    ) -> OCRWindowBlock | None:
        label = self._find_best_match(
            blocks,
            DEVICE_LABEL_CANDIDATES,
            x_range=(0.35, 0.95),
            y_range=(0.12, 0.40),
        )
        if label is None:
            return None

        candidates = [
            block
            for block in blocks
            if block.center_local()[0] >= int(window.width * 0.35)
            and block.center_local()[1] > label.center_local()[1] + 8
            and block.center_local()[1] <= label.center_local()[1] + int(window.height * 0.18)
            and not _matches_any(block.text, DEVICE_LABEL_CANDIDATES)
            and not _matches_any(block.text, NETWORK_PROVIDER_CANDIDATES)
            and not _matches_any(block.text, ROOT_ACCESS_CANDIDATES)
        ]
        if not candidates:
            return None

        candidates.sort(
            key=lambda block: (
                block.center_local()[1] - label.center_local()[1],
                -block.width,
                -block.confidence,
            )
        )
        return candidates[0]

    def _try_click_save_button(self) -> bool:
        window = self._window()
        blocks = self._capture_blocks(window)
        target = self._find_best_match(
            blocks,
            SAVE_BUTTON_CANDIDATES,
            x_range=(0.55, 1.00),
            y_range=(0.80, 1.00),
        )
        if target is None:
            return False
        self._click_block(target)
        time.sleep(0.35)
        return True

    def _wait_for_changed_preset(self, before_preset: str, *, timeout_seconds: float) -> str:
        before_normalized = _normalize_preset_name(before_preset)
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                current = self.current_preset()
            except RuntimeError:
                time.sleep(0.25)
                continue
            if _normalize_preset_name(current) != before_normalized:
                return current
            time.sleep(0.25)
        raise RuntimeError(f"BlueStacks device preset did not change from: {before_preset}")

    def _wait_for(self, predicate, *, timeout_seconds: float, description: str) -> None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.30)
        raise RuntimeError(f"{description} did not become visible.")

    def _capture_blocks(self, window: WindowRect) -> list[OCRWindowBlock]:
        focus_window(window)
        image = self._grab_window_image(window)
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

    def _capture_screen_blocks(self) -> list[OCRWindowBlock]:
        image = self._grab_fullscreen_image()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        blocks = self._ocr.recognize(buffer.getvalue())
        converted: list[OCRWindowBlock] = []
        for block in blocks:
            if block.confidence < DEFAULT_MIN_CONFIDENCE:
                continue
            local_box = [(int(x), int(y)) for x, y in block.box]
            converted.append(
                OCRWindowBlock(
                    text=block.text,
                    confidence=block.confidence,
                    local_box=local_box,
                    screen_box=list(local_box),
                )
            )
        return converted

    def _grab_window_image(self, window: WindowRect):
        focus_window(window)
        try:
            return grab_window_image(window)
        except Exception:
            bbox = (window.left, window.top, window.right, window.bottom)
            try:
                return ImageGrab.grab(bbox=bbox, all_screens=True)
            except TypeError:
                return ImageGrab.grab(bbox=bbox)

    def _grab_fullscreen_image(self):
        try:
            return ImageGrab.grab(all_screens=True)
        except TypeError:
            return ImageGrab.grab()

    def _find_best_match(
        self,
        blocks: list[OCRWindowBlock],
        terms: tuple[str, ...] | list[str],
        *,
        x_range: tuple[float, float],
        y_range: tuple[float, float],
    ) -> OCRWindowBlock | None:
        window = self._window()
        x_min = int(window.width * x_range[0])
        x_max = int(window.width * x_range[1])
        y_min = int(window.height * y_range[0])
        y_max = int(window.height * y_range[1])

        matches = [
            block
            for block in blocks
            if x_min <= block.center_local()[0] <= x_max
            and y_min <= block.center_local()[1] <= y_max
            and _matches_any(block.text, terms)
        ]
        if not matches:
            return None
        matches.sort(key=lambda block: (-block.confidence, -block.width, block.center_local()[1]))
        return matches[0]

    def _click_block(self, block: OCRWindowBlock) -> None:
        focus_window(self._window())
        x, y = block.center_screen()
        click_screen(x, y)

    def _invoke_toolbar_button(self, rank_from_right: int) -> bool:
        script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$proc = Get-Process | Where-Object {{ $_.MainWindowTitle -eq '{self.window_title.replace("'", "''")}' }} | Select-Object -First 1
if (-not $proc) {{ throw 'BlueStacks player window not found.' }}
$root = [System.Windows.Automation.AutomationElement]::FromHandle($proc.MainWindowHandle)
$rootRect = $root.Current.BoundingRectangle
$all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
$buttons = New-Object System.Collections.Generic.List[object]
for ($i = 0; $i -lt $all.Count; $i++) {{
  $el = $all.Item($i)
  try {{
    if ($el.Current.ControlType -ne [System.Windows.Automation.ControlType]::Button) {{ continue }}
    if (-not $el.Current.ClassName.StartsWith('UiImageButton')) {{ continue }}
    $rect = $el.Current.BoundingRectangle
    if ($rect.Top -gt ($rootRect.Top + 50)) {{ continue }}
    $buttons.Add($el) | Out-Null
  }} catch {{}}
}}
if ($buttons.Count -lt {rank_from_right}) {{ exit 3 }}
$sorted = $buttons | Sort-Object {{ $_.Current.BoundingRectangle.Left }}
$target = $sorted[$sorted.Count - {rank_from_right}]
$pattern = $target.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
$pattern.Invoke()
"""
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        return result.returncode == 0

    def _click_settings_menu_item(self) -> bool:
        window = self._window()
        blocks = self._capture_screen_blocks()
        matches = [
            block
            for block in blocks
            if _matches_any(block.text, SETTINGS_TITLE_CANDIDATES)
            and (window.right - MENU_SETTINGS_SEARCH_MARGIN) <= block.center_screen()[0] <= (window.right + MENU_SETTINGS_SEARCH_MARGIN)
            and (window.top - 40) <= block.center_screen()[1] <= (window.top + 360)
        ]
        if not matches:
            return False
        matches.sort(key=lambda block: (-block.confidence, block.center_screen()[1], -block.center_screen()[0]))
        x, y = matches[0].center_screen()
        click_screen(x, y)
        return True


def switch_to_different_device_preset(
    *,
    window_title: str | None = None,
    save_wait_seconds: float = 3.0,
) -> DevicePresetSwitchResult:
    resolved_title = window_title or find_bluestacks_player_window().title
    controller = BlueStacksSettingsController(window_title=resolved_title)
    return controller.switch_preset(
        steps=1,
        save=True,
        save_wait_seconds=save_wait_seconds,
    )
