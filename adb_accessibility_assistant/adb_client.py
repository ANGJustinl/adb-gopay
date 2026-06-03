from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


class AndroidDeviceError(RuntimeError):
    """Raised when an adb operation fails."""


class ADBClient:
    def __init__(self, adb_path: str = "adb", device_serial: str | None = None, adb_port: str | None = None) -> None:
        self.adb_path = adb_path
        self.device_serial = device_serial
        self.adb_port = adb_port

    def _command(self, *args: str) -> list[str]:
        command = [self.adb_path]
        # Only include -P and -s options when their values are present (not None)
        if self.adb_port:
            command.extend(["-P", self.adb_port])
        if self.device_serial:
            command.extend(["-s", self.device_serial])
        command.extend(args)
        return command

    def run(
        self,
        *args: str,
        text: bool = True,
        check: bool = True,
        timeout: float = 30.0,
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                self._command(*args),
                capture_output=True,
                text=text,
                check=check,
                timeout=timeout,
                encoding="utf-8" if text else None,
                errors="replace" if text else None,
            )
        except FileNotFoundError as exc:
            raise AndroidDeviceError(f"adb executable not found: {self.adb_path}") from exc
        except subprocess.CalledProcessError as exc:
            error_text = exc.stderr if text else exc.stderr.decode("utf-8", errors="ignore")
            raise AndroidDeviceError(error_text.strip() or f"adb command failed: {' '.join(self._command(*args))}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AndroidDeviceError(
                f"adb command timed out after {timeout:.1f}s: {' '.join(self._command(*args))}"
            ) from exc

    def assert_available(self) -> None:
        self.run("version")

    def _is_tcp_serial(self) -> bool:
        serial = str(self.device_serial or "").strip()
        return bool(serial and ":" in serial)

    def connect(self, timeout: float = 30.0) -> None:
        serial = str(self.device_serial or "").strip()
        if not serial:
            raise AndroidDeviceError("adb connect requires device_serial")
        command = [self.adb_path]
        if self.adb_port:
            command.extend(["-P", self.adb_port])
        command.extend(["connect", serial])
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise AndroidDeviceError(f"adb executable not found: {self.adb_path}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AndroidDeviceError(
                f"adb command timed out after {timeout:.1f}s: {' '.join(command)}"
            ) from exc

        output = " ".join(part.strip() for part in (result.stdout, result.stderr) if part and part.strip()).strip()
        if result.returncode != 0 and "already connected" not in output.casefold():
            raise AndroidDeviceError(output or f"adb connect failed: {' '.join(command)}")

    def disconnect_all(self, timeout: float = 10.0) -> None:
        command = [self.adb_path]
        if self.adb_port:
            command.extend(["-P", self.adb_port])
        command.append("disconnect")
        try:
            subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise AndroidDeviceError(f"adb executable not found: {self.adb_path}") from exc
        except subprocess.CalledProcessError as exc:
            raise AndroidDeviceError((exc.stderr or exc.stdout or "").strip() or f"adb disconnect failed: {' '.join(command)}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AndroidDeviceError(
                f"adb command timed out after {timeout:.1f}s: {' '.join(command)}"
            ) from exc

    def wait_for_device(self, timeout: float = 30.0, *, reset_tcp_connections: bool = False) -> None:
        if reset_tcp_connections and self._is_tcp_serial():
            self.disconnect_all(timeout=min(timeout, 10.0))
        if self._is_tcp_serial():
            self.connect(timeout=min(timeout, 15.0))
        self.run("wait-for-device", timeout=timeout)

    def list_devices(self) -> list[str]:
        output = self.run("devices").stdout
        assert isinstance(output, str)
        devices: list[str] = []
        for line in output.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            serial, _, state = line.partition("\t")
            if state == "device":
                devices.append(serial)
        return devices

    def screencap_png(self) -> bytes:
        result = self.run("exec-out", "screencap", "-p", text=False, timeout=30.0)
        assert isinstance(result.stdout, bytes)
        if result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
            return result.stdout
        return self._capture_frame_via_screenrecord()

    def tap(self, x: int, y: int) -> None:
        self.run("shell", "input", "tap", str(x), str(y))

    def input_text(self, value: str) -> None:
        escaped = value.replace(" ", "%s")
        self.run("shell", "input", "text", escaped)

    def keyevent(self, keycode: str | int) -> None:
        self.run("shell", "input", "keyevent", str(keycode))

    def clear_app_data(self, package_name: str) -> None:
        self.run("shell", "pm", "clear", package_name)

    def is_package_running(self, package_name: str) -> bool:
        result = self.run("shell", "pidof", package_name, check=False)
        output = result.stdout.strip() if isinstance(result.stdout, str) else result.stdout.decode("utf-8", errors="ignore").strip()
        return bool(output)

    def start_app(self, package_name: str, activity_name: str | None = None) -> None:
        if activity_name:
            self.run("shell", "am", "start", "-n", f"{package_name}/{activity_name}")
            return
        self.run("shell", "monkey", "-p", package_name, "-c", "android.intent.category.LAUNCHER", "1")

    def _capture_frame_via_screenrecord(self) -> bytes:
        try:
            import cv2
        except ModuleNotFoundError as exc:
            raise AndroidDeviceError("OpenCV is required for screenrecord fallback capture") from exc

        result = self.run(
            "exec-out",
            "screenrecord",
            "--output-format=h264",
            "-",
            "--time-limit",
            "1",
            text=False,
            timeout=15.0,
        )
        assert isinstance(result.stdout, bytes)
        if not result.stdout:
            raise AndroidDeviceError("Unable to capture screen: screencap returned no PNG and screenrecord returned no data")

        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".h264", delete=False) as handle:
                handle.write(result.stdout)
                temp_path = Path(handle.name)

            capture = cv2.VideoCapture(str(temp_path))
            ok, frame = capture.read()
            capture.release()
            if not ok or frame is None:
                raise AndroidDeviceError("Unable to decode a video frame from screenrecord fallback")

            encoded, png = cv2.imencode(".png", frame)
            if not encoded:
                raise AndroidDeviceError("Unable to encode screenrecord frame as PNG")
            return png.tobytes()
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
