"""GoPay registration GUI for sighted assistants."""

from __future__ import annotations

import queue
import threading
from pathlib import Path

from .gopay_flow import FlowState
from .runtime import create_gopay_runtime


class GoPayRegistrationWindow:
    """GUI window for GoPay registration flow."""

    def __init__(
        self,
        *,
        config_path: Path,
        adb_path: str | None = None,
        device_serial: str | None = None,
        tts_enabled: bool = False,
    ) -> None:
        import tkinter as tk
        from tkinter import filedialog, messagebox, scrolledtext

        self.tk = tk
        self.root = tk.Tk()
        self.root.title("GoPay Registration Assistant")
        self.root.geometry("800x600")

        self._config_path = config_path
        self._adb_path = adb_path
        self._device_serial = device_serial
        self._tts_enabled = tts_enabled
        self._queue: queue.Queue[str] = queue.Queue()
        self.runtime = None

        # Top frame - Config
        config_frame = tk.LabelFrame(self.root, text="Configuration", padx=10, pady=5)
        config_frame.pack(fill="x", padx=10, pady=5)

        tk.Label(config_frame, text="Config File:").grid(row=0, column=0, sticky="w")
        self.config_var = tk.StringVar(value=str(config_path))
        tk.Entry(config_frame, textvariable=self.config_var, width=50).grid(row=0, column=1, padx=5)
        tk.Button(config_frame, text="Browse", command=self._browse_config).grid(row=0, column=2)

        # Status frame
        status_frame = tk.LabelFrame(self.root, text="Registration Status", padx=10, pady=5)
        status_frame.pack(fill="x", padx=10, pady=5)

        self.state_var = tk.StringVar(value="Not started")
        self.phone_var = tk.StringVar(value="-")
        self.username_var = tk.StringVar(value="-")
        self.pin_var = tk.StringVar(value="-")

        tk.Label(status_frame, text="State:").grid(row=0, column=0, sticky="w")
        tk.Label(status_frame, textvariable=self.state_var, font=("", 12, "bold")).grid(row=0, column=1, sticky="w")

        tk.Label(status_frame, text="Phone:").grid(row=1, column=0, sticky="w")
        tk.Label(status_frame, textvariable=self.phone_var).grid(row=1, column=1, sticky="w")

        tk.Label(status_frame, text="Username:").grid(row=2, column=0, sticky="w")
        tk.Label(status_frame, textvariable=self.username_var).grid(row=2, column=1, sticky="w")

        tk.Label(status_frame, text="PIN:").grid(row=3, column=0, sticky="w")
        tk.Label(status_frame, textvariable=self.pin_var).grid(row=3, column=1, sticky="w")

        # Button frame
        button_frame = tk.Frame(self.root)
        button_frame.pack(fill="x", padx=10, pady=5)

        self._add_button(button_frame, "Initialize", self.initialize)
        self._add_button(button_frame, "Start Registration", self.start_registration)
        self._add_button(button_frame, "Pause", self.pause)
        self._add_button(button_frame, "Resume", self.resume)
        self._add_button(button_frame, "Stop", self.stop)
        self._add_button(button_frame, "Reset", self.reset)

        # Log frame
        log_frame = tk.LabelFrame(self.root, text="Log", padx=10, pady=5)
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.log_box = scrolledtext.ScrolledText(log_frame, wrap="word", height=15)
        self.log_box.pack(fill="both", expand=True)

        # Initialize
        self.root.after(200, self._drain_queue)
        self._log("Ready. Click 'Initialize' to start.")

    def _add_button(self, parent, label: str, action) -> None:
        button = self.tk.Button(parent, text=label, width=15, command=action)
        button.pack(side="left", padx=(0, 5), pady=5)

    def _browse_config(self) -> None:
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="Select GoPay Config",
            filetypes=[("YAML files", "*.yaml"), ("All files", "*.*")],
        )
        if path:
            self.config_var.set(path)
            self._config_path = Path(path)

    def _log(self, message: str) -> None:
        self._queue.put(message)

    def _drain_queue(self) -> None:
        while True:
            try:
                message = self._queue.get_nowait()
            except queue.Empty:
                break
            self.log_box.insert("end", message + "\n")
            self.log_box.see("end")
        self.root.after(200, self._drain_queue)

    def _update_status(self) -> None:
        """Update status display."""
        if self.runtime is None:
            return

        status = self.runtime.flow.get_status()
        self.state_var.set(status["state"])
        self.phone_var.set(status["phone"] or "-")
        self.username_var.set(status["username"] or "-")
        self.pin_var.set("******" if status["has_pin"] else "-")

    def initialize(self) -> None:
        """Initialize the GoPay runtime."""
        try:
            config_path = Path(self.config_var.get())
            if not config_path.exists():
                self._log(f"Config file not found: {config_path}")
                return

            self.runtime = create_gopay_runtime(
                config_path=config_path,
                adb_path=self._adb_path,
                device_serial=self._device_serial,
                tts_enabled=self._tts_enabled,
                log_callback=self._log,
            )
            self._log("Runtime initialized successfully.")
            self._update_status()
        except Exception as exc:
            self._log(f"Initialization failed: {exc}")

    def start_registration(self) -> None:
        """Start the registration flow."""
        if self.runtime is None:
            self.initialize()
            if self.runtime is None:
                return

        self._log("Starting registration flow...")

        def run_flow() -> None:
            try:
                self._log("Starting registration flow from a clean app state...")
                final_state = self.runtime.flow.run(max_steps=50)
                self._log(f"Flow finished with state: {final_state.value}")

                if final_state == FlowState.REGISTRATION_COMPLETE:
                    self._log("Registration completed successfully!")
                elif final_state == FlowState.ERROR:
                    self._log("Registration failed with error.")
            except Exception as exc:
                self._log(f"Flow error: {exc}")
            finally:
                self._queue.put("__UPDATE_STATUS__")

        threading.Thread(target=run_flow, daemon=True).start()

        # Start status update timer
        self._start_status_updater()

    def _start_status_updater(self) -> None:
        """Start periodic status updates."""
        self._update_status()
        if self.runtime and not self.runtime.flow.is_complete and not self.runtime.flow.is_error:
            self.root.after(1000, self._start_status_updater)

    def pause(self) -> None:
        """Pause the flow."""
        if self.runtime:
            self.runtime.flow.pause()
            self._log("Flow paused.")

    def resume(self) -> None:
        """Resume the flow."""
        if self.runtime:
            self.runtime.flow.resume()
            self._log("Flow resumed.")

    def stop(self) -> None:
        """Stop the flow."""
        if self.runtime:
            self.runtime.flow.stop()
            self._log("Flow stopped.")

    def reset(self) -> None:
        """Reset the flow."""
        if self.runtime:
            self.runtime.flow.reset()
            self._log("Flow reset.")
            self._update_status()

    def run(self) -> None:
        """Start the GUI main loop."""
        self.root.mainloop()


def launch_gopay_gui(
    *,
    config_path: Path,
    adb_path: str | None = None,
    device_serial: str | None = None,
    tts_enabled: bool = False,
) -> None:
    """Launch the GoPay registration GUI.

    Args:
        config_path: Path to GoPay config YAML file.
        adb_path: Override ADB path.
        device_serial: Override device serial.
        tts_enabled: Enable text-to-speech.
    """
    window = GoPayRegistrationWindow(
        config_path=config_path,
        adb_path=adb_path,
        device_serial=device_serial,
        tts_enabled=tts_enabled,
    )
    window.run()
