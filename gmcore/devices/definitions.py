"""Driver definitions: each instrument's properties, declared in TOML.

One file per driver in `devices/defs/`, named like the driver module
(`lakeshore_335.toml`). The definition says *what* the properties are (kind,
units, limits, who reads and writes them, what needs a confirmation); the
driver's Python says *how*: `get`, `set`, `attach` ... name its methods or
attributes. `describe()` is generated from the definition, and the GUI's
standard panel is built from it.

Modes: when one setting changes the units or limits of others, or which
property is set and which one reads it back (a Keithley SMU's source
function), that setting is a mode. The main file lists `modes` and
`mode_attr`, and each mode has its own file, `<driver>_<mode>.toml`, whose
`[properties.X]` tables add to the main file's or override them field by
field. A connected device's mode is fixed; `switch` names the driver method
that changes it (the GUI then reloads the definition).

Levels: every property has a `level` by how often it is used, 0 (essential,
always on the panel), 1 (expanded) or 2 (all); the panel folds 1 and 2 into
nested toggle lists.

Templates: `label`, `unit`, `info`, `tooltip`, `safe` and `confirm` texts may
use `{name}` and the driver's attributes, e.g. `"{name} level (channel
{channel})"` or `"Keithley {d.model}"`.
"""

from __future__ import annotations

import inspect
import re
import string
import tomllib
from functools import cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from .protocols import FieldInfo

DEFS = Path(__file__).with_name("defs")
Num = float | str                 # a number, or the name of a driver attribute holding one
_ID = re.compile(r"^[a-z][a-z0-9_]*$")
# Property `level`, by how often it is used. The standard panel always shows
# level 0 and folds 1 and 2 away (Expanded shows up to 1, All shows everything).
# Only what is shown changes: every live property is still read and recorded.
LEVELS = {0: "Essential", 1: "Expanded", 2: "All"}


class DefinitionError(ValueError):
    pass


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Ramp(_Model):
    max_rate: Num
    unit: str                     # explicit rate unit: "V/s", "K/s", "{units}/s"
    start: str | None = None      # start(target, rate) returns at once; without it set() ramps
    stop: str | None = None       # holds a running ramp


class Prop(_Model):
    """One property. Values come from read() (`poll = "live"`, the default
    without `get`) or from `get` on connect and after each write."""
    kind: Literal["float", "int", "bool", "enum", "str", "status", "action"] = "float"
    access: Literal["read", "write", "both"] = "read"
    level: Literal[0, 1, 2] = 0   # how often it is used: see LEVELS
    label: str = ""
    unit: str = ""
    group: str = ""
    tooltip: str = ""
    key: str | None = None        # read() key suffix: "" = the device name; default the id
    poll: Literal["live", "connect"] | None = None
    get: str | None = None        # attribute, method, or "method.key" of a returned dict
    set: str | None = None        # method taking the value (an action: no argument)
    choices: dict[str, Any] = {}  # enum: label -> value that set() takes and get returns
    choices_attr: str | None = None   # enum: driver attribute with a list of values instead
    allow_auto: bool = False      # None (shown as "auto") is a valid value
    limits: tuple[float, float] | str | None = None
    min: Num | None = None
    max: Num | None = None
    readback: str | None = None   # the property whose reading confirms this setting
    tolerance: Num | None = None  # |setting - readback| that counts as reached
    ramp: Ramp | None = None
    stoppable: bool = False       # a long set() that Stop can cancel
    confirm: str = ""             # ask this first
    confirm_when: list[Any] = []  # ... only for these values (enum labels, true/false)
    interlock: str | None = None  # bool property that must be true ("x") or false ("!x")
    when: str | None = None       # driver attribute: left out unless true ("x") or false ("!x")
    severity: Literal["amber", "red"] = "amber"   # status: colour when true
    expose: bool = True           # live: in describe() (recorded, offered to views)

    @property
    def source(self) -> str | None:
        """'live' (from read()), 'connect' (from get) or None (an action)."""
        if self.kind == "action":
            return None
        return self.poll or ("connect" if self.get else "live")

    @property
    def writable(self) -> bool:
        return self.kind != "action" and self.access in ("write", "both")


class Definition(_Model):
    drivers: list[str]
    label: str = ""
    info: str = ""                # one line under the panel title
    mode_attr: str | None = None  # driver attribute and station key holding the mode
    modes: dict[str, str] = {}    # mode -> label
    mode: str | None = None       # set by a mode file
    attach: str | None = None     # queries run on connect, before the properties are read
    check_mode: str | None = None  # returns the instrument's present mode (queries only)
    switch: str | None = None     # switch(mode, **settings): changes the mode on the instrument
    switch_confirm: str = ""
    switch_settings: list[str] = []   # station keys the new mode needs ([devices.X.modes.<mode>])
    switch_defaults: dict[str, Any] = {}   # mode file: those settings' defaults for this mode
    switch_interlock: str | None = None    # property that must be true ("x") / false ("!x")
                                           # before Switch mode… (e.g. "!level": level at 0)
    read_if: str | None = None    # bool property: poll read() only while it is true
    safe: str = ""                # what safe_state() does, in words
    groups: list[str] = []        # display order of property groups
    properties: dict[str, Prop] = {}

    @model_validator(mode="after")
    def _check(self) -> Definition:
        props = self.properties
        for pid, p in props.items():
            where = f"property {pid!r}"
            if not _ID.match(pid):
                raise ValueError(f"{where}: ids are snake_case")
            if p.kind == "action" and not p.set:
                raise ValueError(f"{where}: an action needs `set`")
            if p.writable and not (p.set or (p.ramp and p.ramp.start)):
                raise ValueError(f"{where}: writable but no `set`")
            if p.kind == "enum" and not (p.choices or p.choices_attr):
                raise ValueError(f"{where}: an enum needs `choices` or `choices_attr`")
            if p.kind == "status" and p.access != "read":
                raise ValueError(f"{where}: a status is read only")
            if p.poll == "connect" and not p.get:
                raise ValueError(f"{where}: poll = 'connect' needs `get`")
            for ref, name in ((p.readback, "readback"), (p.interlock, "interlock")):
                if ref and ref.lstrip("!") not in props:
                    raise ValueError(f"{where}: {name} {ref!r} is not a property")
            if p.interlock and props[p.interlock.lstrip("!")].level > p.level:
                raise ValueError(f"{where}: its interlock {p.interlock!r} would be folded "
                                 "away while it is shown (give it a level <= this one)")
            labels = {*p.choices, True, False}
            if bad := [v for v in p.confirm_when if v not in labels]:
                raise ValueError(f"{where}: confirm_when {bad} not among its values")
        keys = [p.key if p.key is not None else pid for pid, p in props.items()
                if p.source == "live"]
        if len(keys) != len(set(keys)):
            raise ValueError(f"duplicate read() keys {keys}")
        if self.modes and not self.mode_attr:
            raise ValueError("`modes` needs `mode_attr`")
        if self.read_if and self.read_if not in props:
            raise ValueError(f"read_if {self.read_if!r} is not a property")
        if self.read_if and props[self.read_if].level != 0:
            raise ValueError(f"read_if {self.read_if!r} needs level 0: it says why "
                             "readings stop")
        if self.switch_interlock and self.switch_interlock.lstrip("!") not in props:
            raise ValueError(f"switch_interlock {self.switch_interlock!r} is not a property")
        if bad := set(self.switch_defaults) - set(self.switch_settings):
            raise ValueError(f"switch_defaults {sorted(bad)} not in switch_settings")
        return self


# -- loading -----------------------------------------------------------------------
def _read(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        raise DefinitionError(f"no definition {path.name} in {path.parent}") from None
    except tomllib.TOMLDecodeError as e:
        raise DefinitionError(f"{path.name}: {e}") from None


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = {**base, **{k: v for k, v in over.items() if k != "properties"}}
    props = {k: dict(v) for k, v in base.get("properties", {}).items()}
    for k, v in over.get("properties", {}).items():
        props[k] = {**props.get(k, {}), **v}           # field by field; new ids go last
    out["properties"] = props
    return out


@cache
def load(name: str, mode: str | None = None) -> Definition:
    """`defs/<name>.toml`, merged with `defs/<name>_<mode>.toml` for a modal driver."""
    raw = _read(DEFS / f"{name}.toml")
    file = f"{name}.toml"
    if raw.get("mode") is not None:
        raise DefinitionError(f"{file}: `mode` belongs in the mode files")
    modes = raw.get("modes", {})
    if modes:
        if mode not in modes:
            raise DefinitionError(f"{name}: mode {mode!r} is not one of {sorted(modes)}")
        file = f"{name}_{mode}.toml"
        over = _read(DEFS / file)
        if over.get("mode") != mode:
            raise DefinitionError(f"{file}: needs mode = {mode!r}")
        raw = _merge(raw, over)
    elif mode is not None:
        raise DefinitionError(f"{name} has no modes (asked for {mode!r})")
    try:
        return Definition.model_validate(raw)
    except ValidationError as e:
        raise DefinitionError(f"{file}: {e}") from None


def definition_of(dev: Any) -> Definition:
    """The definition for a device as it is now (its class's DEFINITION, its mode)."""
    name = type(dev).DEFINITION
    mode_attr = _mode_attr(name)
    return load(name, getattr(dev, mode_attr) if mode_attr else None)


@cache
def _mode_attr(name: str) -> str | None:
    raw = _read(DEFS / f"{name}.toml")
    return raw.get("mode_attr") if raw.get("modes") else None


# -- using a definition ------------------------------------------------------------
class _Lookup(dict):
    def __init__(self, dev: Any, extra: dict[str, Any]):
        super().__init__(extra)
        self.dev = dev

    def __missing__(self, key: str) -> Any:
        try:
            return getattr(self.dev, key)
        except AttributeError:
            raise DefinitionError(f"template field {{{key}}}: {type(self.dev).__name__} "
                                  "has no such attribute") from None


def text(template: str, dev: Any, **extra: Any) -> str:
    """Fill a template from {name}, `extra` and the driver's attributes."""
    if "{" not in template:
        return template
    return string.Formatter().vformat(template, (), _Lookup(dev, {"name": dev.name, **extra}))


def active(p: Prop, dev: Any) -> bool:
    if not p.when:
        return True
    return bool(getattr(dev, p.when.lstrip("!"))) != p.when.startswith("!")


def props(defn: Definition, dev: Any) -> dict[str, Prop]:
    """The properties that apply to this device (its `when` conditions hold)."""
    return {pid: p for pid, p in defn.properties.items() if active(p, dev)}


def field_key(name: str, pid: str, p: Prop) -> str:
    k = pid if p.key is None else p.key
    return name if k == "" else f"{name}.{k}"


def describe(dev: Any) -> dict[str, FieldInfo]:
    """describe() for a driver: one field per exposed live property, in definition
    order. These are what scans record and monitor/chart windows offer."""
    return {field_key(dev.name, pid, p): FieldInfo(text(p.unit, dev), text(p.label, dev))
            for pid, p in props(definition_of(dev), dev).items()
            if p.source == "live" and p.expose}


def number(v: Num | None, dev: Any) -> float | None:
    if v is None:
        return None
    return float(getattr(dev, v) if isinstance(v, str) else v)


def bounds(p: Prop, dev: Any) -> tuple[float | None, float | None]:
    """(low, high) from `limits`, narrowed by `min` / `max`; None = unbounded."""
    lo = hi = None
    if p.limits is not None:
        lo, hi = (getattr(dev, p.limits) if isinstance(p.limits, str) else p.limits)
    if (m := number(p.min, dev)) is not None:
        lo = m if lo is None else max(lo, m)
    if (m := number(p.max, dev)) is not None:
        hi = m if hi is None else min(hi, m)
    return lo, hi


def prop_bounds(dev: Any, pid: str) -> tuple[float | None, float | None]:
    """A property's (low, high) from the device's own definition, so a driver can
    check a value against the min / max its .toml declares (one place for them)."""
    return bounds(definition_of(dev).properties[pid], dev)


def choices(p: Prop, dev: Any) -> list[tuple[str | None, Any]]:
    """[(label, value)]; label None for `choices_attr` values (the GUI formats them).
    With `allow_auto`, ("Auto", None) comes first."""
    auto: list[tuple[str | None, Any]] = [("Auto", None)] if p.allow_auto else []
    if p.choices:
        return auto + list(p.choices.items())
    return auto + [(None, v) for v in getattr(dev, p.choices_attr or "")]


def label_of(p: Prop, value: Any) -> Any:
    """The choice label for a value (the value itself when there is none)."""
    for label, v in p.choices.items():
        if v == value:
            return label
    return value


async def _call(attr: Any, *args: Any) -> Any:
    if callable(attr):
        r = attr(*args)
        return await r if inspect.isawaitable(r) else r
    if args:
        raise DefinitionError(f"{attr!r} is not callable")
    return attr


async def run(dev: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    """Call a driver method named in a definition (awaiting it if async)."""
    r = getattr(dev, method)(*args, **kwargs)
    return await r if inspect.isawaitable(r) else r


async def get_value(dev: Any, p: Prop, memo: dict[str, Any] | None = None) -> Any:
    """Evaluate `get`. "snapshot.pid" calls snapshot() once per `memo`."""
    target, _, item = (p.get or "").partition(".")
    memo = {} if memo is None else memo
    if not item:
        return await _call(getattr(dev, target))
    if target not in memo:
        memo[target] = await _call(getattr(dev, target))
    return memo[target][item]


async def read_connect(defn: Definition, dev: Any) -> dict[str, Any]:
    """Every `get` property, by id (queries only)."""
    memo: dict[str, Any] = {}
    return {pid: await get_value(dev, p, memo) for pid, p in props(defn, dev).items()
            if p.source == "connect"}


async def write(defn: Definition, dev: Any, pid: str, value: Any = None,
                rate: float | None = None) -> Any:
    """Set a property (or run an action) through the driver; returns what set() returns."""
    p = defn.properties[pid]
    if p.kind == "action":
        return await _call(getattr(dev, p.set or ""))
    if p.ramp is not None and p.ramp.start and (rate is not None or not p.set):
        return await _call(getattr(dev, p.ramp.start), value,
                           rate if rate is not None else number(p.ramp.max_rate, dev))
    return await _call(getattr(dev, p.set or ""), value)
