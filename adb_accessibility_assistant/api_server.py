from __future__ import annotations

import json
import queue
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from .gopay_tasks import (
    cleanup_active_gopay_clone,
    inspect_gopay_page_task,
    prepare_phone_input_task,
    resolve_gopay_task_config_path,
    run_gopay_full_task,
    validate_gopay_config_file,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    task_type: str
    status: TaskStatus
    request_payload: dict[str, Any]
    callback_url: str = ""
    callback_headers: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    error: str = ""
    logs: list[str] = field(default_factory=list)
    cancel_requested: bool = False

    def to_dict(self, *, include_logs: bool = True) -> dict[str, Any]:
        payload = {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "request": self.request_payload,
            "metadata": self.metadata,
            "callback_url": self.callback_url,
            "error": self.error,
            "result": self.result,
            "cancel_requested": self.cancel_requested,
        }
        if include_logs:
            payload["logs"] = list(self.logs)
        return payload


class TaskManager:
    def __init__(
        self,
        *,
        default_config_path: str | Path | None = None,
        default_mute: bool = True,
        callback_timeout: float = 10.0,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.default_config_path = str(default_config_path) if default_config_path else None
        self.default_mute = default_mute
        self.callback_timeout = callback_timeout
        self.log = log_callback or (lambda msg: None)
        self._tasks: dict[str, TaskRecord] = {}
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._active_task_id: str | None = None
        self._active_stop: Callable[[], None] | None = None
        self._worker = threading.Thread(target=self._worker_loop, name="gopay-api-worker", daemon=True)
        self._worker.start()

    def shutdown(self) -> None:
        self._stop_event.set()
        self._queue.put("")
        with self._lock:
            active_stop = self._active_stop
        if active_stop:
            try:
                active_stop()
            except Exception:
                pass

    def submit(self, task_type: str, payload: dict[str, Any]) -> TaskRecord:
        normalized_type = task_type.strip().lower()
        if normalized_type not in {"run-gopay", "prepare-phone-input", "inspect"}:
            raise ValueError(f"unsupported task_type: {task_type}")

        config_path = resolve_gopay_task_config_path(payload.get("config_path"), self.default_config_path)
        validate_gopay_config_file(config_path)

        callback_headers: dict[str, str] = {}
        raw_headers = payload.get("callback_headers")
        if isinstance(raw_headers, dict):
            callback_headers = {
                str(key): str(value)
                for key, value in raw_headers.items()
            }

        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}

        request_payload = dict(payload)
        request_payload["config_path"] = str(config_path)

        record = TaskRecord(
            task_id=uuid.uuid4().hex,
            task_type=normalized_type,
            status=TaskStatus.QUEUED,
            request_payload=request_payload,
            callback_url=str(payload.get("callback_url") or "").strip(),
            callback_headers=callback_headers,
            metadata=metadata,
        )
        with self._lock:
            self._tasks[record.task_id] = record
        self._queue.put(record.task_id)
        self.log(f"Queued task {record.task_id} ({record.task_type})")
        self._emit_callback(record, "task.queued")
        return record

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            records = sorted(self._tasks.values(), key=lambda item: item.created_at, reverse=True)
            return [record.to_dict(include_logs=False) for record in records]

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            return self._tasks.get(task_id)

    def cancel_task(self, task_id: str) -> TaskRecord:
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise KeyError(task_id)
            if record.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELED}:
                return record
            record.cancel_requested = True
            record.updated_at = _now_iso()
            is_active = self._active_task_id == task_id
            active_stop = self._active_stop if is_active else None
            if record.status == TaskStatus.QUEUED and not is_active:
                record.status = TaskStatus.CANCELED
                record.error = "Canceled before execution."
                record.finished_at = _now_iso()
                record.updated_at = record.finished_at
                self._emit_callback(record, "task.canceled")
                return record

        if active_stop:
            try:
                active_stop()
            except Exception as exc:
                self._append_log(task_id, f"Cancel warning: {exc}")
        return record

    def _append_log(self, task_id: str, message: str) -> None:
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return
            record.logs.append(message)
            if len(record.logs) > 500:
                del record.logs[:-500]
            record.updated_at = _now_iso()
        self.log(f"[task {task_id}] {message}")

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            task_id = self._queue.get()
            if not task_id:
                continue
            record = self.get_task(task_id)
            if record is None:
                continue
            if record.status == TaskStatus.CANCELED:
                continue
            self._run_task(record)

    def _set_active_control(self, task_id: str, stop_fn: Callable[[], None]) -> None:
        with self._lock:
            self._active_task_id = task_id
            self._active_stop = stop_fn

    def _clear_active_control(self, task_id: str) -> None:
        with self._lock:
            if self._active_task_id == task_id:
                self._active_task_id = None
                self._active_stop = None

    def _run_task(self, record: TaskRecord) -> None:
        with self._lock:
            if record.status == TaskStatus.CANCELED:
                return
            record.status = TaskStatus.RUNNING
            record.started_at = _now_iso()
            record.updated_at = record.started_at
            self._active_task_id = record.task_id
            self._active_stop = None
        self._emit_callback(record, "task.started")

        try:
            result = self._execute_task(record)
            with self._lock:
                fresh = self._tasks.get(record.task_id)
                if fresh is None:
                    return
                fresh.result = result
                fresh.finished_at = _now_iso()
                fresh.updated_at = fresh.finished_at
                if fresh.cancel_requested:
                    fresh.status = TaskStatus.CANCELED
                    fresh.error = "Canceled by request."
                    event_name = "task.canceled"
                elif result.get("ok"):
                    fresh.status = TaskStatus.SUCCEEDED
                    fresh.error = ""
                    event_name = "task.succeeded"
                else:
                    fresh.status = TaskStatus.FAILED
                    fresh.error = str(result.get("message") or "Task failed.")
                    event_name = "task.failed"
                record = fresh
            self._emit_callback(record, event_name)
            self._cleanup_clone_after_callback(record)
        except Exception as exc:
            with self._lock:
                fresh = self._tasks.get(record.task_id)
                if fresh is None:
                    return
                fresh.status = TaskStatus.CANCELED if fresh.cancel_requested else TaskStatus.FAILED
                fresh.error = str(exc)
                fresh.result = {
                    "ok": False,
                    "status": "canceled" if fresh.cancel_requested else "error",
                    "state": "",
                    "message": str(exc),
                    "data": {},
                }
                fresh.finished_at = _now_iso()
                fresh.updated_at = fresh.finished_at
                record = fresh
            self._emit_callback(record, "task.canceled" if record.status == TaskStatus.CANCELED else "task.failed")
            self._cleanup_clone_after_callback(record)
        finally:
            self._clear_active_control(record.task_id)

    def _execute_task(self, record: TaskRecord) -> dict[str, Any]:
        payload = record.request_payload

        def register_stop(stop_fn: Callable[[], None]) -> None:
            self._set_active_control(record.task_id, stop_fn)

        common_kwargs = {
            "config_path": payload["config_path"],
            "adb_path": payload.get("adb_path"),
            "device_serial": payload.get("device_serial"),
            "mute": bool(payload.get("mute", self.default_mute)),
            "log_callback": lambda msg: self._append_log(record.task_id, msg),
            "register_stop": register_stop,
        }

        if record.task_type == "prepare-phone-input":
            return prepare_phone_input_task(
                **common_kwargs,
                max_steps=int(payload.get("max_steps", 10)),
            )
        if record.task_type == "run-gopay":
            return run_gopay_full_task(
                **common_kwargs,
                max_steps=int(payload.get("max_steps", 200)),
                poll_timeout=int(payload["poll_timeout"]) if payload.get("poll_timeout") is not None else None,
                step_delay=float(payload["step_delay"]) if payload.get("step_delay") is not None else None,
                retry_on_otp_timeout=bool(payload.get("retry_on_otp_timeout", False)),
                phone=str(payload["phone"]).strip() if payload.get("phone") else None,
                defer_clone_cleanup=True,
            )
        if record.task_type == "inspect":
            return inspect_gopay_page_task(
                **common_kwargs,
                save_dir=payload.get("save_dir") or "artifacts\\gopay",
            )
        raise ValueError(f"unsupported task_type: {record.task_type}")

    def _emit_callback(self, record: TaskRecord, event_name: str) -> None:
        if not record.callback_url:
            return
        payload = {
            "event": event_name,
            "event_at": _now_iso(),
            "task": record.to_dict(include_logs=True),
        }
        try:
            requests.post(
                record.callback_url,
                json=payload,
                headers=record.callback_headers or None,
                timeout=self.callback_timeout,
            )
        except Exception as exc:
            self._append_log(record.task_id, f"Callback delivery failed: {exc}")

    def _cleanup_clone_after_callback(self, record: TaskRecord) -> None:
        if record.task_type != "run-gopay":
            return
        data = ((record.result or {}).get("data") or {}) if isinstance(record.result, dict) else {}
        if not isinstance(data, dict) or not data.get("clone_cleanup_pending"):
            return
        try:
            cleaned = cleanup_active_gopay_clone(
                config_path=record.request_payload["config_path"],
                adb_path=record.request_payload.get("adb_path"),
                log_callback=lambda msg: self._append_log(record.task_id, msg),
            )
            if cleaned:
                self._append_log(record.task_id, "Temporary BlueStacks clone deleted after callback.")
            else:
                self._append_log(record.task_id, "No active temporary BlueStacks clone remained after callback.")
        except Exception as exc:
            self._append_log(record.task_id, f"Post-callback clone cleanup failed: {exc}")


def _json_response(handler: BaseHTTPRequestHandler, status: HTTPStatus, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status.value)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    content_length = int(handler.headers.get("Content-Length") or 0)
    if content_length <= 0:
        return {}
    raw_body = handler.rfile.read(content_length)
    if not raw_body.strip():
        return {}
    payload = json.loads(raw_body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload


def create_api_handler(manager: TaskManager) -> type[BaseHTTPRequestHandler]:
    class ApiHandler(BaseHTTPRequestHandler):
        server_version = "adb-gopay-api/1.0"

        def log_message(self, format: str, *args: object) -> None:
            manager.log(format % args)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            parts = [part for part in parsed.path.split("/") if part]

            if parts == ["api", "health"]:
                _json_response(
                    self,
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "service": "adb-gopay-api",
                        "time": _now_iso(),
                        "active_task_id": manager._active_task_id,
                        "queue_size": manager._queue.qsize(),
                        "default_config_path": manager.default_config_path,
                    },
                )
                return

            if parts == ["api", "tasks"]:
                _json_response(self, HTTPStatus.OK, {"ok": True, "tasks": manager.list_tasks()})
                return

            if len(parts) == 3 and parts[:2] == ["api", "tasks"]:
                record = manager.get_task(parts[2])
                if record is None:
                    _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "task_not_found"})
                    return
                _json_response(self, HTTPStatus.OK, {"ok": True, "task": record.to_dict(include_logs=True)})
                return

            _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            parts = [part for part in parsed.path.split("/") if part]

            try:
                payload = _read_json_body(self)
            except Exception as exc:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"invalid_json: {exc}"})
                return

            if parts == ["api", "tasks"]:
                task_type = str(payload.get("task_type") or "").strip()
                if not task_type:
                    _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "task_type_required"})
                    return
                try:
                    record = manager.submit(task_type, payload)
                except Exception as exc:
                    _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                _json_response(self, HTTPStatus.ACCEPTED, {"ok": True, "task": record.to_dict(include_logs=True)})
                return

            if parts == ["api", "tasks", "run-gopay"]:
                try:
                    record = manager.submit("run-gopay", payload)
                except Exception as exc:
                    _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                _json_response(self, HTTPStatus.ACCEPTED, {"ok": True, "task": record.to_dict(include_logs=True)})
                return

            if parts == ["api", "tasks", "prepare-phone-input"]:
                try:
                    record = manager.submit("prepare-phone-input", payload)
                except Exception as exc:
                    _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                _json_response(self, HTTPStatus.ACCEPTED, {"ok": True, "task": record.to_dict(include_logs=True)})
                return

            if parts == ["api", "tasks", "inspect"]:
                try:
                    record = manager.submit("inspect", payload)
                except Exception as exc:
                    _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                _json_response(self, HTTPStatus.ACCEPTED, {"ok": True, "task": record.to_dict(include_logs=True)})
                return

            if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "cancel":
                try:
                    record = manager.cancel_task(parts[2])
                except KeyError:
                    _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "task_not_found"})
                    return
                _json_response(self, HTTPStatus.OK, {"ok": True, "task": record.to_dict(include_logs=True)})
                return

            _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    return ApiHandler


def serve_api(
    *,
    host: str,
    port: int,
    default_config_path: str | Path | None = None,
    mute: bool = True,
    callback_timeout: float = 10.0,
    log_callback: Callable[[str], None] | None = None,
) -> None:
    manager = TaskManager(
        default_config_path=default_config_path,
        default_mute=mute,
        callback_timeout=callback_timeout,
        log_callback=log_callback,
    )
    server = ThreadingHTTPServer((host, port), create_api_handler(manager))
    try:
        if log_callback:
            log_callback(
                f"API server listening on http://{host}:{port} "
                f"(default config: {default_config_path or 'none'})"
            )
        server.serve_forever()
    finally:
        manager.shutdown()
        server.server_close()
