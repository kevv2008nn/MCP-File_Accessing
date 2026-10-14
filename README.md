# MCP Agent Platform

A personal AI assistant you run on your own computer. A web chat UI talks to a model (cloud free tier or
local LM Studio) and the model can use **three MCP servers**:

| Server | Tools | What it does |
|---|---|---|
| `files` | 14 | Safe file organizer: plan, preview, apply, undo. Never deletes. |
| `tasks` | 6 | To-do list in SQLite: add, list, agenda, complete, reschedule, delete. |
| `notes` | 6 | Markdown notebook with keyword search. Notes are plain `.md` files you can also edit by hand. |

```
Browser UI ◄─ WebSocket ─► Host (FastAPI + agent loop) ◄─ OpenAI-compatible API ─► LLM (cloud or LM Studio)
                              ├─ MCP/stdio ─► servers/file_organizer
                              ├─ MCP/stdio ─► servers/tasks
                              └─ MCP/stdio ─► servers/notes
```

## Setup (Windows, Command Prompt)

Put the folder somewhere with a short path, e.g. `C:\mcp\agent_platform`. You need Python 3.10+.

```bat
cd C:\mcp\agent_platform
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
notepad .env
```

In `.env`, uncomment **one** provider block and fill it in:

* **LM Studio (local):** start LM Studio's server (Developer tab), load a tool-capable model, and keep the defaults.
* **Groq / OpenRouter / Gemini (free cloud tiers):** create a free API key on the provider's site, set
  `LLM_BASE_URL`, `LLM_MODEL` (choose a model that supports tools/function calling) and `LLM_API_KEY`.
  Free-tier limits and terms change often. Check the provider's site.

Then start it and open the address it prints:

```bat
python run.py
```

→ **http://127.0.0.1:8000**

## Using it

* Try: *"What's on my agenda today?"*, *"Add a task: finish lab record, due tomorrow, high priority"*,
  *"Save a note about what I learned about deadlocks"*, *"Find my note about deadlocks"*.
* **Files:** by default the file tools only touch `data\files_sandbox`. To organize a real folder, set
  `ORGANIZER_ROOT=C:\Users\you\Downloads` in `.env`. Try a copy of a messy folder first.
* **Approvals:** tools labelled *destructive* in the sidebar (apply a file plan, undo, delete task/note) show an
  Approve / Deny box. Reads and additive writes run without asking. Set `"approval": "always"` in `config.json`
  to be asked for every call. The labels come from each tool's MCP annotations; a tool with no annotations is treated as destructive.
* **Server toggles** in the sidebar hide a server's tools from the model. With small local models (8k context), switch off
  servers you don't need; it makes answers faster and more accurate.
* Your data lives in `data\`: `tasks.db`, `notes\`, and the file journal inside the organizer folder.

## Adding your own MCP server

1. Write a server (copy `servers/tasks/server.py` as a template).
2. Add an entry to `config.json` under `servers`.
3. Restart. Its tools appear in the sidebar automatically, namespaced as `<server>__<tool>`.

Any third-party stdio MCP server works too; give it a `command`, `args` and only the `env` vars it needs.

## Tests

```bat
python tests\test_servers.py      :: tasks + notes servers over real MCP stdio
python tests\test_platform.py     :: whole platform: agent loop, approvals, security, provider errors
node tests\ui_test.js             :: the web UI in a simulated browser (needs: npm install jsdom)
```

These use a scripted stand-in model and a mock OpenAI-compatible server. No API key is needed or used.

## Security design (read this before changing things)

* The server listens on **127.0.0.1 only**. Don't expose it to a network.
* The WebSocket rejects foreign `Origin` and `Host` headers; otherwise any website open in your browser could drive your agent.
* Your API key stays in the host process. MCP server processes get only the variables listed in `config.json`.
* All model/tool text is rendered with `textContent`, never `innerHTML`, and the page has a strict Content-Security-Policy.
  File names and note text are untrusted: they can contain instructions aimed at the model.
* Risky actions are gated by the **host** (the approval box), not by trusting the model. The file server adds a second
  gate (a confirm code only `preview_plan` can issue).

## Known limits: what was and wasn't verified

Verified in testing (Linux): all three servers over real MCP stdio, the agent loop, approval/deny/timeout/disconnect paths,
origin checks, the OpenAI-compatible request/response format (against a mock), friendly provider errors, and the UI's
logic and XSS resistance (in jsdom).

**Not verified:**
* **Windows.** Everything was developed and tested on Linux. The likeliest trouble spot is launching MCP subprocesses
  from the web server on Windows. If servers show as "offline" in the sidebar, check the console output of `python run.py`.
* **Real AI providers.** Tested only against a mock. Provider quirks (stricter tool-schema rules, model-specific
  behavior) may need small tweaks. Gemini's compatibility endpoint in particular may reject some schema keywords.
* **A real browser.** The UI logic ran in jsdom, so layout and visual appearance were not checked.
* **Model quality.** Small local models make mistakes with many tools. The host's guards (approvals, loop limit, schema
  enums, error feedback) limit the damage, but can't make a weak model smart.

## Ideas for next steps

Chat history persistence · streaming responses · an `expenses` server · a morning-briefing prompt · embeddings for semantic
note search · MCP *elicitation* for approvals · Streamable HTTP transport · a third-party server such as a web fetcher.
