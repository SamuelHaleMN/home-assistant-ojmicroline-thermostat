import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import test from "node:test"
import vm from "node:vm"

const source = readFileSync(new URL("../custom_components/ojmicroline_thermostat/frontend/ojmicroline-native-schedule-card.js", import.meta.url), "utf8")
const DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
const BASELINE = "a".repeat(64)
const SENSOR = "sensor.bathroom_native_schedule"
const CLIMATE = "climate.bathroom"
const plain = (value) => JSON.parse(JSON.stringify(value))

class FakeNode {
  constructor(attributes = {}) {
    this.dataset = Object.fromEntries(Object.entries(attributes).filter(([key]) => key.startsWith("data-")).map(([key, value]) => [key.slice(5), value]))
    this.value = attributes.value || ""
    this.checked = attributes.checked !== undefined
    this.disabled = attributes.disabled !== undefined
    this.listeners = new Map()
    this.textContent = ""
    this.innerHTML = ""
  }

  addEventListener(event, callback) {
    this.listeners.set(event, callback)
  }

  emit(event) {
    this.listeners.get(event)?.({ target: this })
  }
}

class FakeShadow {
  set innerHTML(html) {
    this.html = html
    this.nodes = new Map()
    this.inputs = []
    for (const tag of html.matchAll(/<(?:button|select|p|div|input)\b([^>]*)>/g)) {
      const attributes = Object.fromEntries([...tag[1].matchAll(/([\w-]+)="([^"]*)"|\b(checked|disabled)\b/g)].map((attribute) => [attribute[1] || attribute[3], attribute[2] ?? ""]))
      const node = new FakeNode(attributes)
      if (attributes.id) {
        this.nodes.set(`#${attributes.id}`, node)
      }
      if (tag[0].startsWith("<input")) {
        this.inputs.push(node)
      }
    }
  }

  get innerHTML() {
    return this.html
  }

  querySelector(selector) {
    return this.nodes.get(selector) || null
  }

  querySelectorAll(selector) {
    if (selector === "input") {
      return this.inputs
    }
    return selector.split(", ").map((part) => this.querySelector(part)).filter(Boolean)
  }
}

function setup(unit = "°F") {
  const definitions = new Map()
  const context = vm.createContext({
    HTMLElement: class {
      attachShadow() {
        this.shadowRoot = new FakeShadow()
        return this.shadowRoot
      }
    },
    customElements: {
      get: (name) => definitions.get(name),
      define: (name, constructor) => definitions.set(name, constructor),
    },
    window: {},
  })
  vm.runInContext(source, context)
  const Card = definitions.get("ojmicroline-native-schedule-card")
  const days = Object.fromEntries(DAYS.map((day) => [day, Array.from({ length: 6 }, (_, slot) => ({
    slot,
    time: `${String(6 + slot * 2).padStart(2, "0")}:00`,
    temperature: slot === 0 ? 25.55 : 18.33,
    active: slot === 0 || slot === 5,
  }))]))
  const state = {
    state: "stored",
    last_updated: "first",
    attributes: { days, schedule_hash: BASELINE, temperature_unit: "C", timezone_offset: "-04:00", friendly_name: "Bathroom schedule" },
  }
  const requests = []
  const hass = {
    config: { unit_system: { temperature: unit } },
    entities: { [SENSOR]: { device_id: "bathroom" }, [CLIMATE]: { device_id: "bathroom" } },
    states: { [SENSOR]: state },
    callWS: async (request) => {
      requests.push(plain(request))
      const candidate = plain(days)
      for (const patch of request.service_data.changes) {
        const event = candidate[patch.day][patch.slot]
        if (patch.time !== undefined) {
          event.time = patch.time
        }
        if (patch.temperature !== undefined) {
          const celsius = request.service_data.temperature_unit === "F" ? (patch.temperature - 32) * 5 / 9 : patch.temperature
          event.temperature = Math.trunc(celsius * 100) / 100
        }
        if (patch.active !== undefined) {
          event.active = patch.active
        }
      }
      const response = {
        dry_run: request.service_data.dry_run,
        changed: request.service_data.changes.length > 0,
        baseline_hash: BASELINE,
        schedule_hash: "b".repeat(64),
        changes: request.service_data.changes,
        days: candidate,
        temperature_unit: "C",
        time_basis: "thermostat_local",
        timezone_offset: "-04:00",
        limits_checked: !request.service_data.dry_run,
        verified: !request.service_data.dry_run,
      }
      return { response: { [CLIMATE]: response } }
    },
  }
  const card = new Card()
  card.setConfig({ entity: SENSOR, climate_entity: CLIMATE })
  card.hass = hass
  return { card, hass, state, requests, context, Card }
}

test("requires an explicit schedule sensor and climate target", () => {
  const { Card } = setup()
  const card = new Card()
  assert.throws(() => card.setConfig({ entity: SENSOR }), /climate_entity/)
  assert.throws(() => card.setConfig({ entity: CLIMATE, climate_entity: CLIMATE }), /schedule sensor/)
})

test("renders six stable slots and locks the first active event", () => {
  const { card } = setup()
  card._startEdit()
  const active = card.shadowRoot.inputs.filter((input) => input.dataset.field === "active")
  assert.equal(active.length, 6)
  assert.equal(active[0].checked, true)
  assert.equal(active[0].disabled, true)
  assert.equal(active[1].checked, false)
  assert.match(card.shadowRoot.innerHTML, /step="900"/)
  assert.match(card.shadowRoot.innerHTML, /thermostat's local clock/)
  assert.match(card.shadowRoot.innerHTML, /<table class="schedule-table is-editing">/)
  assert.equal([...card.shadowRoot.innerHTML.matchAll(/class="field-label" aria-hidden="true">Start/g)].length, 6)
  assert.equal([...card.shadowRoot.innerHTML.matchAll(/class="field-label" aria-hidden="true">Temperature/g)].length, 6)
  for (let slot = 0; slot < 6; slot++) {
    for (const field of ["time", "temperature", "active"]) {
      assert.equal(card.shadowRoot.inputs.filter((input) => input.dataset.slot === String(slot) && input.dataset.field === field).length, 1)
      assert.match(card.shadowRoot.innerHTML, new RegExp(`aria-label="Event ${slot + 1} ${field}`))
    }
  }
})

test("displaying Fahrenheit and editing time preserve all native temperatures", async () => {
  const { card, state, requests } = setup()
  const before = plain(state.attributes.days)
  card._startEdit()
  card._changeField("monday", 0, "time", "06:15")
  await card._callAction(true)
  assert.deepEqual(requests[0].service_data.changes, [{ day: "monday", slot: 0, time: "06:15" }])
  assert.equal(requests[0].service_data.temperature_unit, "F")
  assert.deepEqual(plain(state.attributes.days), before)
  assert.equal(card._preview.days.monday[0].temperature, 25.55)
  assert.equal(card._preview.days.monday[1].temperature, 18.33)
})

test("restoring displayed Fahrenheit removes the temperature patch without a round trip", () => {
  const { card } = setup()
  card._startEdit()
  card._changeField("monday", 0, "temperature", 79)
  assert.equal(card._patches()[0].temperature, 79)
  card._changeField("monday", 0, "temperature", 78)
  assert.equal(card._patches().length, 0)
  assert.equal(card._draft.days.monday[0].temperature, 25.55)
})

test("preview and save send the same sparse patches and frozen baseline", async () => {
  const { card, requests } = setup()
  card._startEdit()
  card._changeField("friday", 4, "active", true)
  card._changeField("monday", 0, "temperature", 79)
  await card._callAction(true)
  await card._callAction(false)
  assert.equal(requests.length, 2)
  assert.equal(requests[0].type, "call_service")
  assert.equal(requests[0].service, "set_native_schedule")
  assert.equal(requests[0].return_response, true)
  assert.deepEqual(requests[0].target, { entity_id: CLIMATE })
  assert.equal(requests[0].service_data.expected_hash, BASELINE)
  assert.deepEqual(requests[1].service_data.changes, requests[0].service_data.changes)
  assert.equal(requests[1].service_data.expected_hash, BASELINE)
  assert.equal(requests[1].service_data.dry_run, false)
  assert.match(card._message, /cloud readback/)
  assert.match(card._message, /Physical device acknowledgement is not measured/)
})

test("edits invalidate a preview and save cannot bypass preview", async () => {
  const { card, requests } = setup()
  card._startEdit()
  card._changeField("monday", 0, "time", "06:15")
  await card._callAction(false)
  assert.equal(requests.length, 0)
  await card._callAction(true)
  card._changeField("tuesday", 1, "active", true)
  assert.equal(card._preview, null)
  assert.equal(card.shadowRoot.querySelector("#preview-result").innerHTML, "")
  await card._callAction(false)
  assert.equal(requests.length, 1)
})

test("remote edits preserve the draft but block all further service calls", async () => {
  const { card, hass, state, requests } = setup()
  card._startEdit()
  card._changeField("monday", 0, "temperature", 79)
  state.attributes.schedule_hash = "c".repeat(64)
  card.hass = hass
  assert.equal(card._draft.hash, BASELINE)
  assert.equal(card._patches()[0].temperature, 79)
  assert.equal(card._stale, true)
  await card._callAction(true)
  assert.equal(requests.length, 0)
  assert.match(card.shadowRoot.querySelector("#status").textContent, /Refresh/)
  card._discard()
  card._startEdit()
  assert.equal(card._draft.hash, "c".repeat(64))
})

test("draft display units remain frozen if HA units change", () => {
  const { card, hass } = setup()
  card._startEdit()
  card._changeField("monday", 0, "temperature", 79)
  hass.config.unit_system.temperature = "°C"
  card.hass = hass
  assert.equal(card._draft.unit, "F")
  assert.equal(card._patches()[0].temperature, 79)
})

test("HA state updates and day selection make no network requests", () => {
  const { card, hass, requests } = setup()
  card._startEdit()
  const day = card.shadowRoot.querySelector("#day")
  day.value = "tuesday"
  day.emit("change")
  card.hass = hass
  assert.equal(card._day, "tuesday")
  assert.equal(requests.length, 0)
})

test("DOM input listeners generate sparse changes", () => {
  const { card } = setup()
  card._startEdit()
  const time = card.shadowRoot.inputs.find((input) => input.dataset.slot === "2" && input.dataset.field === "time")
  time.value = "10:15"
  time.emit("input")
  assert.deepEqual(plain(card._patches()), [{ day: "monday", slot: 2, time: "10:15" }])
})

test("day selection preserves accessible editor controls and cancel restores the read-only table", () => {
  const { card, requests } = setup()
  assert.match(card.shadowRoot.innerHTML, /<table class="schedule-table">/)
  card._startEdit()
  card._changeField("monday", 2, "time", "10:15")
  const day = card.shadowRoot.querySelector("#day")
  day.value = "tuesday"
  day.emit("change")
  assert.equal(card.shadowRoot.inputs.length, 18)
  const temperature = card.shadowRoot.inputs.find((input) => input.dataset.slot === "3" && input.dataset.field === "temperature")
  temperature.value = "68"
  temperature.emit("input")
  assert.deepEqual(plain(card._patches()), [
    { day: "monday", slot: 2, time: "10:15" },
    { day: "tuesday", slot: 3, temperature: 68 },
  ])
  card.shadowRoot.querySelector("#refresh").emit("click")
  assert.match(card.shadowRoot.innerHTML, /<table class="schedule-table">/)
  assert.equal(card.shadowRoot.inputs.length, 0)
  assert.equal(requests.length, 0)
})

test("invalid edited times or empty temperatures never reach the action", async () => {
  const { card, requests } = setup()
  card._startEdit()
  card._changeField("monday", 0, "time", "25:15")
  await card._callAction(true)
  assert.equal(requests.length, 0)
  card._changeField("monday", 0, "time", "06:00")
  card._changeField("monday", 0, "temperature", NaN)
  await card._callAction(true)
  assert.equal(requests.length, 0)
})

test("title, offset, preview values, and action errors are escaped", async () => {
  const { card, hass, state } = setup()
  card.setConfig({ entity: SENSOR, climate_entity: CLIMATE, title: '<img src=x onerror="bad()">' })
  state.attributes.timezone_offset = "<script>bad()</script>"
  card.hass = hass
  card._render()
  assert.match(card.shadowRoot.innerHTML, /&lt;img/)
  assert.match(card.shadowRoot.innerHTML, /&lt;script&gt;/)
  assert.doesNotMatch(card.shadowRoot.innerHTML, /<img src=x/)
  card._startEdit()
  hass.callWS = async () => { throw new Error("<script>error</script>") }
  await card._callAction(true)
  assert.equal(card.shadowRoot.querySelector("#status").textContent, "<script>error</script>")
})

test("ambiguous save failures block another save and retain the error without retry", async () => {
  const { card, hass, requests } = setup()
  card._startEdit()
  card._changeField("monday", 0, "time", "06:15")
  await card._callAction(true)
  hass.callWS = async (request) => {
    requests.push(plain(request))
    throw new Error("Cloud timeout")
  }
  await card._callAction(false)
  await card._callAction(false)
  assert.equal(requests.length, 2)
  assert.equal(card._stale, true)
  assert.match(card.shadowRoot.querySelector("#status").textContent, /Cloud timeout.*No automatic retry/)
})

test("unchanged preview and save remain explicit backend no-ops", async () => {
  const { card, requests } = setup()
  card._startEdit()
  await card._callAction(true)
  assert.equal(card._preview.changed, false)
  await card._callAction(false)
  assert.deepEqual(requests[1].service_data.changes, [])
  assert.match(card._message, /no thermostat write was needed/)
})

test("unavailable or malformed schedules never expose an editor", () => {
  const { card, hass, state } = setup()
  state.state = "unavailable"
  card.hass = hass
  assert.equal(card.shadowRoot.querySelector("#edit"), null)
  state.state = "stored"
  state.attributes.days.monday.pop()
  card._render()
  assert.equal(card.shadowRoot.querySelector("#edit"), null)
})

test("incomplete preview responses cannot enable save", async () => {
  const { card, hass, requests } = setup()
  card._startEdit()
  const originalCall = hass.callWS
  hass.callWS = async (request) => {
    const result = await originalCall(request)
    delete result.response[CLIMATE].days.sunday
    return result
  }
  await card._callAction(true)
  assert.equal(card._preview, null)
  assert.equal(card.shadowRoot.querySelector("#save").disabled, true)
  await card._callAction(false)
  assert.equal(requests.length, 1)
  assert.match(card._message, /complete resulting program/)
})

test("unverified save responses retain the draft and require refresh", async () => {
  const { card, hass } = setup()
  card._startEdit()
  card._changeField("monday", 0, "time", "06:15")
  await card._callAction(true)
  const originalCall = hass.callWS
  hass.callWS = async (request) => {
    const result = await originalCall(request)
    result.response[CLIMATE].verified = false
    return result
  }
  await card._callAction(false)
  assert.notEqual(card._draft, null)
  assert.equal(card._stale, true)
  assert.match(card.shadowRoot.querySelector("#status").textContent, /did not confirm.*No automatic retry/)
})

test("concurrent button presses never dispatch another in-flight action", async () => {
  const { card, hass, requests } = setup()
  card._startEdit()
  const originalCall = hass.callWS
  let finish
  hass.callWS = async (request) => {
    await new Promise((resolve) => { finish = resolve })
    return originalCall(request)
  }
  const pending = card._callAction(true)
  assert.equal(card._busy, true)
  await card._callAction(true)
  assert.equal(requests.length, 0)
  finish()
  await pending
  assert.equal(requests.length, 1)
})

test("preview displays native truncation independently of rounded Fahrenheit", async () => {
  const { card } = setup()
  card._startEdit()
  card._changeField("monday", 0, "temperature", 79)
  await card._callAction(true)
  const result = card.shadowRoot.querySelector("#preview-result").innerHTML
  assert.match(result, /78 °F \(25\.55 °C\)/)
  assert.match(result, /79 °F \(26\.11 °C\)/)
  assert.match(result, /Device time limits will be checked on Save/)
})

test("identical schedule hashes cannot bypass a mismatched climate device", async () => {
  const { card, hass, requests } = setup()
  hass.entities[CLIMATE].device_id = "other-thermostat"
  card.hass = hass
  card._startEdit()
  await card._callAction(true)
  assert.equal(card._draft, null)
  assert.equal(card.shadowRoot.querySelector("#edit"), null)
  assert.match(card.shadowRoot.innerHTML, /different Home Assistant devices/)
  assert.equal(requests.length, 0)
})

test("missing registry metadata fails closed and later metadata enables editing without requests", () => {
  const { card, hass, requests } = setup()
  delete hass.entities[SENSOR].device_id
  card.hass = hass
  card._startEdit()
  assert.equal(card._draft, null)
  assert.match(card.shadowRoot.innerHTML, /Entity registry metadata is unavailable/)
  hass.entities[SENSOR].device_id = "bathroom"
  card.hass = hass
  card._startEdit()
  assert.notEqual(card._draft, null)
  assert.equal(requests.length, 0)
})

test("registry changes after preview block save before dispatch", async () => {
  const { card, hass, requests } = setup()
  card._startEdit()
  card._changeField("monday", 0, "time", "06:15")
  await card._callAction(true)
  hass.entities[CLIMATE].device_id = "other-thermostat"
  await card._callAction(false)
  assert.equal(requests.length, 1)
  assert.equal(card._stale, true)
  assert.match(card.shadowRoot.querySelector("#status").textContent, /different Home Assistant devices/)
})

test("overnight slots and resulting preview explicitly identify the next day", async () => {
  const { card, state } = setup()
  state.attributes.days.monday[5].time = "00:15"
  state.attributes.days.monday[5].next_day = true
  card._render()
  assert.match(card.shadowRoot.innerHTML, /00:15 \(next day\)/)
  card._startEdit()
  assert.match(card.shadowRoot.innerHTML, /value="00:15"> \(next day\)/)
  card._changeField("monday", 5, "temperature", 70)
  await card._callAction(true)
  assert.match(card.shadowRoot.querySelector("#preview-result").innerHTML, /monday · 6 \(next day\)/)
})
