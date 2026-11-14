# Chat

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
  | `delegate in <project>: <task>` | `delegate_task` (a run in the background) | none: stop it on the Dashboard tab |
  | `ask <scout>: <question>` | `scout_ask` | none |
  | `pause <name>` / `resume <name>` (a machine, device, worker or account) | the dashboard's pause switch | the opposite switch |

  Each action shows as a card under the answer: **Done**, **Not allowed** or **Failed**, with what happened, its id, and
  **Undo** where it can be taken back. A card of a task or run also shows its live status ("Queued · waits: …", "Running
  on nuc · 2 min") with a link to the job on the Dashboard tab. Every action, and every Undo, goes into the audit log
  (`<state>/auth-audit.jsonl`, the one the Users tab shows).
- In your own words too: a model (local or online) turns "the legend overlaps on phones, get it fixed" into a proposed
  `backlog_add` card that you confirm (*Tools* below).

## Who may do what

The server decides, for every action, with `auth.act_for` (and so `auth.may_act_for`):

- **Viewers** ask anything, and create work for themselves: tasks, hand-offs and scout questions, for the projects in
  `chat.viewer_projects` (empty: every project), up to `chat.viewer_tasks_per_day` (5) a day. A refused request says why
  and who can change it.
- **Admins** also pause and resume machines, devices, workers and accounts. Settings and users stay on their own tabs.
- **Pending** users (waiting for approval) cannot use the chat.
- Conversations are private: each user sees only their own. Admins have **Household activity**: the actions the chat took
  for each member, from the audit log, never anyone's messages.

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
2. **Online accounts with room**: not in panic, spent or late, not paid per use (`costs_money`), not paused. Only the
   host's owner (the first admin, or the person on this computer when sign-in is off) uses the host's own accounts;
   another member uses one only when its owner shares it (`accounts.<n>.share_spare`). Another member's private quota is
   never spent.
3. When none answers, the **built-in answer**: running jobs, quota, the queue or why a machine is idle, straight from the
   state.

Each answer names its model ("qwen3.8:27b-q8_0 on w5090"). When it fell back, it says why under the name ("Answered by
claude (claude account) because pc is asleep; nuc did not answer."). A model that does not answer (a machine asleep, a
tunnel down) is skipped for the next one within the same answer.

## Tools: the same work from any model

A model works through the same calls as the MCP tools, so a local model can do what an online one can:

| Tool | Kind | What it does |
| --- | --- | --- |
| `usage`, `machines`, `devices`, `backlog_list`, `job_status` | read | runs at once; its output goes back to the model |
| `backlog_add`, `delegate_task`, `scout_ask` | work | proposed; runs when the user confirms |
| `test_on` (a track and a command, on a machine or an Android device) | work | proposed; admins |
| `control` (pause or resume a machine, device, worker or account) | household | proposed; admins |

- **Native tool calls** for local models: Ollama's function calling with each tool's JSON schema. A model under 7B
  parameters only talks (it is not given the tools). Online agents (a CLI such as `claude -p`) get the same schemas and
  answer with `ACTION {"tool": ..., "args": {...}}` lines, which the page never shows.
- **Validation**: every call is checked against its schema (required fields, types, enums, no other fields) and against
  the names that exist here (projects, scouts, machines, devices). A bad call is not run; its error goes back to the model.
- **Confirmation**: a call that changes something is never run by the model. It becomes a card, **Needs your OK**, with
  **Confirm** and **Cancel**; Confirm checks it again, then runs it with the same role checks, audit line and Undo as a
  request you typed. A request you type in so many words ("add task in map: …") runs at once: your words are the
  confirmation.
- Up to 4 tool rounds per answer.

## The page

- Answers stream in as they are written. **Stop** (or Esc) ends one and keeps what was written. **Retry** answers the
  last question again; **Edit** (or ↑ in an empty box) changes your last message and answers again. A message that could
  not be sent says **Not sent** with **Send again**.
- Markdown is drawn safely: the text is parsed into a tree and drawn with DOM nodes and `textContent`, so no message ever
  becomes markup. Only `http`, `https` and `mailto` links are links. Code blocks have **Copy**.
- The list follows new text while you are at the end; scroll up and it stays put, with **Jump to latest**.
- An empty chat suggests questions from the state now (an idle machine, the quota, a project to queue tests for).
- **Attach** text or log snippets: up to 3 files of 32 KB each, text only (checked on the page and on the server).
- Conversations: **New chat**, **Rename**, **Delete**, and search over titles and messages.
- Keys: Enter sends, Shift+Enter adds a line, Esc stops, ↑ edits. New answers are announced to screen readers (a polite
  live region); focus stays visible; animations stop with reduced motion.
- On a phone the list and the conversation take the screen in turn (**‹ Chats** goes back); the composer stays above the
  on-screen keyboard (`interactive-widget=resizes-content` and `visualViewport`), and the bottom tab bar hides while you
  type. Light and dark follow the dashboard's theme and accent.

## What is stored

`<state>/chat/<hash of the user's id>.json`, mode 600 in a 700 folder, one file per user, written whole (a temporary file,
then a rename). It holds the conversations: messages, attachments, the model that answered and the action cards. It
survives restarts (an answer cut off by one is marked stopped, with Retry) and `tutti move` carries it with the state
folder. **Clear my chat history** deletes the user's file; the work it queued and the audit log's lines stay.

## API

`POST /api/chat` with `{"action": ...}`, the dashboard's usual same-origin and token checks, for every signed-in user:

| Action | Body | Answer |
| --- | --- | --- |
| `list` | `query` | conversations (newest first), suggestions, the role, the limits, the host's name, the picker's models and the user's default |
| `get` | `id` | one conversation |
| `send` | `text`, `attachments` [{name, text}], `conversation`, `model`, `edit` (a message id), `retry` (an answer's id) | a stream (`application/x-ndjson`): `start`, `model`, `delta`, `replace`, `card`, `fallback`, `note`, `ping`, `done` |
| `follow` | `message` | the same stream for an answer still being written (after a reload) |
| `stop` | `message` | `{stopped}` (only your own) |
| `new`, `rename`, `delete`, `clear` | `title`, `id` | `{ok}` |
| `undo` | `conversation`, `message`, `card` | the conversation |
| `confirm` | `conversation`, `message`, `card`, `yes` (false cancels) | the card, run or cancelled |
| `model` | `model` (auto, local, online or a worker), `id` (the conversation) | `{model}`: the conversation's choice and the user's default |
| `activity` | | admins: the chat's actions, newest first |

An answer runs in its own thread, so it finishes and is saved even when the page closes.

## Settings

```yaml
chat:
  viewer_tasks_per_day: 5   # 0: viewers only ask
  viewer_projects: []       # empty: every project under autopilot.repos
  history_turns: 10         # earlier messages a model sees
```
