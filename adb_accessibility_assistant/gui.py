from __future__ import annotations

import queue
import threading
from pathlib import Path

from .runtime import create_runtime


class AssistantWindow:
    def __init__(
        self,
        *,
        config_path: Path | None,
        adb_path: str | None,
        device_serial: str | None,
        target_package: str | None,
        launch_activity: str | None,
        tts_enabled: bool,
    ) -> None:
        import tkinter as tk
        from tkinter import scrolledtext

        self.tk = tk
        self.root = tk.Tk()
        self.root.title("ADB Accessibility Assistant")
        self.root.geometry("780x520")

        self._config_path = config_path
        self._adb_path = adb_path
        self._device_serial = device_serial
        self._target_package = target_package
        self._launch_activity = launch_activity
        self._tts_enabled = tts_enabled
        self._queue: queue.Queue[str] = queue.Queue()
        self._loop_stop = threading.Event()
        self.runtime = None

        frame = tk.Frame(self.root)
        frame.pack(fill="x", padx=12, pady=12)

        tk.Label(frame, text="Config").grid(row=0, column=0, sticky="w")
        self.config_var = tk.StringVar(value=str(config_path) if config_path else "")
        tk.Entry(frame, textvariable=self.config_var, width=64).grid(row=0, column=1, columnspan=5, sticky="ew", padx=6)

        button_frame = tk.Frame(self.root)
        button_frame.pack(fill="x", padx=12)
        self._add_button(button_frame, "Reload", self.reload_runtime)
        self._add_button(button_frame, "Start App", self.start_app)
        self._add_button(button_frame, "Scan", self.scan)
        self._add_button(button_frame, "Auto Step", self.auto_step)
        self._add_button(button_frame, "Start Loop", self.start_loop)
        self._add_button(button_frame, "Stop Loop", self.stop_loop)
        self._add_button(button_frame, "Back", self.go_back)

        self.status_var = tk.StringVar(value="Initializing...")
        tk.Label(self.root, textvariable=self.status_var, anchor="w").pack(fill="x", padx=12, pady=(8, 0))

        self.log_box = scrolledtext.ScrolledText(self.root, wrap="word", height=24)
        self.log_box.pack(fill="both", expand=True, padx=12, pady=12)

        self.reload_runtime()
        self.root.after(200, self._drain_queue)

    def _add_button(self, parent, label: str, action) -> None:
        button = self.tk.Button(parent, text=label, width=12, command=action)
        button.pack(side="left", padx=(0, 8), pady=4)

    def log(self, message: str) -> None:
        self._queue.put(message)

    def reload_runtime(self) -> None:
        try:
            config_path = Path(self.config_var.get()) if self.config_var.get().strip() else None
            self.runtime = create_runtime(
                config_path=config_path,
                adb_path=self._adb_path,
                device_serial=self._device_serial,
                target_package=self._target_package,
                launch_activity=self._launch_activity,
                tts_enabled=self._tts_enabled,
                log_callback=self.log,
            )
            self.status_var.set("Ready")
            self.log("Runtime reloaded.")
        except Exception as exc:
            self.status_var.set("Initialization failed")
            self.log(f"Initialization failed: {exc}")

    def _run_worker(self, label: str, func) -> None:
        self.status_var.set(label)

        def runner() -> None:
            try:
                func()
                self._queue.put("Operation finished.")
            except Exception as exc:
                self._queue.put(f"Operation failed: {exc}")
            finally:
                self._queue.put("__STATUS__:Ready")

        threading.Thread(target=runner, daemon=True).start()

    def start_app(self) -> None:
        if self.runtime is None:
            self.reload_runtime()
            return
        self._run_worker("Starting app...", self.runtime.engine.start_app)

    def scan(self) -> None:
        if self.runtime is None:
            self.reload_runtime()
            return

        def worker() -> None:
            snapshot, _ = self.runtime.engine.scan_and_describe()
            for line in self.runtime.engine.list_snapshot_lines(snapshot):
                self.log(line)

        self._run_worker("Scanning...", worker)

    def auto_step(self) -> None:
        if self.runtime is None:
            self.reload_runtime()
            return

        def worker() -> None:
            result = self.runtime.engine.auto_step()
            self.log(result.message)

        self._run_worker("Running auto step...", worker)

    def start_loop(self) -> None:
        if self.runtime is None:
            self.reload_runtime()
            return

        self._loop_stop.clear()
        self.status_var.set("Loop running...")

        def loop_worker() -> None:
            try:
                while not self._loop_stop.is_set():
                    result = self.runtime.engine.auto_step()
                    self.log(result.message)
                    if result.status != "acted":
                        break
                    self._loop_stop.wait(self.runtime.config.polling_interval)
            except Exception as exc:
                self.log(f"Loop failed: {exc}")
            finally:
                self._queue.put("__STATUS__:Ready")

        threading.Thread(target=loop_worker, daemon=True).start()

    def stop_loop(self) -> None:
        self._loop_stop.set()
        self.status_var.set("Loop stopping...")

    def go_back(self) -> None:
        if self.runtime is None:
            self.reload_runtime()
            return

        def worker() -> None:
            self.runtime.adb.keyevent("KEYCODE_BACK")
            self.log("Sent BACK")
            self.runtime.speaker.say("已返回。")

        self._run_worker("Sending BACK...", worker)

    def _drain_queue(self) -> None:
        while True:
            try:
                message = self._queue.get_nowait()
            except queue.Empty:
                break
            if message.startswith("__STATUS__:"):
                self.status_var.set(message.split(":", 1)[1])
                continue
            self.log_box.insert("end", message + "\n")
            self.log_box.see("end")
        self.root.after(200, self._drain_queue)

    def run(self) -> None:
        self.root.mainloop()


def launch_gui(
    *,
    config_path: Path | None,
    adb_path: str | None,
    device_serial: str | None,
    target_package: str | None,
    launch_activity: str | None,
    tts_enabled: bool,
) -> None:
    window = AssistantWindow(
        config_path=config_path,
        adb_path=adb_path,
        device_serial=device_serial,
        target_package=target_package,
        launch_activity=launch_activity,
        tts_enabled=tts_enabled,
    )
    window.run()
