from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any


class PhoneActivationStore:
    """Persist purchased SMS activations for short-lived local reuse."""

    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        if self.db_path.parent != Path():
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS phone_activations (
                    phone_number TEXT PRIMARY KEY,
                    service_code TEXT NOT NULL,
                    country_name TEXT NOT NULL,
                    country_id INTEGER,
                    price REAL,
                    acquired_at_epoch REAL NOT NULL,
                    expiry_epoch REAL NOT NULL,
                    status TEXT NOT NULL,
                    status_reason TEXT NOT NULL DEFAULT '',
                    created_at_epoch REAL NOT NULL,
                    updated_at_epoch REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_phone_activations_lookup
                ON phone_activations (service_code, status, expiry_epoch DESC)
                """
            )

    def expire_old(self, *, now: float | None = None) -> int:
        current_time = float(now if now is not None else time.time())
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE phone_activations
                SET status = 'expired',
                    status_reason = 'expired',
                    updated_at_epoch = ?
                WHERE status = 'active'
                  AND expiry_epoch <= ?
                """,
                (current_time, current_time),
            )
            return int(cursor.rowcount or 0)

    def save_purchase(
        self,
        *,
        phone_number: str,
        service_code: str,
        country_name: str,
        country_id: int | None,
        price: float | None,
        acquired_at_epoch: float,
        expiry_epoch: float,
    ) -> None:
        current_time = time.time()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO phone_activations (
                    phone_number,
                    service_code,
                    country_name,
                    country_id,
                    price,
                    acquired_at_epoch,
                    expiry_epoch,
                    status,
                    status_reason,
                    created_at_epoch,
                    updated_at_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', '', ?, ?)
                ON CONFLICT(phone_number) DO UPDATE SET
                    service_code = excluded.service_code,
                    country_name = excluded.country_name,
                    country_id = excluded.country_id,
                    price = excluded.price,
                    acquired_at_epoch = excluded.acquired_at_epoch,
                    expiry_epoch = excluded.expiry_epoch,
                    status = 'active',
                    status_reason = '',
                    created_at_epoch = excluded.created_at_epoch,
                    updated_at_epoch = excluded.updated_at_epoch
                """,
                (
                    phone_number,
                    service_code,
                    country_name,
                    country_id,
                    price,
                    acquired_at_epoch,
                    expiry_epoch,
                    acquired_at_epoch,
                    current_time,
                ),
            )

    def list_reusable_candidates(
        self,
        *,
        service_code: str,
        min_remaining_seconds: float,
        now: float | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        current_time = float(now if now is not None else time.time())
        self.expire_old(now=current_time)
        minimum_expiry = current_time + max(0.0, float(min_remaining_seconds))
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT
                    phone_number,
                    service_code,
                    country_name,
                    country_id,
                    price,
                    acquired_at_epoch,
                    expiry_epoch,
                    status,
                    status_reason,
                    created_at_epoch,
                    updated_at_epoch
                FROM phone_activations
                WHERE service_code = ?
                  AND status = 'active'
                  AND expiry_epoch > ?
                ORDER BY expiry_epoch DESC, acquired_at_epoch DESC
                LIMIT ?
                """,
                (service_code, minimum_expiry, max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_status(self, phone_number: str, *, status: str, reason: str = "") -> None:
        current_time = time.time()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE phone_activations
                SET status = ?,
                    status_reason = ?,
                    updated_at_epoch = ?
                WHERE phone_number = ?
                """,
                (status, reason, current_time, phone_number),
            )

    def mark_invalid(self, phone_number: str, *, reason: str = "") -> None:
        self.mark_status(phone_number, status="invalidated", reason=reason)

    def mark_consumed(self, phone_number: str, *, reason: str = "") -> None:
        self.mark_status(phone_number, status="consumed", reason=reason)
