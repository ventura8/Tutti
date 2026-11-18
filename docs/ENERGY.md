# Local models and their energy cost

Local models cost no quota, but they cost electricity and they heat the machine. The dashboard's **Usage & pacing** card
shows them in a category of their own, **Local models**, below the subscription accounts: one card per machine running
local models. Every figure there is an **estimate**; each card's *Estimates: how they are made* says which parts were
measured on that machine and which were estimated. The code is `tutti/energy.py` (pure) and `tutti/web/energy.js`.

## Which machines

- the machine of each Ollama worker (`workers.<w>.machine`), else this computer;
- any machine with `energy.machines.<m>.ollama: host:port` (an Ollama that no worker names, read with `curl` on that
  machine over its own ssh).

A dedicated pseudo-machine (`machines.<m>.dedicated`, a desktop session for UI runs such as `laptop-desktop`,
`nuc-desktop` or `w5090-win`) on the same computer as a regular machine (the same `host`) is that machine
(`config.physical`): its Ollama workers join the host's card, it is probed once, and its power and energy are never counted
twice. The fleet keeps no models entry for it either. Each card's `data-machine` is the machine's name.

When the power read fails it is tried once more; the models are still read from the worker's host, so the card lists them
and says only the power was not read (on this computer the reason never mentions ssh). The GPU's name falls back to
`lspci` when no power tool names it.

## What each card shows

| Line | Where it comes from |
| --- | --- |
| Loaded and installed models | Ollama's `/api/ps` and `/api/tags` (from here through the worker's host or tunnel, else on the machine). `(CPU)` marks a model with nothing in VRAM. |
| Busy, jobs, tokens (today, the last 7 days, this calendar month) | `<state>/local-runs.jsonl`: one line per local-model call Tutti made (`tutti/local.py`): the worker, start, end and Ollama's `eval_count` and `prompt_eval_count`. A call in flight leaves a `local-run-*` marker, so the machine counts as busy while it runs. |
| Power now | The read-only `energy.power_script` over the machine's existing ssh (shared connections), at each dashboard probe. |
| kWh and cost | `<state>/energy.json`: per machine and day, the seconds and Wh per local hour, busy and idle. |
| Per job, per busy hour | This month's cost over its jobs; the busy power (the measured busy average once there is a minute of it, else the busy estimate) × the price now. |
| Cloud comparison | This month's tokens priced at a cloud API's list price (`energy.compare_with`). |
| Availability | The machine's dashboard row: paused by the owner, yielding (its yield flag is up), thermal (its reason names a temperature), offline. |

## Power

Read in this order, the first that answers wins:

1. **NVIDIA**: `nvidia-smi --query-gpu=name,power.draw,utilization.gpu,power.limit` (in WSL `/usr/lib/wsl/lib/nvidia-smi`).
2. **AMD**: `rocm-smi --showpower --showuse --json`.
3. **Intel**: `xpu-smi dump -m 0,1`, then `intel_gpu_top -J` (it often needs root or `perf_event_paranoid` lowered).
4. **hwmon**: the `xe`, `i915` or `amdgpu` energy counter read twice, one second apart (else `power1_input`).

The CPU package power is the RAPL counter (`/sys/class/powercap/intel-rapl:0/energy_uj`) read the same way; recent kernels
make it root-only, so it is often estimated.

The power line says `measured` or `estimated` for the GPU and the CPU (`≈` only when the GPU is estimated). When the machine
refused a reading, the card's *how they are made* gives the one-time fix: `scripts/apply-power-rules.sh` (as root) applies the
required permissions.
The period table keeps every number on one line and scrolls sideways inside its card on a narrow screen.

Where nothing is readable, the estimate is:

- GPU: TDP × (0.1 + 0.9 × utilisation); utilisation is the reading, else 90 % while a Tutti run is on the machine and 0
  otherwise. The TDP comes from `energy.machines.<m>.gpu_tdp_w`, else a small table of known GPUs (`energy.TDP`), else 100 W.
- CPU: `cpu_tdp_w` (default 65 W) × 0.9 while a model runs on the CPU, 0.3 while one runs on the GPU, 0.1 idle.
- Base: `base_w` (default 30 W): board, memory, disks and the power supply's loss.

Whole system = GPU + CPU + base.

## Energy

At each probe (every `dashboard.probe_seconds`), the time since the machine's previous sample, at most two probe
intervals, is added at the power just read:

- as **busy** while a Tutti local-model call is running on that machine;
- as **idle** while one of its workers' models stays loaded and the last Tutti call there ended less than
  `energy.keep_alive_minutes` ago (Ollama's default keep-alive), when `energy.include_idle` is on: the draw Tutti causes by
  keeping a model loaded;
- else not at all: the owner's own use and other programs are not Tutti's.

kWh is that integral of power over time (`energy.integrate` does the same for any span of samples).

## Cost and currency

Cost = kWh × the price of the hour it was used in (a time-of-day tariff prices each hour on its own).

The country is found in this order: `energy.country`, the computer's time zone (`/etc/timezone` or `/etc/localtime`,
through `zone.tab`), `LC_MONETARY`, `LC_ALL`, `LANG`. The time zone comes before the locale because many people run an
English locale where they live: a computer in `Europe/Bucharest` with `en_US.UTF-8` is in Romania, and pays in RON. The
currency follows the country unless `energy.currency` says otherwise; then the typical price is converted with the
bundled exchange rate (a currency the table does not know gets the EU average in EUR) until you set your own price.

The price per kWh starts from a bundled table of typical household tariffs by country, in local currency, all taxes
included, rounded (`energy.PRICES`; sources: Eurostat `nrg_pc_204`, households 2 500-4 999 kWh a year, 2025 S1; the US
EIA residential average, 2025; ANRE for Romania and Ofgem for Great Britain; checked 2026-11). A country not in the table
gets the EU average in EUR. The card says **estimated** until you set `energy.price_per_kwh` from your bill.

The cloud comparison uses the providers' public API list prices (US dollars per million input and output tokens,
`energy.CLOUD`, checked 2026-11) converted with a bundled approximate exchange rate (`energy.PER_USD`). It is the order of
magnitude of what local models save, not a quote.

## Pacing

Local models have no quota. The card shows the **cost per busy hour**. With a time-of-day tariff and
`energy.prefer_cheap_hours: true`, the autopilot sends no task to a local model while the price is more than 10 % above the
day's cheapest, and says until when it waits. The subscription accounts' pacing is unchanged.

## Settings

The dashboard's Settings tab, section **Energy**, edits them in place; all under `energy:` (`tutti.config.example.yaml`): `country`, `currency`, `price_per_kwh`, `tariff`
(`[{hours: '23-07', price: 0.9}]`, local hours), `prefer_cheap_hours`, `include_idle`, `keep_alive_minutes`,
`compare_with` (`claude-sonnet`, `claude-haiku`, `gemini-flash`, `gpt-mini`) and per machine `machines.<m>`: `base_w`,
`gpu`, `gpu_tdp_w`, `cpu_tdp_w`, `ollama`.

## Limits

- Calls made outside Tutti (another program using the same Ollama) are not counted as Tutti's.
- `tutti conduct` with a local main agent is not yet logged per call.
- Ollama does not say whether a model is answering right now; Tutti knows only its own calls.
