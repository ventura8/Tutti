# Verdict: would Tutti, fully built, beat the competition?

Written 2026-11-14 for the owner, who asked for an honest answer. Inputs: `README.md`, `docs/ROADMAP.md`,
`docs/COMPETITORS.md`, the code (about 1,700 lines of Python), and a fresh web check of every vendor fact on
2026-11-14. Where a fact comes only from a secondary source it says so. "Unverified" means no primary source was found.

**Short answer.** No. As a whole product (a PWA "best IDE of 2026", chat, remote access, designers, Figma, Android,
macOS, telemetry), Tutti would arrive one to two years late into a market run by companies with billions in revenue,
and would still be behind on the parts users see first. **A narrower Tutti is worth building**: the cross-vendor
quota brain (pacing, panic mode, routing to the cheapest worker that has room) plus the own-machines test queue, as a
CLI + TUI + JSON layer that plugs into the UIs others already ship. Nothing on the market does that part today.

## What changed since COMPETITORS.md (same day, re-checked)

- **Conductor was acquired by Meta** (announced 2026-11-04/05): sign-ups closed, Conductor Cloud shuts down 2026-11-20,
  the local app winds down in early 2027 ([InfoWorld](https://www.infoworld.com/article/4245065/meta-acquires-conductor-as-ai-coding-shifts-toward-multi-agent-development)). Do not adopt.
- **Superset is now Paradev** (macOS first, Linux experimental, no Windows) ([repo](https://github.com/superset-sh/superset), [pricing](https://paradev.com/pricing)).
- **Copilot moved to GitHub AI Credits** at $0.01 each; Pro $10 = 1,500 credits, Pro+ $39 = 7,000, Max $100 = 20,000;
  Business $19 and Enterprise $39 per seat ([plans](https://docs.github.com/en/copilot/get-started/plans)). Its CLI
  takes a local model through `COPILOT_PROVIDER_BASE_URL` (Ollama, vLLM).
- **The big vendors closed several of Tutti's "unique" gaps this year:** Cursor runs cloud agents on your own
  self-hosted machines (up to 200 per user) ([docs](https://cursor.com/docs/cloud-agent/self-hosted)); Codex has remote
  SSH on all plans ([docs](https://developers.openai.com/codex/remote-connections)); Claude Code has agent view,
  Remote Control and phone apps ([docs](https://code.claude.com/docs/en/remote-control.md)); Antigravity has Remote
  Control from any browser and per-5-hour quotas with an overage switch ([docs](https://antigravity.google/docs/plans));
  Friday hands work to another agent when one hits its usage limit ([site](https://tryfridaywork.com)).
- **ACP (Agent Client Protocol) won**: 51 agents in the official registry, including Copilot, Cursor, Gemini CLI,
  Kiro, OpenCode, Goose, Cline, Qwen Code; Claude Code and Codex through adapters
  ([registry](https://agentclientprotocol.com/get-started/agents)). Hand-written worker adapters are now a liability.

## 1. Feature comparison

Tutti column = **as planned** in `ROADMAP.md`, not as built (today it is a CLI). Y = yes, P = partial, N = no,
? = unverified. Vendor cells from the vendors' docs, checked 2026-11-14; links are in §2 and §3.

| Capability | Tutti (planned) | Claude Code | Cursor | Antigravity | Copilot (agent, Agent HQ, CLI) | Codex | Devin | Orca | Emdash | Friday | Warp (Oz) | Kiro |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Parallel agents, worktree per task | Y | Y (agent view, subagents, teams) | Y (cloud agents) | Y (`/agents`, `/swarm`) | Y (several agents per issue) | Y | Y | Y | Y | Y | P (cloud runs) | P (own subagents) |
| Drives other vendors' agents | Y (Claude, Cursor, agy, Copilot, local) | N (Claude only) | N | N | P (Claude + Codex) | N | N | Y (any CLI) | Y | Y (16 tools, any ACP) | P (Claude Code + Codex) | N |
| Local models (Ollama) | Y | P (through Ollama's Anthropic API) | ? | ? | Y (CLI BYO endpoint) | Y (`--oss`) | ? | via agents | via agents | via agents | P (Business) | ? |
| Your own machines / SSH test boxes with slot queues | **Y (unique: slots, memory and heat guards, fall-over)** | P (local + Remote Control) | P (self-hosted workers) | N | P (self-hosted runners ?) | P (remote SSH) | P (Enterprise) | P (SSH host per worktree) | P (SSH) | N | P (Enterprise) | N |
| Quota pacing to the reset | **Y (unique)** | N (limits, dashboards) | N | N (overage switch) | N (budgets) | N (dashboard) | P (ACU budgets) | N | P (cloud spend limits) | P (handoff at limit) | N | N |
| Panic mode: reroute work across vendors near a cap | **Y (unique)** | N | N | N | N | N | N | N | N | P (handoff) | N | N |
| Cross-vendor cheapest-worker routing by task type | **Y (unique as policy)** | N | N | N | N | N | P (model routing, own) | N | N | P (lead agent assigns) | P | N |
| Peer sessions sharing quota and machines | Y | P (cross-session messaging) | N | N | N | N | N | N | N | N | N | N |
| Shared-machine yield flag | **Y (unique)** | N | N | N | N | N | N | N | N | N | N | N |
| Translation with mandatory QA | **Y (unique)** | N | N | N | N | N | N | N | N | N | N | N |
| TUI | Y | Y | Y | Y | Y | Y | Y | P | N | N | terminal app | Y |
| Desktop / web / PWA | Y (PWA) | Y (desktop, web) | Y (IDE, web) | Y (app, IDE) | Y (IDEs, web) | Y (app, cloud) | Y | Y (desktop) | Y | Y | Y | Y (IDE) |
| Phone access / remote control | Y (QR pairing, relay) | Y (iOS, Android) | P (iOS) | Y (browser) | Y (GitHub Mobile) | Y (iOS, Android) | P (web) | Y (iOS, Android) | P | ? | ? | ? |
| Rich chat (queue, @-mentions, attachments, voice) | Y | Y | Y (voice since 2026-11) | Y | Y | Y | Y | via agents | via agents | P | Y | Y |
| Switch main agent with shared context | Y (claimed "no extra tokens") | N | P (models) | P (models) | P (models) | N | N | P | P | P | P | N |
| Plan mode / checkpoints | Y | Y / Y | ? / ? | Y / Y | Y / ? | Y / ? | ? | via agents | via agents | P | ? | Y (spec) |
| Issue → PR, review agent | Y | Y (Actions, Code Review) | Y (Bugbot) | ? / P | Y / Y | Y / Y | Y / ? | Y / ? | Y / ? | P / Y | ? | P |
| Spec-driven tasks | Y | ? | ? | P (plan artifacts) | P | ? | P | ? | ? | P | ? | **Y (the reference)** |
| Hooks, MCP (Figma MCP) | Y | Y, Y | ?, Y | Y, Y | Y, Y | Y, Y | Y, Y | ?, ? | ?, Y | ?, ? | ?, ? | ?, ? |
| Designer workflows | Y | P (screenshots) | P | P | N | N | N | N | N | N | N | N |
| OS: Win / mac / Linux / Android | Y / Y / Y / Y (PWA) | Y/Y/Y/Y (app) | Y/Y/Y/? | Y/Y/Y/browser | Y/Y/Y/Y | Y/Y/Y/Y | Y/Y/Y/? | Y/Y/Y/Y | Y/Y/Y/? | Y/Y/Y/? | Y/Y/Y/? | Y/Y/?/? |

Reading the table: every row a user notices in the first hour (chat, UI, phone, plan mode, review) is already
matched by at least four products with large teams. The rows only Tutti fills are the invisible plumbing rows.

## 2. Prices (USD per month, checked 2026-11-14)

| Product | Individual tiers | Team tiers | Source |
| --- | --- | --- | --- |
| Tutti | free (MIT); you pay your own plans | n/a | `LICENSE` |
| Claude Code | Pro 20 (17 yearly), Max 100, Ultra 200 | Team Standard 25/seat, Team Premium 125/seat, Enterprise 20/seat + API usage | [claude.com/pricing](https://claude.com/pricing) |
| Cursor | Free 0, Plus 8, Premium 30, Super 100, Ultra 200 (cloud agents from Premium) | Teams from 30/user | [cursor.com/pricing](https://cursor.com/pricing) |
| Google Antigravity | Individuals 0 (weekly limits); Pro and Ultra through Google AI Pro / Ultra: **price not on the Antigravity page (unverified there)** | Business via Google Cloud from 30/seat | [antigravity.google/pricing](https://antigravity.google/pricing) |
| GitHub Copilot | Free 0, Pro 10, Pro+ 39, Max 100 | Business 19/seat, Enterprise 39/seat | [docs plans](https://docs.github.com/en/copilot/get-started/plans) |
| OpenAI Codex (ChatGPT) | Free 0, Go 8, Plus 20, Pro 100 / 200 / 500 | Business 25 monthly (20 yearly) | [learn.chatgpt.com/docs/pricing](https://learn.chatgpt.com/docs/pricing) |
| Devin | Free 0, Pro 20, Max 200 | Teams 80 + 40/dev seat | [devin.ai/pricing](https://devin.ai/pricing) |
| Conductor | **unverified** (no pricing page; closing to new users) | n/a | [InfoWorld](https://www.infoworld.com/article/4245065/meta-acquires-conductor-as-ai-coding-shifts-toward-multi-agent-development) |
| Orca | free (MIT); paid tier unverified | n/a | [GitHub](https://github.com/stablyai/orca) |
| Emdash | free (Apache-2.0); Emdash cloud price **unverified** | n/a | [emdash.com](https://emdash.com/) |
| Friday | free (MIT), no account | n/a | [tryfridaywork.com](https://tryfridaywork.com) |
| Paradev (was Superset) | Free 0, Pro 20 (16 yearly), Ultra 200 | Team 60 | [paradev.com/pricing](https://paradev.com/pricing) |
| Warp | Free 0, Build 20, Max 200 | Business 50/user; Factories billed separately (PAYG at API +20 %, Build 20) | [warp.dev/pricing](https://www.warp.dev/pricing) |
| Kiro | Free 0 (50 credits), Pro 20, Pro+ 40, Pro Max 100, Power 200 | Teams 20/seat | [kiro.dev/pricing](https://kiro.dev/pricing/) |
| Nimbalyst | Individual 0 | Teams 20/user (free in beta) | [nimbalyst.com/pricing](https://nimbalyst.com/pricing) |

Price is not where Tutti wins or loses: the owner pays the same plans either way. The free orchestrators (Orca,
Emdash, Friday) already cost nothing.

## 3. Maturity

| Product | Users / scale | Backing | Release cadence | Platforms | Licence |
| --- | --- | --- | --- | --- | --- |
| Tutti | 1 user, 1 maintainer, unreleased v0.1.0 | none | daily (one owner) | Linux (CLI) | MIT |
| Claude Code | run-rate revenue over 2.5 B USD ([Anthropic](https://www.anthropic.com/news/anthropic-raises-30-billion-series-g-funding-380-billion-post-money-valuation)) | Anthropic | several releases a week (CHANGELOG at v2.1.325) | Win, mac, Linux, iOS, Android, web | proprietary |
| Cursor | 2 B USD ARR in Feb 2026 (secondary: [TNW](https://thenextweb.com/news/cursor-anysphere-2-billion-funding-50-billion-valuation-ai-coding)) | Anysphere, multi-billion rounds (secondary) | 5 changelog posts in 3.5 weeks ([changelog](https://cursor.com/changelog)) | Win, mac, Linux, iOS, web | proprietary |
| Antigravity | unverified | Google | unverified | Win, mac, Linux, browser | proprietary |
| Copilot | 4.7 M paid users Jan 2026 (secondary: [Windows Report](https://windowsreport.com/microsofts-fy26-q4-earnings-hit-90-billion-as-ai-growth-accelerates/)) | Microsoft / GitHub | weekly changelog | all | proprietary |
| Codex | 5 M weekly users, June 2026 ([Constellation](https://constellationr.com/insights/news/openai-touts-broadening-codex-usage-5-million-weekly-active-users)) | OpenAI | several releases a week (rust-v0.169.0, 2026-11-13) | all | CLI Apache-2.0 |
| Devin | 73 M USD ARR June 2025 ([Cognition](https://cognition.ai/blog/funding-growth-and-the-next-frontier-of-ai-coding-agents)) | Cognition, 1 B+ round May 2026 ([SiliconANGLE](https://siliconangle.com/2026/05/27/vibe-coding-startup-cognition-doubles-valuation-new-1b-round/)) | CLI changelog | web, Win, mac, Linux | proprietary |
| Conductor | unverified | acquired by Meta | winding down | macOS | closed |
| Orca | 124 k stars (repo created 2026-03, so treat the number with care), ~650 contributors | Stably (YC W22) | ~83 releases in 3 months | Win, mac, Linux, iOS, Android | MIT |
| Emdash | 6.2 k stars, ~140 contributors | YC W26 | 29 releases in 3 months | Win, mac, Linux | Apache-2.0 |
| Friday | unverified (no public repo found) | unverified | unverified | Win, mac, Linux | MIT (per site) |
| Warp | 66 k stars | venture-backed (amount unverified) | ships outside GitHub | Win, mac, Linux | AGPL-3.0 (client repo) |
| Kiro | unverified | AWS | frequent (CLI 2.0) | Win, mac; Linux unverified | proprietary |

GitHub numbers from the GitHub API on 2026-11-14 ([Orca](https://github.com/stablyai/orca),
[Emdash](https://github.com/generalaction/emdash)).

## 4. Effort to reach parity

Person-weeks for **our agents** (Claude orchestrating cheaper workers, owner reviewing), to a quality a stranger
would keep using. Rough, and optimistic by nature: agent-written UI still needs a human eye, and cross-OS e2e is slow.

| Roadmap area | Person-weeks | Who already does it | Worth building? |
| --- | --- | --- | --- |
| Pacing (continuous, per window, shares, editable resets) | 2–3 | nobody | **yes, core** |
| Panic mode + cross-vendor routing by task type and effort | 3–4 | nobody (Friday hands off at a limit) | **yes, core** |
| Usage readers for every account (ccusage, Copilot credits line, agy windows) | 2 | ccusage (reads, no pacing) | yes, integrate ccusage |
| Remote test queue, guards, yield flag, stall watchdog | 2–3 to finish | nobody at this level | **yes, core** |
| Workers incl. Copilot CLI and coding agent, via ACP | 2–3 | Friday, Orca, Emdash (ACP / any CLI) | yes, but through ACP |
| `tutti setup` wizard + doctor (Linux, WSL, Windows, macOS) | 4–6 | each vendor's installer | yes, Linux/WSL first |
| TUI to the stated quality bar (Textual) | 3–5 | Claude Code, agy, Codex, Claude Squad | yes, it is the dashboard of the core |
| Peers (cross-session plans, quota splits, inbox) | 2–3 | Claude Code cross-session messaging | thin layer on top of it |
| PWA: menu, dashboards, tasks, devices, live plan, terminal | 10–14 | Orca, Emdash, Paradev, Claude desktop | no: dashboards only, as read-only web over the TUI's data |
| Chat at 2026 IDE parity (queue, @, attachments, voice, fork, diffs, cost) | 12–20 | every vendor | **no** |
| Main-agent switching with shared context | 4–6, and "no extra tokens" is not achievable: the new agent must read the state | nobody does it well | no, keep plan files + AGENTS.md as the shared context |
| Remote access (QR pairing, E2E relay or Tailscale, push, offline) | 5–8 | Claude Remote Control, Codex mobile, Orca mobile, Antigravity | **no**, use theirs + Tailscale docs |
| Borrowed ideas (checkpoints, best-of-N, shared index, issue→PR, review agent, spec flow, replay, recipes) | 10–15 | Claude Code, Copilot, Kiro, Cursor | only best-of-N and cross-vendor review, which are routing features |
| Design system, designer workflows, Figma MCP | 6–10 | Figma MCP is already in Claude Code, Cursor, Copilot | no, configure the MCP in `setup` (1–2 days) |
| Android, macOS, Windows e2e + performance budgets | 5–8 | n/a | only if the PWA is built |
| Telemetry (opt-in) + Sentry + website | 2–3 | n/a | website yes (small); telemetry after real users exist |
| **Total, everything** | **~75–115** (1.5–2+ years of one owner's agent budget) | | |
| **Total, narrow core (rows marked core/yes)** | **~20–30** | | |

Then maintenance: every vendor CLI changed flags, quotas or terms at least once in 2026 (Gemini CLI free tier closed,
Antigravity plans changed twice, Copilot moved to credits, Codex dropped `--full-auto`). Expect 20–30 % of the
build effort each year just to keep adapters and quota parsers working, more if the PWA exists.

## 5. Strengths and gaps

**Where Tutti would be unique (checked against all of the above):**
- Pacing every quota toward its reset, with several windows per account (weekly, monthly, rolling 5 h) and editable
  shares between projects and peers. Vendors show limits; nobody steers the burn.
- Panic mode that moves work across vendors and local models before a cap, without stopping work.
- A task-type router that picks worker, model and effort across vendors under one policy.
- Test runs on your own machines behind slot queues, memory and heat guards, fall-over and a yield flag for a shared
  GPU. Cursor's self-hosted workers and Codex SSH run the agent there; neither queues heavy runs across boxes.
- Translation with mandatory back-translation QA.
- Lessons from real failures (self-matching waits, uncapped containers) baked into the tools.

**Gaps and risks, plainly:**
- **Vendors' terms are the biggest risk.** Anthropic's consumer terms forbid access "through automated or non-human
  means, whether through a bot, script, or otherwise" except with an API key, and say subscription OAuth is for
  "ordinary use" of its own apps ([consumer terms](https://www.anthropic.com/legal/consumer-terms),
  [Claude Code legal](https://code.claude.com/docs/en/legal-and-compliance)). Antigravity's terms prohibit "using
  third party software, tools, or services to access the Service", with suspension as the stated remedy
  ([terms](https://antigravity.google/terms)). Cursor bans renting or reselling; Copilot bans shared logins
  ([GitHub ToS](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service)); OpenAI's terms page
  returned 403 (unverified). A tool whose purpose is to run subscription CLIs from scripts, keep them busy all week
  and burn quota to 100 % sits exactly where these clauses point. A single user running the unmodified binaries is
  probably tolerated today; a public product advertising it invites a ban for its users and is not something to
  promote without each vendor's written OK or API-key mode.
- **Fast-moving vendors eat the roadmap.** In 2026 alone the big four shipped agent views, remote control, phone
  apps, self-hosted workers, SSH, voice chat and plan modes. Any UI feature Tutti builds is likely to be matched for
  free within a quarter.
- **Platform lock-in, inverted.** Tutti depends on four vendors' CLIs, their output formats (Copilot's "AI Credits"
  line), their log files (agy's `cli.log`) and their permission files. Each is undocumented as an API and can break
  on any release.
- **Maintenance burden and bus factor of one.** Orca ships almost daily with ~650 contributors; Tutti has one owner
  whose agent budget also serves another project. The full roadmap (PWA on four OS, Android, e2e, telemetry, design
  system) is a product-company workload.
- **Some promises cannot be kept as written**: switching the main agent "costs no extra tokens"; "the best IDE of
  2026" against Cursor and Claude Code; Antigravity's two 5 h windows routed by `agy --model` (depends on undocumented
  behaviour).
- **Market shape**: the free orchestrator UIs (Orca, Emdash, Friday) already do the visible 80 %; Conductor and Vibe
  Kanban show that even funded orchestrators get acquired or shut down. A late, single-maintainer entrant with a UI
  has no moat; one with a quota brain does.

## 6. Local coding agent: ours against the open agents

Ours = `hcm-wt/lagent.py` / `lcode.py` (and `tutti agent` / `tutti code`): about 50 lines each, Ollama on the 5090
(`qwen3.8:27b-q8_0`, 64 k context), whole files in, whole files out, run a check, feed back the last 120 lines,
up to 4 rounds. No tool calls, no search, no repo map, no git.

| Agent | Headless CLI | Ollama / local | Quality evidence on real repo tasks | Edit/test/retry loop | Git | Licence | Maintenance (2026-11-14) | Setup | Fit with Tutti |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **Ours** (`lagent.py`) | yes (argv) | yes, native | none measured; whole-file format is evidence-backed for ~27–32B models (below) | yes, check + 4 rounds | none (orchestrator reviews `git diff`) | MIT (Tutti) | ours | none | perfect: yield flag, busy markers, rules text |
| Aider | `aider --message … --yes` ([docs](https://aider.chat/docs/scripting.html)) | yes (litellm); text edit formats, no tool calls needed | polyglot board: Qwen3-32B 40.0 %, Qwen3-235B 59.6 % ([board](https://aider.chat/docs/leaderboards/)); whole format 45.8 % vs diff 41.3 % for Qwen3-32B ([post](https://aider.chat/2025/05/08/qwen3.html)) | yes: `--test-cmd --auto-test`, `--lint-cmd` | auto-commit (can disable) | Apache-2.0 | **near dormant**: last release v0.86.0, 2025-08-09 | low | good: whole format + repo map, but stalled upstream |
| OpenCode | `opencode run` ([docs](https://opencode.ai/docs/cli/)) | yes; tool-call failures reported with some Ollama models ([#20995](https://github.com/anomalyco/opencode/issues/20995)) | no local-model benchmark found | tool-based, no test flag (unverified) | via bash | MIT | very active (v1.19.5, 2026-10-28) | low–medium | good, ACP-capable |
| Kilo Code CLI | `kilo run --auto` ([docs](https://kilo.ai/docs/cli)) | via OpenCode-style provider config (Ollama not named) | none found | tool-based | via bash | MIT | active (v7.8.27, 2026-11-05) | low–medium | same as OpenCode |
| Cline CLI | `cline --auto-approve true …` ([docs](https://docs.cline.bot/cline-cli/overview)) | `ollama launch cline` ([Ollama](https://docs.ollama.com/integrations/cline-cli)); "compact prompt" advised | Mistral lists it as a Devstral harness; no numbers | tool-based, checkpoints | via tools | Apache-2.0 | active (v4.1.26, 2026-10-26) | low | good |
| Goose | `goose run -t … --no-session` ([docs](https://goose-docs.ai/docs/tutorials/headless-goose)) | yes; experimental tool shim for weak tool callers | none found | tool-based, recipes | via tools | Apache-2.0 | active (v1.59.0, 2026-11-10) | medium | fair |
| Continue `cn` | `cn -p` ([docs](https://docs.continue.dev/cli/headless-mode)) | yes (`provider: ollama`) | none found | tool-based | via tools | Apache-2.0 | CLI labelled **alpha** | medium | weak |
| OpenHands CLI | `openhands --headless -t` ([docs](https://docs.openhands.dev/openhands/usage/cli/headless)) | yes (OpenAI-compatible) | platform scores are with frontier models | tool-based, sandbox | yes | MIT | **CLI "no longer actively maintained"** (docs) | medium–high (Docker) | avoid |
| Qwen Code | `qwen -p … --yolo` ([docs](https://qwenlm.github.io/qwen-code-docs/en/users/features/headless)) | yes (OpenAI base URL to Ollama); tuned for Qwen, our model family | Qwen's own scores use their scaffold, not this CLI | tool-based | via bash | Apache-2.0 | very active (v0.27.3, 2026-11-13) | low | **best candidate** for our model |
| Crush | `crush run --yolo` ([docs](https://next-docs.charm.land/crush/cli/run.html)) | yes; needs large `OLLAMA_CONTEXT_LENGTH` | none found | tool-based | via bash | FSL-1.1-MIT (source-available) | active | low | licence friction |
| Codex CLI `--oss` | `codex exec --oss --local-provider ollama` (third-party guide) | yes, built for gpt-oss | none for local | tool-based, sandbox | via bash | Apache-2.0 | very active | low | fair; tuned for gpt-oss, not Qwen |
| mini-SWE-agent | `mini` / 10 lines of Python ([docs](https://mini-swe-agent.com/latest/)) | yes (litellm), **bash only, no tool API** | the SWE-bench "bash only" reference harness ([swebench.com](https://www.swebench.com/)) | agent loop; tests via bash | none | MIT | moderate (v2.4.6, 2026-07-23) | very low | good: as small as ours, but can search |
| Pi | print/JSON/RPC modes (flag unverified) | multi-provider | timed out on both tasks in a 16 GB local test ([lostechies](https://lostechies.com/erichexter/2026/06/06/local-llm-bench-part-5-final-picks/)) | tool-based | via bash | MIT | active | low | weak locally |

**What the evidence says.** Nobody has published a head-to-head of these CLIs on one ~27B local model; the only
independent test is small (6 tasks) and found single-shot scores do not predict agent results, and that
qwen3-coder:30b "requires the whole-file edit format, since diff mode fails silently" ([lostechies](https://lostechies.com/erichexter/2026/06/06/local-llm-bench-part-5-final-picks/)).
Aider's own data shows whole-file edits are 100 % well-formed and score higher than diffs for Qwen3-32B. The vendor
scores for Qwen3.6-27B (77.2 SWE-bench Verified) and Qwen3.8-27B (61.7 SWE-bench Pro) were measured in tool-using
scaffolds, so the model can use tools; whether Ollama's tool-call parsing holds up on our tasks is unknown.

**Plainly:** the owner's doubt is justified. No evidence shows any of these tools beats our loop **on the tasks we
give it** (bounded edits to named files, with a gate as the check). Ours has the right edit format for this model
size, zero dependencies, and Tutti's yield and busy rules built in. Where ours is truly weaker is **context**: it
cannot search or read files nobody named, so it fails on any task that needs discovery, and it rewrites large files at
high output cost. Keep ours as the default. Run a one-day bake-off of 10 real past tasks (same model, same checks)
against Qwen Code (`-p --yolo`) and mini-SWE-agent; adopt one **only** for discovery-type tasks, and only if it wins on
pass rate per wall-clock hour. Do not adopt Aider (stalled), OpenHands CLI (unmaintained) or Continue `cn` (alpha).
The cheap upgrade to ours: add a `grep`/`read` pre-step and a file-size cap that switches to search/replace blocks
above ~400 lines (about 1–2 days).

## 7. Verdict

**Build a narrower niche, and adopt or integrate everything else.** Fully built, Tutti would be a good tool for its
owner and a weak product against the field: its visible features would be late copies of what Claude Code, Cursor,
Copilot, Codex, Orca and Emdash ship for free or already pay for, and its terms risk grows with its visibility. Its
invisible features are genuinely ahead: no one paces quotas, reroutes across vendors near a cap, or queues heavy runs
across your own machines.

**Top 5 decisions**

1. **Cut the PWA, the chat, remote access, designer and Figma work from the roadmap** (about 50–70 person-weeks).
   Use Claude Code's agent view and Remote Control, Orca (Linux, SSH hosts, iOS and Android) or Emdash as the
   session UI, and Tailscale for reaching the home machines. If a web view is wanted, make it a read-only dashboard
   over the TUI's JSON (1–2 weeks), not an IDE.
2. **Make the quota brain the product**: pacing with multiple windows, panic mode, task-type routing with effort,
   shares between peers, and `--json` on every command, so Orca, Friday or a Claude Code hook can call it. This is
   20–30 person-weeks and is the only part with no competitor.
3. **Settle the terms question before any public release.** Keep Tutti a local tool that starts each user's own
   unmodified vendor binaries, never touches credentials, and supports API-key mode for every vendor that has one;
   document each vendor's clause in `docs/WORKERS.md`; ask Anthropic and Google in writing whether scheduled headless
   use of a subscription CLI is allowed. Drop "spend every quota to 100 %" from public marketing until they answer.
4. **Replace hand-written worker adapters with ACP** (Copilot, Cursor, Gemini, Kiro, OpenCode, Cline, Qwen Code are
   in the registry), read usage with ccusage plus small parsers, and carry peers on Claude Code's cross-session
   messaging. Less code to maintain is the main defence against vendor churn.
5. **Keep our local agent, prove it, then extend it**: one-day bake-off against Qwen Code and mini-SWE-agent on 10
   real tasks; add a search pre-step to ours; keep the TUI, `tutti setup` (Linux and WSL first, macOS on CI runners
   later) and the test queue as the polished face of the narrow product.

If after decision 2 the quota brain finds outside users (the website and an npm publish are cheap tests), revisit the
UI question with real demand instead of a roadmap.
