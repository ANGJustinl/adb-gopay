from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


DEFAULT_BLUESTACKS_CONF_PATH = Path(
    os.environ.get("PROGRAMDATA", r"C:\ProgramData")
) / "BlueStacks_nxt" / "bluestacks.conf"

_CONF_LINE_RE = re.compile(r'^(?P<key>[^=]+)="(?P<value>.*)"$')
_INSTANCE_KEY_RE = re.compile(r"^bst\.instance\.([^.]+)\.")


@dataclass(slots=True)
class BlueStacksInstanceProfile:
    instance_name: str
    display_name: str = ""
    adb_port: str = ""
    profile_code: str = ""
    custom_brand: str = ""
    custom_manufacturer: str = ""
    custom_model: str = ""

    @property
    def is_customized(self) -> bool:
        return any([self.custom_brand, self.custom_manufacturer, self.custom_model])


@dataclass(slots=True)
class BlueStacksConf:
    path: Path
    lines: list[str]
    values: dict[str, str]


def load_bluestacks_conf(path: str | Path | None = None) -> BlueStacksConf:
    conf_path = Path(path or DEFAULT_BLUESTACKS_CONF_PATH)
    if not conf_path.exists():
        raise FileNotFoundError(f"BlueStacks config not found: {conf_path}")

    raw_text = conf_path.read_text(encoding="utf-8")
    lines = raw_text.splitlines()
    values: dict[str, str] = {}
    for line in lines:
        match = _CONF_LINE_RE.match(line.strip())
        if not match:
            continue
        values[match.group("key")] = match.group("value")
    return BlueStacksConf(path=conf_path, lines=lines, values=values)


def list_instances(conf: BlueStacksConf) -> list[str]:
    instances = {
        match.group(1)
        for key in conf.values
        if (match := _INSTANCE_KEY_RE.match(key))
    }
    return sorted(instances)


def get_instance_profile(conf: BlueStacksConf, instance_name: str) -> BlueStacksInstanceProfile:
    prefix = f"bst.instance.{instance_name}."
    if not any(key.startswith(prefix) for key in conf.values):
        raise ValueError(f"BlueStacks instance not found: {instance_name}")

    return BlueStacksInstanceProfile(
        instance_name=instance_name,
        display_name=conf.values.get(f"{prefix}display_name", ""),
        adb_port=conf.values.get(f"{prefix}adb_port", "") or conf.values.get(f"{prefix}status.adb_port", ""),
        profile_code=conf.values.get(f"{prefix}device_profile_code", ""),
        custom_brand=conf.values.get(f"{prefix}device_custom_brand", ""),
        custom_manufacturer=conf.values.get(f"{prefix}device_custom_manufacturer", ""),
        custom_model=conf.values.get(f"{prefix}device_custom_model", ""),
    )


def backup_bluestacks_conf(conf_path: str | Path) -> Path:
    source_path = Path(conf_path)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = source_path.with_name(f"{source_path.name}.{timestamp}.bak")
    shutil.copy2(source_path, backup_path)
    return backup_path


def update_instance_profile(
    conf: BlueStacksConf,
    *,
    instance_name: str,
    profile_code: str | None = None,
    custom_brand: str | None = None,
    custom_manufacturer: str | None = None,
    custom_model: str | None = None,
) -> tuple[BlueStacksConf, dict[str, tuple[str, str]]]:
    prefix = f"bst.instance.{instance_name}."
    if not any(key.startswith(prefix) for key in conf.values):
        raise ValueError(f"BlueStacks instance not found: {instance_name}")

    updates = {
        f"{prefix}device_profile_code": profile_code,
        f"{prefix}device_custom_brand": custom_brand,
        f"{prefix}device_custom_manufacturer": custom_manufacturer,
        f"{prefix}device_custom_model": custom_model,
    }
    changes: dict[str, tuple[str, str]] = {}
    new_lines = list(conf.lines)
    line_index: dict[str, int] = {}

    for index, line in enumerate(new_lines):
        match = _CONF_LINE_RE.match(line.strip())
        if not match:
            continue
        line_index[match.group("key")] = index

    new_values = dict(conf.values)
    for key, value in updates.items():
        if value is None:
            continue
        old_value = new_values.get(key, "")
        if old_value == value:
            continue
        changes[key] = (old_value, value)
        serialized = f'{key}="{value}"'
        if key in line_index:
            new_lines[line_index[key]] = serialized
        else:
            new_lines.append(serialized)
        new_values[key] = value

    return BlueStacksConf(path=conf.path, lines=new_lines, values=new_values), changes


def save_bluestacks_conf(conf: BlueStacksConf) -> None:
    text = "\n".join(conf.lines).rstrip("\n") + "\n"
    conf.path.write_text(text, encoding="utf-8")


def list_running_bluestacks_processes() -> list[str]:
    process_names = [
        "HD-Player.exe",
        "HD-MultiInstanceManager.exe",
        "BlueStacksAppplayerWeb.exe",
    ]
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
            encoding="utf-8",
            errors="ignore",
        )
    except OSError:
        return []

    running: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        name = line.split('","', 1)[0].strip('"')
        if name in process_names:
            running.append(name)
    return sorted(set(running))


def stop_bluestacks_processes() -> list[str]:
    process_names = [
        "HD-Player.exe",
        "HD-MultiInstanceManager.exe",
        "BlueStacksAppplayerWeb.exe",
    ]
    stopped: list[str] = []
    for process_name in process_names:
        try:
            result = subprocess.run(
                ["taskkill", "/IM", process_name, "/F"],
                capture_output=True,
                text=True,
                check=False,
                encoding="utf-8",
                errors="ignore",
            )
        except OSError:
            continue
        if result.returncode == 0:
            stopped.append(process_name)
    return stopped
