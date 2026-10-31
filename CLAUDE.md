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
regulation.py → activity.py         ├─ climate_schedule.py (ScheduleMixin)
parameters.py → preheat.py          ├─ climate_summer.py (SummerMixin)
parameters.py → lookahead.py        ├─ climate_activity.py (ActivityMixin)
regulation.py → dosing.py           ├─ climate_preheat.py (PreheatMixin)
                                    ├─ climate_lookahead.py (LookaheadMixin)
                                    ├─ climate_dosing.py (DosingMixin)
                                    └─ __init__.py (coordinator), config_flow.py
```

Build new logic in the HA-free modules first (`parameters.py`,
`regulation.py`, `autotune.py`, `climate_controllers.py`, `activity.py`,
`preheat.py`, `lookahead.py`, `dosing.py`), then wire it into the HA
modules.

### Key files

- `parameters.py`: all defaults (RegulationConfig, PresetConfig,
  CorrectionTuning, BehaviourConfig, AutotuneConfig, LookaheadConfig,
  DoseConfig).
- `regulation.py`: the controller (modes heat / coast / hold / stop, and
  dose: `_dose_step`, the settled state and its pulses, with the pure
  helpers `dose_need_c`, `dose_ready_in_s`, `dose_fall_c_per_s`,
  `dose_floor_landing_c`, see "Dosed top-ups" below);
  `outlook()` holds the predictions a mode is decided on, which the
  activity log explains with; `lookahead_correction_c` (the valve
  look-ahead's lowering of a hold command; it stays in this module so
  regulation.py imports nothing but parameters.py, which the tests that load
  it on its own rely on).
- `lookahead.py`: the valve look-ahead's learner (`LookaheadLearner`, see
  "Valve look-ahead" below), HA-free.
- `dosing.py`: dosed top-ups, HA-free (see "Dosed top-ups" below): the
  model (`fopdt_response`, `fit_episode`), the heat in flight
  (`TopupPredictor`), the learner (`DoseLearner`), the house's pulse
  coordinator (`PulseCoordinator`), and the per-room glue the entity and
  the simulators share (`RoomDoser`: booking, the disturbance observer,
  the regulator's inputs, episodes, `summary` / `diagnostics`).
- `autotune.py`: background tuning of Kp, Ki and the braking time Td. Each
  judged episode leaves a verdict (`last_verdict`, `last_verdict_until`,
  `verdicts`): what it changed, or why it changed nothing (a vote short, on
  trial, rate limit, at the targets).
- `activity.py`: the activity log (`ActivityLog`), the decision line per
  cycle (`describe_cycle`, `decision_key`) and `RoomLogAdapter` (see
  "Activity log" below).
- `climate_controllers.py`: window, presence, summer and schedule state
  machines (`window_recovery_wait`: the close delay runs on until the room
  has stopped re-warming by itself), restart persistence helpers,
  `choose_trv` (configured TRV or its fallback), `trv_on_schedule`, the
  last command's bookkeeping (`pending_command`, `resend_grace_s`,
  `trv_holds_command`, `CommandRecord`), the
  fail-cold helpers (timer overlays, watchdog test, `OutOfBandMonitor`),
  the config entry option migration, and the options form's checks
  (`options_form_errors`, `options_form_values`: shown again after an
  error with what was entered).
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
- `preheat.py`: preheat on the way home, HA-free (see "Preheat on the way
  home" below): `PreheatController` (the decision per evaluation, the
  heading-home latch, persistence), `RecoveryLearner` (the 30-minute
  recovery), `describe` (the log line for a change of decision),
  `TIMING_OPTIONS` / `config_from_options` (the settings), and the Home
  ETA parsers. `PreheatConfig` / `RecoveryConfig` are in `parameters.py`.
- `climate_preheat.py`: preheat on the entity (`PreheatMixin`): the inputs
  (`_preheat_inputs`, `_preheat_scheduled_c`), the evaluation at the start
  of every cycle and on a Home ETA change, the learner's
  feed, persistence (`preheat`), the attributes and sensors' values.
- `climate_lookahead.py`: the valve look-ahead on the entity
  (`LookaheadMixin`): the valve reading's slope and the look-ahead for the
  regulator (`_lookahead_inputs`), releases and their answers
  (`_lookahead_after_send`, `_lookahead_observe`), persistence
  (`lookahead`, with the TRV it was learned on), the sensor's values.
- `climate_dosing.py`: dosed top-ups on the entity (`DosingMixin`): the
  house's `PULSE_COORDINATOR`, every command tried booked
  (`_dosing_note_command`), the zone's heating reported every cycle
  (`_dosing_note_zone`), the regulator's inputs (`_dosing_inputs`), the
  episodes and the pulse-end timer after the send (`_dosing_after_compute`,
  `_async_dose_wake`), the coordinated wake (`_dosing_wake`), persistence
  (`dosing`, with the TRV it was learned on), the sensor's values.
- `climate_activity.py`: the room's activity log on the entity: a decision
  line at every cycle exit, events from `self._log(_LOGGER)`, auto-tune
  verdicts, the sensor's state and attributes, persistence (`activity`).
- `number.py`: preset temperatures. `sensor.py`: boost remaining, schedule
  override remaining, auto-tune sensors, and the regulation trace (command to
  the valve, room error as measurement sensors — long-term statistics outlive
  the recorder's purge, 7 days on the owner's 3-GB MariaDB — and the mode as
  an enum), the activity log (`RoomstatActivitySensor`), preheat's
  decision (`preheat`, an enum) and 30-minute recovery (`preheat_recovery`),
  the valve look-ahead in use (`valve_lookahead`, only while its option
  is on) and the dosed top-ups' gain (`topup_dose`, only while its option
  is on).
  `button.py`: resume schedule, reset auto-tune.
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
  and `en.json` identical, and add new keys to `de.json` too. No `<word>`
  in a string: the frontend reads it as a markup tag and shows
  "Translation error: UNCLOSED_TAG" (`tests/test_translations.py`).
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
  - `coast` (minimum): after a heat-up (or a full push), while the room is
    below the target and still rising. One that reaches the target (error ≤
    0) becomes a stop; one that stalls short hands over to hold.
  - `hold`: the feedforward + PI command. Kp is the gain near the target:
    high (default 4) because a Tado answers small pushes late and partly.
    The integral only moves in hold. **A full push ends like a heat-up**
    (2.10.4, `hold_push_c` 2.5, `RegulationState.full_push`): once the
    hold's push over the valve's reading ((1 + Kp) × error + I) reaches
    2.5 °C, the valve is at or near full output and fills the radiator as
    a heat-up does, so until the room leaves the hold it coasts on the
    heat-up's exit (error − Td × rise ≤ 0.1, rising only) instead of
    waiting for the stop rule's 0.1 over. A hold entered afresh starts
    gentle, also after a reload (the flag is not kept, like the integral:
    a push still at 2.5 °C sets it again). 26 Oct 2026, second bedroom:
    0.76 below at 06:00 (just under `heatup_enter_c`), +3.1 over the valve,
    which ran at 100 %; the stop rule shut it at 06:31, the heat-up exit
    says 06:28 (Td 25 min, replayed in `tests/test_control.py`), and the
    room rose on the radiator's heat for 24 min after. 2.5 is the owner's
    choice (3.0 proposed). Simulated (four rooms, 5–12 °C, deficits
    0.4–1.2, two seeds): morning peaks +0.19…+0.22 → +0.10…+0.14 in the
    generic living room at 0.6–0.7 deficits (Td 15–18 min learned), up to
    0.06 lower in the generic slow room, the fitted living room within
    ±0.01, the fitted second bedroom +0.01…+0.03 (its coast stalls just
    short and is topped up), hold RMS within ±0.01 (+0.02 once), commands
    −9…+13 %, heat-ups unchanged. 3.0 helped the generic living room less
    (+0.15 at 12 °C / 0.6); every rising hold (no threshold) is the
    dropped "stop at the target" (more than twice the commands in the
    fitted second bedroom).
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
  - `dose` (2.15.0, the option `dosed_topups`): in the *settled* state a
    full-power pulse sized by a Smith predictor replaces the hold; see
    "Dosed top-ups" below.

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

## Activity log

The owner's request (25 Oct 2026): one sensor per room that reads like a
debug log — what Roomstat does and why, why it does *not* do something, the
valve's reading and what the valve has been told, events, and what
auto-tune changed or why not — so the diagnostics and the HA log need not
be read.

- **The sensor** (`sensor.<room>_activity`, "Activity"): the state is the
  latest line, so its history and the logbook are the log. It is written
  once per line (`_activity_write`), so an event and the decision after it
  in one cycle are both recorded. Its only attribute is `log` (the last 30
  lines, newest first, `HH:MM  text`, unrecorded), which changes only with
  a new line: anything that drifts every cycle would make the recorder
  write a `states` row per coordinator refresh (2.10.0 review). The live
  decision line is the thermostat's `activity` attribute (unrecorded).
  Lines fit HA's 255 characters: a decision line shortens its phrase, never
  the `| room … | TRV … | Tado …` tail; a longer event is split into
  `… `-continued lines (`split_lines`).
- **Decision lines** (`describe_cycle`): `<what and why>[; why not] | room R
  → S <preset> | TRV T [heating / off / on its Tado schedule] | Tado <what it
  was told>[ (X waits: rate limit / X waits: a change by hand settling / X
  waits: the window's delay / X wanted: under the 0.3 step / X failed)]`. The first decision after a
  start is no transition (a shut valve resumes as coast, then stop: no coast
  happened), and the line uses the braking time the cycle decided with, not
  one the tuner set after, and the preset and window as the cycle decided
  (`_activity_decided_as`, read before the send: 25 Oct 17:12 a line read
  `room 16.6 → 20.0 Frost protection`, the preset picked while the 25 °C
  went out). The phrases use the
  regulator's own predictions (`regulation.outlook`): what stopping a
  heat-up now would leave, the coast's predicted landing, the stop's
  look-ahead, how far a stop is from its release; in hold P and I, and why
  not more (`I rests outside ±0.3`, `full heat only from 0.8 below`). A stop
  names what keeps it shut (2.10.4): still rising below the stop's line,
  the overshoot forecast (`on that slope it would pass 20.1 within 25 min,
  so it stays off`); otherwise where it heats again (`heats again at 19.9,
  or 20 min ahead of it once falling`, with the 20-min forecast only while
  falling). 26 Oct 06:42 it read `heats again once heading 0.1 below within
  20 min (predicted: 0.11 below)` — met on paper while the valve stayed
  shut, the release only looking at falls. A coast whose target fell below
  the room (a lower preset mid heat-up) reads `Heat-up ended` on the cycle
  the coast begins (2.13.0: one that carries on is still short by the
  regulator's own error, so a result rounded to 0.00 reads `Coasting`; 28
  Oct 06:45, study), a hold a
  target step released reads `Holding (released): 0.76 below` (two
  decimals where one would read as the 0.8 of `full heat only from 0.8
  below`), a full push `Holding, full push: … stops once the leftover heat
  covers the rest` and its end `Full push ended, coasting`, and an open window
  promises `valve off after its delay` only while the open delay runs
  (`WindowAutomationController.open_delay_active`; otherwise `not acted on
  until it next opens`). Every
  cycle exit writes one (summer, off, no data, room sensor lost, waiting,
  regulated). A new line at once when the decision changes
  (`decision_key`: the mode (a full push counts as its own), the window,
  the target and preset, what the valve is told, a waiting or failed
  command), and at most every
  `DECISION_REFRESH_S` (5 min) while only its numbers move; always after an
  event, so the latest line is again what the room is doing. A toy heat-up,
  coast and hold wrote 32 lines in 150 min (125 without the refresh rule).
- **Events**: every room message at info or above goes through
  `self._log(_LOGGER)` (a `RoomLogAdapter`: the room's name in front, and a
  line in the activity log whatever the logger's level — HA logs only
  warnings by default). **New room-level log calls must use it**, not
  `_LOGGER` with `self._config_entry.title`. Preset changes (`(by hand)`),
  temperatures set, switching on and off, and every start are events.
- **Auto-tune**: the tuner keeps a numbered journal (`journal_since`): each
  event (`last_event` is a property that journals), and each verdict merged
  with the episode it judges. After each cycle `_activity_tuner` logs what
  is new, in order (`auto-tune: <episode> → <verdict>`, "possible from
  HH:MM" for a rate-limited change), so a heat-up judged and the next one
  started in the same sample are both logged.
- The lines ride along in the restore-state extra data (`activity`), so a
  reload (every preset temperature change) keeps them; local times use HA's
  time zone (`dt_util.get_default_time_zone`).

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
  regulation takes the valve out of off with one
  `set_temperature` (`hvac_mode: heat`) once the change has settled (see
  below), or leaves it off if the room needs
  no heat (`set_hvac_mode heat` would heat a Tado zone at the last
  temperature HA sent, 25 °C after a heat-up).
- **A change by hand settles before it adds heat (2.10.4,
  `MANUAL_SETTLE_S` 15 s):** a preset, temperature, preset temperature
  (the number entities), HVAC mode or resume schedule accepted from a
  service call (`note_change_by_hand`, after the call is validated: an
  unknown preset or a resume without a schedule is no change) holds back
  any command that gives the valve more heat than it may hold until no
  further change has come for 15 s (`_hold_back`; a cycle is scheduled for
  then, `_schedule_settled_cycle`, reason `settling(Ns)`, the activity line
  `X waits: a change by hand settling`). "May hold": a heat command of ours
  that may be on its way (`pending_command`, a timed-out one included),
  else the setpoint it shows (on its Tado schedule, the block's); off, an
  off of ours on its way, or nothing known is the frost level. Less heat
  and off go at once. The wait is by time, not by who asks: within the
  15 s an automation's heat waits too (the press may yet be taken back);
  outside it nothing waits. A clock that went back ends it, and the time
  rides along in the restore-state extra data (`manual_change_ts`, taken
  back only within the 15 s), since a preset temperature change reloads
  the room (it matters only when an event runs a cycle within those 15 s:
  the first timed cycle after a reload comes after the 60-s interval).
  More heat is committed only after its turn at the bridge
  (`_wait_for_command_slot` before the check is asked again, then
  `paced=True` to the send): after a heating script four rooms queue
  0–15 s, and a change by hand during that wait was not seen (its own
  cycle waits for the lock), so the stale heat went out with the off 5 s
  behind it (2.10.4 review). While a window's open delay runs
  (`open_delay_active`, reason `window_pending`, `X waits: the window's
  delay`), more heat waits too, for the whole delay: the window is open
  and its off is due. The window action marks its timer fired first
  (`open_timer_fired`); one that finds the sensor shut runs a cycle, and
  so does a window shut again within the delay. A repeated "on" (an
  attribute update, on → unavailable → on) keeps the first open timer:
  restarting it, as before 2.10.4, kept the window's off away for as long
  as the sensor kept reporting. Presses further apart than the 15 s
  (Comfort, then Frost 20 s later, sends the heat at 15 s and the off 5 s
  after it) are caught by the **confirmation off** (`OFF_CONFIRM_S` 60 s,
  owner's choice): an off the TRV took less than 60 s after a heat command
  was tried (`_last_heat_command_ts`, `off_confirm_after`) is sent once
  more at the first cycle 60 s or more after it (`_off_confirm_due`,
  reason `off_confirm`, an event line "Sending the off again"), in every off path
  (regulation, Roomstat's own off, summer), whatever the state shows: the
  zone showed off at once on 25 Oct while the valve heated. A heat command
  in between cancels it; a failed one is retried at the rate limit; the
  confirmation itself comes a minute after the heat, so it is never
  confirmed again; both times ride along in `CommandRecord`
  (`heat_sent_ts`, `off_confirm_ts`). Whether a second off reaches a valve
  that kept the heat over the first is not proven yet: check the zone's
  heating % after the next one. Reason: a Tado valve that gets an off seconds
  after a heat command can take the heat and not the off. 25 Oct 2026:
  25 °C then off 5 s later (master bedroom 11:09) and 15 s later (second
  bedroom 17:12, Comfort then Frost protection picked 9 s apart); both zones
  showed off at once (Roomstat logged "Tado off") while the valves heated
  at 100 % for 15 and 13 min (the zones' heating %). Over 19–26 Oct those
  were the only two valves heating while told off; an off half an hour
  after the heat is taken within 5 min. 19 Oct: 20 → 22 → 20 within 10 s
  had sent 25 °C too.
- **A lost off (2.15.0, every room with a heating power sensor, dosed or
  not; `lost_off_due`, `_check_lost_off`):** a zone still reporting
  heating ≥ `LOST_OFF_MIN_PCT` (20 %) in a report made `LOST_OFF_AFTER_S`
  (8 min) or more after an off the valve took, with no heat command since,
  gets the off again at once through the confirmation-off path
  (`_off_confirm_reason` "lost": a warning, "Sending the off again: the
  valve still reports heating …"), at most `LOST_OFF_MAX_RESENDS` (2) per
  heat command (the count is not kept across a reload; the command record
  is only taken back within 360 s, so a reload ends the check anyway). The
  report time is the heating sensor's `last_reported`
  (`heating_power_reported_ts`), so a report from before the off never
  counts. 8 min, not 3: the zone's heating % reaches HA 10–276 s after the
  valve and the valve takes 1.5 min to close; at 3 min a zone whose reports
  came 200 s late got every off twice, stock rooms after a heat-up too, and
  at 7 min again with 370 s (2.15.0 reviews). The 25 Oct valves heated
  13–15 min. The confirmation off covers offs within a minute of a
  heat command; this covers any later one, which dosed pulses (an off
  3–12 min after each 25 °C) make the normal case.
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
  Switching on or off by hand ends the window state (its saved preset is
  restored; OFF → HEAT re-arms the window if it is still open), but **Heat
  while already heating is no change and leaves the window alone** (2.10.4;
  also its close delay; to end the window state by hand, Off then Heat):
  the owner's heating scripts send `set_hvac_mode heat` to every room
  before each preset, which until then ended the frost protection of any
  room with a window open and restored its saved preset, and the preset
  after it then heated at full power with the window open (25 Oct 2026,
  master bedroom 11:09 and second bedroom 17:12: 25 °C sent, then the
  preset's off seconds later, and the valves heated 13–15 min anyway; see
  "A change by hand settles").
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
  is the cause** (2.10.3): the TRV reported heating (`TrvReading.heating`,
  the entity's `hvac_action`; the Tado cloud derives it from the zone's
  heating power, HomeKit from the valve) *during* the excursion (Roomstat
  never heats a room 0.5 over its target), or it heated at least band /
  `RADIATOR_MAX_RATE_C_PER_H` (4 °C/h: 7.5 min for 0.5 °C) in the 30 min
  before it began (`HEATED_LOOKBACK_S`: the heat arrives after the valve
  shut; each report stands until the next, at most `HEATING_SAMPLE_MAX_S`;
  taken once, when the excursion begins), or the TRV − room gap stands
  ≥ 2 °C (`RADIATOR_RISE_C`) above its lowest of the last 3 h
  (`GAP_WINDOW_S`: the radiator cold; a hot radiator lifts the valve's
  reading far more than the room, the sun lifts both; judged each update,
  so a radiator that cools turns hot into warm). Otherwise the state is
  `warm`: attributes only, sensor off, one info log. The evidence is in
  `BandState` (`heated_during`, `heated_before_s`, `radiator_rise_c`, also
  attributes) and the log line says it in words
  (`OutOfBandMonitor.describe`). 16 Oct 2026: a sunny afternoon lifted four
  rooms 1.5–2.3 °C with every valve at 0 % and produced four hot alarms
  under the plain rule; the replay in `tests/test_fail_cold.py` is warm.
  2.10.3 (`TestOutOfBandReplays`, every alarm of 18–25 Oct): until then any
  heating in the look-back counted and the gap needed only 1 °C. 25 Oct,
  second bedroom: a hold's top-up heated 8 min (3 in the look-back), Frost
  was picked, and the room rose 1.5 °C with the valve shut and its reading
  flat — hot. 22 Oct, study: the valve shut all day, its reading fell
  15.4 → 13.9 and recovered with the room flat (the gap swung 1.0–1.4) —
  three hot alarms. 25 Oct, study: a 12-min heat-up landed at +0.06, the
  room went on to +0.9 by itself while the radiator cooled (gap +1.6) —
  hot. All warm now. Still hot: 22 Oct, living room, full power 12 min
  right before a +0.7 overshoot after a window (gap only +1.1, so the
  look-back carries it). The thresholds: 25 Oct, 12 min (study) and 35 min
  (living room) at full power lifted the gap 4–5 °C and it stayed 2 °C up
  for about an hour; the fastest rise per hour of heating seen is 3.3 °C
  (the study's 12 minutes gave 0.66 °C, coast included). With nothing
  known about the valve (no `hvac_action`, no reading) hot needs no
  evidence. The 11 Oct setback (20 → 18 at 21:30, still 18.66 at 06:00)
  never fires; "setpoint + 0.5" alone was on all night. Cold: 0.5 °C below
  for 120 min, restarted by every setpoint rise. `since` is set while on or
  warm.
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
- **Away or Resume schedule picked while nobody is home is back to
  automatic** (2.12.0, `_async_back_to_automatic`; owner, 27 Oct 2026: "an
  easy way to reset the heating status to be automatic"), never a manual
  (sticky) Away: it cancels an override (an Off too) and any schedule
  override, and coming home restores `_automatic_return`: the schedule's
  preset, without one the pre-change state, never an Off, Away or Boost
  (Comfort then). With an override the base is applied quietly
  (`_apply_quietly`) before the away action, so no heat call is sent and
  taken back; already in away mode only what comes back with you changes
  (until 2.11.0 Away then left an Off picked before leaving saved for the
  return, and preheat held the room cold). Once the sensor reads home again
  (home delay running), Away is an ordinary Away picked by someone at home.
  Switching the room back on (Off → Heat) while nobody is home is back to
  automatic too (2.12.0 review: it routed the schedule's Comfort into an Off
  until home, heating the empty house with no limit). Not
  changes by hand: the schedule, Off → Heat (it resumes the schedule), Heat
  when already heating (the heating scripts send it before the preset; it
  leaves an open window alone too, see "Windows"). Every user call still
  starts the settle before more heat (`MANUAL_SETTLE_S`).
  Every other service call counts, an automation's or a voice assistant's
  too: a pre-heat from an automation should work the same (deliberate; the
  owner has no automation setting presets, schedules go through the helper).
- **Off picked by hand lasts until someone is home (2.12.0, owner, 27 Oct
  2026).** Frost protection picked while nobody is home, or the one the room
  is in when everyone leaves (`_off_picked_by_hand`: with a schedule a sticky
  schedule override, the schedule's own Off is ordinary away mode; without
  one any Off; with a window open what it will restore), is an override with
  no limit (`off_until_home`, persisted): the room stays Off, not Away, and
  preheat stands aside (`off_by_hand`). Coming home ends it
  (`_end_off_until_home`): the schedule's preset (Comfort without one), its
  sticky override dropped, the window's restore target with a window open.
  An Off picked once the sensor reads home again (home delay running) is an
  ordinary change and stays. Evidence: 27 Oct, Off picked at 08:48 in all
  four rooms, out 12:34–17:50; replayed, the rooms were still Off two days
  later (only a Resume schedule at 13:11 undid it), and Away while out
  wouldn't have. A pre-2.12 snapshot of away mode entered from an Off picked
  by hand is read with these rules (`_load_presence_snapshot`; such a
  snapshot has no `presence_override_off_until_home`): away mode entered
  from an Off picked by hand becomes this override, the same after an Away
  picked while out (2.11.0's preheat `held_cold`) is back to automatic, and
  an Off picked while out (then a change with a limit) gets the flag; the
  line saying so is logged unless summer mode is on. A Boost started from an
  Off while nobody is home ends back on the Off until home (without a
  schedule; with one it ends on the schedule's preset). At startup the
  schedule's state is taken back before presence and window
  (`_startup_schedule_state`; following it stays last, `_startup_schedule`),
  so their restores see its preset and override as at any other time
  (2.12.0 review: with a window open at a startup restore, Comfort came back
  and the Off's sticky override hid the schedule).
- **The limit only guards against heating an empty house.** When it fires
  and the change heats no more than away would (the room switched off, a
  temperature at or below the away target; with a window open, what the
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

## Preheat on the way home

Read this before changing `preheat.py`, `climate_preheat.py` or the presence
paths that call it.  The plan, its evidence and the owner's decisions:
project doc `claude/roomstat-preheat-wiring-plan-2026-10-24.md`.

- **Shadow only (2.11.0, owner's decision, 27 Oct 2026: gather data first,
  never action the heating requests).** Modes `off` / `shadow`
  (`preheat_mode`, default off). The decision is worked out, shown and
  logged; nothing in `climate_preheat.py` touches `_effective_setpoint`, the
  setpoint or a command, and `tests/test_preheat_entity.py` proves shadow
  sends exactly what off sends over a whole outing. Switching it on later
  means: the effective setpoint while away mode is in charge is the
  decision's `target_c` (evaluated before step 3 of the cycle, which it
  already is), a new mode `on`, and the card's status line; nothing else
  in the rule changes.
- **Inputs** (`_preheat_inputs`), from Home ETA (igiannakas/ha-home-eta 2.3.0):
  the first expected arrival (`sensor.home_eta_expected_home`, a timestamp;
  unknown while nobody is heading home), someone close to home
  (`binary_sensor.home_eta_someone_close_to_home`: home or within 800 m) and
  someone nearby (`binary_sensor.home_eta_someone_nearby`: within each
  person's near-home radius, 3 km here). Home ETA's group sensors are
  unknown while a member could change the answer; unknown and anything but
  on / off count as no (`parse_on_off`), an arrival without a UTC offset as
  none (`parse_arrival`). From the room: away mode in charge
  (`_preheat_inactive_reason`: summer, off, no presence sensor, a change by
  hand, someone home, a window open — `_window_open`, the sensor too),
  the target coming home would restore (`_preheat_scheduled_c`, mirroring
  `_restore_presence_state` and `_effective_setpoint`: the saved preset,
  Boost replaced by the schedule's, Comfort at its option; a test checks it
  against the restore), the room reading, the auto-tuner's heating rate and
  dead time (`Autotuner.heatup_model`, only while auto-tune is on), the
  learned 30-minute recovery.
- **The rule** (`PreheatController.evaluate`, every regulation cycle at its
  start — so the cycle's own state write carries it — and on every change
  of a Home ETA entity):
  - not in charge → `inactive`. Someone home, no presence sensor or none of
    the three entities set (`no_home_eta`) end the trip home too (everything
    reset, `_ENDS_THE_TRIP`); any other reason — a window, a change by hand,
    an Off picked by hand (`off_by_hand`), switched off, summer, nothing
    warmer than Away to restore (a schedule at Away or Frost protection) —
    only stand preheat aside (`_stand_aside`: no latch, no level) and keep
    the trip's start, its cap and its arrival's missing time, so a short
    break can't re-arm the cap (review finding: a window opened and shut
    after the cap gave 4 more hours of "due"). The decision's key includes
    the reason, so the sensor shows it; the thermostat's
    `preheat_inactive_reason` is live;
  - close → the restore target (`close`); nearby → the **30-minute
    temperature** `floor_tenth(restore − recovery)`, never below Away
    (`nearby`; the owner, 25 Oct 2026: aim for 30 min, not aggressively, up
    to 45 is fine, hence the median and the rounding down: the study 19.4,
    not 19.9); otherwise Away (`waiting`). The level is the warmest wanted
    at any time in the last `fall_delay_s` (`_level_now`, the wanted levels
    with their times): a rise at once, a drop only once nothing warmer was
    wanted for the whole delay — on the level, not the temperature, so a
    schedule change while away applies at once (review finding: counting
    from the first drop let close → none → nearby within seconds read
    close, waiting, nearby);
  - heading home: due once `now >= arrival − needed`, `needed = dead time
    + max(0, restore − room) / (rate_factor × rate) + lead` (`due`, the
    restore target). An arrival in the past counts as now; one more than 12 h
    ahead is ignored (and counts as missing). It ends when the arrival has
    been missing for `arrival_grace_s` (the trip is over: `arrival_lost`;
    counted through breaks too), `cap_s` after the trip's first start (`cap`;
    slips, restarts and breaks can't stretch it; no new start until that
    trip is over, `capped`), or when
    the arrival is more than `slip_s` later than the one it started for and
    the room is not due for the new one (`slipped`: no waste, owner's
    decision; due again later starts it again). "Not due" alone never ends
    it: a room that warmed up needs less time and would stop ahead of
    schedule. With no room reading nothing new starts; a started one
    carries on (a slip still ends it);
  - the target is the restore target while due, else the level's.
- **Evidence for the heat-up time** (Step 1, 25 Oct 2026, 37 heat-ups
  18–25 Oct, four rooms, today's learned rates 2.52 / 1.75 / 1.80 / 2.41
  °C/h and dead times 22.3 / 12.7 / 10.2 / 27.9 min): with 0.8 and 5 min,
  34 (92 %) would have started early enough (to within 0.3 °C), typically
  24 min early (−24 … +69). The three late: the living room on 24 and 25
  Oct at 06:00 (within 0.4 °C at 51–53 min of the 81–84 allowed, then heat
  → coast at 19.2–19.4 and a crawl to 19.7: a regulation question), and
  the master bedroom right after airing to 15.8 °C. 1.0 starts rooms 4–8 min
  later (median) and makes three more late. The study starts about 30 min
  early whatever the deficit: its learned dead time alone is most of it
  (watch in the shadow week).
- **The 30-minute recovery** (`RecoveryLearner`, fed by every regulated
  cycle after the regulator, whatever the mode; every other cycle — off,
  summer, no data, the room sensor lost, an error — feeds it an unusable
  one from `_async_regulation_cycle`'s `finally`, `_recovery_fed`): an
  episode starts on a target rise ≥ 0.5 °C that leaves the room ≥ 0.5 °C
  below, not within 30 min of a window; it is dropped when the target
  changes, a window opens, a cycle is unusable (no fresh reading — a bridged
  one counts as none —, Boost, off, summer), cycles stop for more than 10
  min, or the room comes within `reach_c` (0.1 °C) of the target by its 30
  min (review finding: its rise is capped by the deficit, so it would
  understate the room and make the nearby target too warm); at 30 min
  (interpolated) the rise is kept, the median of the last 10 is used once
  there are 2 (as the auto-tuner's dead time).
  Heat or hold alike: from a 30-minute temperature a room is 0.4–0.8 °C
  short, either side of `heatup_enter_c`. Measured 18–25 Oct: living room
  0.60–0.95 (median 0.77), master 0.30–0.57 (0.43), second 0.50–0.56 (0.54),
  study 0.43–0.65 (0.58); `tests/test_preheat.py` replays four mornings.
- **Changes by hand while nobody is home** (2.12.0, see "Presence"): Away
  or Resume schedule is back to automatic, where preheat works (it decides
  again at once on what was saved for the return); an Off picked by hand
  holds the room off until someone is home (`off_by_hand`); any other change
  is in charge until its limit or until someone is home (`change_by_hand`).
  The 2.11.0 Away held cold (owner, 25 Oct 2026, "an explicit Away must not
  be second-guessed") went on 27 Oct 2026, when the owner asked for Away to
  mean "back to automatic"; Off keeps a room cold now. A 2.11.0 save's
  `held_cold` is ignored, and a decision logged as held cold is not taken
  back. An Away picked at home is not carried: the away action turns it into
  away mode, as before.
- **Shown and logged:** a line per change of decision (`decision.key`: the
  state and the target to 0.1 °C) through `self._log(_LOGGER)` with the
  prefix `preheat (shadow)`, worded by `describe` (what it would heat to,
  why, the heat-up time's parts, why a heading-home start ended); off to
  inactive and inactive to inactive say nothing. The Preheat sensor's state
  and attributes are the decision last logged (`LoggedDecision`, fixed until
  the next, one recorder row per decision); the live numbers are the
  thermostat's `preheat_*` attributes, unrecorded
  (`PREHEAT_LIVE_ATTRIBUTES`: the climate entity writes a row every cycle,
  and ten more attributes on it were about 2 MB a day for four rooms). A
  learned recovery is an event line too.
- **Never stops the regulation:** the evaluation, the learner (with their
  log lines and state writes) and the restore catch everything (logged once
  per distinct error, with the traceback; a save that can't be taken back
  starts preheat afresh).
- **Persistence** (`preheat` in the restore-state extra data): the
  controller (latch, the arrival it started for, the trip's start and cap,
  the level and its pending drop, the arrival's missing time), the learner
  and the decision last logged (a reload, which every
  preset temperature change causes, writes no second line). Times from the
  future count as now.
- **Settings** (`TIMING_OPTIONS`, section "Preheat timing"; each one's help
  text is in `strings.json`, the README explains them too): lead 5 min,
  heating rate factor 0.8, arrival slip 20 min, longest preheat 240 min,
  level drop delay 10 min, arrival lost grace 10 min, default 30-minute
  recovery 0.5 °C, default heating rate 1.0 °C/h and start-up delay 20 min
  (until auto-tune has learned them; slow on purpose). Out-of-range or
  non-numeric values keep the default (`config_from_options`). The three
  entities join the entity-gone repairs (warnings) while preheat is not off
  (`PREHEAT_ROLES`).

## Valve look-ahead (2.14.0)

Read this before changing `lookahead.py`, `climate_lookahead.py`,
`LookaheadConfig` or `regulation.lookahead_correction_c`.  Evidence: project
doc `claude/roomstat-valve-lookahead-2026-10-30.md`.

- **The owner's request (30 Oct 2026):** a negative adjustment to the hold
  command, from how fast the valve's own reading falls, starting low and
  learned. An option per room (`valve_lookahead`, off by default) with the
  zone's heating power sensor it learns from (`heating_power_entity_id`,
  Tado's `sensor.<zone>_heating`; the options form refuses the option
  without it, `options_form_errors`). The owner chose it knowing the
  simulation below; holding the release until the slide slows was
  rejected (the room may cool too far).
- **The mechanism (real data, living room, 36 releases 23-30 Oct 2026):** a
  Tado looks ahead on its own reading. Its first answer to a release follows
  push + 13 min × fall (r = 0.85; push alone 0.50; fit: first % = 16 + 38 ×
  (push + 15 × fall per min), R² 0.73; a dynamic P + I + look-ahead fit on
  the heating % series gives 8.5-15 min). Fast slides (≥ 2.5 °C/h) got 5.7
  valve-minutes in the first 10 min against 1.4 for slow ones, and peaked
  +0.29 (median) against +0.15. **But a release's peak does not follow its
  dose** (r = 0.04), and night releases (no people or sun, reading falling
  0.9 °C/h) still peak +0.21: most of a top-up's overshoot is the 7-9-min
  dead time (no feedback on the room can dose less; a derivative only sees
  the rise once the heat is committed), the look-ahead adds at most ~0.1 on
  morning re-fires.
- **What it does:** in hold only, the command is lowered by
  `lookahead_correction_c` = look-ahead × (fall − 0.3 °C/h deadband), at
  most 1.5 °C (`lookahead_deadband_c_per_h`, `lookahead_cap_c`). One-sided:
  a flat, rising or unknown reading changes nothing; heat, coast and stop are
  untouched. The full push is decided on the uncorrected push; the
  integral's saturation check uses the command actually sent
  (`raw − lookahead`), and the lowering is not "saturated". The slope is a
  12-min Theil-Sen (`SlopeEstimator`, 4 samples, 400-s gap) over the
  reading of the TRV entity in use, fed every regulated cycle; a switch of
  entity starts it afresh, a gap over 400 s clears it.
- **Learning (`LookaheadLearner`):** a release is a hold command sent while
  the valve was shut (`_lookahead_valve_shut`: `trv.off`, or the frost
  level as the baseline with no heat setpoint showing; never while a heat
  command of ours may still be on its way, whose stale "off" a cloud zone
  shows for a minute), with the reading's slope known and a heating power
  sensor set. The command booked is the one tried (`_last_attempt_c`, kept
  inside what the valve accepts). Its answer is the first heating power
  report above 0 whose report time (the state's `last_changed`) is after
  the command and within `observe_s` (8 min): cycles that skip the
  look-ahead (room sensor lost, no data, waiting) must not make a later
  report the answer. The push is the setpoint the valve first shows after
  the command's own cycle (`note_setpoint(seen_ts=…)`; a cloud zone rounds
  to 0.5 °C: 18.3 → 18.5) minus its reading at the send.
  A report ≥ 98 % (fully open) is not counted; an off or full power sent
  before the answer cancels it (also Roomstat's own off, summer, the
  watchdog: `_lookahead_observe` checks the last attempt). Least squares
  over the last 24 releases, response = a + b × push + d × fall, look-ahead
  = d / b, trusted from 8 releases when the fall rates spread ≥ 0.5 °C/h,
  b ≥ 10 %/°C and |r(push, fall)| ≤ 0.9 (with the look-ahead in use the push
  is lowered on a falling reading, so they correlate). The value in use
  starts at 0 and moves towards the fit by at most 2 min per release (gap
  under 1 min left alone), within 0-20 min. Each release's lesson is an
  activity line (`valve look-ahead: the valve answered 62 % to a push of
  +0.5 on a reading falling 3.0 °C/h → …`).
- **The auto-tuner** gets the look-ahead taken off the command the valve
  holds (`AutotuneSample.lookahead_c`, `_held_lookahead_c`, set with each
  command sent, 0 for an off and on a drift): its demand is the push the
  valve answers. Without it a top-up on a falling reading could read as no
  top-up (demand under `heatup_reheat_demand_c`) and its heat as coast,
  inflating the braking time.
- **Simulated** (scratch `lookahead/sim.py`: the room fitted to seven
  mornings, room rms 0.09-0.14 °C, 30 Oct reproduced: 06:00 18.54 against
  18.55, target in 61 min against 57, the re-fire at +90 against +88; the
  valve reading fitted separately, its slide −1.2…−4.6 °C/h against
  −1.1…−4.6; the Tado fit above; cloud steps, latency, off, 3-min polls;
  the real regulator and learner; 3 plants × 3 outdoor temperatures × 3
  seeds × with and without people and sun, 8 days, metrics from day 4):
  the learner settles at 11-20 min for a 13-min valve (once 8; it learns in
  its own 12-min slope, which lags the valve's instant one) and 2-20 for an
  8.5-min one, and cuts a fast-slide release's first opening 73 → 52 % and
  its 10-min dose by a quarter, but **the peaks after top-ups barely move**
  (median 0…−0.01, −0.01…−0.03 with a faster reading slide; p90 within
  −0.015…+0.002; the worst peak +0.00…+0.02), time below −0.2 rises 52-84
  → 72-124 min/day, below −0.3 0.3-9 → 20-59 min/day, commands +14-27 %,
  heat −0.2…−0.8 %. The simulator's top-ups are
  gentler than the real ones (its peaks about half), so it may understate
  the benefit; the cost (heat later on a cooling room) is mechanistic.
  This matches "tried and dropped: cancelling the Tado's own anticipation"
  under Regulation concepts. Judge it live on the trace sensors.
- **On the entity:** the sensor `valve_lookahead` (minutes in use, only while
  the option is on; attributes: the fit, releases, the last lesson), the
  thermostat's `valve_lookahead_min` and `valve_lookahead_c` (this cycle's
  lowering, unrecorded), the hold's activity phrase (`0.6 off for its
  reading falling 2.8 °C/h`), the debug line (`, look-ahead -0.62` and the
  reading's slope), diagnostics. **Reset auto-tune** forgets it too. The
  learner rides along in the restore-state extra data (`lookahead`, with
  the TRV it was learned on: another valve starts afresh), with the
  reading's slope samples (taken back under the SlopeEstimator's own
  rules) and the look-ahead taken off the command the valve holds (taken
  back only with that command, `CommandRecord`): a reload, which every
  preset temperature change causes, neither sends an unlowered hold on a
  still-sliding reading nor understates the tuner's demand. The off
  baseline (`_note_trv_holds_off`, `_async_send_off_to_tado`) zeroes the
  held look-ahead. With the option
  on and no heating sensor (a repair can remove it) a warning at start; the
  look-ahead then stays where it is. The heating sensor joins the
  entity-gone repairs (warning) while the option is on (`LOOKAHEAD_ROLES`).
- **Never stops the regulation:** every step catches everything, logs once
  with the traceback, and switches the look-ahead off for the session
  (`lookahead_active`).

## Dosed top-ups (2.15.0)

Read this before changing `dosing.py`, `climate_dosing.py`, `DoseConfig`,
the `dose` mode or `_dose_step` in `regulation.py`.  Design, fit and
evidence: project docs
`claude/roomstat-smith-predictor-dosed-topups-2026-10-30.md` and
`claude/roomstat-dosed-topups-implementation-guide-2026-10-30.md`.

- **The owner's request (30 Oct 2026):** once a room has landed, hold it in
  a 0.2 °C band centred on the target (±0.1; short rather than hot, but in
  the band), with full-power pulses of at least 3 minutes rather than the
  valve modulating, no extra gas, and no more boiler cycles (an on/off
  boiler). An option per room (`dosed_topups`, off by default) that needs
  the zone's heating power sensor (`heating_power_entity_id`, shared with
  the valve look-ahead; the options form refuses it without one,
  `dosing_needs_heating_power`). With 2.15.0 it is on in the living room
  only, a live trial (owner, 31 Oct 2026, after the simulation below
  failed his gate): judge it on the living room's trace sensors and boiler
  calls. 2.14.1 (commit f293136) is the rollback point; the option off is
  2.14.1's regulation (below).
- **The problem it fixes:** in the settled state a release re-opens the
  valve and the room shows the heat only 8–12 min later (cloud latency,
  water, radiator, sensor), so the valve runs until the stop rule sees the
  rise: a 0.1 °C shortfall worth ~2.4 open-minutes got 10–30. Living room,
  25–30 Oct 2026, 19 settled top-ups: peaks median +0.23 (quartiles
  +0.09 / +0.42, worst +0.94). Nothing acting on the measured room (PI, a
  derivative, the valve look-ahead) can dose less than the dead time
  dictates.
- **The Smith predictor:** a first-order-plus-dead-time model of Roomstat's
  own heat (delay θ, spread τ, gain in °C per *open-minute*) runs beside
  the room (`TopupPredictor`), and the settled state decides on the virtual
  error `e* = setpoint − room − rise still in flight`. An open-minute is a
  minute the valve is commanded fully open, booked from our own commands
  (`RoomDoser.note_command`, every attempt through `_note_attempt`): a
  maximum (≥ max − 0.05) opens from send + `latency_s` (60), any other
  command closes at send + `close_delay_s` (90). Never from the heating %:
  it lags the valve and the valve is strongly non-linear, which scattered
  the gain 10× in the fit (open-minutes 2×). The heating % only checks
  that a pulse opened the valve (a pulse with no report above 0 by its end
  + `opened_check_s` is not learned from; its heat stays booked) and tells
  the house that a zone burns.
- **Hold heat is not booked** (2.15.0 review): a hold command's opening is
  unknown (the Tado throttles, 30–40 % on average), and booking it as fully
  open made the observer read a false −2.7…−3.0 °C/h for 20 min after a
  hold (first pulse +0.15). Unbooked, the room after a hold reads as
  warming (smaller doses, the safe side). Hold commands are marked as
  unbooked heat, and any learner episode whose window (widened by θ + 2τ)
  meets one is dropped.
- **The settled state** (`RegulationState.settled`, `_dose_step`): entered
  from a stop with |e*| < `settled_window_c` (0.5); left on a shortfall of
  `unsettle_c` (0.3), virtual or measured (a model that under-doses keeps
  the virtual one small while the room sinks), on the room itself 0.5
  above, a target move over 0.3, a window, Boost (`_dosing_allowed`), dosing
  off, or `capped_pulses_unsettle` (2) pulses in a row at the 12-min cap
  (more than pulses can give; not entered again until the room is back at
  its target, `RegulationState.capped_pulses`). Not on a virtual excess:
  in cold weather a correct pulse carries more than 0.5 °C in flight, and
  that exit turned every pulse into a hold at 2 °C/h. While settled with
  dosing on there is never a hold command: `stop` (off) or `dose` (25 °C),
  and the integral does not integrate. The ordinary modes decide
  otherwise, a pulse still running counting as a just-ended maximum (a
  coast).
- **Firing:** ready once `min(θ + 2τ, 30 min)` has passed since the last
  pulse started (the refractory: its heat has landed); then a pulse starts
  when `e* + (θ + τ/2) × fall ≥ release_below_c` (0.1): the release looks
  one visible dead time ahead, so the trough is about −0.1 and the landing
  on the target. No "never above the target" check: in cold weather it
  fires at or above the target, and a 25 °C pulse cannot trickle as a hold
  command there would. An in-flight gate (no pulse while heat is still
  arriving) was tried and dropped: the virtual error already counts it, and
  the gate broke the band above 0.5 °C/h (B's check, perfect model: with
  it troughs to −0.18 at 0.8 °C/h; without, −0.09…−0.01 at 0.2,
  −0.10…+0.02 at 1.0).
- **Sizing:** `need = dose_need_c(e* − tail, fall, θ + 2τ, aim 0, bias)`,
  the shortfall plus the cooling until the heat has landed; the pulse ends
  once the heat committed covers it (`covered`, not before `pulse_min_s`
  180 s, the owner's minimum, unless the target was lowered since it
  started, `RegulationState.pulse_setpoint_c`), at `pulse_max_s` (720 s,
  `max`), or at once when the measured stop rule fires (`stop_rule`). A
  running pulse is re-judged at the second it should end
  (`_async_dose_wake`, `async_call_later`), not at the next minute. A
  pulse held back (a change by hand settling, a failed call) starts when it
  goes out: the regulator reports it as fired again on the cycle its start
  is unknown, and only a cycle that sent it (`after_compute(sent=True)`)
  starts its episode, counts it and wakes other rooms. A failed call is not
  held back: the pulse keeps its clock and the failed 25 °C is booked as
  open (it may have landed; at worst more heat in flight, a smaller dose),
  so it still ends by its need or its maximum (2.15.0 second review:
  restarted every cycle, a call that timed out but landed ran the valve
  15 min, +0.39). A clock that went back counts the pulse from now, the
  same pulse. The watchdog shutting the valve ends a pulse like the room
  sensor lost (`_dosing_interrupted`).
- **The cooling rate** is a disturbance observer
  (`RoomDoser.loss_slope_c_per_s`): the 15-min Theil–Sen slope of room −
  the rise of Roomstat's booked heat that has arrived, so the last pulse's
  heat does not hide the room's real loss (the plain room slope left
  troughs at −0.13…−0.19 in the entity tests; with the observer
  −0.06…+0.02). Bridged room readings feed neither it nor the learner.
- **The learner** (`DoseLearner`): each pulse is an episode up to the
  room's first peak, judged 5 min after it over [start − 15 min, landing +
  5 min] from 90 min of kept readings and open intervals (`fit_history_s`,
  persisted). The fit is joint: gain, θ, τ (grids, closed-form gain) and
  the room's loss in one least squares (`fit_episode(fit_loss=True)`); a
  fixed loss (the pre-pulse slope continued) coupled the fit to the
  learner's own gain and drifted it to 0.08–0.09 for a 0.045 plant.
  Episodes overlap (up to 3 open): a new pulse cuts the open one's landing
  search at its own opening + θ, and the fit models every booked opening
  (2.15.0 review: dropping the open episode when the next pulse started —
  the refractory is shorter than the time to judge — meant a model that
  under-doses never learned, the room 0.08–0.13 under all day; now 53 of
  55 learned in 24 h, the room −0.10…+0.08). Rejected: RMS > 0.06, a gain
  outside 0.2–4× the gain in use, a loss below −0.05 °C/h ("a room warming
  by itself") or above 3.0. Dropped: a window, Boost, a target change, hold
  heat in the window, a pulse that never opened. Steps towards the median
  of the last six: the gain up (smaller doses) half the way at once, down
  15 % at most every 12 h (the owner prefers short landings); θ and τ half
  the way either way (neither direction is safe: longer is an earlier,
  bigger pulse, shorter a second pulse before the first has landed). An
  aim bias moves 0.3 of each landing's miss (±0.25); two landings above
  +0.3: the gain ×1.5 and the bias −0.1 at once. Seeds while nothing is
  learned: the auto-tuner's heating rate / 60 for the gain only; θ and τ
  start at the top-ups' own 5 and 8 min (the tuner's dead time comes from
  cold-radiator heat-ups, 15+ min in the living room and study; seeded
  from it the sim landed +0.15). Fitted on the living room's 19 top-ups:
  gain 0.041 median (0.021 / 0.062), θ 4 min, τ 6 min, every episode within
  0.044 °C RMS; with the loss fitted, 0.036 and a loss of 0.34 °C/h. D1's
  refit on 23–30 Oct (32 settled top-ups): living 0.041 / θ 4 / τ 8,
  master 0.023 / 6 / 11, second 0.038 / 3.5 / 24 (6 top-ups), study too
  few to trust.
- **The pulse coordinator** (`PulseCoordinator`, one per process,
  `climate_dosing.PULSE_COORDINATOR`): every room reports its zone's heating
  every cycle that reads the TRV, dosed or not (`_dosing_note_zone`, at the
  report's time). A ready dosed room rides another zone's burn (`burn`; a
  report at most `zone_report_max_age_s`, 5 min, old), and one due within
  its visible dead time fires
  with a room that has just started a pulse (`together`; that room wakes it,
  `_dosing_wake` runs a cycle), only when a minimum pulse would land at
  most +0.05 above the target (`fire_max_overshoot_c`). The boiler is
  on/off: a single-radiator call already cycles it on its flow thermostat,
  so the lever is how many radiators share each burn (23–30 Oct 2026: 10
  calls a day, median 18 min, 72 % of them one radiator). It only picks the
  moment; each room sizes its own pulse. Wakes and pulse-end timers of a
  removed entity run no cycle (`_dosing_live`).
- **Interactions:** a pulse is a strong command (it skips the rate limit
  once, like a heat-up), and so is its off; at most one pulse per
  refractory, so about 2 commands per pulse. The change-by-hand settle, the
  window's delay and the command spacing hold a pulse back like any heat.
  The look-ahead never applies (no hold while settled). The auto-tuner
  sees pulses as top-ups (they end a heat-up's coast) and takes no hold
  window on dosed cycles (`AutotuneSample.dosed`; Kp and Ki are judged on
  the hold they drive). A reload resumes a pulse still running
  (`_dosing_resume`: a 25 °C held by the valve is the pulse, not a
  heat-up) and keeps the heat in flight, the observer and the learner
  (restore-state `dosing`, with the TRV it was learned on; another TRV
  starts afresh). The room sensor lost ends a pulse and the settled state.
  Reset auto-tune forgets it.
- **On the entity:** the sensor `topup_dose` ("Top-up gain", °C per
  open-minute, only while the option is on): its attributes
  (`RoomDoser.summary`) change only on a pulse start or a judged episode
  (the recorder writes a row per change; the first version changed them
  on 489 of 504 cycles, ~2.9 MB a day per room); the heat in flight and
  the rest are the thermostat's `topup_*` attributes (unrecorded) and the
  diagnostics (`RoomDoser.diagnostics`). The mode `dose` ("Top-up pulse")
  on the regulation-mode sensor; activity lines (`Top-up pulse: …`,
  `Top-up ended (…)`, `Valve off, landing: …`, `Valve off (settled): …;
  next top-up …`, the learner's `dosed top-up: …` lessons).
- **Never stops the regulation:** every step catches everything, logs once
  with the traceback and switches dosing off for the session
  (`_dosing_failure`); a dosed `compute()` that raises is computed again
  without the dose arguments. The room is then regulated as without it.
- **Option off is 2.14.1:** `tests/test_dosing_entity.py`
  `test_option_off_is_2_14_1s_regulation` replays six hours on a Tado-like
  valve and compares every command and its time with the run recorded on
  commit f293136 (`tests/fixtures/option_off_2_14_1_calls.json`); the
  regulator alone matched 2.14.1 on 200,000 random cycles (review).
- **Simulated: the owner's gate failed** (31 Oct 2026; scratch
  `dosing/house/`, project doc
  `claude/roomstat-dosed-topups-simulation-results-2026-10-31.md`). The
  real entity code ran the four rooms in one process, sharing the
  coordinator, with the live options (look-ahead and auto-tune on, 150-s
  rate limit, timer overlays). Plants were refitted on 23–30 Oct (D1:
  `dosing/sim/plants.py`). Tado cloud semantics. Each room's real 7-day
  targets, 8 days, metrics from day 3, 4 seeds × (real outdoor, real −
  4 °C). Stock gives 10.5 boiler calls a day against 10.1 measured. House,
  today → dosed:
  - settled time within ±0.1: 0.46 → 0.48 (colder 0.33 → 0.40);
  - time below −0.2: 138 → 116 min/day (240 → 171);
  - mean daily peak: +0.17 → +0.21;
  - boiler calls: 10.5 → 15.4 a day, median 39 → 7 min, under 10 min
    1 → 9 a day;
  - heat: equal; TRV commands: 88 → 68 a day.

  Calls were worse in 8 of 8 scenarios, the mean peak in 7. Without the
  coordinator it is the same: it had nothing to share at mild
  temperatures (no burn ridden). Why:
  - each settled living-room pulse is its own boiler call, and pulses come
    every θ + 2τ (under 30 min); matching today's calls needs pulses about
    60 min apart, a 0.3 °C sawtooth;
  - full output lifts the sensor through the radiator's plume (+0.42 °C in
    the fit).

  Tried and failed: 6-min pulses, 10–15-min pulses with a 60-min
  refractory, capping the gain. The simulator cannot count burner starts:
  if today's long low-% calls already cycle the on/off boiler on its flow
  thermostat (design §12), stock's starts are undercounted. Its learned
  gain also came out 2–3× high, because the plants' after-peak decay goes
  into the fitted loss; on the real living-room top-ups the fit is right
  (0.036).

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

  **A heat-up is measured only up to its own first peak** (2.9.2,
  `_coast_end`). Once the room, having risen, has stalled after the stop
  (10-min slope ≤ `coast_stall_c_per_h`, 0.1 °C/h) and then rises again
  (≥ `rate_min_c_per_h`, 0.2 °C/h), the new rise is not this radiator: the
  peak, overshoot, coast and the dead-time tangent end where it began
  (`HeatupMetrics.later_rise_c`, shown in the status). A coast that just
  creeps on is kept whole, and a stall before the room was seen rising
  (inside the dead time) is not one. Only the shape is used, no CO2 or
  presence. Evidence, living room: 23 Oct (on target at 07:05, people from
  07:40 took it to 20.35: booked +0.35, coast 20 min) and 24 Oct lunch
  (stalled at 19.97, guests took it to 20.49: +0.48, coast 33 min, θ 46 min)
  raised Td 7.9 → 13.1 min and cut the SIMC Ki 0.00056 → 0.0003 (θ median
  22 min); on 25 Oct the morning heat-up stopped 0.74 short and peaked
  −0.35. Replayed 21–25 Oct: those two become +0.05 / −0.02 with 11-min
  coasts and θ 12 / 10.5 min, the five others are unchanged, and the
  learned Td is 7.6 instead of 13.2 min, Ki 0.00045 instead of 0.0003.
  Simulator (four rooms, 5 and 11 °C, two seeds, heat-ups at 06:00 and
  12:00): empty rooms unchanged (hold RMS 0.060, commands, no rollbacks);
  with +0.8 °C/h of people after each heat-up, rollbacks 18 → 4, hold RMS
  0.144 → 0.140, lowest heat-up peak −0.68 → −0.63, highest +0.02. Stored
  episodes are kept on the update (owner's decision: no learning reset);
  the two bad ones roll out of the 5-episode history. Not handled: people
  in the room *during* a heat-up steepen its rise, so the exit predicts too
  much coast (25 Oct: 3.3 °C/h at the exit against about 2.5 on empty
  mornings).
- **SIMC is used only for Ti** (= 4(τc + θ), τc = 1.5 θ), as a basis for Ki,
  and as the justification for Td ≈ coast. **SIMC's Kc is not used:** the only
  gain estimate (rate / demand) is self-referential, because the demand is
  proportional to Kc. In simulation it ratcheted Kp down to its floor. Kp moves
  only on evidence: down on oscillation, up on repeated stalls short of the
  target.
- **Safety:**
  - hard bounds, plus bounds relative to the configured values;
  - small steps and dead-bands;
  - **the braking time leans towards stopping early** (2.13.0, `_td_step`;
    the owner, 28 Oct 2026: a room that lands short and takes another go is
    better than one that lands hot and needs a window opened). More braking
    steps as before (50 % or 5 min, a step under the 2-min dead-band
    dropped); less braking is a moving average: `td_less_smoothing` (30 %)
    of the way to the target, at most `td_less_step_frac` (15 %) or
    `td_less_step_min_s` (1 min), and only once the target is the dead-band
    below, so it settles up to 2 min above the model. Its steps are smaller
    than the dead-band, so the dead-band is on the gap, not the step; any
    step down is still "more heat" (12 h, on trial). The verdict names the
    target (`…, a step towards 8.0`). Evidence, living room 21–28 Oct: heat-
    up results scattered ±0.33 °C (sd) around +0.04, the coast 4–12 min
    empty and 15–33 min with people, while 5 min of braking moves a landing
    by only 0.1–0.25 °C; full steps chased it (11.0 → 7.9 → 10.3 → 13.1 →
    8.4 → 13.1 rollback → 8.0 min). Those 12 heat-ups replayed: 9.0–13.2,
    down at most 1.6 min a time. Simulated (`plant_sim`, four rooms, 40
    days, people on half the heat-ups at +0.5–1.0 °C/h for 45–120 min from
    15–75 min in, outdoor 5–13 °C day to day, six seeds, from day 7):
    heat-ups with people peak +0.60/+0.32/+0.60/+0.27 → +0.50/+0.26/+0.57/
    +0.24 (generic living, generic slow, fitted living, fitted second
    bedroom), the share above +0.3 down 5–9 points, empty rooms' peaks
    unchanged (±0.02), more short landings in the generic slow room (34 →
    45 %), heat-ups 0–12 min slower, commands +2–9 %, rollbacks −2…+0.4,
    the braking time's swing −24 % / ±0 / ±0 / −12 %. Tried: the same
    smoothing both ways calms the swing more (−40…−61 % in three rooms) but
    lands hotter with people than this; a 9-episode median drifts up to 16
    min on the real heat-ups; ignoring heat-ups with people (an oracle)
    holds the braking time steady but then nothing brakes for them (+0.96
    against +0.60 in the generic living room). From the 10-min default an
    empty fitted second bedroom takes about 6 days to come down (to 2.3
    min; 1 day to 0.6 before), the generic living room as before, the
    generic slow room a few days longer;
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
- `tests/test_preheat.py` (the rule, the learner, the log lines, HA-free)
  and `tests/test_preheat_entity.py` (through the entity: shadow sends what
  off sends, an outing line by line, changes by hand while out, restarts).
- `tests/test_lookahead.py` (the correction, the regulator's hold, the
  tuner's demand, the learner on a synthetic Tado, persistence, the options
  form's checks) and `tests/test_lookahead_entity.py` (through the entity:
  a release and its answer, the lowered command, the tuner's sample, an off
  before the answer, no heating sensor, persistence with its TRV, reset, a
  failure that leaves the room regulated).
- `tests/test_dosing.py` (the model, the fit, the learner on the 19 real
  top-ups of `tests/fixtures/living_room_topups_2026_10.json`, the
  coordinator, the doser's booking and observer, closed loops on plants
  that do not match the model: a weak or slow radiator, a long delay, late
  heating reports, a hold into the settled state),
  `tests/test_dosing_regulation.py` (the settled state, the release, the
  pulse and its ends, capped pulses, the coordinator's early fire, the
  steady state on a perfect model, the option off unchanged) and
  `tests/test_dosing_entity.py` (through the entity: pulses in the band,
  learning in closed loop, the option off against the recorded 2.14.1
  trace, reload, window, target change, two rooms on one boiler, the lost
  off, late reports, hold heat, a weak radiator, capped pulses, the
  sensor's churn, removal, the room sensor lost mid-pulse, failures).
  Its `_TadoPlant` holds every command as a Tado does.
- `tests/test_off_and_automatic_while_away.py` (Off until someone is home,
  Away and Resume schedule while out back to automatic, the 27 Oct 2026 day
  replayed, restarts and pre-2.12 snapshots).
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
