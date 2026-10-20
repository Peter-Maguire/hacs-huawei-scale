# Huawei Scale 3 for Home Assistant

A custom integration (installable with [HACS](https://hacs.xyz)) that reads the
**Huawei Scale 3** body-composition scale over Bluetooth Low Energy and exposes
the results as Home Assistant sensors.

It is a port of the Huawei driver in [openScale](https://github.com/oliexdev/openScale)
(see [openScale issue #763](https://github.com/oliexdev/openScale/issues/763),
which requested Scale 3 support), reworked around Home Assistant's Bluetooth
stack so it works with local adapters **and** ESPHome Bluetooth proxies.

> **Status: unverified on real hardware.** The author of this port did not have
> a Scale 3. What *has* been checked: the framing, CRCs, key derivation, AES
> encryption and measurement decoding reproduce, byte for byte, a real capture
> from a Scale 3 taken from openScale's own test suite; the body-composition model
> reproduces openScale's reference vectors; and the full connect → authenticate →
> sync → measure → history flow runs against a *simulated* scale
> (`tests/fake_scale.py`). A simulation built from the same understanding cannot
> catch firmware behaviour nobody has documented, so please report what you
> see (the **Status** sensor and debug log below make that easy).

## What you get

| Entity | Notes |
| --- | --- |
| Weight, BMI, Heart rate | |
| Body fat, Body water, Skeletal muscle, Protein | % |
| Bone mass, Lean body mass | kg |
| Basal metabolic rate, Visceral fat | kcal, index |
| Measured user, Last measurement | who stepped on, and when |
| Impedance | diagnostic, disabled by default |
| **User** (select) | which profile the scale is set up for |
| **Status** (diagnostic) | `idle`, `connecting`, … `auth_error`; `last_error` attribute |

Values persist across restarts. Every weigh-in also fires a
`huawei_scale3_measurement` event, so you can route data per person:

```yaml
automation:
  - alias: Log weigh-in
    triggers:
      - trigger: event
        event_type: huawei_scale3_measurement
    conditions:
      - condition: template
        value_template: "{{ not trigger.event.data.history }}"
    actions:
      - action: notify.notify
        data:
          message: >
            {{ trigger.event.data.user }}: {{ trigger.event.data.weight_kg | round(1) }} kg,
            {{ trigger.event.data.body_fat_percent | round(1) }} % fat
```

## Requirements

* Home Assistant **2025.8** or newer with the Bluetooth integration working
  (a local adapter or an ESPHome Bluetooth proxy with active connections).
* A Huawei Scale 3. (Scale 3 **Pro**, Scale 2 Pro and Honor scales use the same
  family of protocol but behave differently; they are deliberately not matched.)
* **An authentication key (CAK, C1 and C2, concatenated)** — see below. This
  is the one non-obvious part.
* A Linux/Home Assistant OS host. The key derivation needs the scale's real
  MAC address, which macOS hides from Bluetooth clients.

## The authentication key (CAK, C1, C2)

The Scale 3 does not just hand out its readings. Each session it proves
knowledge of a shared secret (the *CAK*), and the data is encrypted with a key
derived from two further secrets (*C1*, *C2*) and the scale's Bluetooth
address. Each is 16 bytes.

Setup asks for all three as **one field**: 96 hex characters, CAK then C1 then
C2 concatenated with nothing in between (whitespace, `:` and `-` are stripped
for you, so a spaced or colon-grouped string also works). The integration
splits it into the three values itself; nothing is derived or computed beyond
that split.

**This integration does not supply the keys, cannot generate them, and contains
no extraction code.** Where they come from is not documented anywhere I could
verify. openScale's maintainer keeps them user-supplied and does not discuss
obtaining them in its tracker, and reports there suggest the values may not be
unique to each scale (so they may not be something your own scale generates).
Treat the combined key like a password: it goes into this config entry only
(redacted from diagnostics), never into any file in this project.

## Installation

1. HACS → ⋮ → **Custom repositories** → add this repository, category
   **Integration** → install **Huawei Scale 3** → restart Home Assistant.
   *(Manual: copy `custom_components/huawei_scale3` into your config's
   `custom_components` folder.)*
2. Step on the scale so it wakes up and starts advertising. Home Assistant
   should offer **Huawei Scale 3** under *Discovered*; otherwise
   **Settings → Devices & services → Add integration → Huawei Scale 3** and pick it
   or type its Bluetooth address.
3. Enter the authentication key, then your profile (name, sex, date of birth, height).

## Using it

The scale only advertises while someone is standing on it, so the integration
simply waits. When it hears the scale it connects, authenticates, **tells the
scale who is about to be weighed**, waits for the measurement, publishes it and
disconnects. It then ignores the scale for 60 s (configurable) so one weigh-in
is not read twice.

Because the scale needs your age, height and sex *before* it measures, it
assumes whoever steps on next is the person chosen in the **User** select.
Several people: **Settings → Devices & services → Huawei Scale 3 → Configure →
Add a person**, then switch the **User** select before weighing (an automation
on a phone/NFC tag works well).

Body composition (fat, water, muscle, bone, protein, BMR, visceral fat) is
computed from the scale's impedance reading with openScale's model of the Huawei
Health app's adult algorithm. That model covers **adults (18+)** only; for
younger profiles you get weight, BMI, heart rate and the scale's own fat %.
If the scale sends no impedance (shoes on, dry feet), you get weight only.

**Options** (Configure): add/remove people; *read stored measurements* (off by
default) which replays measurements the scale kept while Home Assistant was out
of range as `huawei_scale3_measurement` events with `history: true`
(they do not overwrite the sensors); and the cool-down.

## Troubleshooting

* **Nothing happens when I step on the scale.** Check the **Status** sensor and
  the Bluetooth integration's *Advertisement monitor* for the scale. A local
  adapter/proxy must be within range *and* allow active connections.
* **`auth_error` / re-authentication prompt.** The CAK, C1 or C2 is wrong. After three
  consecutive failures Home Assistant asks you to re-enter the keys.
* **`error`: timed out waiting for a measurement.** You stepped off before the
  scale finished (about 20–30 s with bare feet).
* Debug log:

  ```yaml
  logger:
    logs:
      custom_components.huawei_scale3: debug
  ```

  When reporting a problem, include the log and the diagnostics download (keys,
  address and profile data are redacted).

## How it works

Frames: `DB/DC` writes and `BD/CD` notifications carrying ≤ 15 payload bytes, a
frame counter and a CRC (XMODEM outbound, MODBUS inbound). Handshake:
challenge `randA` ← scale; we answer `randB ‖ HMAC` keyed by the CAK; verify the
scale's HMAC; derive a root key from C1, C2 and the MAC; send a random work key
AES-CTR-encrypted with it; everything after that is encrypted with the work
key. Code: `protocol.py` (wire format and crypto), `session.py` (the flow),
`body_composition.py` (model), `coordinator.py` (Home Assistant lifecycle).

The integration also reads `GET_WEIGHT_UNIT` and writes `SET_WEIGHT_UNIT` (1 =
kg, 2 = lb) to match the scale's own display to whichever unit system Home
Assistant is configured with. It does not write the scale's standalone
`SET_AGE`/`SET_GENDER`/`SET_HEIGHT` characteristics: their UUIDs are known,
but not their payload layout, and `SET_USER_INFO` (used here, and checked
byte-for-byte against a real capture) already carries the same data in a
format that's confirmed correct. It also doesn't touch the scale's Wi-Fi
provisioning, OTA update or reset/delete-user-data characteristics — out of
scope for a BLE weigh-in reader, and the last two are destructive.

## Development

```bash
pip install -r requirements_test.txt
pytest
```

Home Assistant does not run natively on Windows; use WSL or a Linux container.

## Credits and licence

The protocol knowledge, GATT layout and body-composition coefficients come from
the openScale project and its contributors, who reverse-engineered them from a
real scale. This repository is a derivative work and is therefore licensed
under the **GNU GPL v3** (see `LICENSE`), like openScale.

This is an unofficial project, not affiliated with or endorsed by Huawei.
