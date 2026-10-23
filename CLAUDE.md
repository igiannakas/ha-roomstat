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
  machines (`window_recovery_wait`: the close delay runs on until the room
  has stopped re-warming by itself), restart persistence helpers,
  `choose_trv` (configured TRV or its fallback), `trv_on_schedule`, the
  last command's bookkeeping (`pending_command`, `resend_grace_s`,
  `trv_holds_command`, `CommandRecord`), the
  fail-cold helpers (timer overlays, watchdog test, `OutOfBandMonitor`),
  and the config entry option migration.
- `climate.py`: the ClimateEntity (properties, lifecycle, options).
- `climate_regulation.py`: regulation cycle, rate limiting, TRV commands,
  feeding the auto-tuner. At debug it logs one line per cycle with the
  whole decision (mode, setpoint, room and slope, the valve's reading and
  state, the command relative to that reading, P, I, send reason). The
  coordinator's own refresh chatter is on
  `custom_components.roomstat.coordinator`: set the package to debug and
  that logger to info (the owner does it with a `logger.set_level`
  automation on HA start).
- `climate_presets.py`: presets, boost timer, window and presence actions
  (the window close action polls the recovery, see "Windows" below; a
  change by hand while nobody is home, see "Presence" below). Its log lines,
  and the schedule's, name the room.
- `climate_schedule.py`: follows an `input_select` schedule helper, with
  manual overrides that expire.
- `climate_summer.py`: summer lock (valve off, service calls refused, TRV held).
- `number.py`: preset temperatures. `sensor.py`: boost remaining, schedule
  override remaining, auto-tune sensors, and the regulation trace (command to
  the valve, room error as measurement sensors — long-term statistics outlive
  the recorder's purge, 7 days on the owner's 3-GB MariaDB — and the mode as
  an enum). `button.py`: resume schedule, reset auto-tune.
  `binary_sensor.py`: sensor degraded, out of band.
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
  - `heat` (maximum): a deficit ≥ `heatup_enter_c` (0.8 °C since 2.8.2,
    owner's choice on trial; 1.0 before: a 0.8–0.9 deficit then ran as a
    hold at +4 above the valve's reading — full power, but ended by the
    stop rule instead of the Td exit and coast, and a mid-range command is
    no strong signal: the second bedroom's valve stayed idle 21 min on it,
    19 Oct 2026, while 25 °C commands are answered in 1–3 min; simulated
    0.7 vs 1.0 at 0.6–0.9 deficits: peaks lower everywhere, commands
    −5…−12 %, 14–26 min slower in the slow rooms). Ends when the
    coast is predicted to land within `heatup_exit_c` (0.1 °C): error −
    Td × rise ≤ 0.1, Td = the braking time (default 10 min, 0 = no lead).
  - `coast` (minimum): after a heat-up, while the room is below the target
    and still rising. One that reaches the target (error ≤ 0) becomes a
    stop; one that stalls short hands over to hold.
  - `hold`: the feedforward + PI command. Kp is the gain near the target:
    high (default 4) because a Tado answers small pushes late and partly.
    The integral only moves in hold.
  - `stop` (minimum): a firm stop ahead of an overshoot, once error −
    max(`stop_lead_s` (10 min), Td) × rise ≤ −`stop_over_c` (0.1 °C): the
    radiator's coast is the same however the heat got there (11 Oct: a
    0.6 °C step handled by hold got a 10-min look-ahead while the coast was
    19 min, +0.5 °C peak). Released only once the room is predicted to
    need heat: error + `release_lead_s` (20 min) × fall ≥
    `release_below_c` (0.1 °C below the target; 0.05 in 2.7.0, 0.1 since
    2.7.1 by the owner's choice: some time a little under rather than
    always a little over. Simulated: hold RMS +0.01, overshoot and
    commands unchanged, time > 0.05 under 4 → 8 % in the fitted living
    room; 0.15 doubles the RMS in the generic rooms). A falling room is
    released 20 min ahead, so the radiator is warm again in time; a flat
    one at or just above the target stays shut. **Never release above the
    target** (until 2.7 it released 0.02 above): a hold entered there
    commands 0.2–0.5 below the valve's reading, where a Tado trickles on
    its own (17 Oct 2026, three rooms, trace sensors: 16 % for 10 min,
    valve reading +2.8 °C, room +0.2…+0.4 for the next 1–2 h with the valve
    shut again). The same trickle followed every HA restart, when a shut
    valve resumed as coast and fell through to hold — hence coast → stop
    above. In the simulator the two changes are neutral (fitted rooms
    0.030 → 0.025 °C RMS, generic living room +0.01, commands unchanged);
    the simulator does not reproduce the trickle, so the live traces are
    the evidence.

  Everything but hold is a strong command: it skips the rate limit once
  (not on retries). A reload resumes from what the TRV holds
  (`resume_mode`: maximum → heat, minimum → coast, which the next cycle
  turns into stop or hold; unknown → stop, since 2.8.1). After a restart
  the TRV entity may not have loaded when the room is added
  (`_resume_pending`): the mode is then decided again from the valve on
  the first cycle that sees it, before anything is sent. 18 Oct 2026,
  right after the 2.8.0 restart: the study's valve held 5 but its entity
  had no state yet, the resume defaulted to hold, and the first cycle
  (0.07 above the target) commanded 0.3 below the valve's reading — the
  grey zone again. A heat-up, or any maximum the TRV holds, is ended
  (coast) when the room sensor is lost — also one found on the valve after
  a restart with the sensor dead.
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
  term in hold (the firm stop anticipates), a shorter slope window for the
  stop and the heat-up end (Theil–Sen 15 → 10 or 8 min, 16 Oct 2026, after
  a real stop came 7 min after the rise was visible): no change in heat-up
  overshoot or cold-start peaks in any of the four rooms, +15–30 % commands
  in the fitted living room (a noisier slope toggles hold ↔ stop), Kp below
  4 (16 Oct 2026, owner's settings, auto-tune on: Kp 0.8 doubles the hold
  RMS in the fitted rooms, 0.03 → 0.06, worst 0.08 → 0.16; Kp 2 costs
  +0.01 RMS for 10–25 % fewer commands; the heat-up overshoot is the same
  at every Kp, and a cold radiator takes full power on any push).
- **Predictions are one-sided.** The heat-up end and the stop use the rise
  rate only while the room rises (slope deadband 0.1 °C/h); only the
  release looks at a fall, and it only hands back to hold. A falling reading
  (window, glitch) must never add heat. No slope (sensor degraded): no
  prediction. **Never make them two-sided.** A reading that fell
  `fast_fall_c` (0.5 °C) since the previous cycle is a draught or a glitch:
  that cycle keeps the previous reading (`RegulationState.last_room_c`,
  `RegulationResult.room_held`, marked in the debug line), so neither a
  heat-up nor a hold push starts on it; a fall that goes on is accepted one
  cycle later, a one-sample glitch never (2.8.3; 19 Oct 2026: 19.94 → 18.92
  in one minute as a window opened, 25 °C sent 16 s before the window
  action's 5 °C).
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
  target, and decays outside it, so a heat-up never winds it up. **Ki
  default 0.001** (2.8.0; 0.003 before): with the stop released only 0.1 °C
  under the target the integral winds up while the room waits there (17 Oct
  2026: +0.2 in 9 min at 0.003) and the hold then pushes above it; 0.001
  was the owner's choice on 17 Oct for all four rooms (simulated sweep: a
  hold RMS within 0.01 of 0.003, fewer commands), and the auto-tuner's
  SIMC Ki raises it where a room's dead time asks for more. Draining or
  resetting the integral in stop was rejected (it hurts cold, high-loss
  rooms with the tuner on).
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
  schedule (see "Failure handling"). An off command is held only while the
  valve reports `off` (see "Shut is off"). A valve that keeps not showing a
  command it took (a state that stopped updating, a zone that overrides it)
  is asked again less and less often (2.8.6, `resend_grace_s`): the grace
  doubles per resend in a row, up to `RESEND_BACKOFF_MAX_S` (30 min: at 0,
  6, 18, 42 min, then every 30 on a cloud TRV; 2.8.5 sent 60 offs in 6 h to
  a zone stuck on `auto`). The back-off ends only when the valve is seen
  holding our command (`trv_holds_command`), or on an entity switch; a hold
  command creeping by 0.1 °C does not end it, and a resend of a command the
  valve had shown and then left does not add to it. The same pacing holds
  for the summer lock and Roomstat's own off.

## Failure handling

Read this before changing the send path or the cycle's early returns. The
loop must keep running, and the TRV must never be left on a command nobody
is updating.

- **Shut is off (2.8.4, owner's decision, urgent):** every command at the
  frost level (`is_off_command`: ≤ `min_target_c` + 0.05 — coast, stop, a
  hold clamped there, the frost preset, window and away shuts, the
  room-sensor-lost end of a maximum, the watchdog, the summer lock) goes to
  the TRV as `climate.set_hvac_mode off` (`_async_send_off_to_tado`; on a
  Tado cloud zone a never-ending Manual OFF overlay), never as a 5 °C
  setpoint. It is done only while the TRV reports `off`: a valve in heat at
  any setpoint (a Manual 5 °C of an older version included) or on its Tado
  schedule (`auto`; the owner: "auto isn't usable in tado, it has to be
  either heating or off") is switched off, a new off skips the rate limit
  when strong or an urgent decrease, and an off that went out gets the drift
  grace to show before it is sent again. After an off the baseline is the
  frost level (`_last_sent_setpoint`; the auto-tuner still sees a negative
  demand). **A state right after a command is not believed**
  (`pending_command`, 2.8.5, judged on the last command *tried* since
  2.8.6): for the drift grace after a heat command an "off" or "auto" may
  be the state from before it, so an off is sent at once rather than taken
  as done, and the heat command is not re-sent until the grace has passed;
  an off the TRV took is likewise given the grace before it is sent again.
  19 Oct 2026, second bedroom: the target went 20 → 22 → 20 within 10 s,
  25 °C went out, the cloud state still showed off, 2.8.4 took the off as
  done and the valve ran at 100 % until switched off by hand. 2.8.5 judged
  on the last command the TRV *took*, which left the same 25 °C for 5–6 min
  after a heat call that timed out but landed, after a fallback switch
  (which forgets the last sent setpoint), and after an entry reload (2.8.6
  review, all reproduced). So every send path (regulation, keep-off,
  summer, watchdog) books its attempt with `_note_attempt`: the command,
  whether its call succeeded, its entity, and what the sending cycle saw.
  The last heat command is kept apart (`_last_heat_attempt_*`) until the
  state shows it or an off is *taken*: a failed off does not end it (a
  second review found the stale "off" then taken as done after all,
  clearing the maximum so a coordinator outage left the 25 °C on with the
  watchdog disarmed). A failed heat call counts as pending for the plain
  grace only (it may have landed), a failed off not at all (retried at the
  rate limit). A command has been *shown* (`_note_trv_shows`) only when the
  state reads like it after having read otherwise since it was tried: a
  state that looked like it all along may be a stale one from before (an
  "off" from before the heat, a 25 °C from before the off), so it proves
  nothing; once shown, the state is current, and a later change (app, dial,
  schedule) is a new event — taken back at the rate limit, not after the
  grace, and no reason to back off. Heat from an off (or scheduled)
  valve is never "already at target", whatever setpoint it shows (Tado
  reports 5 °C while off), and is
  **one** `climate.set_temperature` with `hvac_mode: heat`: HA's Tado
  integration (2026.10.1, `tado/climate.py` async_set_temperature) and
  HomeKit's thermostat switch an off valve to heat and set the temperature
  in the same call — checked live on the second bedroom, 19 Oct 2026 (off,
  then `set_temperature 6` → heat at 6). Never `set_hvac_mode heat` first:
  Tado re-uses the last temperature HA sent (25 °C after a heat-up) until
  the second call lands, or for good if it fails. A timer overlay
  (`tado.set_climate_timer`) switches to heat by itself. Reasons:
  finding M6 (a 5 °C setpoint opens the valve once its offset-corrected
  reading nears 5: a real 11–15 °C here) and the owner's rule that a valve is
  only ever heating or off. Whether Tado's own frost protection in OFF uses
  the corrected reading is still untested (plan §5H.1). The debug line shows
  `cmd off`, and ` off` / ` auto` after the valve's reading; the command
  trace sensor shows 5.0 for off.
- **Valve off:** a HomeKit Tado valve that is off has no setpoint, and HA
  accepts only 7–35 °C for it (5–25 °C in heat); every command is clamped to
  the TRV's current `min_temp`/`max_temp` (`command_limits`). The
  coordinator reads min/max/off with `read_trv`.
- **Commands never raise:** `_async_call_trv` catches everything except
  cancellation. A failure is logged once per distinct error (an unexpected
  exception type keeps its traceback), shown as `last_command_error`, and
  the reason becomes `send_failed(…)`. A failed command still counts for the
  rate limit, so an outage costs one attempt per interval. The baseline stays
  at what the TRV last took, so the next cycle sends again.
- **Off stays off:** a failed OFF keeps Roomstat off (no revert). Switching
  Roomstat off runs a regulation cycle (2.8.6, `_async_keep_trv_off(user=True)`,
  under the regulation lock, so a cycle in progress can't overtake the off
  with a heat command), which switches the TRV off at once unless it is off
  (and no heat command of ours may be landing): one API call fewer. While
  off, a failed OFF, or one the valve showed and then left (app, dial), is
  sent again once per rate-limit interval; one the TRV took but never showed
  after the drift grace, with the back-off (see "Lost commands"). Summer
  mode starting sends its off at once the same way. OFF → HEAT forwards no
  mode change to the TRV (2.8.4): the
  regulation cycle that runs at once takes the valve out of off with one
  `set_temperature` (`hvac_mode: heat`), or leaves it off if the room needs
  no heat (`set_hvac_mode heat` would heat a Tado zone at the last
  temperature HA sent, 25 °C after a heat-up).
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
  blocked until the clock catches up; its age is unknown, so it no longer
  counts as on its way either (reset to before the longest back-off).
- **The last command across a reload (2.8.6):** every preset temperature
  change reloads the room, so the last command is kept in the restore-state
  extra data (`command`, `CommandRecord`: attempt, its time, success,
  entity and whether it was shown, the heat command on its way, the last
  setpoint the TRV took, the timer's start). It is taken
  back only when younger than the longest drift grace (360 s) and sent to
  the configured TRV or the fallback; then the rate limit carries on, the
  resume mode is decided from a command still on its way (`_held_command`:
  a 25 °C in flight resumes as heat, an off as coast), and the timer is not
  renewed needlessly. 2.8.5 forgot it: a reload 10 s after a 25 °C command
  took the stale "off" as done (2.8.6 review).
- **The room slope across a restart (2.9.1):** the heat-up end and the stop
  predict from the 15-min slope, which needs 7.5 min of readings. The
  samples ride along in the restore-state extra data (`slope`,
  `SlopeEstimator.to_dict` / `restore`) and are taken back only when they
  still join up with now (last sample within the estimator's own 300-s gap
  rule, none in the future, in order); the spike filter is primed with the
  last one. 23 Oct 2026: a restart at 20:18 left the slope unknown for 8 min
  in the middle of four heat-ups; the study and the second bedroom, due to
  stop at 20:19 (replayed), ran at full power until 20:28 (study +0.35 °C).
  A window open at startup still drops the history on the first cycle.
- **Faulty readings:** room and TRV readings outside −30…60 °C are treated as
  missing (`plausible_c`).
- **Windows (2.8.0):** while the window sensor is on, or the window
  automation is active and not yet in its close delay, the room readings are
  the draught: the slope history is dropped each cycle (`_window_airing`)
  and refilled from the close on. The configured close delay is a minimum:
  when its timer fires, `_async_window_close_action` asks
  `window_recovery_wait` and re-arms the timer every
  `WINDOW_RECOVERY_POLL_S` (60 s) while the fresh slope is unknown (6
  samples, 7.5 min) or ≥ `WINDOW_RECOVERY_RATE_C_PER_H` (1 °C/h, faster than
  any radiator here heats), up to `WINDOW_RECOVERY_MAX_S` (30 min) after
  the close; a room sensor that has never read, or is degraded, ends the
  wait at the configured delay, and a close delay of 0 skips it. Evidence
  (18 Oct 2026, four rooms): the air comes back 60–90 % within 10–20 min
  of the close (study +1.9 °C in 9 min after a 3.5 °C dip), decelerating
  over 15–40 min; at the end of a 5-min delay the slope still carried the
  dip, so a hold pushed full power for 3 min on a room rising 2.5 °C/h by
  itself (master bedroom), and the living room flapped heat → coast →
  heat on a 7–11 °C/h recovery slope. The auto-tuner starts no heat-up
  episode within `window_settle_s` (30 min) of a window (see "Auto-tune").
  The simulator has no windows; this is judged on the trace sensors.
- **Fail cold (timer overlays):** a maximum command left behind by a dead
  HA, network or cloud kept the valve open (plan H2). With `heat_timer_s` > 0
  (TRV option, default 0: the safe landing needs the Tado app schedule at
  Off, which Roomstat cannot check), a maximum (≥ the TRV's max −
  0.05, any mode) to a cloud TRV is `tado.set_climate_timer` {temperature,
  time_period}; everything else stays Manual, off included. Renewed at
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
  when enabled), and so is an off, whatever the schedule's block (2.8.4;
  until then a shut block counted as holding the frost-level command). An
  unknown setpoint with no reading is still not drift.
- **Watchdog** (own `async_track_time_interval` callback, never the
  cycle's; `cycle_watchdog_s` 600 s, deliberately not an option): no cycle
  reached its decision (regulated, summer lock, off, or the room-sensor
  pause with the TRV readable) for 10 min → one warning, attribute
  `regulation_stalled`, and if the last command the TRV took was the maximum
  the valve is switched off (strong; retried per rate-limit interval), only
  if it gives a reading and still shows the maximum (one that is off, on its
  schedule or lower is left for the regulation to take back).
- **Out-of-band sensor** (`binary_sensor.<room>_out_of_band`, reporting
  only): while heating, no summer lock, no window open and a fresh room
  reading. Hot: room ≥ max(setpoint, `min_since`) + 0.5 °C for 30 min,
  `min_since` = the lowest reading since the setpoint was last lowered
  (persisted; the first reading when nothing is saved), **and the radiator
  is the cause**: the TRV reported heating (`TrvReading.heating`, the
  entity's `hvac_action`; the Tado cloud derives it from the zone's heating
  power, HomeKit from the valve) during the excursion or within 30 min
  before it (`HEATED_LOOKBACK_S`: the heat arrives after the valve shut), or
  the TRV − room gap stands ≥ 1 °C (`RADIATOR_RISE_C`) above its lowest of
  the last 3 h (`GAP_WINDOW_S`: the radiator cold; a hot radiator lifts the
  valve's reading far more than the room, the sun lifts both). Otherwise the
  state is `warm`: attributes only, sensor off, one info log. 16 Oct 2026:
  a sunny afternoon lifted four rooms 1.5–2.3 °C with every valve at 0 %
  and produced four hot alarms under the plain rule; the replay in
  `tests/test_fail_cold.py` is warm. With nothing known about the valve
  (no `hvac_action`, no reading) hot needs no evidence. The 11 Oct setback
  (20 → 18 at 21:30, still 18.66 at 06:00) never fires; "setpoint + 0.5"
  alone was on all night. Cold: 0.5 °C below for 120 min, restarted by every
  setpoint rise. `since` is set while on or warm.
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

## Presence

Read this before changing the presence logic (`PresenceAutomationController`,
`climate_presets.py`).

- Priority, highest first: summer → open window → presence away → manual
  override → schedule. While away, the schedule and a boost ending only
  update what coming home restores.
- **A change by hand while nobody is home takes effect (2.9.0).** A
  temperature, a preset or a Boost picked by the user (`manual=True` through
  `_route_preset`, or `async_set_temperature`) calls `_presence_takeover`:
  the controller leaves `active` for `override` (`nobody_home` covers both)
  and the room is regulated as if someone were home. It lasts until someone
  is home (after the home delay, so a flicker back keeps it; nothing is
  restored, the change stays) or `presence_override_max_min` after the last
  change (option, 240 min by default, 0 = until home; each change restarts
  it). Then the away action runs again and saves the current preset, so
  coming home carries on with the change. Evidence: 23 Oct 2026, the owner
  left at 13:18 and from 18:16 to 18:19 pressed Comfort / Night / Boost ~30
  times on the way home (the heating scripts and single rooms); each press
  was only saved for the return, and the rooms got heat when he walked in at
  19:32 (17.4–19.0 °C against 20). A 60-min limit (the schedule override's)
  would have dropped back to Away 15 min before he arrived, and a Comfort
  that matches the schedule starts no schedule override at all, hence the
  separate limit.
- Leaving away mode for an override, the room first takes back the preset
  away mode had saved (as coming home would), so a Boost started then ends on
  it, never on Away. That pre-away state is kept (`_before_override`,
  persisted) for a cancel.
- **Away picked while nobody is home** is away mode itself, never a manual
  (sticky) Away: it cancels an override, the schedule override the change
  started too, and coming home restores the schedule's preset (else the
  pre-away state). **Resume schedule** while nobody is home does the same.
  Both apply the base quietly (`_apply_quietly`) before the away action, so
  no heat call is sent and taken back. Once the sensor reads home again (home
  delay running), Away is an ordinary Away picked by someone at home. Not
  changes by hand: the schedule, Off → Heat (it resumes the schedule), Heat
  when already heating (the heating scripts send it before the preset).
  Every other service call counts, an automation's or a voice assistant's
  too: a pre-heat from an automation should work the same (deliberate; the
  owner has no automation setting presets, schedules go through the helper).
- **The limit only guards against heating an empty house.** When it fires
  and the change heats no more than away would (Off, the room switched off,
  a temperature at or below the away target; with a window open, what the
  window will restore), the change stays and is looked at again after
  another limit. When it fires with the presence sensor reporting nothing
  (unavailable, unknown), the room goes back to away anyway
  (`unknown_is_away`): skipping, as a stale away timer does, left the change
  heating with no limit until the sensor next read off.
- An open window still wins: the change is saved for its close. An override
  whose limit runs out with the window open goes back to away through the
  window branch (Away saved for the close). A change by hand during the
  close delay ends the wait (`_end_window_close_delay`): the room first takes
  back what the window had saved, timer cancelled, then the change applies.
  Before, the close delay was simply cancelled, so a Boost picked then
  ended on the window's frost protection and stayed Off (also when home),
  and a temperature set then waited for the end of the re-warm wait.
- A sticky schedule override belongs to an Away or Off picked by hand. Away
  mode turns a manual Away into away mode (it never saves AWAY), so the away
  action clears the sticky override then; one still left behind (older
  versions) is cleared whenever an automation restores another preset
  (`_drop_stale_sticky_override`: presence and window restores, and at
  startup after the schedule state is loaded). It hid the schedule until the
  next change by hand, or kept a room Off for good when the schedule's own
  preset was Off when leaving.
- The override survives a restart or reload (`presence_override_*` in the
  restore data). At startup: someone home, or no sensor → it ends, the change
  stays; still nobody, or not reported yet → its limit is re-armed for the
  rest; one that ran out while HA was down fires at once and is handled like
  any expiry.
- The card shows `Nobody home · <mode> · <time left | until home>`
  (`presence_override_active`, `presence_override_until`); while nobody is
  home the Schedule button is not shown as active (pressing it goes back to
  Away). `tests/test_card_status.py` runs the card's `roomState` in Node.

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
    oscillation. "Actually heating" is `hold_min_heating_fraction` (20 %
    of the 3-h window): on a mild day the owner's rooms heat 4–7 % of the
    time and the tuner stands back, by design (see "tried and dropped"
    above). Since the stop is released only 0.1 °C under the target
    (2.7.1), a badly set room swings with the valve shut most of the time
    too, so it is only calmed on cold days (`test_calms_an_oscillating_room`
    runs at 0 °C: 21 % heating, Ki 0.01 → 0.0006 in a week; at 12 °C, 7 %
    and nothing happens).

  Heat-ups cut short by the schedule are kept when they carry enough
  information. **No episode starts within `window_settle_s` (30 min) of a
  window** (open, or in its close delay; `AutotuneSample.window`, the time
  is persisted with the episode): heating resuming after a window used to
  count as a heat-up (`resumed`), but the room's rise is its own recovery.
  18 Oct 2026, living room: the 10 min after a close gave a heating rate
  of 3.73 °C/h (real 1.4–2.2), a 5-min "coast" and an overshoot of −1.16
  (one stall vote); the study's episode would have booked its 45-min
  recovery as coast. Resuming after presence-away, summer or off still
  counts: there the room really cooled.
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
