### Added

- Decisional model (t440): `decisional.candidates` lists free models best first; Tutti picks the strongest suitable machine
  (measured graphics memory, or `decisional.hardware`) and the first candidate that fits, asks it first for judgement steps
  (unblocker, triage, scout, Needs-you, welcome-back summaries, routing advice) through `brain.ask`, and fetches it when
  missing (`tutti decisional pull`, or `decisional.auto_pull` in the dashboard probe, never while a person is at this
  computer). Owner questions, paid agents and the online fallback are unchanged. `tutti decisional [status|pull|advise]`.

### Fixed

- Scout webhook HTTP 400: surface paused-routine and GitHub Connect-apps / wrong-repo causes from the response body
  instead of only the generic "confirm the routine is not paused" hint (t443).
- Invalid `GH_TOKEN` / `GITHUB_TOKEN` in the agent shell no longer shadows the GitHub CLI keyring: Tutti retries without
  those vars, and `tutti doctor` names them when they are refused (t443).