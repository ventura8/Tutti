# Home Assistant

Set up the entities in Home Assistant first, then add their IDs to your local `tutti.config.yaml`. Put the Home
Assistant long-lived access token in a private file on the dispatcher's host. Give Tutti its path and the direct
Home Assistant URL. The connector sends the token only to that URL and refuses redirects.

```yaml
home_assistant:
  url: https://ha.example
  token_file: ~/.config/tutti/secrets/home-assistant.token
  presence_entities: [person.example, binary_sensor.office_occupied]
  machine_presence:
    builder: [binary_sensor.office_occupied, binary_sensor.builder_hot]
  energy_entities:
    builder: sensor.builder_power
  writable_entities:
    - media_player.example_apple_tv
    - remote.example_apple_tv
    - media_player.example_speaker
    - tts.example_engine
    - sensor.example_tutti
    - script.test_bench_start
    - script.test_bench_stop
  status_entity: sensor.example_tutti
  notify_service: mobile_app_example_phone
  tts_engine: tts.example_engine
  tts_speaker: media_player.example_speaker
  test_power:
    bench:
      machine: builder
      start: script.test_bench_start
      stop: script.test_bench_stop
```

Every write is opt-in. List individual entities you control. Leave shared and other users' Plex entities out of
the allow list. Inspect the targets inside any allowed Home Assistant script and any media group before enabling it.
The separately configured `notify_service` authorizes just that mobile app destination.

## Media tests

Use the connector from the test harness on the dispatcher. Power, volume, keys, app IDs and playback metadata
depend on the Home Assistant integration and the capabilities of the specific entity.

| Device | Entity and operation |
| --- | --- |
| Apple TV | Use its media player for power and volume. Launch a source with `control_media_player(cfg, player, 'source', source=app)`. Send keys to its remote. |
| Sony TV | Use the Bravia media player for power and volume. Launch apps with `control_media_player(cfg, player, 'app', app=app)` and use the Bravia remote for keys. Use its Google Cast entity for playback metadata when available. |
| Google Streamer | Use the Android TV Remote integration for keys and `launch_activity(cfg, remote, deep_link)` for apps. Use the Cast media player for playback status. |
| HT-A7000 | Use the media player or remote exposed by your HA setup for supported power, source, volume and key actions. |
| Pixel Tablet | Use the media player and remote exposed by your HA setup. App launching and key injection require an integration that exposes those actions. |

`tutti.homeassistant.press_key(cfg, remote, command)` uses the key name Home Assistant exposes.
`get_now_playing(cfg, player)` returns the app, media title, artist and player state reported by HA.
Wrap playback tests with `temporary_volume(cfg, player, level)` to save the volume, apply a test level from 0 to 1,
and restore the previous level on exit.

[Apple TV](https://www.home-assistant.io/integrations/apple_tv/) ·
[Sony Bravia](https://www.home-assistant.io/integrations/braviatv/) ·
[Android TV Remote](https://www.home-assistant.io/integrations/androidtv_remote/)

## Presence, environment and energy

A configured `home`, `on` or `occupied` state activates quiet mode or reserves the named machine's slots.
The dashboard refreshes machine availability at each probe. When everyone leaves, the next successful probe
releases the HA reservation. HA automations can expose room occupancy, a TV-in-use flag, a night schedule or a
temperature threshold as binary sensors. Map those sensors to the quiet mode or machine behavior you want.
When a sensor is unavailable Tutti follows its existing local checks.

Map each machine's whole-system power sensor to `energy_entities`. Tutti reads watts or kilowatts at every dashboard
probe and uses those readings in the Local models energy cards. A managed meter also gets a card on a machine
without an Ollama worker. Synced-tree tests sample the meter before and after the remote run and use any intervening
dashboard readings to estimate kWh and cost in the finished job row. Set `energy.price_per_kwh` or `energy.tariff`
to your actual tariff. The estimate covers the machine's whole draw during the run window, including other
activity if it shares the meter.

## Test power and notifications

For a track in `test_power`, the regular synced-tree `tutti test` selects the named machine unless a machine or device
was explicitly chosen. Tutti runs the start script before sending inputs and the stop script after the run and
result fetch, including test failures. Scripts run only when the test is assigned to their configured machine.
Configured machine presence sensors must be readable and clear before Tutti starts a power-managed test.
A start script should wait for the machine to be ready. A stop script should request graceful shutdown, confirm
shutdown and check for other workloads before switching off a machine's outlet. Use dedicated test resources or
arbitrate access within HA when other dispatchers or jobs can use the same hardware. Handle charger battery limits
and cleanup after an interrupted dispatcher in the HA scripts or automations. Each script call waits up to two minutes.

New owner questions and completed tests, transfers and worker runs can generate Companion app notifications and
TTS. The messages contain status and an identifier for an owner question. Resolve the question in Tutti.
[Home Assistant TTS](https://www.home-assistant.io/integrations/tts/)

Connector tests use fake HTTP responses. The tests and repository examples use no live Home Assistant instance.

## Home Assistant dashboard

Set `status_entity` and allow-list that sensor. The active Tutti dashboard publishes the running job count as the
sensor state, plus queued and stalled counts, the highest account usage percentage and local-model energy cost
for the month as attributes. Add an entity card in Home Assistant to display them.

Use Home Assistant toggle helpers for household controls. A toggle in `presence_entities` requests quiet mode.
A toggle in a machine's `machine_presence` list reserves its slots until turned off. Put the helpers on the
same HA dashboard, or set them from your automations. Tutti applies those changes at the next probe.
