"""Read and patch WG4 weekly schedules while preserving native JSON exactly.

This model consumes the ``Schedules`` member of a WG4 thermostat response.
It does not fetch data, send a write request, change a thermostat mode, or
interpret recurring wall-clock times as UTC dates. The complete wire shape
is seven distinct weekday records, each containing six distinct event slots.
Disabled slots and unknown JSON fields are retained verbatim.

The weekday and slot identifiers, rather than response array positions, are
authoritative. Read parsing does not impose command constraints on accepted
vendor data. Explicit edits use the verified WG4 editor's quarter-hour input,
15-minute active-event spacing, first-slot activity, per-device slot bounds,
and temperature limits, with deterministic integer handling of overnight times.
"""

# Native JSON integers must exclude bool, and flags must exclude integer 0/1.
# pylint: disable=unidiomatic-typecheck

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from math import isfinite
from typing import Any, cast

type JSONValue = (
    bool | int | float | str | list[JSONValue] | dict[str, JSONValue] | None
)

WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
WEEKDAY_IDS = frozenset(range(1, 8))
EVENT_IDS = frozenset(range(6))
_CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]")
_EDIT_CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
_DAY_SECONDS = 86400
_MIN_GAP = 900


class WG4ScheduleError(ValueError):
    """The thermostat response does not contain a complete valid WG4 schedule."""


@dataclass(frozen=True, slots=True)
class WG4ScheduleEvent:
    """One identified slot, with its temperature in native Celsius hundredths."""

    schedule_type: int
    clock: str
    temperature_centi_celsius: int
    active: bool


@dataclass(frozen=True, slots=True)
class WG4ScheduleDay:
    """One identified weekday; events are ordered by their slot identifiers."""

    weekday_id: int
    events: tuple[WG4ScheduleEvent, ...]

    @property
    def weekday(self) -> str:
        """Return the normalized weekday name (1 is Monday, 7 is Sunday)."""
        return WEEKDAYS[self.weekday_id - 1]


class WG4Schedule:
    """Validated schedule with independent native JSON and immutable views."""

    __slots__ = ("_days", "_native_payload")

    def __init__(self, payload: object) -> None:
        """Validate and copy a complete native ``Schedules`` array.

        Validation errors identify field paths without including values from
        the thermostat response. Mutating the input or an exported copy cannot
        modify this snapshot.
        """
        _validate_json(payload, "Schedules")
        if not isinstance(payload, list) or len(payload) != len(WEEKDAY_IDS):
            msg = "Schedules must contain exactly seven weekday records."
            raise WG4ScheduleError(msg)

        days = tuple(
            _parse_day(day, f"Schedules[{index}]") for index, day in enumerate(payload)
        )
        if frozenset(day.weekday_id for day in days) != WEEKDAY_IDS:
            msg = "Schedules must contain weekday identifiers 1 through 7 once each."
            raise WG4ScheduleError(msg)

        self._days = tuple(sorted(days, key=lambda day: day.weekday_id))
        self._native_payload = copy.deepcopy(
            cast("list[dict[str, JSONValue]]", payload)
        )

    @property
    def days(self) -> tuple[WG4ScheduleDay, ...]:
        """Return Monday-first immutable views, independent of wire ordering."""
        return self._days

    def day(self, weekday_id: int) -> WG4ScheduleDay:
        """Return a day by native identity, never by response array position."""
        if type(weekday_id) is not int or weekday_id not in WEEKDAY_IDS:
            msg = "A weekday identifier must be an integer from 1 through 7."
            raise WG4ScheduleError(msg)
        return self._days[weekday_id - 1]

    def to_payload(self) -> list[dict[str, JSONValue]]:
        """Return an independent exact native copy, including disabled slots."""
        return copy.deepcopy(self._native_payload)

    def attributes(self) -> dict[str, list[dict[str, Any]]]:
        """Return all six identified events per day for sensors and previews.

        Temperatures are explicitly Celsius; HA does not automatically convert
        nested temperature attributes. The original native integers remain
        authoritative when applying a time/activity-only patch.
        """
        result = {}
        for day in self.days:
            events = []
            previous = None
            for event in day.events:
                item: dict[str, Any] = {
                    "slot": event.schedule_type,
                    "time": event.clock[:5],
                    "temperature": event.temperature_centi_celsius / 100,
                    "active": event.active,
                }
                if event.active:
                    seconds = _clock_seconds(event.clock)
                    if previous is not None and seconds < previous:
                        seconds += _DAY_SECONDS
                    if seconds >= _DAY_SECONDS:
                        item["next_day"] = True
                    previous = seconds
                events.append(item)
            result[day.weekday] = events
        return result

    def with_changes(  # pylint: disable=too-many-arguments,too-many-locals
        self,
        changes: list[dict[str, Any]],
        unit: str,
        min_temp: int,
        max_temp: int,
        limits: dict[str, Any] | None = None,
    ) -> WG4Schedule:
        """Apply explicit slot patches to a copy, preserving all other fields.

        Limits contain the target device's native MinTimeLimits/MaxTimeLimits
        arrays. They can be absent for a cached preview; the caller must require
        verified device limits before issuing any write. Only edited days and
        their adjacent active transitions are checked. Untouched dormant data
        is not normalized or rejected against newly inferred command rules.
        """
        if not isinstance(unit, str) or unit not in {"C", "F"}:
            msg = "Temperature unit must be C or F."
            raise WG4ScheduleError(msg)
        if (
            type(min_temp) is not int
            or type(max_temp) is not int
            or min_temp > max_temp
        ):
            msg = "Thermostat temperature limits must be ordered native integers."
            raise WG4ScheduleError(msg)
        if not isinstance(changes, list) or len(changes) > 42:
            msg = "Changes must be a list with at most 42 distinct slot patches."
            raise WG4ScheduleError(msg)

        native = self.to_payload()
        day_map = {cast("int", day["WeekDayGrpNo"]): day for day in native}
        edited_days: set[int] = set()
        seen: set[tuple[int, int]] = set()
        for change in changes:
            if not isinstance(change, dict) or not set(change) <= {
                "day",
                "slot",
                "time",
                "temperature",
                "active",
            }:
                msg = "Each change must contain only supported slot patch fields."
                raise WG4ScheduleError(msg)
            day_name = change.get("day")
            slot = change.get("slot")
            if not isinstance(day_name, str) or day_name not in WEEKDAYS:
                msg = "Each change requires a weekday name from monday through sunday."
                raise WG4ScheduleError(msg)
            if type(slot) is not int or slot not in EVENT_IDS:
                msg = (
                    "Each change requires an integer slot identifier from 0 through 5."
                )
                raise WG4ScheduleError(msg)
            if not set(change) & {"time", "temperature", "active"}:
                msg = "Each slot patch must specify time, temperature, or active."
                raise WG4ScheduleError(msg)
            day_id = WEEKDAYS.index(day_name) + 1
            identity = (day_id, slot)
            if identity in seen:
                msg = "The same weekday/slot may only be patched once."
                raise WG4ScheduleError(msg)
            seen.add(identity)
            edited_days.add(day_id)
            events = cast("list[dict[str, JSONValue]]", day_map[day_id]["Events"])
            event = next(item for item in events if item["ScheduleType"] == slot)
            _apply_patch(event, change, unit, min_temp, max_temp)

        result = WG4Schedule(native)
        bounds = None if limits is None else _parse_limits(limits)
        # Validation belongs to the newly created snapshot of this same class.
        result._validate_edited_days(edited_days, bounds)  # pylint: disable=protected-access
        return result

    def _validate_edited_days(  # pylint: disable=too-many-locals
        self, edited_days: set[int], bounds: dict[int, tuple[int, int]] | None
    ) -> None:
        """Use an integer timeline, retaining native slot identities and wall time."""
        timelines: dict[int, list[int]] = {}
        relevant = edited_days | {day % 7 + 1 for day in edited_days}
        relevant |= {(day - 2) % 7 + 1 for day in edited_days}
        for day_id in relevant:
            day = self.day(day_id)
            previous = None
            timeline = []
            if day_id in edited_days and not day.events[0].active:
                msg = "The first event of an edited day must remain active."
                raise WG4ScheduleError(msg)
            for event in day.events:
                if not event.active:
                    continue
                seconds = _clock_seconds(event.clock)
                if previous is not None and seconds < previous:
                    seconds += _DAY_SECONDS
                if day_id in edited_days:
                    if previous is not None and seconds < previous + _MIN_GAP:
                        msg = "Active events must be at least 15 minutes apart."
                        raise WG4ScheduleError(msg)
                    if bounds is not None:
                        minimum, maximum = bounds[event.schedule_type]
                        if not minimum <= seconds <= maximum:
                            msg = "An active event is outside its device time limits."
                            raise WG4ScheduleError(msg)
                timeline.append(seconds)
                previous = seconds
            timelines[day_id] = timeline

        for day_id in edited_days | {(day - 2) % 7 + 1 for day in edited_days}:
            current = timelines[day_id]
            following = timelines[day_id % 7 + 1]
            if (
                current
                and following
                and current[-1] + _MIN_GAP > following[0] + _DAY_SECONDS
            ):
                msg = "Overnight events need a 15-minute gap before the next day."
                raise WG4ScheduleError(msg)

    def fingerprint(self) -> str:
        """Hash complete native content, ignoring only weekday/slot array order.

        Unknown fields and inactive slot settings participate in the hash.
        This is suitable as a draft's optimistic concurrency token; it is not
        a server-side revision or a guarantee against an external edit between
        a final read and a later write.
        """
        normalized = self.to_payload()
        normalized.sort(key=lambda day: cast("int", day["WeekDayGrpNo"]))
        for day in normalized:
            events = cast("list[dict[str, JSONValue]]", day["Events"])
            events.sort(key=lambda event: cast("int", event["ScheduleType"]))
        encoded = json.dumps(
            normalized, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()


def _validate_json(value: object, path: str) -> None:
    """Reject non-JSON or non-finite values while retaining unknown JSON fields."""
    if value is None or type(value) in {bool, int, str}:
        return
    if type(value) is float and isfinite(value):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json(item, f"{path}[{index}]")
        return
    if isinstance(value, dict) and all(type(key) is str for key in value):
        for key, item in value.items():
            _validate_json(item, f"{path}.{key}")
        return
    msg = f"{path} must contain finite JSON values."
    raise WG4ScheduleError(msg)


def _integer(record: dict[str, JSONValue], key: str, path: str) -> int:
    """Require native integers without accepting booleans or coercing strings."""
    value = record.get(key)
    if type(value) is not int:
        msg = f"{path}.{key} must be an integer."
        raise WG4ScheduleError(msg)
    return value


def _parse_day(value: JSONValue, path: str) -> WG4ScheduleDay:
    """Validate one day and retain event identities in its immutable view."""
    if not isinstance(value, dict):
        msg = f"{path} must be an object."
        raise WG4ScheduleError(msg)
    weekday_id = _integer(value, "WeekDayGrpNo", path)
    if weekday_id not in WEEKDAY_IDS:
        msg = f"{path}.WeekDayGrpNo must be between 1 and 7."
        raise WG4ScheduleError(msg)
    events = value.get("Events")
    if not isinstance(events, list) or len(events) != len(EVENT_IDS):
        msg = f"{path}.Events must contain exactly six event records."
        raise WG4ScheduleError(msg)
    parsed = tuple(
        _parse_event(event, f"{path}.Events[{index}]")
        for index, event in enumerate(events)
    )
    if frozenset(event.schedule_type for event in parsed) != EVENT_IDS:
        msg = f"{path}.Events must contain slot identifiers 0 through 5 once each."
        raise WG4ScheduleError(msg)
    return WG4ScheduleDay(
        weekday_id=weekday_id,
        events=tuple(sorted(parsed, key=lambda event: event.schedule_type)),
    )


def _parse_event(value: JSONValue, path: str) -> WG4ScheduleEvent:
    """Validate supported fields without assuming a device's timing rules."""
    if not isinstance(value, dict):
        msg = f"{path} must be an object."
        raise WG4ScheduleError(msg)
    schedule_type = _integer(value, "ScheduleType", path)
    if schedule_type not in EVENT_IDS:
        msg = f"{path}.ScheduleType must be between 0 and 5."
        raise WG4ScheduleError(msg)
    clock = value.get("Clock")
    if not isinstance(clock, str) or _CLOCK.fullmatch(clock) is None:
        msg = f"{path}.Clock must be a wall-clock time in HH:MM:SS format."
        raise WG4ScheduleError(msg)
    temperature = _integer(value, "TempFloor", path)
    active = value.get("Active")
    if type(active) is not bool:
        msg = f"{path}.Active must be a boolean."
        raise WG4ScheduleError(msg)
    return WG4ScheduleEvent(schedule_type, clock, temperature, active)


def _clock_seconds(clock: str) -> int:
    """Interpret an already validated recurring wall-clock time as integer seconds."""
    hour, minute, second = (int(part) for part in clock.split(":"))
    return hour * 3600 + minute * 60 + second


def _apply_patch(
    event: dict[str, JSONValue],
    change: dict[str, Any],
    unit: str,
    min_temp: int,
    max_temp: int,
) -> None:
    """Modify only explicitly requested fields and validate user input strictly."""
    if "time" in change:
        clock = change["time"]
        if (
            not isinstance(clock, str)
            or _EDIT_CLOCK.fullmatch(clock) is None
            or int(clock[3:5]) % 15
        ):
            msg = "Edited times must be HH:MM on a quarter of an hour."
            raise WG4ScheduleError(msg)
        event["Clock"] = clock + ":00"
    if "active" in change:
        if type(change["active"]) is not bool:
            msg = "An edited active flag must be a boolean."
            raise WG4ScheduleError(msg)
        event["Active"] = change["active"]
    if "temperature" in change:
        value = change["temperature"]
        if type(value) not in {int, float} or (
            type(value) is float and not isfinite(value)
        ):
            msg = "An edited temperature must be a finite number."
            raise WG4ScheduleError(msg)
        try:
            celsius = value if unit == "C" else (value - 32) * (5 / 9)
        except OverflowError as error:
            msg = "An edited temperature is outside the thermostat limits."
            raise WG4ScheduleError(msg) from error
        if not min_temp / 100 <= celsius <= max_temp / 100:
            msg = "An edited temperature is outside the thermostat limits."
            raise WG4ScheduleError(msg)
        # The vendor serializer uses parseInt(celsius * 100), truncating toward
        # zero rather than rounding (78 Fahrenheit produces native 2555).
        event["TempFloor"] = int(celsius * 100)
    if change.get("active") is True and not (
        min_temp <= cast("int", event["TempFloor"]) <= max_temp
    ):
        msg = "An activated event's stored temperature is outside thermostat limits."
        raise WG4ScheduleError(msg)


def _parse_limits(limits: dict[str, Any]) -> dict[int, tuple[int, int]]:
    """Validate native device bounds by slot identity, independent of array order."""
    if not isinstance(limits, dict):
        msg = "Device time limits must be an object."
        raise WG4ScheduleError(msg)
    parsed: dict[str, dict[int, int]] = {}
    for key in ("MinTimeLimits", "MaxTimeLimits"):
        entries = limits.get(key)
        if not isinstance(entries, list) or len(entries) != len(EVENT_IDS):
            msg = "Device time limits must include six minimum and maximum records."
            raise WG4ScheduleError(msg)
        by_slot = {}
        for item in entries:
            if not isinstance(item, dict):
                msg = "Device time limit records must be objects."
                raise WG4ScheduleError(msg)
            slot = item.get("ScheduleType")
            seconds = item.get("Clock")
            if (
                type(slot) is not int
                or slot not in EVENT_IDS
                or slot in by_slot
                or type(seconds) is not int
                or not 0 <= seconds < 2 * _DAY_SECONDS
            ):
                msg = "Device time limits require distinct slots and integer seconds."
                raise WG4ScheduleError(msg)
            by_slot[slot] = seconds
        parsed[key] = by_slot
    result = {
        slot: (parsed["MinTimeLimits"][slot], parsed["MaxTimeLimits"][slot])
        for slot in EVENT_IDS
    }
    if any(minimum > maximum for minimum, maximum in result.values()):
        msg = "Device minimum time limits must not exceed maximum time limits."
        raise WG4ScheduleError(msg)
    return result
