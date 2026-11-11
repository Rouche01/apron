"""UIAutomator hierarchy dump → trimmed elements with refs."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

import adbutil

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def dump_hierarchy_xml(*, timeout: float = 45) -> str:
    """Capture current window hierarchy as XML text."""
    remote = "/sdcard/computer_window_dump.xml"
    adbutil.run_adb(
        ["shell", "uiautomator", "dump", remote],
        timeout=timeout,
    )
    r = adbutil.run_adb(["shell", "cat", remote], timeout=timeout)
    assert isinstance(r.stdout, str)
    xml = r.stdout.strip()
    if not xml.startswith("<?xml") and "<hierarchy" not in xml:
        raise adbutil.AdbError("uiautomator dump did not return XML")
    return xml


def _parse_bounds(raw: str | None) -> dict[str, int] | None:
    if not raw:
        return None
    m = _BOUNDS_RE.fullmatch(raw.strip())
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}


def _truthy(v: str | None) -> bool:
    return (v or "").lower() == "true"


def _keep_node(attrs: dict[str, str]) -> bool:
    if _truthy(attrs.get("clickable")) or _truthy(attrs.get("long-clickable")):
        return True
    if (attrs.get("text") or "").strip():
        return True
    if (attrs.get("content-desc") or "").strip():
        return True
    if (attrs.get("resource-id") or "").strip():
        return True
    if _truthy(attrs.get("checkable")) or _truthy(attrs.get("checked")):
        return True
    if _truthy(attrs.get("focusable")) and (attrs.get("class") or "").endswith("EditText"):
        return True
    return False


def _center(bounds: dict[str, int]) -> dict[str, int]:
    return {
        "x": (bounds["x1"] + bounds["x2"]) // 2,
        "y": (bounds["y1"] + bounds["y2"]) // 2,
    }


def trim_elements(
    xml_text: str,
    *,
    max_elements: int = 200,
) -> list[dict[str, Any]]:
    """Parse dump XML into a trimmed list with stable-enough refs for this frame."""
    # uiautomator sometimes prefixes a status line
    start = xml_text.find("<?xml")
    if start < 0:
        start = xml_text.find("<hierarchy")
    if start < 0:
        raise adbutil.AdbError("no hierarchy XML in dump")
    root = ET.fromstring(xml_text[start:])

    candidates: list[dict[str, str]] = []
    for node in root.iter("node"):
        attrs = dict(node.attrib)
        if not _keep_node(attrs):
            continue
        if _parse_bounds(attrs.get("bounds")) is None:
            continue
        candidates.append(attrs)

    used_ids: set[str] = set()
    elements: list[dict[str, Any]] = []
    for i, attrs in enumerate(candidates):
        if len(elements) >= max_elements:
            break
        bounds = _parse_bounds(attrs.get("bounds"))
        assert bounds is not None
        rid = (attrs.get("resource-id") or "").strip()
        if rid and rid not in used_ids:
            ref = rid
            used_ids.add(rid)
        else:
            ref = f"e{len(elements) + 1}"
            while ref in used_ids:
                ref = f"e{len(elements) + 1}_{i}"
            used_ids.add(ref)

        el: dict[str, Any] = {
            "ref": ref,
            "bounds": bounds,
            "center": _center(bounds),
            "clickable": _truthy(attrs.get("clickable")),
            "long_clickable": _truthy(attrs.get("long-clickable")),
            "class": attrs.get("class") or "",
        }
        text = (attrs.get("text") or "").strip()
        desc = (attrs.get("content-desc") or "").strip()
        if text:
            el["text"] = text
        if desc:
            el["content_desc"] = desc
        if rid:
            el["resource_id"] = rid
        if _truthy(attrs.get("checked")):
            el["checked"] = True
        if _truthy(attrs.get("selected")):
            el["selected"] = True
        if "enabled" in attrs and not _truthy(attrs.get("enabled")):
            el["enabled"] = False
        elements.append(el)
    return elements


def observe_elements(*, max_elements: int = 200) -> list[dict[str, Any]]:
    return trim_elements(dump_hierarchy_xml(), max_elements=max_elements)


def find_element_by_ref(ref: str, *, max_elements: int = 200) -> dict[str, Any] | None:
    for el in observe_elements(max_elements=max_elements):
        if el.get("ref") == ref:
            return el
    return None
