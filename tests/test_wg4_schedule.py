"""WG4 schedule preservation and malformed-response tests, without HA or I/O."""

from __future__ import annotations

import copy
import importlib.util
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "warmtiles_wg4_schedule",
    Path(__file__).parents[1]
    / "custom_components/ojmicroline_thermostat/wg4_schedule.py",
)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
WG4Schedule = MODULE.WG4Schedule
WG4ScheduleError = MODULE.WG4ScheduleError


@pytest.fixture
def native_schedule():
    """Use distinctive values per weekday and native slot, including inactive slots."""
    return [
        {
            "WeekDayGrpNo": weekday,
            "VendorDayField": {"description": f"Day {weekday}", "values": [True, None]},
            "Events": [
                {
                    "ScheduleType": slot,
                    "Clock": f"{slot * 3:02}:00:00",
                    "TempFloor": 2333 + weekday * 100 + slot,
                    "Active": slot in {0, 5},
                    "VendorEventField": {"label": "Unchanged Δ", "version": 1.5},
                }
                for slot in range(6)
            ],
        }
        for weekday in range(1, 8)
    ]


def test_round_trip_preserves_every_native_field(native_schedule):
    native_schedule[0]["Events"][1]["TempFloor"] = 3222
    native_schedule.reverse()
    native_schedule[3]["Events"].reverse()
    model = WG4Schedule(native_schedule)

    assert model.to_payload() == native_schedule
    assert model.day(1).events[1].temperature_centi_celsius == 3222
    assert model.day(1).events[1].active is False
    assert model.day(1).weekday == "monday"
    assert model.day(7).weekday == "sunday"


def test_identities_are_independent_of_wire_positions(native_schedule):
    native_schedule[:] = native_schedule[3:] + native_schedule[:3]
    for day in native_schedule:
        day["Events"][:] = day["Events"][2:] + day["Events"][:2]
    model = WG4Schedule(native_schedule)

    assert [day.weekday_id for day in model.days] == list(range(1, 8))
    for weekday in range(1, 8):
        day = model.day(weekday)
        assert [event.schedule_type for event in day.events] == list(range(6))
        assert day.events[4].temperature_centi_celsius == 2333 + weekday * 100 + 4


def test_input_and_exported_payload_are_independent(native_schedule):
    expected = copy.deepcopy(native_schedule)
    model = WG4Schedule(native_schedule)
    fingerprint = model.fingerprint()
    native_schedule[0]["Events"][0]["TempFloor"] = 500
    native_schedule[0]["VendorDayField"]["values"].append("outside mutation")
    exported = model.to_payload()
    exported[1]["Events"].clear()
    exported[2]["Events"][0]["VendorEventField"]["label"] = "changed"

    assert model.to_payload() == expected
    assert model.fingerprint() == fingerprint


def test_views_are_immutable(native_schedule):
    model = WG4Schedule(native_schedule)
    with pytest.raises(FrozenInstanceError):
        model.day(1).events[0].active = False
    with pytest.raises(FrozenInstanceError):
        model.day(1).weekday_id = 7


def test_semantic_hash_ignores_only_known_identity_order(native_schedule):
    original = WG4Schedule(native_schedule)
    shuffled = copy.deepcopy(native_schedule)
    shuffled.reverse()
    for day in shuffled:
        day["Events"].reverse()

    assert original.fingerprint() == WG4Schedule(shuffled).fingerprint()
    assert WG4Schedule(shuffled).to_payload() == shuffled


@pytest.mark.parametrize("change", ["clock", "temperature", "active", "unknown"])
def test_hash_detects_edits_to_inactive_and_unknown_fields(native_schedule, change):
    original_hash = WG4Schedule(native_schedule).fingerprint()
    event = native_schedule[0]["Events"][1]
    if change == "clock":
        event["Clock"] = "06:30:00"
    elif change == "temperature":
        event["TempFloor"] += 1
    elif change == "active":
        event["Active"] = True
    else:
        event["VendorEventField"]["version"] = 2

    assert WG4Schedule(native_schedule).fingerprint() != original_hash


def test_unknown_list_order_remains_significant(native_schedule):
    original_hash = WG4Schedule(native_schedule).fingerprint()
    native_schedule[0]["VendorDayField"]["values"].reverse()

    assert WG4Schedule(native_schedule).fingerprint() != original_hash


def test_parser_does_not_apply_speculative_command_constraints(native_schedule):
    # Read-only transport validation does not invent slot windows, gaps,
    # temperature limits, active-slot ordering, or first-slot activity rules.
    for event in native_schedule[0]["Events"]:
        event.update(Clock="23:59:59", TempFloor=-100, Active=False)
    model = WG4Schedule(native_schedule)

    assert model.to_payload() == native_schedule
    assert model.day(1).events[0].clock == "23:59:59"


@pytest.mark.parametrize("payload", [None, {}, "schedule", (), [], [None] * 7])
def test_rejects_invalid_outer_shape(payload):
    with pytest.raises(WG4ScheduleError):
        WG4Schedule(payload)


@pytest.mark.parametrize("change", ["missing_day", "extra_day", "duplicate_day"])
def test_requires_complete_distinct_weekday_identities(native_schedule, change):
    if change == "missing_day":
        native_schedule.pop()
    elif change == "extra_day":
        native_schedule.append(copy.deepcopy(native_schedule[0]))
    else:
        native_schedule[-1]["WeekDayGrpNo"] = 1
    with pytest.raises(WG4ScheduleError):
        WG4Schedule(native_schedule)


@pytest.mark.parametrize("value", [None, True, False, 1.0, "1", 0, 8])
def test_rejects_invalid_weekday_identity(native_schedule, value):
    native_schedule[0]["WeekDayGrpNo"] = value
    with pytest.raises(WG4ScheduleError, match="WeekDayGrpNo"):
        WG4Schedule(native_schedule)


@pytest.mark.parametrize(
    "change", ["missing", "extra", "duplicate", "not_list", "not_object"]
)
def test_requires_complete_distinct_event_slots(native_schedule, change):
    events = native_schedule[0]["Events"]
    if change == "missing":
        events.pop()
    elif change == "extra":
        events.append(copy.deepcopy(events[0]))
    elif change == "duplicate":
        events[-1]["ScheduleType"] = 0
    elif change == "not_list":
        native_schedule[0]["Events"] = {}
    else:
        events[0] = None
    with pytest.raises(WG4ScheduleError, match="Events"):
        WG4Schedule(native_schedule)


@pytest.mark.parametrize("value", [None, True, 0.0, "0", -1, 6])
def test_rejects_invalid_event_identity(native_schedule, value):
    native_schedule[0]["Events"][0]["ScheduleType"] = value
    with pytest.raises(WG4ScheduleError, match="ScheduleType"):
        WG4Schedule(native_schedule)


@pytest.mark.parametrize(
    "value", [None, True, "3222", 3222.0, float("nan"), float("inf")]
)
def test_rejects_non_native_temperature_values(native_schedule, value):
    native_schedule[0]["Events"][0]["TempFloor"] = value
    with pytest.raises(WG4ScheduleError, match="TempFloor"):
        WG4Schedule(native_schedule)


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false", [], {}])
def test_rejects_non_boolean_active_flags(native_schedule, value):
    native_schedule[0]["Events"][0]["Active"] = value
    with pytest.raises(WG4ScheduleError, match="Active"):
        WG4Schedule(native_schedule)


@pytest.mark.parametrize(
    "value",
    [
        None,
        600,
        "6:00:00",
        "06:00",
        "24:00:00",
        "00:60:00",
        "00:00:60",
        "06:00:00Z",
        "06:00:00\n",
        "٠٦:00:00",
    ],
)
def test_rejects_malformed_wall_clock_values(native_schedule, value):
    native_schedule[0]["Events"][0]["Clock"] = value
    with pytest.raises(WG4ScheduleError, match="Clock"):
        WG4Schedule(native_schedule)


@pytest.mark.parametrize("field", ["ScheduleType", "Clock", "TempFloor", "Active"])
def test_rejects_missing_supported_event_fields(native_schedule, field):
    del native_schedule[0]["Events"][0][field]
    with pytest.raises(WG4ScheduleError, match=field):
        WG4Schedule(native_schedule)


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf"), object(), (1, 2), {1: "value"}]
)
def test_rejects_non_json_unknown_fields(native_schedule, value):
    native_schedule[0]["VendorDayField"] = value
    with pytest.raises(WG4ScheduleError, match="VendorDayField"):
        WG4Schedule(native_schedule)


@pytest.mark.parametrize("weekday", [True, 1.0, "1", 0, 8])
def test_day_lookup_requires_a_native_identity(native_schedule, weekday):
    with pytest.raises(WG4ScheduleError):
        WG4Schedule(native_schedule).day(weekday)


def test_validation_errors_do_not_echo_values(native_schedule):
    private_value = "private response content"
    native_schedule[0]["Events"][0]["TempFloor"] = private_value
    with pytest.raises(WG4ScheduleError) as error:
        WG4Schedule(native_schedule)

    assert "Schedules[0].Events[0].TempFloor" in str(error.value)
    assert private_value not in str(error.value)


@pytest.fixture
def device_limits():
    return {
        "MinTimeLimits": [
            {"ScheduleType": slot, "Clock": minimum}
            for slot, minimum in enumerate([0, 1800, 3600, 5400, 7200, 14400])
        ],
        "MaxTimeLimits": [
            {"ScheduleType": slot, "Clock": maximum}
            for slot, maximum in enumerate([79200, 81000, 82800, 84600, 86400, 97200])
        ],
    }


def test_attributes_keep_slot_identity_and_inactive_native_values(native_schedule):
    attributes = WG4Schedule(native_schedule).attributes()
    assert list(attributes) == [
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ]
    assert attributes["monday"][1] == {
        "slot": 1,
        "time": "03:00",
        "temperature": 24.34,
        "active": False,
    }


def test_time_only_patch_preserves_exact_native_inactive_temperature(
    native_schedule, device_limits
):
    original = copy.deepcopy(native_schedule)
    native_schedule[0]["Events"][1]["TempFloor"] = 3222
    original[0]["Events"][1]["TempFloor"] = 3222
    model = WG4Schedule(native_schedule)
    result = model.with_changes(
        [{"day": "monday", "slot": 1, "time": "03:15"}],
        "F",
        500,
        4000,
        device_limits,
    )
    original[0]["Events"][1]["Clock"] = "03:15:00"

    assert result.to_payload() == original
    assert model.to_payload() == native_schedule
    assert result.day(1).events[1].temperature_centi_celsius == 3222


@pytest.mark.parametrize(
    ("unit", "temperature", "native"),
    [("C", 25.5, 2550), ("F", 78, 2555), ("F", 90, 3222)],
)
def test_temperature_conversion_matches_vendor_truncation(
    native_schedule, device_limits, unit, temperature, native
):
    result = WG4Schedule(native_schedule).with_changes(
        [{"day": "sunday", "slot": 5, "temperature": temperature}],
        unit,
        500,
        4000,
        device_limits,
    )
    assert result.day(7).events[5].temperature_centi_celsius == native


def test_empty_patch_preserves_every_native_byte_value(native_schedule, device_limits):
    result = WG4Schedule(native_schedule).with_changes(
        [], "F", 500, 4000, device_limits
    )
    assert result.to_payload() == native_schedule
    assert result.fingerprint() == WG4Schedule(native_schedule).fingerprint()


def test_shuffled_device_bounds_are_matched_by_slot_identity(
    native_schedule, device_limits
):
    device_limits["MinTimeLimits"].reverse()
    device_limits["MaxTimeLimits"].reverse()
    result = WG4Schedule(native_schedule).with_changes(
        [
            {"day": "monday", "slot": 0, "time": "21:45"},
            {"day": "monday", "slot": 5, "time": "22:00"},
        ],
        "C",
        500,
        4000,
        device_limits,
    )
    assert result.day(1).events[0].clock == "21:45:00"


def test_overnight_event_allowed_by_actual_device_bound(native_schedule, device_limits):
    result = WG4Schedule(native_schedule).with_changes(
        [
            {"day": "sunday", "slot": 0, "time": "06:00"},
            {"day": "sunday", "slot": 5, "time": "00:15"},
            {"day": "monday", "slot": 0, "time": "00:30"},
        ],
        "C",
        500,
        4000,
        device_limits,
    )
    assert result.day(7).events[5].clock == "00:15:00"
    assert "EventIsOnNextDay" not in result.to_payload()[6]["Events"][5]
    assert result.attributes()["sunday"][5]["next_day"] is True


def test_rejects_cross_day_collision_incoming_to_edited_day(
    native_schedule, device_limits
):
    native_schedule[6]["Events"][0]["Clock"] = "06:00:00"
    native_schedule[6]["Events"][5]["Clock"] = "00:30:00"
    with pytest.raises(WG4ScheduleError, match="Overnight"):
        WG4Schedule(native_schedule).with_changes(
            [{"day": "monday", "slot": 0, "time": "00:30"}],
            "C",
            500,
            4000,
            device_limits,
        )


@pytest.mark.parametrize(
    "change",
    [
        {"day": "monday", "slot": 0, "active": False},
        {"day": "monday", "slot": 5, "time": "03:00"},
        {"day": "monday", "slot": 0, "time": "22:15"},
        {"day": "monday", "slot": 1, "time": "00:15", "active": True},
    ],
)
def test_rejects_invalid_active_timeline_and_actual_slot_limits(
    native_schedule, device_limits, change
):
    with pytest.raises(WG4ScheduleError):
        WG4Schedule(native_schedule).with_changes(
            [change], "C", 500, 4000, device_limits
        )


@pytest.mark.parametrize(
    "temperature", [True, "25", float("nan"), float("inf"), 4.99, 40.01]
)
def test_rejects_invalid_requested_temperature(native_schedule, temperature):
    with pytest.raises(WG4ScheduleError):
        WG4Schedule(native_schedule).with_changes(
            [{"day": "monday", "slot": 0, "temperature": temperature}],
            "C",
            500,
            4000,
        )


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"day": "monday", "slot": 0},
        {"day": "Monday", "slot": 0, "active": True},
        {"day": "monday", "slot": True, "active": True},
        {"day": "monday", "slot": 6, "active": True},
        {"day": "monday", "slot": 1, "active": "false"},
        {"day": "monday", "slot": 1, "time": "03:01"},
        {"day": "monday", "slot": 1, "time": "03:00:00"},
        {"day": "monday", "slot": 1, "time": "03:00", "unknown": True},
    ],
)
def test_rejects_malformed_slot_patches(native_schedule, change):
    with pytest.raises(WG4ScheduleError):
        WG4Schedule(native_schedule).with_changes([change], "C", 500, 4000)


def test_rejects_duplicate_patch_identity(native_schedule):
    patch = {"day": "monday", "slot": 0, "temperature": 25}
    with pytest.raises(WG4ScheduleError, match="once"):
        WG4Schedule(native_schedule).with_changes([patch, patch], "C", 500, 4000)


@pytest.mark.parametrize("change", ["missing", "duplicate", "non_integer", "inverted"])
def test_rejects_malformed_device_time_limits(native_schedule, device_limits, change):
    if change == "missing":
        device_limits["MinTimeLimits"].pop()
    elif change == "duplicate":
        device_limits["MinTimeLimits"][5]["ScheduleType"] = 0
    elif change == "non_integer":
        device_limits["MinTimeLimits"][0]["Clock"] = True
    else:
        device_limits["MinTimeLimits"][0]["Clock"] = 80000
    with pytest.raises(WG4ScheduleError):
        WG4Schedule(native_schedule).with_changes([], "C", 500, 4000, device_limits)


def test_preserves_dormant_and_unrelated_accepted_data(native_schedule, device_limits):
    native_schedule[0]["Events"][1].update(Clock="23:59:59", TempFloor=-50)
    native_schedule[4]["Events"][0].update(Clock="23:59:59", Active=False)
    result = WG4Schedule(native_schedule).with_changes(
        [{"day": "monday", "slot": 5, "temperature": 25}],
        "C",
        500,
        4000,
        device_limits,
    )
    expected = copy.deepcopy(native_schedule)
    expected[0]["Events"][5]["TempFloor"] = 2500
    assert result.to_payload() == expected


@pytest.mark.parametrize("unit", [None, True, "c", "K", [], {}])
def test_rejects_invalid_temperature_units(native_schedule, unit):
    with pytest.raises(WG4ScheduleError):
        WG4Schedule(native_schedule).with_changes([], unit, 500, 4000)


@pytest.mark.parametrize("unit", ["C", "F"])
def test_huge_numeric_temperature_is_a_validation_error(native_schedule, unit):
    with pytest.raises(WG4ScheduleError):
        WG4Schedule(native_schedule).with_changes(
            [{"day": "monday", "slot": 0, "temperature": 10**1000}],
            unit,
            500,
            4000,
        )


@pytest.mark.parametrize("limits", [[], True, "limits"])
def test_rejects_non_object_device_limits(native_schedule, limits):
    with pytest.raises(WG4ScheduleError):
        WG4Schedule(native_schedule).with_changes([], "C", 500, 4000, limits)


def test_activating_dormant_event_checks_its_preserved_temperature(
    native_schedule, device_limits
):
    native_schedule[0]["Events"][1]["TempFloor"] = -50
    with pytest.raises(WG4ScheduleError, match="activated"):
        WG4Schedule(native_schedule).with_changes(
            [{"day": "monday", "slot": 1, "active": True}],
            "C",
            500,
            4000,
            device_limits,
        )
    result = WG4Schedule(native_schedule).with_changes(
        [{"day": "monday", "slot": 1, "active": True, "temperature": 25}],
        "C",
        500,
        4000,
        device_limits,
    )
    assert result.day(1).events[1].temperature_centi_celsius == 2500
    assert result.day(1).events[1].active is True


def test_disallows_two_overnight_slots_at_the_same_time(native_schedule, device_limits):
    # Both slots 4/5 can be next-day, but the actual normalized timeline still
    # requires a 15-minute gap. Vendor Date reuse previously weakened this case.
    with pytest.raises(WG4ScheduleError, match="15 minutes"):
        WG4Schedule(native_schedule).with_changes(
            [
                {"day": "monday", "slot": 0, "time": "06:00"},
                {"day": "monday", "slot": 4, "time": "00:00", "active": True},
                {"day": "monday", "slot": 5, "time": "00:00"},
            ],
            "C",
            500,
            4000,
            device_limits,
        )
