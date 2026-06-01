from __future__ import annotations

import threading


class Speaker:
    def __init__(self, enabled: bool = True, rate: int = 175, voice_name: str | None = None) -> None:
        self.enabled = enabled
        self._lock = threading.Lock()
        self._engine = None
        if not enabled:
            return

        try:
            import pyttsx3
        except ModuleNotFoundError as exc:
            raise RuntimeError("pyttsx3 is not installed") from exc

        engine = pyttsx3.init()
        engine.setProperty("rate", rate)
        if voice_name:
            selected = next((voice.id for voice in engine.getProperty("voices") if voice_name.casefold() in voice.name.casefold()), None)
            if selected:
                engine.setProperty("voice", selected)
        self._engine = engine

    def say(self, text: str) -> None:
        if not self.enabled or not text.strip() or self._engine is None:
            return
        with self._lock:
            self._engine.say(text)
            self._engine.runAndWait()
