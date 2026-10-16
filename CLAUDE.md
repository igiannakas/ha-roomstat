# CLAUDE.md – project instructions

## Project

**Roomstat** (domain `roomstat`, repo `igiannakas/ha-roomstat`): a Home Assistant
custom integration (HACS) that runs a proxy thermostat for each Tado radiator
thermostat (TRV). The main target is Tado V3+, ideally through HA's HomeKit
Device integration (local, no API quota); Tado X works too. Control on an
external room sensor: full-power heat-ups with a predicted stop, a
feedforward + PI hold with a firm stop ahead of an overshoot, and background
auto-tune.

Roomstat started as a fork of
[kinimodb/ha-tadox-proxy](https://github.com/kinimodb/ha-tadox-proxy) (MIT).
The README describes only what Roomstat changes and points to upstream for
everything else. Keep it that way.

## Language

English everywhere: talking to the user, code, comments, commits and docs.

## Working rules

- Make changes on `main`, in the working tree. **Do not commit or push.** The
  owner reviews the changes and commits them with GitHub Desktop.
- No feature branches and no pull requests.
- Before handing over, all tests and the linter must pass:
  `python -m pytest tests/ -q` and `ruff check .`
- End with a ready-to-use commit title: `feat: …`, `fix: …`, `docs: …` or
  `refactor: …`, with `!` for breaking changes (`feat!: …`).

## Architecture

```
HA-free (testable directly):        HA bridge:
parameters.py → regulation.py       climate.py (entity, mixin composition)
parameters.py → autotune.py         ├─ climate_regulation.py (RegulationMixin)
climate_controllers.py              ├─ climate_presets.py (PresetMixin)
                                    ├─ climate_schedule.py (ScheduleMixin)
                                    ├─ climate_summer.py (SummerMixin)
                                    └─ __init__.py (coordinator), config_flow.py
```

Build new logic in the HA-free modules first (`parameters.py`,
`regulation.py`, `autotune.py`, `climate_controllers.py`), then wire it into
the HA modules.

### Key files

- `parameters.py`: all defaults (RegulationConfig, PresetConfig,
  CorrectionTuning, BehaviourConfig, AutotuneConfig).
- `regulation.py`: the controller (modes heat / coast / hold / stop).
- `autotune.py`: background tuning of Kp, Ki and the braking time Td.
- `climate_controllers.py`: window, presence, summer and schedule state
  machines, restart persistence helpers, `choose_trv` (configured TRV or
  its fallback), `trv_on_schedule`, the fail-cold helpers (timer overlays,
  watchdog test, `OutOfBandMonitor`), and the config entry option
  migration.
- `climate.py`: the ClimateEntity (properties, lifecycle, options).
- `climate_regulation.py`: regulation cycle, rate limiting, TRV commands,
  feeding the auto-tuner.
- `climate_presets.py`: presets, boost timer, window and presence actions.
- `climate_schedule.py`: follows an `input_select` schedule helper, with
  manual overrides that expire.
- `climate_summer.py`: summer lock (5 °C, service calls refused, TRV held).
- `number.py`: preset temperatures. `sensor.py`: boost remaining, schedule
  override remaining, auto-tune sensors. `button.py`: resume schedule, reset
  auto-tune. `binary_sensor.py`: sensor degraded, out of band.
- `config_flow.py`: setup and the options flow (sections).
- `repairs.py`: repairs for entity settings whose entity was deleted, renamed
  or disabled, with a fix flow to pick a replacement.
- `card.py` + `www/roomstat-card.js`: the dashboard card
  (`custom:roomstat-card`). Served by `async_setup` and added as a dashboard
  resource (a frontend module when resources are in YAML). It registers only
  after `home-assistant` is defined (scoped registry), and reads
  `boost_duration_min` and `source_entity_id` from the climate attributes.
- `const.py`: DOMAIN, option keys, custom preset names, `safe_float()`.
- `strings.json` + `translations/` (en, de): UI texts. Keep `strings.json`
  and `en.json` identical, and add new keys to `de.json` too.
- `manifest.json`: version and metadata.

## Regulation concepts

Read this before changing `regulation.py` or `parameters.py`.

- **The Tado needs strong signals.** Recorder data (Oct 2026, V3+ over
  HomeKit, heating % from the cloud): the valve is fully open only when its
  setpoint is ≥ ~5 °C above its own reading; at +1..+3 °C it throttles and
  pulses (30–40 % average in the living room), because its controller
  anticipates on its own reading, which climbs fast next to the radiator.
  It stays shut only at ≤ ~−2 °C; just below its reading it re-opens as the
  radiator cools.
- **Feedforward** compensates the TRV's placement bias (radiator vs room)
  in hold: `command = setpoint + (trv_reading − room) + Kp·error + I`.
- **Loop gain is (1 + Kp).** The feedforward cancels the TRV's own reading, so
  the Tado controller acts on `(1 + Kp)·error + I`. Kp = 0 does not mean "no
  proportional action".
- **Modes** (one controller since 2.2; config names in `RegulationConfig`):
  - `heat` (maximum): a deficit ≥ `heatup_enter_c` (1 °C). Ends when the
    coast is predicted to land within `heatup_exit_c` (0.1 °C): error −
    Td × rise ≤ 0.1, Td = the braking time (default 10 min, 0 = no lead).
  - `coast` (minimum): after a heat-up, while the room is below the target
    and still rising.
  - `hold`: the feedforward + PI command. Kp is the gain near the target:
    high (default 4) because a Tado answers small pushes late and partly.
    The integral only moves in hold.
  - `stop` (minimum): a firm stop ahead of an overshoot, once error −
    max(`stop_lead_s` (10 min), Td) × rise ≤ −`stop_over_c` (0.1 °C): the
    radiator's coast is the same however the heat got there (11 Oct: a
    0.6 °C step handled by hold got a 10-min look-ahead while the coast was
    19 min, +0.5 °C peak). Released once
    error + `release_lead_s` (20 min) × fall ≥ −`release_c` (0.02 °C), so
    the radiator is warm again in time.

  Everything but hold is a strong command: it skips the rate limit once
  (not on retries). A reload resumes from what the TRV holds
  (`resume_mode`: maximum → heat, minimum → coast). A heat-up, or any
  maximum the TRV holds, is ended (coast) when the room sensor is lost.
  Why this shape (simulated in the fitted rooms below, valve modulating or
  pulsing): only 25/5 (2.1's first version) can't dose, so near the target
  it is a relay (0.06–0.12 °C RMS); the high-gain hold with the firm stop
  roughly halves that. The old PI (Kp 0.8 with a derivative brake and gain
  scheduling, removed in 2.2) held 0.19–0.27 °C. Only a high-gain PI,
  without full-power heat-ups, overshoots +0.6 °C in rooms with quicker
  radiators. Without the firm stop the valve keeps heating just below its
  reading (+0.3 °C peaks). Tried and dropped: heating at target + 1.5 °C
  instead of 25 (slower, not tighter), stopping at the target itself (2–3×
  the commands), cancelling the Tado's own anticipation (+40–50 % commands
  for ~5 %), Kp above 4 (commands rise fast for little gain), a derivative
  term in hold (the firm stop anticipates).
- **Predictions are one-sided.** The heat-up end and the stop use the rise
  rate only while the room rises (slope deadband 0.1 °C/h); only the
  release looks at a fall, and it only hands back to hold. A falling reading
  (window, glitch) must never add heat. No slope (sensor degraded): no
  prediction. **Never make them two-sided.**
- **Stock values come from real rooms.** `tests/plant_sim.py`
  `LIVING_ROOM_FIT` / `SECOND_BEDROOM_FIT` were fitted to the morning of
  11 Oct 2026 by replaying the real valve setpoints and fitting room,
  radiator, TRV and Tado-controller parameters to the room sensor, TRV
  reading and cloud heating % (RMS 0.14 / 0.08 °C). Findings: a strongly
  non-linear valve (30 % open ≈ 50–65 % flow), warm radiator air reaching
  the room sensor (+0.5 / +1.3 °C at full output), radiator lag 30–35 min,
  and the Tado anticipating on its own reading (up to 13 min). In those
  rooms and two generic ones, at 5 and 11 °C outdoors, with the valve
  modulating or pulsing (opening fully for part of a 20-min cycle) and
  auto-tune on, the stock values hold 0.04–0.08 °C RMS on average,
  heat-up overshoot ≤ ~0.2 °C, 45–100 commands a day at 180 s / 0.3 °C.
  Sunny days overshoot with every variant (nothing can cool). Re-check
  against these presets when changing them (`tests/test_control.py`).
- **Integral deadband (0.3 °C):** the integral only accumulates near the
  target, and decays outside it, so a heat-up never winds it up.
- **Anti-windup:** no integration outside hold or while the command is
  saturated, plus the deadband decay above. The integral is clamped to
  ±2 °C.
- **Command clamp 5–25 °C.** Tado V3+ rejects anything above 25 (upstream
  sent up to 30, so V3+ rooms never heated). Clamp in the regulator, not at
  send time, so anti-windup stays honest.
- **Options migration:** the config entry's `MINOR_VERSION` (2) marks the
  option layout. `__init__.py` `async_migrate_entry` converts older entries
  (`migrate_options_v1_to_v2`: obsolete keys out, Kp → 4, braking time 0 →
  10 min), and the options flow drops `OBSOLETE_OPTIONS` on save. When an
  option changes meaning or goes, bump the minor version and migrate.
- **Rate limit:** default 180 s between commands to save TRV batteries. Don't
  lower the default.
- **TRV fallback** (option `fallback_entity_id`): a second climate entity for
  the same room, typically the Tado cloud thermostat behind a HomeKit valve.
  Each cycle uses the configured TRV whenever it gives a reading, otherwise
  the fallback (`choose_trv`); readings, commands, HVAC on/off and the summer
  lock all go through the one chosen. On a switch the last sent setpoint is
  forgotten, so the next command is compared with what the newly used entity
  holds. On the fallback the auto-tuner keeps learning from the room sensor,
  but gets no TRV reading (it can be minutes old from cloud polling), so the
  TRV demand is unknown for those cycles. Each cloud command costs Tado API
  calls, so the fallback is never the default path.
- **Prefer the fallback** (option `prefer_fallback`, off by default): for an
  owner whose local link drops out more than the cloud (and who has the API
  quota, Auto-Assist), the roles swap in `choose_trv`: the fallback whenever
  it reads (or when neither does), the configured TRV only while the
  fallback is down. A fallback reading keeps `is_fallback` (slower drift
  grace) either way. The auto-tuner still gets the configured TRV's own
  reading while it is available (the same valve, pushed at once over
  HomeKit), even when the commands go through the fallback. Leaving the
  preferred entity is logged as a warning, coming back as info.
- **Cloud TRVs** (`CLOUD_TRV_PLATFORMS`, today `tado`): the coordinator marks
  each TRV entity from a cloud integration (`<prefix>_cloud`, from the entity
  registry), and `TrvReading.slow` (fallback or cloud) picks the 6-min drift
  grace. So a room can run on the Tado cloud thermostat alone, with HomeKit
  disabled (owner, Oct 2026: HomeKit was suspected of locking up the
  bridge). Then the cloud reading also feeds the auto-tuner, as the only
  one there is: the demand crossing comes from the command step, which the
  command's own refresh shows at once. The TRV can be changed in the options
  flow (TRV section); it is written to the entry data and unique ID together
  with the options (`change_trv`, one reload), and a fallback that becomes
  the TRV is removed with "prefer the fallback".
- **Command spacing:** one `CommandPacer` for all rooms keeps TRV commands at
  least `TRV_COMMAND_SPACING_S` (5 s) apart. All rooms run their cycle on the
  same minute after a restart, and all V3+ valves share one bridge.
- **Lost commands:** if the TRV still reports a different setpoint
  `TRV_DRIFT_GRACE_S` (2 min; 6 min on a fallback or cloud TRV) after a command, the
  command is sent again (`trv_setpoint_drifted`). This covers a command lost
  on the way and a Tado schedule block that ended the override, so blind
  overlay refreshes are not needed with HomeKit (`overlay_refresh_s` 0).
  A valve that is off while the last command asked for heat (switched off in
  the Tado app) counts as drifted too, and so does a valve back on its
  schedule (see "Failure handling").

## Failure handling

Read this before changing the send path or the cycle's early returns. The
loop must keep running, and the TRV must never be left on a command nobody
is updating.

- **Valve off:** a HomeKit Tado valve that is off has no setpoint, and HA
  accepts only 7–35 °C for it (5–25 °C in heat); a lower command is refused.
  A frost-level command (≤ `min_target_c`) to an off valve counts as already
  at target, and every command is clamped to the TRV's current
  `min_temp`/`max_temp` (`command_limits`). In summer mode off counts as
  locked. The coordinator reads min/max/off with `read_trv`.
- **Commands never raise:** `_async_call_trv` catches everything except
  cancellation. A failure is logged once per distinct error (an unexpected
  exception type keeps its traceback), shown as `last_command_error`, and
  the reason becomes `send_failed(…)`. A failed command still counts for the
  rate limit, so an outage costs one attempt per interval. The baseline stays
  at what the TRV last took, so the next cycle sends again.
- **Off stays off:** a failed OFF keeps Roomstat off (no revert). While off,
  the cycle sends OFF again if the TRV is not off, at most once per rate-limit
  interval. OFF → HEAT runs the regulation even if the mode change fails;
  `set_temperature` carries `hvac_mode: heat` anyway.
- **Room sensor lost:** the room sensor is meant to be reliable (owner's
  decision), so a gap is only bridged with the last value for the sensor
  grace period, counted from the first cycle without a reading. After that
  the TRV keeps its last setpoint, except a maximum (heat, or a hold clamped
  at 25), which is ended; the loss is logged once, and after
  `ROOM_SENSOR_UNAVAILABLE_REPORT_S` (15 min) it becomes a repair. Don't
  hold the room on the TRV's own reading: the Tado offsets (−5 to −9.9 °C
  here) and the radiator's heat make its relation to the room swing by
  10 °C, so any fixed "target + offset" overheats some rooms.
- **Pauses and the clock:** the integral covers at most
  `MAX_INTEGRATION_STEP_S` (2 cycles) per cycle (`integration_step`). A last
  command time in the future (clock went back) is reset, so commands are not
  blocked until the clock catches up.
- **Faulty readings:** room and TRV readings outside −30…60 °C are treated as
  missing (`plausible_c`).
- **Fail cold (timer overlays):** a maximum command left behind by a dead
  HA, network or cloud kept the valve open (plan H2). With `heat_timer_s` > 0
  (TRV option, default 0: the safe landing needs the Tado app schedule at
  5 °C or Off, which Roomstat cannot check), a maximum (≥ the TRV's max −
  0.05, any mode) to a cloud TRV is `tado.set_climate_timer` {temperature,
  time_period}; everything else, 5 °C included, stays Manual. Renewed at
  half life while the target is still the maximum (strong: skips the rate
  limit once); with the start unknown (reload, TRV switch, clock back) at
  once. At least `HEAT_TIMER_MIN_S` (900 s: shorter only costs renewals).
  Simulated kill mid heat-up (3600 s): valve shut 3,660 s after the last
  command, peak 20.1 / 19.3 °C vs 26.2 / 23.4 °C Manual; running, the room's
  path is unchanged and renewals add 1–4 commands/day.
- **A TRV back on its schedule** (hvac_mode `auto`, or a reading without a
  setpoint; `trv_on_schedule`, `TrvReading.scheduled`, kept apart from
  `off`) holds no command of ours: a timer ran out or the app ended it. An
  Off block shows `auto` with no setpoint, which the drift check used to
  ignore, stalling a heat-up shut. Now a heat command is re-sent (as a timer
  when enabled); a frost-level one counts as done while the schedule keeps
  the valve shut (no setpoint, or a block at the floor) and is re-sent
  against a block that heats. An unknown setpoint with no reading is still
  not drift.
- **Watchdog** (own `async_track_time_interval` callback, never the
  cycle's; `cycle_watchdog_s` 600 s, deliberately not an option): no cycle
  reached its decision (regulated, summer lock, off, or the room-sensor
  pause with the TRV readable) for 10 min → one warning, attribute
  `regulation_stalled`, and if the last command the TRV took was the maximum
  the minimum is sent (strong; retried per rate-limit interval), only to a
  valve that gives a reading and still shows the maximum (a 7 °C command
  would turn an off HomeKit valve on).
- **Out-of-band sensor** (`binary_sensor.<room>_out_of_band`, reporting
  only): while heating, no summer lock, no window open and a fresh room
  reading. Hot: room ≥ max(setpoint, `min_since`) + 0.5 °C for 30 min,
  `min_since` = the lowest reading since the setpoint was last lowered
  (persisted; the first reading when nothing is saved). The 11 Oct setback
  (20 → 18 at 21:30, still 18.66 at 06:00) never fires; "setpoint + 0.5"
  alone was on all night. Cold: 0.5 °C below for 120 min, restarted by every
  setpoint rise. `since` is set only while on.
- **Entity gone:** every coordinator update checks the entity settings (TRV,
  room sensor, fallback, window, presence, summer, schedule). One with
  neither a state nor a registry entry (deleted, renamed), or disabled in the
  registry, for `ENTITY_MISSING_REPORT_S` (5 min, only once HA is running)
  becomes a repair (`repairs.py`; error for the TRV and room sensor, warning
  otherwise). A registered entity without a state (still loading,
  unavailable) is not a problem. The fix flow writes the replacement (the
  TRV into the entry data, the rest into the options; empty removes an
  optional setting), and the update listener reloads the room. Repairs are
  not persistent, clear themselves when the entity is back, and are removed
  with the room. A room sensor that exists but has no usable value for 15
  min is reported too (`entity_unavailable`, no fix flow).

## Auto-tune

Read this before changing `autotune.py` or `AutotuneConfig`.

- The radiator keeps heating after its demand turns negative (the "coast").
  The demand crosses zero at the `coast` command, so the measured coast is
  the radiator's own (about 0–20 min, short where warm radiator air reaches
  the sensor; 30–60 min under the old weak PI, when the TRV's integrator
  unwound slowly), and Td, the heat-up lead, is learned from it. A demand ≥
  `heatup_reheat_demand_c` after the crossing (a top-up, e.g. the hold
  picking up a shortfall) ends the episode: counting the top-up as coast
  inflated Td, which ended the next heat-up earlier, needing another top-up
  (a loop the simulator showed: Td 28 → 44 min in five days).
- Kp bounds (`kp_min`/`kp_max` 0.5–8, and 0.25–2× the configured value)
  are for the hold gain. In practice Kp only moves down (on oscillation):
  it moves up only on stalls with no braking time set.
- The tuner learns only from everyday episodes, never from test signals:
  - **heat-ups** (setpoint step ≥ 0.5 °C with the room ≥ 0.4 °C below) give
    the dead time θ (tangent method), the heating rate, the coast and the
    overshoot. θ and the rate need a rise ≥ 0.6 °C (`theta_min_step_c`,
    `rate_min_step_c`): with 0.8, mild nights (living room, 15 Oct 2026:
    0.75 °C to climb) never gave a θ, so the SIMC Ki never came. Simulated
    (four rooms, five outdoor temperatures): no change in 23 of 24 cases;
    the generic living room at 15 °C went 0.061 → 0.033 °C RMS (its learned
    θ brings the SIMC Ki, 0.003 → 0.0005). Tried and dropped (Oct 2026):
    judging swinging hold windows that heated < 20 % of the time — it calms
    badly swinging rooms (0.31 → 0.14 °C RMS) but made moderately swinging
    ones worse (up to +0.03 °C RMS);
  - **holds** (steady setpoint, valve actually heating) give offset and
    oscillation.

  Heat-ups cut short by the schedule are kept when they carry enough
  information.
- **SIMC is used only for Ti** (= 4(τc + θ), τc = 1.5 θ), as a basis for Ki,
  and as the justification for Td ≈ coast. **SIMC's Kc is not used:** the only
  gain estimate (rate / demand) is self-referential, because the demand is
  proportional to Kc. In simulation it ratcheted Kp down to its floor. Kp moves
  only on evidence: down on oscillation, up on repeated stalls short of the
  target.
- **Safety:**
  - hard bounds, plus bounds relative to the configured values;
  - small steps and dead-bands;
  - rate limits (3 h gentler, 12 h "more heat");
  - two agreeing episodes needed for "more heat";
  - a watchdog rolls back a "more heat" change if overshoot rises or an
    oscillation appears. There is no freeze after a rollback and no blocked
    moves (owner's decision);
  - emergency detune;
  - any tuner exception falls back to the configured values.
- Learned values live **only** in the restore-state extra data (key
  `autotune`). Never write them to the config entry options: that would
  reload the integration.
- A heat-up in progress and the previous cycle's setpoint are saved there too
  (`episode`), because changing a preset temperature reloads the entry. A
  restart is then just a gap between cycles. A gap longer than `max_gap_s`
  (10 min: a long restart, or a HomeKit valve that drops off, which stops the
  cycle feeding the tuner) ends the heat-up early like a schedule change: what
  came before is kept as a cut-short episode, which can only ever argue for
  more braking. Hold windows are not saved.
- All limits live in `AutotuneConfig` (`parameters.py`). `tests/plant_sim.py`
  simulates room + radiator + Tado TRV (with its own PI) + proxy in closed
  loop. Check control or auto-tune changes against it.

## Brand assets

- Home Assistant (2026.3+) loads the integration logo locally from
  `custom_components/roomstat/brand/` (`icon.png` 256×256, `icon@2x.png`
  512×512, `logo.png` 256×256). A `brand/` folder in the repo root is not read.
- The HACS store resolves icons from brands.home-assistant.io, which needs a PR
  to `home-assistant/brands` (`custom_integrations/roomstat/`).

## Tests

```bash
python -m pytest tests/ -q
ruff check .
```

- `tests/ha_harness.py` provides a minimal HA stub, so end-to-end tests run the
  real `RoomstatClimate` code (`test_frost_preset_persistence.py`,
  `test_summer_mode.py`, `test_schedule.py`, `test_autotune_climate.py`,
  `test_fail_cold_entity.py`) and the binary sensors. It needs Python ≥ 3.11
  (`asyncio.timeout`); CI uses 3.12.
- `tests/plant_sim.py` models timer overlays (`ProxySettings.heat_timer_s`,
  `PlantParams.tado_schedule_c`: 5 °C, or None for Off) and
  `RoomSim.kill()` (the proxy stops, the room and valve carry on); the
  fail-cold checks are in `tests/test_fail_cold.py`.
- Tests bypass `__init__.py` by loading modules with
  `importlib.util.spec_from_file_location`.

## Versions and releases

- Bump together: `manifest.json` → `"version"`, and the version badge in
  `README.md`.
- Release notes (GitHub release, English, Markdown):

  ```
  ## v{VERSION} – Short title

  ### New
  - …

  ### Fixes
  - …

  ### Breaking changes (if any)
  - What changed and what users need to do

  ### Installation
  Via HACS → Roomstat → Update, then restart Home Assistant.
  ```

## Documentation

- One file: `README.md`. Plain, short English.
- It lists only what Roomstat changes compared with upstream, plus how to
  install and use those changes. Point to the upstream README for everything
  else. No parameter tables.
- When a feature changes, update its line in "What's different".
- Design rationale belongs in code docstrings and in this file, not in extra
  docs.

## Known quirks

- **iOS Companion App:** `ha-entity-picker` crashes in the iOS WebView. It's an
  HA bug, not this integration. Use a browser for the options flow.
