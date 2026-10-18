"""Engineering number format and SI-prefix parsing (no Qt: used by drivers' definitions too)."""

from __future__ import annotations

import math
import re
from typing import Any

SI_UNITS = {"V", "A", "Ohm", "Hz", "T", "W"}
_PREFIXES = [(1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"), (1e-6, "µ"),
             (1e-9, "n"), (1e-12, "p")]
_PARSE = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "": 1.0, "k": 1e3,
          "M": 1e6, "G": 1e9}
_NUM = re.compile(r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([pnuµmkMG]?)")


def fmt(value: Any, unit: str = "") -> str:
    """Engineering format: 1.05e-06 A -> '1.05 µA'; overrange and NaN spelled out."""
    if value is None:
        return "—"
    v = float(value)
    if math.isnan(v):
        return "NaN"
    if abs(v) >= 9e37:
        return "overrange"
    if unit in SI_UNITS and v != 0:
        for scale, p in _PREFIXES:
            if abs(v) >= scale * 0.9995 or scale == 1e-12:
                return f"{v / scale:.5g} {p}{unit}"
    return f"{v:.6g} {unit}".strip()


def parse_si(text: str) -> float:
    """'100n', '1e-7', '7.054 G', '-2.5' -> float. Raises ValueError."""
    m = _NUM.match(text)
    if not m:
        raise ValueError(f"not a number: {text!r}")
    return float(m.group(1)) * _PARSE[m.group(2)]
