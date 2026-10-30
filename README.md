# Roomstat

[![Tests](https://github.com/igiannakas/ha-roomstat/actions/workflows/tests.yml/badge.svg)](https://github.com/igiannakas/ha-roomstat/actions/workflows/tests.yml)
![Version](https://img.shields.io/badge/version-2.14.0-blue)
![HA](https://img.shields.io/badge/Home%20Assistant-2026.3%2B-41BDF5)

Roomstat heats each room to the temperature measured by a sensor in the room,
not the temperature at the radiator. It works with Tado V3+ and Tado X radiator
thermostats in Home Assistant.

Roomstat is based on
**[Tado X Proxy Thermostat](https://github.com/kinimodb/ha-tadox-proxy)** by
kinimodb. Read the upstream README for what the integration does, how it
regulates and what its settings mean. This page lists only what Roomstat
changes.

## What's different

**Tado V3+**
- **Works with Tado V3+ radiator thermostats**, not just Tado X. Connect them
  through Home Assistant's **HomeKit Device** integration, paired with the Tado
  Internet Bridge. That connection is local and fast, with no daily limit.
- **Commands stay at 25 °C or below**, the highest value V3+ accepts. The
  original sent up to 30 °C. V3+ refused those commands, so the room didn't
  heat.
- **Cloud fallback.** A valve sometimes drops off HomeKit for a few minutes
  or longer. Pick the room's Tado cloud thermostat under **Configure → TRV
  Communication → Fallback thermostat**, and Roomstat controls the room
  through it until the valve is back. Auto-tune keeps learning meanwhile.
  If the cloud is the more reliable link for you, turn on **Use the fallback
  first**: Roomstat then commands the room through the cloud and uses HomeKit
  only while the cloud is down, still reading the valve over HomeKit for
  auto-tune. Each cloud command costs Tado API calls, so this needs a plan
  with enough daily quota (Auto-Assist).
- **Cloud only.** To stop using HomeKit altogether, pick the room's Tado
  cloud thermostat as **Configure → TRV Communication → Thermostat (TRV)**
  (if it was the fallback, the fallback is removed), then disable the
  HomeKit Device integration. Roomstat recognises a cloud thermostat and
  allows for its polling delay before it re-sends a command.
- **Gentle on the bridge.** Commands from different rooms go out at least
  5 seconds apart. Roomstat checks every minute that each valve still holds
  its last command, and sends it again only if it doesn't, so you can set
  **Overlay refresh interval** to 0. A valve that keeps not showing a command
  is asked less and less often, down to once every 30 minutes.
- **Quick changes don't reach the valve.** After a change from the
  dashboard, a script or a voice assistant (a preset, a temperature, a
  preset temperature, on/off, resume schedule), more heat waits until
  nothing has changed for 15 seconds, so pressing Comfort then Frost
  protection sends nothing. A Tado valve that gets an off seconds after a
  heat command can heat for a quarter of an hour anyway, so an off sent
  within a minute of a heat command is sent once more a minute later. More
  heat also waits while a window's open delay runs. Less heat and off always
  go at once; the schedule, presence and windows wait only within those 15
  seconds. **Heat** sent to a room that is already heating no longer ends
  an open window's frost protection: switch it **Off** and back to **Heat**
  for that.
- **Handles failures.** A valve that is off isn't sent temperatures it would
  refuse. A failed command is tried again a few minutes later and logged
  once, not every time; the thermostat's `last_command_error` attribute shows
  it. **Off** stays off and is sent again if the valve doesn't take it. A
  valve put back on its Tado schedule (from the app, or by an expired timer)
  gets its command again.
- **Fails cold.** Full power on a Tado cloud thermostat can be a timer, so a
  dead Home Assistant, network or cloud can't leave a radiator on for hours.
  A watchdog shuts the valve if regulation stops, and an **Out of band**
  sensor per room reports a room stuck too warm or too cold. See
  [Fail safe](#fail-safe).
- **Repairs for lost entities.** If the valve, the room sensor or any helper
  a room uses is deleted, renamed or disabled, a repair appears under
  **Settings → Repairs** after 5 minutes. **Fix** lets you pick the entity to
  use instead. A room sensor with no reading for 15 minutes is reported there
  too; until it's back, the radiator keeps its last setting.

**Control**
- **A controller made for the Tado valve.** A Tado valve has its own
  controller: it only opens fully when asked for much more than it reads,
  only stays shut when asked for much less, and in between reacts late and
  partly. So Roomstat heats up at full power and shuts the valve early
  enough for the radiator's leftover heat to reach the target. Near the
  target it adjusts the valve firmly (**Kp**, 4 by default), shuts it a
  little ahead of an overshoot, and keeps it shut until the room needs heat
  again: a Tado asked for just under its own reading trickles heat into a
  room that is already warm enough. Rooms hold within about 0.05 °C.
  Settings under **Configure → Controller**.
- **Off means off.** When a room needs no heat, Roomstat switches its valve
  off instead of setting 5 °C, and a valve it finds on its Tado schedule is
  taken back. A valve is always either heating at Roomstat's setpoint or off.
  Heating again is one command: setting a temperature turns an off valve back
  on.
- **Heat-up braking.** **Heat-up braking time** (10 minutes by default) is
  how far ahead Roomstat predicts where the radiator's leftover heat will
  carry the room, so heating stops before the target rather than after it.
  A small deficit that the controller still answers with a strong push
  (2.5 °C or more over the valve's reading, which a Tado answers at about
  full power) stops the same way. Near the target the valve is shut at least 10
  minutes ahead. 0 = no anticipation in heat-ups. Auto-tune can learn it.
- **After a window closes, the room gets time to re-warm by itself.** The
  walls, furniture and radiator kept their heat; only the air went, and it
  comes back on its own within 15–40 minutes. **Window close delay** is the
  minimum: heating then waits until the room is rising less than 1 °C/h
  (at most 30 minutes after the close). 0 resumes at once. Auto-tune does
  not learn from the heating that follows a window.
- **Auto-tune.** Learns Kp, Ki and the braking time of each room in the
  background, from everyday heat-ups. Off by default. See [Auto-tune](#auto-tune).
- **Valve look-ahead (experimental).** A Tado valve looks ahead on how fast
  its own reading moves. After a heat-up, when the radiator cools and the
  valve's reading falls, it opens harder than Roomstat asked. With this on,
  Roomstat lowers its hold commands while the valve's reading falls, by a
  look-ahead it learns from the valve's own answers. Off by default. See
  [Valve look-ahead](#valve-look-ahead).
- **A trace per room.** Diagnostic sensors for the command sent to the valve,
  the room error and the regulation mode, updated every cycle. The first two
  keep long-term statistics, so you can look at a room's behaviour weeks later
  even after the recorder has purged the detail. With debug logging on for
  `custom_components.roomstat`, every cycle also writes one line to the log
  with its whole decision.
- **An activity log per room.** The **Activity** sensor says in a line what
  the room is doing and why, and why it isn't doing something else (no full
  heat yet, a command waiting for the rate limit), with the valve's own
  reading and what the valve has been told. It also notes events (modes,
  schedule, windows, presence, failed commands) and what auto-tune changed
  or why it changed nothing. See [Activity log](#activity-log).

**Modes**
- **Schedule.** The room follows a helper that your scheduler sets. Manual
  changes win for a while, then the schedule takes over again. See
  [Schedule](#schedule).
- **Summer mode.** One switch turns every valve off and keeps it off. See
  [Summer mode](#summer-mode).
- **Away and Off stay** until you change them, also when the schedule moves on.
  An Off ends when someone comes home after everyone was out (below).
- **Eco is called Night.**
- **Presence** can be an `input_boolean` as well as a binary sensor.
- **A change by hand while nobody is home takes effect.** Pick a mode or a
  temperature on your way home and the room heats straight away. The change
  lasts until someone is home, or at most **Configure → Presence sensor →
  Change while away lasts** (240 minutes by default, 0 = until someone is
  home); then the room goes back to Away. A temperature that heats less than
  Away stays. Anything that sets a mode or temperature counts, an automation
  too, so keep schedules in the schedule helper. The original only saved the
  change for when you got back.
- **Off lasts while nobody is home.** Pick **Off** (Frost protection) while
  out, or leave with a room on Off, and it stays Off, not Away, with no
  limit. When someone comes home the schedule comes back (Comfort without a
  schedule). An Off picked while you stay home lasts until you change it.
- **Away or Resume schedule while nobody is home is back to automatic.** Any
  change by hand, an Off included, ends: the room goes to Away (with preheat
  on the way home), and coming home brings back the schedule. It is the easy
  way to undo whatever was set while out, from the cards' Away or Schedule
  buttons or a script. Switching the room off and on again does the same.
- **Preheat on the way home, in shadow for now.** While nobody is home,
  Roomstat works out from [Home ETA](https://github.com/igiannakas/ha-home-eta)
  what each room should aim for: the temperature coming home restores once
  someone is close to home or heading home in time, a little lower while
  someone is nearby. This version only logs what it would do; the room stays
  at Away. See [Preheat on the way home](#preheat-on-the-way-home).
- **Fixes:**
  - the chosen mode survives restarts, reloads and window open/close;
  - a running Boost carries on after a restart;
  - a warm-up running during a restart still stops on time: Roomstat keeps
    the room's recent readings it predicts from;
  - a Boost saved by the window or presence logic comes back as Comfort.

**Dashboard**
- A room card comes with the integration. See [Dashboard card](#dashboard-card).

**Renamed**
- New name and internal ID (`roomstat`). Rooms set up in Tado X Proxy aren't
  taken over; add them again in Roomstat.

**Removed**
- The "Follow physical thermostat" switch and its two settings. If you turn
  the dial on the radiator, the next command from Roomstat overrides it.
- The original PI controller, its derivative brake and adaptive gain
  scheduling (with its four settings), replaced by the controller above.
  Rooms set up before 2.2 are converted on the first start: Kp becomes 4,
  and a braking time of 0 becomes 10 minutes.

## Install

1. In HACS, open **⋮ → Custom repositories**. Add
   `https://github.com/igiannakas/ha-roomstat` as an **Integration**.
2. Download **Roomstat** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Roomstat**.
4. Pick the radiator thermostat (for V3+, the one from HomeKit Device), a
   temperature sensor in the room, and a name. Add one entry per radiator
   thermostat.

Everything else is under **Configure** on each entry.

## Auto-tune

Turn it on per room: **Configure → Auto-tune → Enable auto-tune**.

- It learns from heat-ups: a target that rises by 0.5 °C or more while the
  room is cooler, for example a schedule going from Night to Comfort. The
  dead time and heat-up rate need a rise of 0.6 °C or more.
- It measures a heat-up only up to the room's own peak. If the room stops
  rising and then rises again with no heat (people, sun), that later rise is
  not counted. The status shows how much was left out.
- The first values appear after the first heat-up, and they settle in about a
  week.
- A heat-up carries on after a restart or a settings change. If readings stop
  for more than 10 minutes (a long restart, or a valve that drops off), it
  ends early but keeps what it measured.
- It changes values a little at a time, stays within safe limits around your
  own values, and undoes any change that makes things worse.
- The braking time leans towards stopping early. More braking (a cooler
  landing, topped up after) is taken in full steps. Less braking (a warmer
  landing) moves only part of the way after each heat-up, about 1–2 minutes,
  because one heat-up's result says more about people and the weather than
  about the radiator.
- Sensors per room:
  - Kp, Ki and braking time in use;
  - auto-tune status;
  - what it measured (dead time, coast time, heat-up rate, last overshoot).
- **Reset auto-tune** forgets everything it learned. Changing Kp, Ki or the
  braking time yourself also starts learning again.

You can set the braking time by hand instead, under **Configure →
Controller → Heat-up braking time**, with **Learn heat-up braking** off.

## Valve look-ahead

Turn it on per room: **Configure → Valve look-ahead**, then pick the
zone's heating power sensor (for a Tado zone, `sensor.<zone>_heating`).

- While the room holds near its target, each command is lowered by the
  look-ahead times how fast the valve's own reading is falling (at most
  1.5 °C). A flat or rising reading changes nothing, and heat-ups at full
  power are not touched, so it only ever takes heat away.
- The look-ahead starts at 0. Each top-up from a shut valve teaches it: the
  valve's first heating power report, against what it was asked and how fast
  its reading fell. From 8 top-ups on it moves towards what they show, at
  most 2 minutes per top-up (up to 20 minutes).
- The **Valve look-ahead** sensor shows the value in use, with what the
  learning has seen. The activity log has a line for each top-up it learns
  from.
- The trade-off: in a simulation of a living room it cut the valve's first
  heat after a heat-up by about a quarter, but the overshoot after top-ups
  barely moved, and the room spent more time a little under its target.
  Judge it on your own room's trace sensors.
- **Reset auto-tune** forgets the learned look-ahead too. Another valve
  (a changed TRV) starts afresh.

## Activity log

Each room has a sensor named **Activity** (its entity ID follows the room's
name, for example `sensor.living_room_thermostat_activity`). Its state is the
latest line, so its history (and the logbook) is the room's log. For example:

```
Holding (the coast stalled 0.5 short): 0.5 below, valve pushed 1.9 over its
reading (P +1.4, I +0.00; I rests outside ±0.3; full heat only from 0.8 below)
| room 19.5 → 20.0 Comfort | TRV 19.2 heating | Tado 21.1
```

- A new line is written when the decision changes (the mode, what the valve
  is told, the valve's own state, a command waiting or failing, the target),
  and every 5 minutes while only the numbers move. The thermostat's
  `activity` attribute is always the live line.
- Events get a line too: a mode or temperature change, the schedule, a
  window, nobody home, a command that failed or was sent again, a restart,
  and each auto-tune verdict (what it changed, or why it changed nothing). A
  verdict longer than a state allows continues on the next line (`…`).
- The `log` attribute keeps the last 30 lines, newest first. It isn't stored
  in the recorder. To read it on a dashboard:

```yaml
type: markdown
content: >-
  {% for line in state_attr('sensor.living_room_thermostat_activity', 'log') %}
  - {{ line }}
  {% endfor %}
```

## Preheat on the way home

Shadow only in this version: Roomstat works out what each room would do and
logs it, and the room stays at Away. Nothing is sent to the valve. The
point is a week of decisions to check against real arrivals before it is
switched on.

**Set up**, per room, under **Configure → Preheat on the way home**:

- **Mode:** **Off**, or **Shadow** (log only).
- **Expected arrival:** a timestamp of the first expected arrival home,
  such as Home ETA's `sensor.home_eta_expected_home`.
- **Someone close to home:** on while someone is home or within 800 m, such
  as `binary_sensor.home_eta_someone_close_to_home`.
- **Someone nearby:** on while someone is within their near-home radius,
  such as `binary_sensor.home_eta_someone_nearby`.

The room needs a presence sensor and at least one of the three entities:
preheat only works while away mode is in charge (nobody home, no change by
hand, no window open, heating on, not summer mode). The Preheat sensor says
**Not in charge** otherwise, with the reason in its attributes.

**What each room would aim for**, the warmest that applies:

- **Someone close to home:** the temperature coming home restores (the
  schedule's, for example Comfort 20 °C), however long they stay.
- **Someone nearby:** the **30-minute temperature**: the temperature coming
  home restores, less what this room typically recovers in its first 30
  minutes of heating, rounded down to 0.1 °C. The room is then back at its
  target about 30 minutes after someone walks in, up to about 45 on a slow
  day.
- **Someone heading home:** the temperature coming home restores, from the
  room's heat-up time before the expected arrival. The heat-up time is the
  room's dead time, plus the degrees to go at its heating rate (both learned
  by auto-tune), plus a lead. It stops when the trip home is over, when the
  arrival slips later and the room isn't due any more (it starts again when
  the new arrival is due), or after the longest preheat.
- **Otherwise** Away.

If coming home would heat no more than Away (the schedule says Away or
Frost protection), there is nothing to preheat.

**Manual control wins.** While nobody is home, **Off** keeps the room off
with no preheat until someone is home (then the schedule comes back), and
any other mode or temperature takes effect at once (see "A change by hand
while nobody is home" above); preheat stands aside until either ends.
**Away** or **Resume schedule** puts the room back to automatic: Away, with
preheat. An Away picked while someone is home isn't carried into the
absence: when the house empties it becomes away mode and preheat applies as
usual.

**The settings**, under **Configure → Preheat timing** (each is explained
there too):

- **Lead** (5 minutes): added to the heat-up time, so the room is at its
  temperature a little before the expected arrival, not on the dot. Raise
  it if arrivals often come earlier than expected.
- **Heating rate factor** (0.8): the heat-up time uses this share of the
  room's learned heating rate. Auto-tune's rate is the steepest rise of a
  heat-up; the whole heat-up averages slower. Lower starts earlier. With
  0.8, 34 of 37 recorded heat-ups would have started early enough, typically
  24 minutes early; with 1.0, rooms start only 4–8 minutes later and three
  more would have been late.
- **Arrival slip** (20 minutes): if the expected arrival moves more than
  this much later than when the room started, and the room isn't due yet for
  the new time, it stops heating rather than waste it. It starts again when
  the new arrival is due. Raise it if traffic stops it too often.
- **Longest preheat** (240 minutes): a preheat for someone heading home
  stops this long after it first started for that trip home, and doesn't
  start again on it. Slips, restarts and breaks (a window, a change by hand)
  don't reset it. A backstop for a trip home that never ends.
- **Level drop delay** (10 minutes): a drop to a cooler level (close to
  home, then nearby, then Away) waits this long; a rise applies at once. A
  phone hovering at 800 m or at the near-home radius can't make the target
  flap.
- **Arrival lost grace** (10 minutes): if the expected arrival goes unknown
  (the trip home is over, or Home ETA is reloading), a room already heating
  for it carries on this long before it stops. Nothing new starts meanwhile.
- **Default 30-minute recovery** (0.5 °C): used for the nearby temperature
  until the room has learned its own from two heat-ups.
- **Default heating rate** (1.0 °C/h) and **default start-up delay** (20
  minutes): used for the heat-up time until auto-tune has learned the room's
  own, or while auto-tune is off. Slow on purpose: starting early wastes a
  little, starting late arrives cold.

The 800 m and the near-home radius are Home ETA's.

**What it shows**, per room:

- **Preheat** sensor: the decision (Off, Not in charge, Away, Someone
  nearby, Someone close, Heading home). Its attributes are what the
  decision was made with (the target, the room's temperature, the heat-up
  time, when it was due, the expected arrival, the learned values it used),
  fixed until the next decision, so its history is a list of decisions.
- **30-minute recovery** sensor: how much the room typically rises in its
  first 30 minutes after its target rises by 0.5 °C or more, with the room
  0.5 °C or more below it (the median of the last 10). Not counted: Boost,
  the half hour after a window, a heat-up interrupted (a window, the room
  switched off, the target changed), and one that reaches its target within
  the 30 minutes (it shows only that the room can do at least that much). It
  learns whatever the mode, so a room has its own value by the time preheat
  is switched on.
- The thermostat's `preheat_*` attributes: the live numbers, updated every
  minute (not recorded: the Preheat sensor's history has the decisions).
- The activity log: one line per change of decision, for example
  `Preheat (shadow): heading home: it would heat to 20.0 °C now for the
  expected arrival at 18:42: from 17.4 °C it needs 106 min (start-up 22 min,
  2.6 °C at 0.8 × 2.5 °C/h, lead 5 min)`, and one per learned 30-minute
  recovery.

## Schedule

**Configure → Schedule → Schedule helper:** an `input_select` that your
scheduler sets to `comfort`, `night`, `away` or `frost_protection`.

- The room switches to whatever the helper says.
- A manual change (preset, temperature or Boost) wins until the next schedule
  change, or until the **Override duration** has passed. 0 means until the
  next schedule change.
- **Resume schedule** (a button, and a preset) ends a manual change straight
  away.

## Summer mode

**Configure → Summer mode switch:** an `input_boolean`. Use the same one for
every room.

- **On:** every valve is held off. Changes from Home Assistant are refused,
  and a turn of the dial on the radiator is put back.
- **Off:** every room goes back to its schedule, or to Comfort.

## Fail safe

Every command Roomstat sends stays on the valve until the next one. If Home
Assistant, the network or the Tado cloud stop while a room heats at full
power, the valve stays open. With Tado cloud thermostats, full power can
expire on its own:

1. In the Tado app, set the schedule of every zone Roomstat controls to
   **Off**, all day. When full power expires, or you end Roomstat's
   setting in the app, the zone goes back to this schedule, and Roomstat
   takes it back (heat at its setpoint, or off) once it notices.
2. Then, per room, set **Configure → TRV Communication → Full-power time
   limit** to 3600 s. Full power then goes to Tado as a one-hour timer,
   renewed every half hour while it's still needed (one extra command per
   half hour of heating). Everything else, off included, stays until the
   next command. 0, the default, leaves full power on until the next command.

If no regulation cycle completes for 10 minutes (Home Assistant busy, no
reading from the valve, an error), Roomstat logs a warning, sets the
thermostat's `regulation_stalled` attribute, and switches a valve left at
full power off.

Roomstat never sends 5 °C to shut a valve, because "5 °C" isn't 5 °C in the
room if you set negative temperature offsets on the valves in the Tado app
(they read low by that much): a valve at 5 °C opens when the room falls to
about 10–15 °C. That still applies to a 5 °C Tado schedule, and possibly to
a valve's own frost protection while it is off (untested).

**Out of band.** Each room has an **Out of band** sensor (named after the
room's device: look under **Settings → Devices**). It turns on when the room
stays 0.5 °C too warm for 30 minutes, or 0.5 °C too cold for 2 hours, while
heating is on, with no summer mode, no window open and a working room sensor:

- A room still cooling down after its target was lowered (Night) isn't too
  warm. One that warms up again by 0.5 °C from its lowest point is — if the
  radiator did it: the valve reported heating while the room was too warm,
  or it heated long enough in the half hour before to explain the rise
  (7.5 minutes for 0.5 °C; a short top-up can't), or the valve's own reading
  stands 2 °C further above the room than when the radiator was cold. A
  room warmed by the sun, people or an appliance is **warm**, not an
  alarm: the sensor stays off and `reason` says `warm`.
- The 2 hours start again with each rise of the target, so a heat-up doesn't
  count.
- Attributes: `reason` (`hot`, `cold`, or `warm` while off), `deviation_c`
  (room minus target), `since` (when it went out of band, set while on or
  warm), `radiator_heated`, and the evidence for it: `heated_during`,
  `heated_before_min` and `radiator_rise_c`. The room's activity log says
  the same in words.
- Change the limits under **Configure → Out-of-band alarm**.

A notification for it (put your own entity ids in):

```yaml
alias: Room out of band
triggers:
  - trigger: state
    entity_id:
      - binary_sensor.living_room_thermostat_out_of_band
      - binary_sensor.study_thermostat_out_of_band
    to: "on"
actions:
  - action: notify.notify
    data:
      title: "{{ state_attr(trigger.entity_id, 'friendly_name') }}"
      message: >-
        Too {{ state_attr(trigger.entity_id, 'reason') }}:
        {{ state_attr(trigger.entity_id, 'deviation_c') }} °C off the target
        since {{ as_timestamp(state_attr(trigger.entity_id, 'since'))
        | timestamp_custom('%H:%M') }}.
```

## Dashboard card

Add a card, choose **Roomstat room** and pick the thermostat. Or use YAML:

```yaml
type: custom:roomstat-card
entity: climate.living_room_thermostat
```

| Option | What it does |
|---|---|
| `entity` | The Roomstat thermostat (required). |
| `name` | The title. Defaults to the area name. |
| `icon` | The room icon. Defaults to the area icon. |
| `heating_entity` | The valve's heating % sensor, if it isn't found automatically. |

It shows the room and target temperature, the heating %, and what the room is
doing. Buttons: Off, Night, Day, Boost and Schedule.

## License

MIT. The original work is © kinimodb, and the Roomstat changes are
© igiannakas. See [LICENSE](LICENSE).
