"""Zone classification helpers (no Home Assistant imports)."""
from __future__ import annotations

import re

_PLACEHOLDER = re.compile(r"^zone\s*\d+$", re.IGNORECASE)


def is_placeholder(name: str | None) -> bool:
    """Unnamed slot such as "zone N" (created but disabled by default)."""
    return bool(name) and bool(_PLACEHOLDER.match(name.strip()))


def device_class(name: str | None, ui_type: str | None = None) -> str:
    """Best-effort HA binary_sensor device class. BAT-Connect/DSC reports a generic
    type for every zone, so the zone name is the main hint."""
    ui = (ui_type or "").strip().lower()
    n = (name or "").lower()
    if ui:
        if "motion" in ui or "pir" in ui:
            return "motion"
        if "glass" in ui:
            return "window"
        if ui == "co" or "carbon" in ui:
            return "carbon_monoxide"
        if "smoke" in ui or "fire" in ui or "heat" in ui:
            return "smoke"
        if "flood" in ui or "water" in ui or "leak" in ui:
            return "moisture"
    if "motion" in n or "pir" in n:
        return "motion"
    if "roll up" in n or "rollup" in n or "overhead" in n:
        return "garage_door"
    if "glass" in n or "window" in n:
        return "window"
    if "smoke" in n or "fire" in n or "heat" in n:
        return "smoke"
    if n.endswith(" co") or "carbon" in n:
        return "carbon_monoxide"
    if "flood" in n or "water" in n or "leak" in n:
        return "moisture"
    if "holdup" in n or "hold up" in n or "panic" in n:
        return "safety"
    if "telco" in n or "tamper" in n:
        return "tamper"
    if "door" in n:
        return "door"
    if "garage" in n:
        return "garage_door"
    return "opening"


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return s or "zone"
