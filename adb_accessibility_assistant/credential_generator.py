"""Credential generator for GoPay registration."""

from __future__ import annotations

import json
import random
import string
from datetime import datetime
from pathlib import Path


def generate_username(length: int = 8, charset: str | None = None) -> str:
    """Generate a random alphabetic username.

    Args:
        length: Length of username (default 8).
        charset: Character set to use (default lowercase letters).

    Returns:
        Random username string like 'akmxpqbt'.
    """
    if charset is None:
        charset = string.ascii_lowercase
    return "".join(random.choices(charset, k=length))


def generate_pin(length: int = 6) -> str:
    """Generate a random numeric PIN.

    Args:
        length: Length of PIN (default 6).

    Returns:
        Random PIN string like '837291'.
    """
    return "".join(random.choices(string.digits, k=length))


def save_credentials(
    username: str,
    pin: str,
    phone: str,
    path: str | Path = "credentials.json",
) -> Path:
    """Save registration credentials to a JSON file.

    Args:
        username: The generated username.
        pin: The generated PIN.
        phone: The phone number used.
        path: Path to credentials file.

    Returns:
        Path to the saved file.
    """
    file_path = Path(path)

    # Load existing credentials if file exists
    existing: list[dict] = []
    if file_path.exists():
        try:
            existing = json.loads(file_path.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = [existing]
        except (json.JSONDecodeError, OSError):
            existing = []

    # Append new credential
    credential = {
        "username": username,
        "pin": pin,
        "phone": phone,
        "created_at": datetime.now().isoformat(),
    }
    existing.append(credential)

    # Write back
    file_path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return file_path


def load_credentials(path: str | Path = "credentials.json") -> list[dict]:
    """Load saved credentials from file.

    Args:
        path: Path to credentials file.

    Returns:
        List of credential dicts.
    """
    file_path = Path(path)
    if not file_path.exists():
        return []

    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else [data]
    except (json.JSONDecodeError, OSError):
        return []
