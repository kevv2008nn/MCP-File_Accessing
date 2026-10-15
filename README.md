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

