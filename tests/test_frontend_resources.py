"""Lovelace resource ownership, upgrades, loading, and concurrency regressions."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

BASE = "/ojmicroline_thermostat/ojmicroline-native-schedule-card.js"
CURRENT = BASE + "?v=1.6.2"


class StorageCollection:
    """Public ResourceStorageCollection-shaped API with lazy loading."""

    def __init__(self, items=()):
        self.pending = copy.deepcopy(list(items))
        self.items = []
        self.loaded = False
        self.info_calls = 0
        self.loads = 0
        self.mutations = []
        self.fail_update = False

    async def async_get_info(self):
        self.info_calls += 1
        await asyncio.sleep(0)
        if not self.loaded:
            self.items = self.pending
            self.loaded = True
            self.loads += 1
        return {"resources": len(self.items)}

    def async_items(self):
        assert self.loaded
        return self.items

    async def async_create_item(self, data):
        await asyncio.sleep(0)
        assert set(data) == {"url", "res_type"}
        item = {"id": "created", "url": data["url"], "type": data["res_type"]}
        self.items.append(item)
        self.mutations.append(("create", copy.deepcopy(data)))
        return item

    async def async_update_item(self, identity, data):
        if self.fail_update:
            raise OSError("Synthetic storage write failure")
        await asyncio.sleep(0)
        assert set(data) == {"url", "res_type"}
        item = next(item for item in self.items if item["id"] == identity)
        item.update(url=data["url"], type=data["res_type"])
        self.mutations.append(("update", identity, copy.deepcopy(data)))
        return item

    async def async_delete_item(self, identity):
        self.items[:] = [item for item in self.items if item["id"] != identity]
        self.mutations.append(("delete", identity))


@pytest.fixture
def registration(monkeypatch):
    # Exercise the helper without importing the HA-dependent integration init.
    constants = ModuleType("homeassistant.components.lovelace.const")
    constants.LOVELACE_DATA = "lovelace"
    resources = ModuleType("homeassistant.components.lovelace.resources")
    resources.ResourceStorageCollection = StorageCollection
    monkeypatch.setitem(sys.modules, constants.__name__, constants)
    monkeypatch.setitem(sys.modules, resources.__name__, resources)
    spec = importlib.util.spec_from_file_location(
        "warmtiles_frontend_resources",
        Path(__file__).parents[1]
        / "custom_components/ojmicroline_thermostat/frontend_resources.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.async_register_native_card_resource


def hass_for(collection):
    return SimpleNamespace(data={"lovelace": SimpleNamespace(resources=collection)})


@pytest.mark.asyncio
async def test_creates_missing_module_after_loading_storage(registration):
    collection = StorageCollection()
    assert await registration(hass_for(collection), CURRENT) is True
    assert collection.info_calls == 1
    assert collection.loads == 1
    assert collection.items == [{"id": "created", "url": CURRENT, "type": "module"}]
    assert collection.mutations == [("create", {"url": CURRENT, "res_type": "module"})]


@pytest.mark.asyncio
async def test_upgrades_version_and_type_preserving_identifier(registration):
    collection = StorageCollection(
        [{"id": "original-id", "url": BASE + "?v=1.6.1", "type": "js"}]
    )
    assert await registration(hass_for(collection), CURRENT) is True
    assert collection.items == [{"id": "original-id", "url": CURRENT, "type": "module"}]
    assert collection.mutations == [
        ("update", "original-id", {"url": CURRENT, "res_type": "module"})
    ]


@pytest.mark.asyncio
async def test_repeated_setup_is_idempotent(registration):
    collection = StorageCollection(
        [{"id": "original-id", "url": CURRENT, "type": "module"}]
    )
    hass = hass_for(collection)
    assert await registration(hass, CURRENT) is True
    assert await registration(hass, CURRENT) is True
    assert collection.info_calls == 2
    assert collection.loads == 1
    assert collection.mutations == []


@pytest.mark.asyncio
async def test_query_and_fragment_versions_are_one_owned_resource(registration):
    collection = StorageCollection(
        [{"id": "old", "url": BASE + "?cache=old&v=1.6.1#module", "type": "module"}]
    )
    await registration(hass_for(collection), CURRENT)
    assert collection.items == [{"id": "old", "url": CURRENT, "type": "module"}]


@pytest.mark.asyncio
async def test_deduplicates_only_owned_paths_preserving_unrelated_resources(
    registration,
):
    unrelated = [
        {
            "id": "other-card",
            "url": "/hacsfiles/example/card.js?v=12",
            "type": "module",
        },
        {
            "id": "other-oj",
            "url": "/ojmicroline_thermostat/ojmicroline-schedule-card.js",
            "type": "module",
        },
        {"id": "absolute", "url": "https://example.invalid" + BASE, "type": "module"},
        {
            "id": "protocol-relative",
            "url": "//example.invalid" + BASE,
            "type": "module",
        },
        {"id": "triple-slash", "url": "//" + BASE, "type": "module"},
        {"id": "different-path", "url": BASE + ".map", "type": "js"},
        {"id": "invalid-url", "url": "https://[", "type": "module"},
    ]
    collection = StorageCollection(
        [
            unrelated[0],
            {"id": "keep", "url": BASE + "?v=1", "type": "module"},
            *unrelated[1:],
            {"id": "duplicate", "url": CURRENT, "type": "module"},
            {"id": "duplicate-plain", "url": BASE, "type": "js"},
        ]
    )
    await registration(hass_for(collection), CURRENT)
    assert [item for item in collection.items if item["id"] != "keep"] == unrelated
    assert next(item for item in collection.items if item["id"] == "keep") == {
        "id": "keep",
        "url": CURRENT,
        "type": "module",
    }
    assert collection.mutations == [
        ("update", "keep", {"url": CURRENT, "res_type": "module"}),
        ("delete", "duplicate"),
        ("delete", "duplicate-plain"),
    ]


@pytest.mark.asyncio
async def test_yaml_resources_are_neither_loaded_nor_modified(registration):
    yaml = SimpleNamespace(
        loaded=True,
        data=[{"url": CURRENT, "type": "module"}],
    )
    expected = copy.deepcopy(yaml.data)
    assert await registration(hass_for(yaml), CURRENT) is False
    assert yaml.data == expected
    assert set(vars(yaml)) == {"loaded", "data"}


@pytest.mark.asyncio
async def test_missing_lovelace_uses_caller_fallback(registration):
    assert await registration(SimpleNamespace(data={}), CURRENT) is False


@pytest.mark.asyncio
async def test_concurrent_setup_does_not_create_duplicates(registration):
    collection = StorageCollection()
    hass = hass_for(collection)
    outcomes = await asyncio.gather(*(registration(hass, CURRENT) for _ in range(10)))
    assert all(outcomes)
    assert collection.loads == 1
    assert collection.items == [{"id": "created", "url": CURRENT, "type": "module"}]
    assert collection.mutations == [("create", {"url": CURRENT, "res_type": "module"})]


@pytest.mark.asyncio
async def test_failed_upgrade_preserves_old_resources_and_propagates(registration):
    existing = [
        {"id": "keep", "url": BASE + "?v=1", "type": "module"},
        {"id": "duplicate", "url": BASE + "?v=0", "type": "module"},
    ]
    collection = StorageCollection(existing)
    collection.fail_update = True
    with pytest.raises(OSError, match="Synthetic storage"):
        await registration(hass_for(collection), CURRENT)
    assert collection.items == existing
    assert collection.mutations == []


@pytest.mark.parametrize(
    "url",
    [
        "https://example.invalid" + BASE,
        "//example.invalid" + BASE,
        "//" + BASE,
        "/other.js",
        None,
    ],
)
@pytest.mark.asyncio
async def test_rejects_foreign_registration_before_any_storage_access(
    registration, url
):
    collection = StorageCollection()
    with pytest.raises(ValueError, match="root-relative"):
        await registration(hass_for(collection), url)
    assert collection.info_calls == 0
    assert collection.mutations == []
