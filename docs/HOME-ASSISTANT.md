# Home Assistant: Tutti's household partner

Tutti integrates with Home Assistant to manage household devices, monitor environment and presence, and report job status.

## Configuration

Add the Home Assistant URL and token to your local `tutti.config.yaml` (which is git-ignored):

```yaml
home_assistant:
  url: http://192.168.1.98:8123
  token_file: ~/.config/tutti/secrets/home-assistant.token
```

## Roles

1. **Device control**: Control TVs, media players, and tablets via Home Assistant (power, app launch, volume, media playback).
2. **Presence detection**: Phones and room occupancy trigger quiet mode or machine availability.
3. **Energy monitoring**: Power consumption data from smart plugs/meters is used to calculate cost per task.
4. **Power management**: Wake/shut down machines and chargers via smart plugs.
5. **Environment monitoring**: Temperature and schedules (night mode) from Home Assistant sun/schedules.
6. **Notifications**: Needs attention and finished jobs sent to HA mobile app / TTS speakers.
7. **Household dashboard**: Tutti sensors (jobs, queue, usage) and buttons (pause, quiet, yield) exposed to Home Assistant.
8. **Automations**: Events triggered by Home Assistant change Tutti's behavior (e.g., owner leaves -> more work).
9. **Safety**: Read-only by default; writes only to listed entities.

## Testing

For testing, use a fake Home Assistant server that mocks the API. See `tests/fixtures/fake_ha.py`.
