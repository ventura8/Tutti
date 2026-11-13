# Tutti: competitors and alternatives

Researched 2026-11-14. Prices are USD per month, read that day from the vendor's own page unless marked. "Unverified" means the vendor page did not state it, or I did not open a primary source for it. Written to `hcm-wt/` because `Projects/tutti/docs/` does not exist yet.

**Short answer:** nothing found replaces Tutti as a whole. Three of its parts have no equivalent anywhere (routing tests to your own boxes, pacing a quota toward its reset, translation QA). One part should not be built at all (a worktree and sessions UI: use Orca or Emdash). Two parts should lean on existing pieces (Claude Code's cross-session messaging, `ccusage`).

## 1. Comparison table

Columns: **Orch** = orchestrates several agents; **Mix** = mixes vendors (Claude + Cursor + Google + local); **Local** = local or self-hosted models; **Own boxes** = runs on your machines and remote test boxes; **Pacing** = paces usage against a quota.

| Product | What it is | Orch | Mix | Local | Own boxes | Pacing | Licence | Price (2026-11-14) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **Tutti** | Bash + Python + CLI, YAML config; Claude Code briefs and reviews Cursor CLI, `agy` and Ollama workers | yes | yes | yes (Ollama) | yes: slot queues, fall-over, yield flag | yes: spend each quota about 3 h before reset | open source (licence to choose) | free; you pay your own plans |
| Claude Code (subagents, agent teams, Agent SDK) | Anthropic's coding agent; subagents, experimental agent teams, cross-session messaging | yes | no (Claude models only) | no | partial: messages reach your sessions on other machines through Remote Control | no (limits only) | proprietary | Pro 20, Max 100, Ultra 200; Team 25 or 125 per seat [S1] |
| OpenAI Codex (CLI + cloud) | CLI, IDE, cloud tasks | partial | no | unverified (`--oss`) | CLI local; cloud is OpenAI's | no | CLI Apache-2.0 [S4] | Free 0, Go 8, Plus 20, Pro 100 / 200 / 500, Business 25 per user [S3] |
| Cursor (cloud agents, CLI) | IDE + CLI + cloud agents | partial (parallel cloud agents) | models yes, agents no | no | cloud is Cursor's | no | proprietary | Free 0, Plus 8, Premium 30, Super 100, Ultra 200, Teams 30+ per user; cloud agents from Premium [S5] |
| Google Antigravity (IDE, CLI, Agent Manager) | Agent-first IDE and CLI | partial | Gemini + some non-Gemini models | no | local IDE and CLI | no | proprietary | via Google AI Pro 20, Ultra 100, Ultra 200 [S6] |
| Google Jules | Async cloud agent, makes PRs | partial (3 / 15 / 60 concurrent tasks) | no | no | no (Google VMs) | daily task caps: 15 / 100 / 300 | proprietary | free tier; "in Pro" and "in Ultra" tiers, USD not on page: unverified [S7] |
| GitHub Copilot cloud agent + third-party agents | Assign issues to Copilot, Claude or Codex agents from the Agents tab | yes | partial: Claude + Codex (preview), no Cursor, Google or local | no | no (Actions minutes); self-hosted runners unverified | no (AI credits) | proprietary | Free 0, Pro 10, Pro+ 39, Max 100; Pro's third-party access is inconsistent on the page [S8][S9] |
| Devin (Cognition) | Cloud software engineer + Devin CLI | partial (parallel sessions) | no | "open source models" hosted, not yours | no; Enterprise dedicated deployment | no (quotas refresh daily and weekly) | proprietary | Free 0, Pro 20, Max 200, Teams 80 + 40 per seat [S10] |
| Factory (Droid) | Desktop, CLI, SDK agents | partial | models yes (BYO key, custom endpoint on Free) | unverified (custom endpoint) | local CLI; on-prem on Enterprise | no | proprietary | Free 0 (BYO inference), Pro 20, Plus 100, Max 200, Teams 60 + 40 per seat [S11] |
| OpenHands | Open agent platform, "Agent Canvas" | partial | "any agent (Codex, Claude, etc.)", BYO key | unverified today | yes: laptop or shared VM | no on free; budgets on Teams | MIT (Agent Canvas) | Free 0; Teams 500 (10 users, +50 per seat) [S12] |
| Aider | Terminal pair programmer | no | models yes | yes ("including local models") | local only | no | Apache-2.0 | free [S13] |
| Cline | VS Code extension + CLI, "Multi-Agent Teams" | partial (coordinator + specialists, one tool) | models yes | yes (Ollama, LM Studio) | local; headless CLI | no | Apache-2.0 | free, pay inference; Enterprise custom [S14][S15] |
| Roo Code | Was a Cline fork | n/a | n/a | n/a | n/a | n/a | Apache-2.0 | **shut down, repository archived 2026-05-15** [S16] |
| Kilo Code | VS Code, JetBrains, CLI + cloud agents | partial | models yes | yes (Ollama, LM Studio) | local; cloud agents 0.60–1.20 per hour | no | open source extensions (licence unverified) | Free 0, Teams 15 per user; Kilo Pass 19 / 49 / 199 [S17] |
| Continue | IDE assistant + CLI `cn` | no | models yes | Ollama guides exist (unverified) | local | no | open source (licence unverified) | unverified (pricing page returned 404) [S18] |
| Amp | Agent with "orbs" (hosted or self-hosted) | partial | models yes; use your ChatGPT or Claude subscription, BYOK | unverified | yes: self-hosted orbs unlimited and free | no | proprietary | Hobby 0, Individual 20, Enterprise custom [S19] |
| Warp (Terminal + Factories) | Agentic terminal; Factories runs fleets, with Claude Code and Codex harnesses | yes | partial (Claude Code + Codex) | no | self-hosted cloud agents on Enterprise only | no | proprietary | Free 0, Build 20, Max 200, Business 50 per user [S20] |
| Conductor | Mac app, parallel agents in workspaces | yes (parallel, human-led) | Claude Code, Codex, Cursor, OpenCode | no | Mac only | no | closed (unverified) | unverified; a report that it is joining Meta is also unverified [S21] |
| Sculptor (Imbue) | UI for parallel agents in containers | yes (parallel, human-led) | Claude Code, Codex | no | Mac (Apple Silicon), Linux | no | unverified | unverified [S22] |
| Claude Squad | TUI: tmux + worktrees per agent | yes (parallel, human-led) | Claude Code, Codex, Gemini, Aider, OpenCode, Amp, any command | whatever the agent supports | local only | no | AGPL-3.0 | free [S23] |
| Vibe Kanban | Kanban board for agents | yes | 10 agents incl. Cursor, Gemini CLI | via agents | self-host, remote SSH | no | Apache-2.0 | free; **sunsetting** [S24] |
| Crystal → Nimbalyst | Crystal deprecated February 2026; Nimbalyst is its successor | yes (parallel, human-led) | Claude Code, Codex; others alpha | no | macOS, Windows, Linux, iOS | no | MIT (apps) | free for individuals [S25][S26] |
| Emdash | Open "agentic development environment", worktree per task | yes (parallel, human-led) | 9 named incl. Claude Code, Cursor, Codex; Antigravity not listed | "any provider" (unverified) | **yes: remote machines over SSH** | no | Apache-2.0 | free (no pricing on page) [S27] |
| Orca | Desktop + mobile, worktree per agent | yes (parallel, human-led) | Claude Code, Cursor, Codex, Copilot, Cline, Kiro, "any CLI agent" | via agents | **yes: pick the SSH host per worktree** | no | MIT | free [S28] |
| Friday | Desktop app, agents share a board and check each other | yes | 16 tools incl. Claude Code, Cursor, Codex, Gemini CLI, Cline, Kilo; any ACP agent | via agents | your computer only; no remote hosts described | no | MIT | free, no account [S29] |
| Kiro (AWS) | Spec-driven IDE + CLI | no | no | no | local IDE | no (credits) | proprietary | Free 0, Pro 20, Pro+ 40, Pro Max 100, Power 200 [S30] |
| CrewAI | General multi-agent framework | yes (you write it) | any LLM | unverified | self-host the framework | no | open source (licence unverified) | Basic free (50 executions a month), Enterprise custom [S31] |
| AutoGen | General multi-agent framework | yes (you write it) | any LLM | unverified | self-host | no | MIT | free; **archived, successor Microsoft Agent Framework** [S32] |
| LangGraph (+ LangSmith) | Graph framework for agents | yes (you write it) | any LLM | unverified | self-host the library | no | open source (licence unverified) | library free; LangSmith Developer 0, Plus 39 per seat [S33] |
| ccusage (a part, not a rival) | Reads local usage logs of 19 agent CLIs | no | reads Claude Code, Codex, Antigravity, Gemini CLI and others; Cursor not listed | n/a | local | **reports** 5-hour blocks; does not pace | MIT | free [S34] |

## 2. Pros and cons

**Claude Code (subagents, teams, SDK)**
- Pro: the strongest orchestrator; subagents, hooks, and cross-session messaging that reaches other machines with nothing to build [S2][S35].
- Con: workers are Claude models only, so every worker spends the dearest quota; agent teams are experimental and "use significantly more tokens" [S2].
- Con: no pacing, no test routing.

**OpenAI Codex**
- Pro: open CLI (Apache-2.0), cloud tasks, cheap entry (Go 8, Plus 20) [S3][S4].
- Con: one vendor; cloud runs on OpenAI's machines.

**Cursor**
- Pro: good cheap worker; cloud agents from Premium [S5].
- Con: the public lineup is now Plus 8 / Premium 30 / Super 100 / Ultra 200; a "$20" plan is a legacy tier, so check what the current one still includes [S5].
- Con: not an orchestrator of other vendors' agents.

**Antigravity and Jules**
- Pro: the $20 Google AI Pro plan covers Antigravity [S6]; Jules lists 100 tasks a day and 15 at once "in Pro" [S7], which Tutti does not use today.
- Con: quotas changed twice in 2026; Jules runs only on Google's VMs against GitHub.

**GitHub Copilot agents**
- Pro: issue → agent → PR with Claude or Codex, from 10 a month [S8][S9].
- Con: no Cursor, Google or local models; runs on Actions minutes, not on your boxes.

**Devin**
- Pro: polished cloud agent, 20 entry tier [S10].
- Con: cloud only below Enterprise; closed; one vendor.

**Factory**
- Pro: Free tier with your own inference or a custom endpoint [S11].
- Con: closed; multi-agent features are not on the pricing page (unverified).

**OpenHands**
- Pro: MIT, self-hosted, "any agent" [S12].
- Con: Teams is 500 a month; budgets only there; heavier than a CLI toolkit.

**Aider**
- Pro: small, Apache-2.0, any model including local ones [S13].
- Con: single agent; no orchestration.

**Cline / Kilo Code** (Roo Code is gone [S16])
- Pro: open, headless CLIs, Ollama and LM Studio [S15][S17].
- Con: their "teams" stay inside one tool; no other vendors' agents, no pacing.

**Continue**
- Pro: open, has a CLI [S18].
- Con: not an orchestrator; pricing unverified.

**Amp**
- Pro: self-hosted orbs are free and unlimited; works with your existing subscriptions [S19].
- Con: closed; subagent details not on the pricing page.

**Warp**
- Pro: fleets with Claude Code and Codex harnesses [S20].
- Con: closed; self-hosting only on Enterprise; no Cursor, Google or local.

**Conductor, Sculptor, Nimbalyst, Claude Squad**
- Pro: ready-made parallel-session UIs over worktrees or containers [S21][S22][S23][S26].
- Con: a human drives each session; no reviewer, no routing; Conductor is Mac only; Claude Squad is AGPL.

**Vibe Kanban, Crystal**
- Con: Vibe Kanban is sunsetting [S24]; Crystal is deprecated [S25]. Do not adopt either.

**Emdash, Orca**
- Pro: open (Apache-2.0, MIT), Linux builds, worktree per agent, **remote SSH hosts**, Cursor supported [S27][S28].
- Con: UI for a human, not an orchestrator that briefs and reviews; no pacing; `agy` not listed (Orca says any CLI agent).

**Friday**
- Pro: the closest in spirit: MIT, free, 16 tools, shared board, agents check each other, reuses each tool's sign-in [S29].
- Con: one computer; no remote test boxes, pacing or local-model path described. I read its site only, not its repository.

**Kiro**
- Pro: spec-driven flow [S30]. Con: one vendor; no orchestration of others.

**CrewAI, AutoGen, LangGraph**
- Pro: general, flexible. Con: you write the whole orchestrator; none drives coding CLIs out of the box; AutoGen is archived [S32].

## 3. Where Tutti stands

**Unique (found nowhere else):**
- Pacing a paid quota so it is spent about 3 h before it resets. Everyone else only shows limits; `ccusage` only reports.
- Routing test runs to your own remote boxes with slot queues, memory guards and fall-over.
- A yield flag so a shared machine favours another project.
- Translation with mandatory QA.
- An expensive orchestrator that briefs and reviews cheaper workers from other vendors plus local models, as a policy and not as a UI.

**Ahead:**
- Vendor mix including local Ollama and `agy`; most rivals stop at Claude + Codex.
- Free, self-hosted, plain bash and Python: nothing to install but the CLIs.

**Behind:**
- No UI: Orca, Emdash, Friday and Nimbalyst show sessions, diffs and PRs; Tutti is scripts.
- No isolation beyond worktrees (Sculptor uses containers).
- No issue-tracker intake (Emdash, Copilot, Jules take issues directly).
- Worker adapters are hand-written per CLI; Friday and Nimbalyst use the Agent Client Protocol [S26][S29].
- Cross-session coordination is home-made while Claude Code now ships it [S35].
- Bus factor of one, no community yet.

## 4. Recommendations

**Borrow**
- ACP as the worker adapter, so a new agent is config, not code (Friday, Nimbalyst).
- A shared board and "agents check each other" as a visible review step (Friday).
- Issue intake from GitHub or Linear (Emdash).
- Container isolation for risky workers (Sculptor).
- Hook names from Claude Code's agent teams (`TaskCompleted`, `TeammateIdle`) for quality gates [S2].

**Integrate**
- `ccusage` as the usage reader behind the pacer [S34].
- Claude Code cross-session messaging as the transport between sessions and machines [S35].
- Orca or Emdash as an optional front end over Tutti's worktrees [S27][S28].
- Jules as one more worker on the Google plan already paid for [S7].
- A Claude Code plugin or skill as Tutti's packaging, so the orchestrator loads the rules itself.

## 5. Verdict per part of the current setup

Judged on: low cost (Claude Ultra + a $20 Cursor plan + a $20 Antigravity plan), self-hosted on the laptop, NUC and PC, vendor mix, minimum owner input. Effort figures are my estimates.

| Part | Verdict | Why | Cost | Effort | First step |
| --- | --- | --- | --- | --- | --- |
| Orchestration | **KEEP**, and **INTEGRATE** Claude Code cross-session messaging | No product has Claude brief and review Cursor + `agy` + Ollama workers. Agent teams are Claude-only and cost more tokens. Native messaging already crosses machines. | 0 | about half a day | On two sessions run `/list-agents`; if they see each other across laptop and NUC, drop the home-made message path and keep only the yield flag. |
| Worktrees and sessions UI | **ADOPT** (Orca first, Emdash second); build nothing | Both are free, open, run on Linux, take SSH hosts and Cursor. A UI is the costliest thing to build and the least unique. | 0 | 1–2 h trial | Install Orca on the laptop, add the NUC as an SSH host, open two Tutti worktrees. Drop it if `agy` or the NUC host does not work. |
| Routing tests to remote machines | **KEEP** | No candidate does slot queues, memory guards and fall-over on your own boxes. GitHub self-hosted runners were not researched and are the only plausible alternative. | 0 | 0 | None; document `slot.sh` and `q.sh` as Tutti's flagship feature. |
| Usage pacing | **KEEP** the pacer, **INTEGRATE** `ccusage` as its reader | Nobody else paces toward a reset. `ccusage` already parses Claude Code, Codex and Antigravity logs; Cursor is not covered. | 0 | about half a day | Run `npx ccusage blocks` and compare with Tutti's own numbers for one day. |
| Cheap workers | **KEEP** Cursor CLI, `agy`, Ollama; **INTEGRATE** Jules and an open agent CLI for the local loop | Worker choice is already the cheapest mix. Jules is included with the Google plan (USD unverified). An existing open CLI with Ollama (Cline, Kilo, Aider) can replace the hand-written `lagent.py` retry loop. | 0 | Jules 2 h; local loop half a day | Confirm in the Google account that Jules shows "in Pro", then give it one small issue. Also check what the $20 Cursor plan still includes, since the public tiers are now 8 / 30 / 100 / 200. |
| Translation QA | **KEEP** | No candidate offers it. | 0 | 0 | None. |

Net: no subscription to add or cancel. About two days of work in total, most of it removing home-made code.

## 6. Sources

All opened 2026-11-14.

- [S1] Claude plans: https://claude.com/pricing
- [S2] Claude Code agent teams: https://code.claude.com/docs/en/agent-teams
- [S3] Codex pricing: https://learn.chatgpt.com/docs/pricing (redirect from developers.openai.com/codex/pricing)
- [S4] Codex CLI repository: https://github.com/openai/codex
- [S5] Cursor pricing: https://cursor.com/pricing
- [S6] Antigravity plan changes (2026-05-19): https://www.antigravity.google/blog/changes-to-antigravity-plans
- [S7] Jules: https://jules.google/
- [S8] Copilot plans: https://github.com/features/copilot/plans
- [S9] Copilot third-party agents: https://docs.github.com/en/copilot/concepts/agents/about-third-party-agents
- [S10] Devin pricing: https://devin.ai/pricing
- [S11] Factory pricing: https://factory.com/pricing
- [S12] OpenHands pricing: https://www.openhands.dev/pricing
- [S13] Aider: https://github.com/Aider-AI/aider
- [S14] Cline pricing: https://cline.bot/pricing
- [S15] Cline repository: https://github.com/cline/cline
- [S16] Roo Code (archived): https://github.com/RooCodeInc/Roo-Code
- [S17] Kilo pricing: https://kilo.ai/pricing
- [S18] Continue docs: https://docs.continue.dev/ (https://www.continue.dev/pricing returned 404)
- [S19] Amp pricing: https://ampcode.com/pricing
- [S20] Warp pricing: https://www.warp.dev/pricing
- [S21] Conductor: https://conductor.build/ and https://www.conductor.build/docs/ ; the Meta report came from a search summary of https://www.morphllm.com/comparisons/superset-vs-conductor-vs-emdash (unverified)
- [S22] Sculptor: https://imbue.com/sculptor/
- [S23] Claude Squad: https://github.com/smtg-ai/claude-squad
- [S24] Vibe Kanban: https://github.com/BloopAI/vibe-kanban
- [S25] Crystal: https://github.com/stravu/crystal
- [S26] Nimbalyst: https://nimbalyst.com/
- [S27] Emdash: https://github.com/generalaction/emdash
- [S28] Orca: https://github.com/stablyai/orca
- [S29] Friday: https://tryfridaywork.com/
- [S30] Kiro pricing: https://kiro.dev/pricing/
- [S31] CrewAI pricing: https://www.crewai.com/pricing
- [S32] AutoGen: https://github.com/microsoft/autogen
- [S33] LangChain pricing: https://www.langchain.com/pricing
- [S34] ccusage: https://github.com/ryoppippi/ccusage
- [S35] Claude Code cross-session messaging: https://code.claude.com/docs/en/cross-session-messaging

**Not verified, listed in one place:**
- prices of Conductor, Sculptor, Continue, and Jules' paid tiers in USD;
- licences of CrewAI, LangGraph, Continue, Kilo and Sculptor;
- local-model support of Codex (`--oss`), OpenHands, Factory, Amp, CrewAI, AutoGen and LangGraph;
- whether Copilot Pro (10) includes third-party agents (the page contradicts itself);
- whether Copilot agents can run on self-hosted runners;
- Conductor joining Meta;
- that Orca and Emdash work with `agy` and with the NUC in practice;
- Friday beyond its own site;
- other tools seen only in search results and not opened: Superset, Multica, Coral, OpenHuddle, Composio Agent Orchestrator.
