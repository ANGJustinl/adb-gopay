from __future__ import annotations

import base64
import ctypes
import json
import subprocess
import time
from dataclasses import dataclass


VK_DOWN = 0x28
VK_UP = 0x26
VK_RETURN = 0x0D
VK_CONTROL = 0x11
VK_S = 0x53
KEYEVENTF_KEYUP = 0x0002
SW_RESTORE = 9
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

SETTINGS_TITLE_CANDIDATES = ("設定", "设置", "Settings")
SAVE_BUTTON_CANDIDATES = ("儲存變更", "储存变更", "Save changes", "Save Changes")

TOP_MENU_BUTTON_X_RATIO = 0.764
TOP_MENU_BUTTON_Y_RATIO = 0.018
TOP_MENU_SETTINGS_X_RATIO = 0.878
TOP_MENU_SETTINGS_Y_RATIO = 0.039
PHONE_SETTINGS_TAB_X_RATIO = 0.083
PHONE_SETTINGS_TAB_Y_RATIO = 0.497
PHONE_PRESET_COMBO_X_RATIO = 0.617
PHONE_PRESET_COMBO_Y_RATIO = 0.356


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)


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


user32 = ctypes.windll.user32


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
    return [window for window in _list_visible_windows() if window.title.startswith("BlueStacks App Player")]


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
    user32.ShowWindow(window.hwnd, SW_RESTORE)
    user32.SetForegroundWindow(window.hwnd)
    time.sleep(0.3)


def click_screen(x: int, y: int) -> None:
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.1)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def click_window_ratio(window: WindowRect, x_ratio: float, y_ratio: float) -> None:
    x = window.left + int(window.width * x_ratio)
    y = window.top + int(window.height * y_ratio)
    click_screen(x, y)


def tap_virtual_key(key_code: int) -> None:
    user32.keybd_event(key_code, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(key_code, 0, KEYEVENTF_KEYUP, 0)


def tap_ctrl_shortcut(key_code: int) -> None:
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(key_code, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(key_code, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _run_powershell(script: str) -> str:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or "unknown PowerShell error"
        raise RuntimeError(stderr)
    return result.stdout.strip()


def _base_ui_script(window_title: str) -> str:
    return f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$proc = Get-Process | Where-Object {{ $_.MainWindowTitle -eq {_ps_quote(window_title)} }} | Select-Object -First 1
if (-not $proc) {{ throw 'BlueStacks player window not found.' }}
$root = [System.Windows.Automation.AutomationElement]::FromHandle($proc.MainWindowHandle)
"""


def _json_output(script: str) -> dict[str, object]:
    output = _run_powershell(script)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("PowerShell command returned no output.")
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse PowerShell JSON output: {lines[-1]}") from exc


def is_settings_window_open(window_title: str) -> bool:
    candidates = ", ".join(_ps_quote(name) for name in SETTINGS_TITLE_CANDIDATES)
    script = (
        _base_ui_script(window_title)
        + f"""
$titles = @({candidates})
$all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
$found = $false
foreach ($el in $all) {{
  try {{
    if ($el.Current.ControlType -eq [System.Windows.Automation.ControlType]::Window -and $titles -contains $el.Current.Name) {{
      $found = $true
      break
    }}
  }} catch {{}}
}}
Write-Output ($found | ConvertTo-Json -Compress)
"""
    )
    return bool(_json_output(script))


def get_device_combo_name(window_title: str) -> str:
    script = (
        _base_ui_script(window_title)
        + """
$combos = $root.FindAll(
  [System.Windows.Automation.TreeScope]::Descendants,
  (New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::ComboBox
  ))
)
if ($combos.Count -lt 1) { throw 'Device preset combo not found.' }
$combo = $combos.Item(0)
Write-Output (@{ name = $combo.Current.Name } | ConvertTo-Json -Compress)
"""
    )
    data = _json_output(script)
    return str(data["name"])


def focus_device_combo(window_title: str) -> None:
    script = (
        _base_ui_script(window_title)
        + """
$combos = $root.FindAll(
  [System.Windows.Automation.TreeScope]::Descendants,
  (New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::ComboBox
  ))
)
if ($combos.Count -lt 1) { throw 'Device preset combo not found.' }
$combo = $combos.Item(0)
$combo.SetFocus()
Write-Output 'ok'
"""
    )
    _run_powershell(script)


def invoke_save_button(window_title: str) -> None:
    button_terms = ", ".join(_ps_quote(name) for name in SAVE_BUTTON_CANDIDATES)
    script = (
        _base_ui_script(window_title)
        + f"""
$buttonTerms = @({button_terms})
$buttons = $root.FindAll(
  [System.Windows.Automation.TreeScope]::Descendants,
  (New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Button
  ))
)
$target = $null
foreach ($button in $buttons) {{
  try {{
    $name = $button.Current.Name
    if (-not $name) {{ continue }}
    foreach ($term in $buttonTerms) {{
      if ($name.StartsWith($term)) {{
        $target = $button
        break
      }}
    }}
    if ($target) {{ break }}
  }} catch {{}}
}}
if (-not $target) {{ throw 'Save button not found.' }}
$invoke = $target.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
$invoke.Invoke()
Write-Output 'ok'
"""
    )
    _run_powershell(script)


class BlueStacksSettingsController:
    def __init__(self, *, window_title: str) -> None:
        self.window_title = window_title

    def _window(self) -> WindowRect:
        return find_bluestacks_player_window(self.window_title)

    def ensure_phone_settings_open(self, *, timeout_seconds: float = 5.0) -> None:
        if self._phone_settings_ready():
            return

        if is_settings_window_open(self.window_title):
            self._click_phone_tab()
        else:
            self._open_settings_from_toolbar()
            self._click_phone_tab()

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if self._phone_settings_ready():
                return
            time.sleep(0.25)
        raise RuntimeError("BlueStacks phone settings page did not become ready.")

    def current_preset(self) -> str:
        return get_device_combo_name(self.window_title)

    def cycle_once(self, *, direction: str = "next") -> str:
        window = self._window()
        focus_window(window)
        focus_device_combo(self.window_title)
        time.sleep(0.2)
        if direction == "previous":
            tap_virtual_key(VK_UP)
        else:
            tap_virtual_key(VK_DOWN)
        time.sleep(0.15)
        tap_virtual_key(VK_RETURN)
        time.sleep(0.4)
        return self.current_preset()

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
        invoke_save_button(self.window_title)

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

    def _phone_settings_ready(self) -> bool:
        try:
            get_device_combo_name(self.window_title)
            return True
        except RuntimeError:
            return False

    def _open_settings_from_toolbar(self) -> None:
        window = self._window()
        focus_window(window)
        click_window_ratio(window, TOP_MENU_BUTTON_X_RATIO, TOP_MENU_BUTTON_Y_RATIO)
        time.sleep(0.5)
        click_window_ratio(window, TOP_MENU_SETTINGS_X_RATIO, TOP_MENU_SETTINGS_Y_RATIO)
        time.sleep(1.0)

    def _click_phone_tab(self) -> None:
        window = self._window()
        focus_window(window)
        click_window_ratio(window, PHONE_SETTINGS_TAB_X_RATIO, PHONE_SETTINGS_TAB_Y_RATIO)
        time.sleep(0.5)


def switch_to_different_device_preset(
    *,
    window_title: str | None = None,
    save_wait_seconds: float = 3.0,
) -> DevicePresetSwitchResult:
    """Switch the visible BlueStacks player to a different device preset once."""
    resolved_title = window_title or find_bluestacks_player_window().title
    controller = BlueStacksSettingsController(window_title=resolved_title)
    try:
        return controller.switch_preset(
            steps=1,
            save=True,
            save_wait_seconds=save_wait_seconds,
        )
    except RuntimeError:
        return _switch_preset_by_coordinates(
            window_title=resolved_title,
            save_wait_seconds=save_wait_seconds,
        )


def _switch_preset_by_coordinates(
    *,
    window_title: str,
    save_wait_seconds: float,
) -> DevicePresetSwitchResult:
    controller = BlueStacksSettingsController(window_title=window_title)
    window = controller._window()
    focus_window(window)
    controller._open_settings_from_toolbar()
    controller._click_phone_tab()

    click_window_ratio(window, PHONE_PRESET_COMBO_X_RATIO, PHONE_PRESET_COMBO_Y_RATIO)
    time.sleep(0.3)
    tap_virtual_key(VK_DOWN)
    time.sleep(0.15)
    tap_virtual_key(VK_RETURN)
    time.sleep(0.5)
    tap_ctrl_shortcut(VK_S)
    time.sleep(save_wait_seconds)

    return DevicePresetSwitchResult(
        window_title=window_title,
        before_preset="unknown-ui-fallback",
        after_preset="next-preset-ui-fallback",
        changed=True,
    )


def _normalize_preset_name(value: str) -> str:
    return " ".join(value.casefold().split())
