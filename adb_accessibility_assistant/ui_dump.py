from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .adb_client import ADBClient, AndroidDeviceError


@dataclass(slots=True)
class UINode:
    index: int
    class_name: str
    text: str
    content_desc: str
    clickable: bool
    enabled: bool
    bounds: str
    focusable: bool = False

    def summary(self) -> str:
        text_part = f"text={self.text!r}" if self.text else "text=''"
        desc_part = f"desc={self.content_desc!r}" if self.content_desc else "desc=''"
        return (
            f"{self.index}. class={self.class_name} clickable={self.clickable} "
            f"enabled={self.enabled} {text_part} {desc_part} bounds={self.bounds}"
        )

    @property
    def label(self) -> str:
        return self.content_desc or self.text

    def center(self) -> tuple[int, int]:
        left, top, right, bottom = parse_bounds(self.bounds)
        return ((left + right) // 2, (top + bottom) // 2)


def dump_ui_xml(adb: ADBClient) -> str:
    try:
        result = adb.run("exec-out", "uiautomator", "dump", "/dev/tty", timeout=30.0)
        output = result.stdout
        assert isinstance(output, str)
        xml_text = _extract_xml_text(output)
        if xml_text:
            return xml_text
    except Exception:
        pass

    adb.run("shell", "uiautomator", "dump", "/sdcard/window_dump.xml", timeout=30.0, check=False)
    file_result = adb.run("shell", "cat", "/sdcard/window_dump.xml", timeout=30.0, check=False)
    file_output = file_result.stdout
    assert isinstance(file_output, str)
    xml_text = _extract_xml_text(file_output)
    if not xml_text:
        raise AndroidDeviceError("uiautomator dump did not return XML")
    return xml_text


def _extract_xml_text(output: str) -> str:
    marker = "UI hierchary dumped to:"
    if marker in output:
        output = output.split(marker, 1)[0].strip()
    xml_start = output.find("<?xml")
    if xml_start >= 0:
        output = output[xml_start:].strip()
    return output if output.startswith("<?xml") else ""


def parse_ui_nodes(xml_text: str) -> list[UINode]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise AndroidDeviceError(f"Failed to parse uiautomator XML: {exc}") from exc

    nodes: list[UINode] = []
    for raw_index, node in enumerate(root.iter("node"), start=1):
        text = (node.attrib.get("text") or "").strip()
        content_desc = (node.attrib.get("content-desc") or "").strip()
        clickable = node.attrib.get("clickable") == "true"
        enabled = node.attrib.get("enabled") == "true"
        if not text and not content_desc and not clickable:
            continue
        nodes.append(
            UINode(
                index=len(nodes) + 1,
                class_name=node.attrib.get("class", ""),
                text=text,
                content_desc=content_desc,
                clickable=clickable,
                enabled=enabled,
                bounds=node.attrib.get("bounds", ""),
                focusable=node.attrib.get("focusable") == "true",
            )
        )
    return nodes


def dump_ui_nodes(adb: ADBClient) -> tuple[str, list[UINode]]:
    xml_text = dump_ui_xml(adb)
    return xml_text, parse_ui_nodes(xml_text)


def normalize_label(value: str) -> str:
    return " ".join(value.casefold().split())


def parse_bounds(bounds: str) -> tuple[int, int, int, int]:
    try:
        left_top, right_bottom = bounds.split("][")
        left, top = left_top.strip("[").split(",")
        right, bottom = right_bottom.strip("]").split(",")
        return int(left), int(top), int(right), int(bottom)
    except Exception as exc:
        raise AndroidDeviceError(f"Invalid bounds string: {bounds}") from exc


def node_matches(node: UINode, terms: list[str]) -> bool:
    haystacks = [normalize_label(node.text), normalize_label(node.content_desc)]
    for term in terms:
        normalized = normalize_label(term)
        if normalized and any(normalized in haystack for haystack in haystacks):
            return True
    return False


def find_first_node(
    nodes: list[UINode],
    *,
    terms: list[str] | None = None,
    class_names: list[str] | None = None,
    clickable: bool | None = None,
    enabled: bool | None = None,
    focusable: bool | None = None,
) -> UINode | None:
    for node in nodes:
        if terms and not node_matches(node, terms):
            continue
        if class_names and node.class_name not in class_names:
            continue
        if clickable is not None and node.clickable != clickable:
            continue
        if enabled is not None and node.enabled != enabled:
            continue
        if focusable is not None and node.focusable != focusable:
            continue
        return node
    return None


def tap_node(adb: ADBClient, node: UINode) -> tuple[int, int]:
    x, y = node.center()
    adb.tap(x, y)
    return x, y
