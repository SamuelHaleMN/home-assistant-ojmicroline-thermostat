/**
 * Edit a WG4 thermostat's stored weekly program through the OJ integration.
 *
 * type: custom:ojmicroline-native-schedule-card
 * entity: sensor.bathroom_native_schedule
 * climate_entity: climate.bathroom
 *
 * Drafts retain their original schedule hash. Only explicitly changed fields
 * are sent, so displaying native Celsius values in Fahrenheit cannot rewrite
 * untouched temperatures. All times belong to the thermostat's local clock.
 * The card makes requests only when Preview or Save is pressed.
 */

const NATIVE_DOMAIN = "ojmicroline_thermostat"
const NATIVE_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
const nativeEscape = (value) => String(value).replace(/[&<>"']/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[character])

const nativeUnit = (hass) => hass?.config?.unit_system?.temperature === "°F" ? "F" : "C"

const nativeTemperature = (celsius, unit) => {
  const value = unit === "F" ? celsius * 9 / 5 + 32 : celsius
  return String(unit === "F" ? Math.round(value) : Math.round(value * 100) / 100)
}

const nativeScheduleValid = (state) => {
  if (state?.state !== "stored" || !/^[a-f0-9]{64}$/i.test(state.attributes?.schedule_hash || "")) {
    return false
  }
  return NATIVE_DAYS.every((day) => {
    const events = state.attributes?.days?.[day]
    return Array.isArray(events) && events.length === 6 && events.every((event, slot) => (
      event.slot === slot && /^\d{2}:\d{2}(?::\d{2})?$/.test(event.time) &&
      Number.isFinite(event.temperature) && typeof event.active === "boolean"
    ))
  })
}

class OJMicrolineNativeScheduleCard extends HTMLElement {
  constructor() {
    super()
    this.attachShadow({ mode: "open" })
    this._day = "monday"
    this._changes = new Map()
    this._busy = false
    this._message = ""
  }

  static getConfigForm() {
    return {
      schema: [
        { name: "entity", required: true, selector: { entity: { domain: "sensor", integration: NATIVE_DOMAIN } } },
        { name: "climate_entity", required: true, selector: { entity: { domain: "climate", integration: NATIVE_DOMAIN } } },
        { name: "title", selector: { text: {} } },
      ],
    }
  }

  setConfig(config) {
    if (!config?.entity?.startsWith("sensor.") || !config?.climate_entity?.startsWith("climate.")) {
      throw new Error("Set entity to the native schedule sensor and climate_entity to its thermostat.")
    }
    this._config = { ...config }
    this._discard()
  }

  set hass(hass) {
    this._hass = hass
    if (!this._config) {
      return
    }
    const state = hass.states[this._config.entity]
    if (this._draft) {
      const deviceError = this._deviceError()
      if (deviceError || !nativeScheduleValid(state) || state.attributes.schedule_hash !== this._draft.hash) {
        this._stale = true
        this._staleMessage = deviceError || "The stored schedule changed or became unavailable while editing. Refresh from Home Assistant before saving."
      }
      this._updateControls()
      return
    }
    const key = `${state?.last_updated}|${state?.state}|${state?.attributes?.schedule_hash}|${nativeUnit(hass)}|${this._deviceError()}`
    if (key !== this._stateKey) {
      this._stateKey = key
      this._render()
    }
  }

  getCardSize() {
    return 7
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6, rows: "auto" }
  }

  _discard() {
    this._draft = null
    this._preview = null
    this._stale = false
    this._staleMessage = ""
    this._changes.clear()
    this._message = ""
    this._render()
  }

  _startEdit() {
    const state = this._hass?.states[this._config.entity]
    if (!nativeScheduleValid(state) || this._deviceError()) {
      return
    }
    this._draft = {
      hash: state.attributes.schedule_hash,
      days: JSON.parse(JSON.stringify(state.attributes.days)),
      unit: nativeUnit(this._hass),
      offset: state.attributes.timezone_offset,
    }
    this._preview = null
    this._stale = false
    this._staleMessage = ""
    this._changes.clear()
    this._message = ""
    this._render()
  }

  _changeField(day, slot, field, value) {
    if (!this._draft || this._busy || this._stale) {
      return
    }
    const original = this._draft.days[day][slot]
    const baseline = field === "temperature"
      ? Number(nativeTemperature(original.temperature, this._draft.unit))
      : field === "time" ? original.time.slice(0, 5) : original.active
    const key = `${day}:${slot}`
    const patch = { ...(this._changes.get(key) || { day, slot }) }
    if (value === baseline) {
      delete patch[field]
    } else {
      patch[field] = value
    }
    if (Object.keys(patch).length === 2) {
      this._changes.delete(key)
    } else {
      this._changes.set(key, patch)
    }
    this._preview = null
    this._message = ""
    const preview = this.shadowRoot.querySelector("#preview-result")
    if (preview) {
      preview.innerHTML = ""
    }
    this._updateControls()
  }

  _patches() {
    return [...this._changes.values()].sort((left, right) => (
      NATIVE_DAYS.indexOf(left.day) - NATIVE_DAYS.indexOf(right.day) || left.slot - right.slot
    )).map((patch) => ({ ...patch }))
  }

  _draftValid() {
    return this._patches().every((patch) => (
      (patch.temperature === undefined || Number.isFinite(patch.temperature)) &&
      (patch.time === undefined || /^([01]\d|2[0-3]):(00|15|30|45)$/.test(patch.time))
    ))
  }

  _isCurrent() {
    const state = this._hass?.states[this._config.entity]
    return !this._deviceError() && nativeScheduleValid(state) && state.attributes.schedule_hash === this._draft?.hash
  }

  _deviceError() {
    const sensorDevice = this._hass?.entities?.[this._config?.entity]?.device_id
    const climateDevice = this._hass?.entities?.[this._config?.climate_entity]?.device_id
    if (!sensorDevice || !climateDevice) {
      return "Entity registry metadata is unavailable. Check the card's sensor and thermostat entities before editing."
    }
    if (sensorDevice !== climateDevice) {
      return "The configured schedule sensor and thermostat belong to different Home Assistant devices. Correct climate_entity before editing."
    }
    return ""
  }

  async _callAction(dryRun) {
    if (!this._draft || this._busy || this._stale || !this._draftValid()) {
      return
    }
    if (!this._isCurrent()) {
      this._stale = true
      this._staleMessage = this._deviceError() || "The stored schedule changed or became unavailable while editing. Refresh from Home Assistant before saving."
      this._updateControls()
      return
    }
    if (!dryRun && !this._preview) {
      return
    }
    this._busy = true
    this._message = dryRun ? "Preparing preview…" : "Saving stored program…"
    this._updateControls()
    try {
      const result = await this._hass.callWS({
        type: "call_service",
        domain: NATIVE_DOMAIN,
        service: "set_native_schedule",
        target: { entity_id: this._config.climate_entity },
        service_data: {
          changes: this._patches(),
          expected_hash: this._draft.hash,
          temperature_unit: this._draft.unit,
          dry_run: dryRun,
        },
        return_response: true,
      })
      const wrapped = result?.response ?? result
      const response = wrapped?.[this._config.climate_entity] ?? wrapped
      if (!response || response.dry_run !== dryRun || response.baseline_hash !== this._draft.hash) {
        throw new Error("Unexpected action response. Refresh the stored program before trying again.")
      }
      if (dryRun) {
        if (response.temperature_unit !== "C" || response.time_basis !== "thermostat_local" || !nativeScheduleValid({
          state: "stored",
          attributes: { days: response.days, schedule_hash: response.schedule_hash },
        })) {
          throw new Error("The preview did not include a complete resulting program in native units.")
        }
        this._preview = response
        this._message = response.changed ? "Preview ready. Save uploads the stored program and preserves the operating mode." : "No changes. Saving will make no thermostat write."
        const preview = this.shadowRoot.querySelector("#preview-result")
        if (preview) {
          preview.innerHTML = this._previewHtml(response)
        }
      } else {
        if (response.changed && !response.verified) {
          throw new Error("Cloud readback did not confirm the stored program.")
        }
        this._draft = null
        this._preview = null
        this._stale = false
        this._changes.clear()
        this._message = !response.changed
          ? "No changes; no thermostat write was needed."
          : response.verified ? "Stored program saved and confirmed by cloud readback. Physical device acknowledgement is not measured."
            : "Cloud readback did not confirm the stored program. Refresh before another save."
        this._render()
      }
    } catch (error) {
      this._preview = null
      this._message = error?.message || String(error)
      const preview = this.shadowRoot.querySelector("#preview-result")
      if (preview) {
        preview.innerHTML = ""
      }
      if (!dryRun) {
        this._stale = true
        this._message += " No automatic retry was made. Refresh before another save."
        this._staleMessage = this._message
      }
    } finally {
      this._busy = false
      this._updateControls()
    }
  }

  _previewHtml(response) {
    const rows = this._patches().flatMap((patch) => {
      const original = this._draft.days[patch.day][patch.slot]
      const candidate = response.days[patch.day]?.[patch.slot]
      if (!candidate) {
        return [`<tr><td colspan="4">Preview missing ${nativeEscape(patch.day)} event ${patch.slot + 1}.</td></tr>`]
      }
      return ["time", "temperature", "active"].filter((field) => patch[field] !== undefined).map((field) => {
        const show = (event) => field === "temperature"
          ? `${nativeTemperature(event.temperature, this._draft.unit)} °${this._draft.unit} (${event.temperature} °C)`
          : field === "active" ? event.active ? "Active" : "Inactive" : `${event.time.slice(0, 5)}${event.next_day ? " (next day)" : ""}`
        return `<tr><td>${nativeEscape(patch.day)} · ${patch.slot + 1}${candidate.next_day ? " (next day)" : ""}</td><td>${field}</td><td>${nativeEscape(show(original))}</td><td>${nativeEscape(show(candidate))}</td></tr>`
      })
    }).join("")
    return `<h4>Resulting changes</h4>${rows ? `<table><thead><tr><th>Day · event</th><th>Field</th><th>Before</th><th>After</th></tr></thead><tbody>${rows}</tbody></table>` : "<p>The stored program is unchanged.</p>"}<p class="hint">${response.limits_checked ? "Device time limits checked." : "Preview uses cached data. Device time limits will be checked on Save."}</p>`
  }

  _updateControls() {
    const status = this.shadowRoot.querySelector("#status")
    if (status) {
      status.textContent = this._stale
        ? this._staleMessage
        : this._draft && !this._draftValid() ? "Use quarter-hour times (00, 15, 30, 45) and a numeric temperature." : this._message
    }
    const preview = this.shadowRoot.querySelector("#preview")
    const save = this.shadowRoot.querySelector("#save")
    if (preview) {
      preview.disabled = this._busy || this._stale || !this._draftValid()
    }
    if (save) {
      save.disabled = this._busy || this._stale || !this._preview || !this._draftValid()
    }
    for (const button of this.shadowRoot.querySelectorAll("#refresh, #edit, #day")) {
      button.disabled = this._busy
    }
    for (const input of this.shadowRoot.querySelectorAll("input")) {
      input.disabled = this._busy || this._stale || input.dataset.locked === "true"
    }
  }

  _render() {
    if (!this._config || !this._hass) {
      return
    }
    const state = this._hass.states[this._config.entity]
    const title = this._config.title || state?.attributes?.friendly_name || "Native thermostat schedule"
    const deviceError = this._deviceError()
    if (!this._draft && deviceError) {
      this.shadowRoot.innerHTML = `<ha-card><h3>${nativeEscape(title)}</h3><p>${nativeEscape(deviceError)}</p></ha-card>`
      return
    }
    if (!this._draft && !nativeScheduleValid(state)) {
      this.shadowRoot.innerHTML = `<ha-card><h3>${nativeEscape(title)}</h3><p>Stored program unavailable. Select this thermostat's native schedule sensor.</p></ha-card>`
      return
    }
    const days = this._draft?.days || state.attributes.days
    const unit = this._draft?.unit || nativeUnit(this._hass)
    const offset = this._draft ? this._draft.offset : state.attributes.timezone_offset
    const options = NATIVE_DAYS.map((day) => `<option value="${day}"${day === this._day ? " selected" : ""}>${day[0].toUpperCase() + day.slice(1)}</option>`).join("")
    const rows = days[this._day].map((event, slot) => {
      const patch = this._changes.get(`${this._day}:${slot}`) || {}
      const time = patch.time ?? event.time.slice(0, 5)
      const temperature = patch.temperature ?? nativeTemperature(event.temperature, unit)
      const active = patch.active ?? event.active
      const nextDay = event.next_day ? patch.time === undefined ? " (next day)" : " (was next day; see preview)" : ""
      return this._draft
        ? `<tr><th scope="row">${slot + 1}</th><td><span class="field-label" aria-hidden="true">Start</span><input aria-label="Event ${slot + 1} time" data-slot="${slot}" data-field="time" type="time" step="900" value="${nativeEscape(time)}">${nextDay}</td><td><span class="field-label" aria-hidden="true">Temperature</span><span class="temperature-control"><input aria-label="Event ${slot + 1} temperature in ${unit}" data-slot="${slot}" data-field="temperature" type="number" step="${unit === "F" ? "1" : "0.5"}" value="${nativeEscape(temperature)}"><span>°${unit}</span></span></td><td class="schedule-active"><label class="active-control"><input aria-label="Event ${slot + 1} active" data-slot="${slot}" data-field="active" data-locked="${slot === 0}" type="checkbox"${active ? " checked" : ""}${slot === 0 ? " disabled" : ""}><span${slot === 0 ? "" : ' class="field-label"'}>${slot === 0 ? "Always active" : "Active"}</span></label></td></tr>`
        : `<tr><th scope="row">${slot + 1}</th><td>${nativeEscape(time)}${nextDay}</td><td>${nativeEscape(temperature)} °${unit}</td><td>${active ? "Active" : "Inactive"}</td></tr>`
    }).join("")
    this.shadowRoot.innerHTML = `<style>
      :host { display: block; min-width: 0 }
      ha-card { padding: 16px; box-sizing: border-box; width: 100%; min-width: 0 }
      h3 { margin: 0 0 12px }
      p { line-height: 1.4 }
      .hint { color: var(--secondary-text-color); font-size: 0.85em }
      table { width: 100%; border-collapse: collapse }
      th, td { padding: 8px 4px; text-align: left; border-bottom: 1px solid var(--divider-color) }
      input, select, button { font: inherit; color: var(--primary-text-color); background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 4px; padding: 6px; box-sizing: border-box; min-width: 0; max-width: 100% }
      input[type=number] { width: 5em }
      .field-label { display: none }
      .temperature-control { display: inline-flex; align-items: center; gap: 4px }
      .active-control { display: inline-flex; align-items: center; gap: 4px }
      button { cursor: pointer; margin: 12px 8px 0 0 }
      button:disabled { opacity: 0.5; cursor: default }
      #status { min-height: 1.5em }
      #preview-result { overflow-x: auto }
      @media (max-width: 420px) {
        th, td { padding: 6px 2px }
        input[type=number] { width: 4em }
        .schedule-table.is-editing, .schedule-table.is-editing tbody { display: block }
        .schedule-table.is-editing thead { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap }
        .schedule-table.is-editing tbody tr { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; padding: 12px 0; border-bottom: 1px solid var(--divider-color) }
        .schedule-table.is-editing tbody th, .schedule-table.is-editing tbody td { padding: 0; border: 0; min-width: 0 }
        .schedule-table.is-editing tbody th, .schedule-table.is-editing .schedule-active { grid-column: 1 / -1 }
        .schedule-table.is-editing tbody th::before { content: "Event " }
        .schedule-table.is-editing .field-label { display: block; margin-bottom: 4px; color: var(--secondary-text-color); font-size: 0.85em }
        .schedule-table.is-editing input[type=time] { width: 100%; min-height: 44px }
        .schedule-table.is-editing .temperature-control { display: flex }
        .schedule-table.is-editing input[type=number] { width: 100%; flex: 1 1 0; min-height: 44px }
        .schedule-table.is-editing .active-control { display: flex; min-height: 44px }
      }
    </style><ha-card>
      <h3>${nativeEscape(title)}</h3>
      <p class="hint">Times follow the thermostat's local clock. Reported UTC offset: ${nativeEscape(offset || "unknown")}. Daylight-saving behavior is not established here.</p>
      <label>Day <select id="day">${options}</select></label>
      <table class="schedule-table${this._draft ? " is-editing" : ""}"><thead><tr><th>Event</th><th>Start</th><th>Temperature</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table>
      <p class="hint">Six stored slots per day. Edit times in 15-minute steps. Event 1 stays active. Inactive slots retain their settings. Editing the program does not activate schedule mode.</p>
      ${this._draft ? '<button id="preview">Preview changes</button><button id="save" disabled>Save stored program</button><button id="refresh">Refresh / discard draft</button>' : '<button id="edit">Edit stored program</button>'}
      <p id="status" role="status" aria-live="polite"></p><div id="preview-result"></div>
    </ha-card>`
    this.shadowRoot.querySelector("#day").addEventListener("change", (event) => {
      this._day = event.target.value
      this._render()
    })
    this.shadowRoot.querySelector("#edit")?.addEventListener("click", () => this._startEdit())
    this.shadowRoot.querySelector("#refresh")?.addEventListener("click", () => this._discard())
    this.shadowRoot.querySelector("#preview")?.addEventListener("click", () => { void this._callAction(true) })
    this.shadowRoot.querySelector("#save")?.addEventListener("click", () => { void this._callAction(false) })
    for (const input of this.shadowRoot.querySelectorAll("input")) {
      input.addEventListener("input", (event) => {
        const target = event.target
        const field = target.dataset.field
        const value = field === "active" ? target.checked : field === "temperature" ? target.value === "" ? NaN : Number(target.value) : target.value
        this._changeField(this._day, Number(target.dataset.slot), field, value)
      })
    }
    if (this._preview) {
      this.shadowRoot.querySelector("#preview-result").innerHTML = this._previewHtml(this._preview)
    }
    this._updateControls()
  }
}

if (!customElements.get("ojmicroline-native-schedule-card")) {
  customElements.define("ojmicroline-native-schedule-card", OJMicrolineNativeScheduleCard)
}
window.customCards = window.customCards || []
if (!window.customCards.some((card) => card.type === "ojmicroline-native-schedule-card")) {
  window.customCards.push({
    type: "ojmicroline-native-schedule-card",
    name: "OJ Microline native schedule",
    description: "Preview and edit the weekly program stored on a WG4 thermostat.",
  })
}
