# Chat

When messages are redrawn, Chat reuses an unchanged message's private immutable Markdown parse tree while creating fresh
DOM nodes. Editing or streaming new text reparses that message. This reduces repeated parsing; it does not virtualize the
message list or page the browser's message DOM yet (t407). Durable storage no longer drops older turns (t387).
Renaming updates the title without rebuilding the message list; if the user changes chats while the rename request is in
flight, its response does not rename the newly selected chat (t407).

For a dashboard-requested restart, Tutti checks the dashboard's live answer jobs and persisted streaming messages while
holding the Chat store lock, then rechecks at restart time. New Chat sends wait during the final check. An active answer,
unreadable store or unsupported cross-process lock refuses or defers the restart; if the delayed check defers it, Settings
reports that the dashboard did not restart and offers a retry. A deleted conversation's live answer in
another dashboard process is not visible to this guard. Daily self-update uses the same in-process Chat restart gate and
retries while Chat is busy. It does not coordinate failover takeover, other dashboard processes, or remote job stops;
those remain under t309/t326.

Confirmed Chat actions, plans and undo operations are also admitted through a local gate until their final card state is
saved; a settings restart waits for them without serializing unrelated actions. If an action has begun and an exception
prevents its result from being saved, this process refuses the guarded restart until the outcome is inspected and a
separately coordinated recovery is used. The in-memory hold lasts for this engine's lifetime and has no in-app clearing
path; inspecting the outcome alone does not clear it. It is not durable across crashes and does not cover other dashboard
processes or automatic updates.

The assistant can call `chat_history` to page through the full text of its current user's current conversation. Each page
is bounded; history read this way is treated as untrusted for automatic actions. Conversations are no longer capped at 400
messages: the index lists chats, each conversation's messages live in their own file, and `page_messages` offers bounded
reads for future UI paging (t407). Messages already deleted by an older build cannot be recovered (t387).

Chat binds each online worker to a configured account with a pacing state and passes that account's configured arguments
to the CLI. For a worker shared by multiple accounts, an admin must set `workers.<name>.chat_account_selector` to the CLI
flag selecting the account or model, and every linked account (including paused ones) must have exactly one distinct value
for that flag in `accounts.<name>.args`. This is the admin's assertion about the provider's CLI, not provider verification.
Chat checks the final command before launch and refuses ambiguous bindings. Paused accounts and accounts whose pacing state
is absent are not selected. A worker with no linked account remains unmetered.

When the preferred main is set aside after panic or spent (Cursor or another fallback is current), Chat does not call that
preferred provider until a fresh reading below `panic_at` confirms its reset — including when the reading is a synthetic
after-reset 0 %. Spent, panic, stale and unconfirmed quota stay out. If a CLI still returns a usage-limit message, Chat
logs the account as spent and tries the next model instead of showing that text as the answer (t388).

On hosts with POSIX flock and no-follow file opens, cooperating updated Chat writers serialize storage changes through a
same-host lock, including message and attachment registration and handoff notes. This does not make simultaneous dashboard
engines safe: answer ownership is still in memory, and an older process does not honor the lock. Stop old writers before
starting a new one. On other platforms Chat keeps its single-process behavior; cross-process safety is not provided. Undo
still has only in-process execution protection and must not be run concurrently from multiple engines.

Cursor CLI JSON events are opt-in: set `workers.<name>.chat_output_format: cursor-stream-json` and include
`--output-format stream-json --stream-partial-output` in that worker's command or Chat arguments. Chat shows generic tool
start/completion notes without tool arguments or results, then displays the canonical final result when the CLI finishes;
it does not stream partial answer text. Intermediate assistant events are ignored to avoid duplicating buffered output. Unknown
events are ignored, and malformed events or a missing successful terminal result fail the answer; plain-text workers keep
their existing output. This decoding does not restrict tools the Cursor process itself can run: its configured mode and CLI
permissions still govern that. It does not enable agent mode or verify provider account identity. See [Cursor's output format
reference](https://cursor.com/docs/cli/reference/output-format). Stop or timeout terminates the CLI process but does not
guarantee its native tool descendants or actions have stopped; inspect the work before retrying an incomplete answer.

The dashboard opens on **Chat**, the first tab, for every signed-in user (viewers too). Ask about the household in plain
words, or ask for work. The chat runs on the household host, inside `tutti dashboard`, so it answers while your own computer,
or your main agent's machine, is off.

## What it does

- **Answers questions** from the dashboard's own state, for example "What's running?", "How much quota is left?", "Why is the
  NUC idle?" or "What is waiting?". It reads the same snapshot as `/api/state`: jobs, the queue, accounts and their verdicts,
  machines and their presence, devices, pauses and the autopilot.
- **Turns requests into work.** It uses the same calls as the MCP tools (`tutti/mcpserver.py`, docs/CONNECT.md):

  | You write | It calls | Undo |
  | --- | --- | --- |
  | `add task in <project>: <what to change>` (optionally `[size M]`) | `backlog_add` | removes the task (`backlog rm`) |
  | `queue tests for <project>` | `backlog_add` ("Run the full test suite and fix what fails") | removes the task |
  | `delegate in <project>: <task>` (admins) | `delegate_task` (a run in the background) | none: stop it on the Dashboard tab |
  | `ask <scout>: <question>` (admins) | `scout_ask` | none |
  | `pause <name>` / `resume <name>` (a machine, device, worker or account) | the dashboard's pause switch | the opposite switch |

  Each action shows as a card under the answer: **Done**, **Not allowed** or **Failed**, with what happened, its id, and
  **Undo** where it can be taken back. A card of a task or run also shows its live status ("Queued · waits: …", "Running
  on nuc · 2 min") with a link to the job on the Dashboard tab. Every action, and every Undo, goes into the audit log
  (`<state>/auth-audit.jsonl`, the one the Users tab shows).
- In your own words too: a model (local or online) turns "the legend overlaps on phones, get it fixed" into a proposed
  `backlog_add` card that you confirm (*Tools* below).

## Who may do what

The server decides, for every action, with `auth.act_for` (and so `auth.may_act_for`):

- **Viewers** ask anything, in any mode, and queue backlog tasks for the projects in `chat.viewer_projects` (empty: every project), up
  to `chat.viewer_tasks_per_day` (5) a day. The autopilot runs those under its own pacing and sharing rules. A refused
  request says why and who can change it. Viewers see the same household state the Dashboard tab shows them (jobs, queue,
  accounts, machines), so the chat's answers and read-only tools tell them nothing more.
- **Admins** also hand work straight to a worker (`delegate`), ask scouts, run tests on machines and devices, and pause
  and resume machines, devices, workers and accounts: each of these spends an account or a machine at once, so a viewer's
  request never spends another member's private quota. Settings and users stay on their own hamburger-menu views.
- **Pending** users (waiting for approval) cannot use the chat.
- Conversations are private: each user sees only their own. Admins have **Household activity**: the actions the chat took
  for each member, from the audit log, never anyone's messages.

## Main agent (admins)

The Chat header has a **main agent** selector (admins only; the same code as `tutti main <agent|auto>` and the Dashboard
tab's Switch, `POST /api/main`):

- **Auto (now: &lt;agent&gt;)**: Tutti keeps `main.preferred` while it has room, else the first worker in `main.fallback`
  with room, and switches back to preferred after a fresh reading below `panic_at` (docs/CONDUCT.md).
- **Each worker** by name: pins that agent as both preferred and current (leaves auto).

Viewers do not see the control; the server refuses a non-admin `POST /api/main` when sign-in is on.

## Which model answers

The picker above the conversation chooses who answers, per conversation; the last choice is the default for new chats
(kept in the user's chat file, so it survives restarts and moves):

- **Auto** (the default): the household's local models first, then online agents.
- **Local models only**: never an online agent, even when no local model can answer (the built-in answer then says so).
- **Online agents only**.
- **One model or agent** by name (each shows "not now" with the reason when it cannot answer).

The order and the rules, from the planner:

1. The household's **local models** (the `ollama` workers, on the NUC, the laptop or the PC), which have no quota, the most
   capable first (by size: `qwen3.8:27b` on the PC before a 9B model): on machines that are here (presence), not paused, not
   lent out (the yield flag), and never the asking user's own computer (its `main_machine`).
2. **Online accounts with room**: not in panic or spent, not paid per use (`costs_money`), not paused. A late account
   remains eligible when its own and linked quota windows have current measured usage below their panic thresholds;
   stale or estimated readings, synthetic post-reset readings and reader failures hold it until refreshed. Only the
   host's owner (the first admin, or the person on this computer when sign-in is off) uses the host's own accounts;
   another member uses one only when its owner shares it (`accounts.<n>.share_spare`). Another member's private quota is
   never spent.
3. When none answers, the **built-in answer**: running jobs, quota, the queue or why a machine is idle, straight from the
   state.

Each answer names its model ("qwen3.8:27b-q8_0 on w5090"). When it fell back, it says why under the name ("Answered by
claude (claude account) because pc is asleep; nuc did not answer."). A model that does not answer (a machine asleep, a
tunnel down) is skipped for the next one within the same answer.

## Escalation (Auto)

With the model on **Auto**, a request the local model cannot handle goes to an online model with room (pacing and the
opt-ins apply: an account in panic, spent, paused or paid per use is never used; with none left, the local answer
stays). Each answer says why: "Answered by claude (claude account) because …".

- **Before answering**, a local model is skipped when the request (with its context) is longer than its `num_ctx` at the
  chosen effort, it has an image and the model has no `vision`, it is code work (a code block, a traceback, or "write", "fix",
  "implement", "refactor", "debug" or "review" followed by code, a function, a script, a test, a bug…) and the model is
  under 14B parameters, or the effort is max.
- **After answering**, the same context is sent online when the local model gave nothing, called its tools wrongly twice
  in a row, timed out, or (in a short answer that used no tool) opened by declining ("I can't help…") or by not being sure
  ("I'm not sure…"). Its text is replaced; an action it already did stays (with Undo), and then its answer stands.
- If no online model answers after all, the local answer stays ("Kept … answer"), and a local model skipped up front tries
  anyway. Each online model gets the request fitted to its own window (the compaction above runs for it).

Local only, Online only and a named model never escalate.

## Tools: the same work from any model

A model works through the same calls as the MCP tools, so a local model can do what an online one can:

| Tool | Kind | What it does |
| --- | --- | --- |
| `usage`, `machines`, `devices`, `backlog_list`, `job_status` | read | runs at once; its output goes back to the model. Job status reports and logs are for admins. |
| `backlog_add` | work | as the mode says (*Modes* below) |
| `delegate_task`, `scout_ask` | work | as the mode says; admins (they spend an account at once) |
| `test_on` (a track and a command, on a machine or an Android device) | work | as the mode says; admins |
| `control` (pause or resume a machine, device, worker or account) | household | as the mode says; admins |

- **Native tool calls** for local models: Ollama's function calling with each tool's JSON schema. A model under 7B
  parameters, or whose size neither the fleet's reading of its Ollama (docs/FLEET.md) nor its name tells, only talks
  (it is not given the tools). Online agents (a CLI such as `claude -p`) get the same schemas and
  answer with `ACTION {"tool": ..., "args": {...}}` lines, which the page never shows.
- **Validation**: every call is checked against its schema (required fields, types, enums, no other fields) and against
  the names that exist here (projects, scouts, machines, devices). A bad call is not run; its error goes back to the model.
  Outside Auto, refused calls never end the answer early: the model gets the reasons and its next rounds, and an answer
  that only made refused calls says so in plain words.
- Every call that changes something runs through the same server checks whichever mode is on: the role rules above, the
  viewer's daily limit, the audit line and Undo. Work it queues or starts still follows pacing and the opt-ins: a task waits
  in the backlog for an account with room, a hand-off goes through `tutti run auto`, a paid worker stays off until opted in.
- Up to 4 tool rounds per answer (by effort: 2 at low, 6 at high, 8 at max), at most 4 calls a round (the rest are told to ask again); a round that only looked things
  up and never answered says so. A model that stops halfway is replaced by the next one on a clean answer; a CLI agent
  that prints nothing past its deadline (180 s) is stopped. Confirm runs the tool outside the store's lock, and the daily
  limit and its audit line are taken together, so two quick Confirms cannot both pass the limit.

## Modes

Per conversation, with a default in your settings (the last one you picked); the **Mode** picker, or `/mode`:

- **Autonomous** (the default): the chat acts, then shows each action as a card with **Undo**; the result goes back to the
  model, which says what it did.
- **Ask before acting**: each call that changes something becomes a card, **Needs your OK**, with **Confirm** and
  **Cancel**; Confirm checks it again, then runs it.
- **Plan review**: the model proposes every step at once as a plan card (a request you type is "your request, as a
  one-step plan"). Untick a step to leave it out, change a step's
  text (a task's title, a hand-off's task, a question), then **Run the plan**: the steps run in order, each with its own
  result and Undo; **Cancel** runs nothing. If a plan stops unexpectedly or is found running after a restart, Tutti holds it
  rather than replaying potentially completed actions: inspect its steps and the actual work before issuing a new request.
  This is a manual hold, not automatic recovery or proof that an action stopped; an older Tutti version can still replay
  such a plan, so do not use an older version to resume it. A request you type in so many words also
  becomes a one-step plan.

In Autonomous and Ask before acting, a request you type in so many words ("add task in map: …") runs at once: your words
are the confirmation.

## Controls

Under the message box, as in a chat IDE, each starting at Auto:

- **Model**: Auto (local first, then online), Local models only, Online agents only, or one model (*Which model answers*).
- **Effort**: Auto, low, medium, high or max, mapped to each model's own setting:

  | Effort | A local model (Ollama) | Claude Code | Tool rounds |
  | --- | --- | --- | --- |
  | Auto | no thinking, the worker's `num_ctx` | as configured | 4 |
  | low | no thinking, at most 8,192 tokens of context | `--effort low` | 2 |
  | medium | no thinking, the worker's `num_ctx` | `--effort medium` | 4 |
  | high | thinks first, at least 32,768 | `--effort high` | 6 |
  | max | thinks first, 65,536 | `--effort max` | 8 |

  A local model's context never passes `workers.<n>.max_ctx`. Another online agent takes `workers.<n>.effort_args` (e.g.
  `['--reasoning', '{effort}']`); without it, it answers as it always does.
- **Mode**: *Modes* above.
- **Context** and tokens: how full the model's context was on the last answer, and the conversation's tokens in and out
  (Ollama's own counts; an online agent's are estimated, four characters a token) with what they would cost at API prices
  (an account's quota is spent, not money; a local model costs nothing). When the next request would fill the context past
  your threshold (`autocompact.threshold_pct`, 60 % by default; yours with `compact_pct`), the older messages are folded
  into a summary: a local model writes it without tools (or uses the first lines if it fails); for CLI agents Tutti uses
  the first lines directly, without launching another agent. The first-line summary can omit details; older turns stay on
  disk and remain readable through `chat_history`. Stopping during compaction preserves the previous summary when cancellation is observed before its save;
  the last four messages stay whole, and the chat marks where. Tutti sends summaries as historical data and
  asks before running managed changes when a summary is included. CLI agents may have native tools outside those checks;
  these measures cannot guarantee that a model will ignore instructions in historical text.
- **Stop** (Esc) ends an answer and keeps what was written. While an answer is running in this Chat engine, wait for it
  to finish before editing, retrying, deleting or clearing its history; clearing all chats also waits for this user’s other
  active answers. This check does not fence another process or prove that a CLI agent’s native descendants have stopped; **Edit** on any of your messages resends it and replaces what
  followed; **Regenerate** answers the last question again; **Branch** on any message starts a new chat with the messages
  up to it (same model, effort and mode), the original untouched.
- **@-mentions** add context: `@project:map` (its check and what waits), `@job:<id>` (its line and the end of its log),
  `@machine:nuc`, `@file:map/src/app.py` (a file git tracks in the repository, up to 32 KB; never an untracked or ignored
  file, never outside the repository, never a file that looks like a secret). Type `@` for a list (`@map/` lists the
  project's files); arrows choose, Enter or Tab adds. What could not be read is named under the answer. Viewers mention
  what the Dashboard tab shows them (projects, jobs' lines, machines); files and job logs are for admins.
  Mentioned content and attachments reach the model fenced as data, never instructions, and an answer that read any
  (an attachment on any message of the history the model sees, not only the last) asks
  before acting even in Autonomous mode, so a line in a log can never start work by itself.
- **Attachments**: text and logs (32 KB each) and images (PNG, JPEG, WebP, GIF, 1 MB each), 3 a message; paste a screenshot
  or a long log (over 4,000 characters) and it is attached. A local model given `workers.<n>.vision: true` sees images; the
  others (and every CLI agent) are told one is attached. Images are kept in their own files beside the user's chats
  (`<state>/chat/<user>.files/`, mode 600), so conversations stay small; Clear my chat history removes them too. Only a
  message may be large (5 MB); every other action takes at most 64 KB.
- **Slash commands**: `/new`, `/model <name>`, `/effort <level>`, `/mode auto|ask|plan`, `/clear` (empty this chat, after a
  confirmation; its settings stay), `/help`. Type `/` for the list. A path such as `/home/…` is sent as a message.
- **Keys**: Enter sends, Shift+Enter adds a line, Esc stops or closes a list, ↑ in an empty box edits your last message,
  `/` (outside a field) goes to the message box, Ctrl+K or ⌘K searches your chats, Alt+N starts a new one, `?` lists them.

## The page

- Answers stream in as they are written. A message that could not be sent says **Not sent** with **Send again**.
- Markdown is drawn safely: the text is parsed into a tree and drawn with DOM nodes and `textContent`, so no message ever
  becomes markup. Only `http`, `https` and `mailto` links are links. Code blocks have **Copy**.
- Links and paths (t304): a bare `https://` address in a message is a link; a path on the host (`/home/…`, `~/…`, `./…`,
  two parts or more) is underlined and a click copies it (a browser cannot open the host's files).
- Artifacts (t304): a code block in Markdown, HTML, SVG or JSON has **Preview**: Markdown drawn as above, JSON indented (or
  why it is not valid), HTML and SVG in a sandboxed frame with no scripts and no access to the dashboard, with **Open in
  the Browser panel**.
- **Terminal** and **Browser** panels (t304), hidden until their buttons beside the title (each remembered per browser).
  Terminal follows the log of this chat's work on the Dashboard (running first, a picker for the others), the last 200
  lines every 3 seconds with credentials replaced, from `GET /api/job-log/<job id>` (admins; only a log the dashboard
  lists for that job). Browser shows links you click in the chat while it is open, in a sandbox without the dashboard's
  cookies, with **Open in a new tab** for sites that refuse to be shown inside another page.
- The list follows new text while you are at the end; scroll up and it stays put, with **Jump to latest**.
- **This chat's work** (t298), under the title: every task, run or job this conversation started, newest first, with its
  live status from the dashboard's poll (queued and why it waits, running and where, or finished). It is open while any
  of them is queued or running; a running one links to its job on the Dashboard. Nothing started: nothing shown.
- An empty chat suggests questions from the state now (an idle machine, the quota, a project to queue tests for).
- Conversations: **New chat**, **Rename**, **Delete**, and search over titles and messages.
- Keys: Enter sends, Shift+Enter adds a line, Esc stops, ↑ edits. New answers are announced to screen readers (a polite
  live region); focus stays visible; animations stop with reduced motion.
- On a phone the list and the conversation take the screen in turn (**‹ Chats** goes back); the composer stays above the
  on-screen keyboard (`interactive-widget=resizes-content` and `visualViewport`), and the composer stays above it while you
  type. Light and dark follow the dashboard's theme and accent.

## What is stored

`<state>/chat/<hash of the user's id>.json`, mode 600 in a 700 folder, one file per user, written whole (a temporary file,
then a rename). It holds the conversations: messages, attachments, the model that answered and the action cards. It
survives restarts (an answer cut off by one is marked stopped) and `tutti move` carries it with the state
folder. **Clear my chat history** deletes the user's file; the work it queued and the audit log's lines stay.

Before a Chat tool changes something or a CLI agent starts, the answer records that work may have started. A stopped
or failed answer with that marker cannot be regenerated: check the work's status before sending a new request. Answers
known to be safe can still be retried. Older interrupted answers without the marker are treated as uncertain. This
guard requires one active engine and cooperating current-version writers; it does not stop or deduplicate existing jobs.

## API

`POST /api/chat` with `{"action": ...}`, the dashboard's usual same-origin and token checks, for every signed-in user:

| Action | Body | Answer |
| --- | --- | --- |
| `list` | `query` | conversations (newest first), suggestions, the role, the limits, the host's name, the picker's models and the user's default |
| `get` | `id` | one conversation and its `settings` (model, effort, mode, compact_pct) |
| `send` | `text`, `attachments` [{name, text} or {name, type, data}], `conversation`, `model`, `effort`, `mode`, `edit` (a message id), `retry` (an answer's id) | a stream (`application/x-ndjson`): `start`, `model`, `delta`, `replace`, `card`, `fallback`, `note`, `compacted`, `usage`, `ping`, `done` |
| `follow` | `message` | the same stream for an answer still being written (after a reload) |
| `stop` | `message` | `{stopped}` (only your own) |
| `new`, `rename`, `delete`, `clear` | `title`, `id` | `{ok}` |
| `undo` | `conversation`, `message`, `card`, `step` (a plan's) | the conversation |
| `plan` | `conversation`, `message`, `card`, `steps` [{id, text}] (the kept ones, in order; none: as proposed), `yes` | the plan card after it ran |
| `settings` | `id`, any of `model`, `effort`, `mode`, `compact_pct` | the conversation's settings (also the user's defaults) |
| `mentions` | `query` | what `@query` may stand for |
| `branch` | `conversation`, `message` | the new conversation |
| `empty` | `id` | `{ok}`: the conversation without its messages |
| `confirm` | `conversation`, `message`, `card`, `yes` (false cancels) | the card, run or cancelled |
| `model` | `model`, `id` | the same as `settings` with a model (kept for older pages) |
| `activity` | | admins: the chat's actions, newest first |

An answer runs in its own thread, so it finishes and is saved even when the page closes.

## Settings

```yaml
chat:
  viewer_tasks_per_day: 5   # 0: viewers only ask
  viewer_projects: []       # empty: every project under autopilot.repos
  history_turns: 10         # earlier messages a model sees
```

A Chat `job_status` check for a known delegated run without a report polls for up to two minutes while the answer is
open. Stop cancels polling after any in-flight status call returns, not the delegated run. A timeout reports uncertainty; monitoring is not persisted
across a dashboard restart.

A local model response must finish with its completion frame; an interrupted stream is incomplete. If a Chat action was already attempted, Tutti preserves its cards and partial text and does not retry another model automatically. Inspect the action before retrying. This does not make every provider failure recoverable (t303).

A CLI agent that fails after launch may have used native tools. Chat keeps the partial answer and asks you to inspect the work before retrying; it does not automatically try another model. A failure before launch may still fall back. This does not prove a stopped process or remote work has ended (t303).
