"""
ScriptedLLM: NOT an AI. A tiny deterministic stand-in for a model, used to test the
host (agent loop, approvals, MCP plumbing, UI) without an API key.
Enable with LLM_MODE=scripted. It understands only these phrases:

  how many tools            add task <title> [due YYYY-MM-DD]     agenda
  delete task <id>          save note <title> :: <text>           find note <query>
  tidy                      undo
"""
import json
import re

from .llm import ToolCall, Turn

HELP = ("I'm the scripted test model, not a real AI. Try: 'agenda', 'add task Buy milk due 2026-10-20', "
        "'delete task 1', 'save note Idea :: text', 'find note idea', 'tidy', 'undo', 'how many tools'.")


class ScriptedLLM:
    model = "scripted"

    async def chat(self, messages: list[dict], tools: list[dict]) -> Turn:
        last_user = max((i for i, m in enumerate(messages) if m["role"] == "user"), default=-1)
        text = (messages[last_user]["content"] if last_user >= 0 else "").strip()
        low = text.lower()
        names = {}
        for m in messages:
            for tc in m.get("tool_calls") or []:
                names[tc["id"]] = tc["function"]["name"]
        results = [(names.get(m.get("tool_call_id"), "?"), m["content"])
                   for m in messages[last_user + 1:] if m["role"] == "tool"]
        last = results[-1] if results else None
        n = len(results) + 1

        def call(name, **args):
            return Turn(tool_calls=[ToolCall(id=f"call_{n}", name=name, arguments=json.dumps(args))])

        def denied():
            return last is not None and last[1].startswith("The user DENIED")

        def js():
            try:
                return json.loads(last[1])
            except (ValueError, TypeError):
                return {}

        if "how many tools" in low:
            servers = sorted({t["function"]["name"].split("__")[0] for t in tools})
            return Turn(text=f"I can see {len(tools)} tools from servers: {', '.join(servers) or 'none'}.")

        if low.startswith("tidy"):
            if last is None:
                return call("files__plan_sort_by_type", subdir=".")
            tool, data = last[0], js()
            if tool == "files__plan_sort_by_type":
                if not data.get("plan_id"):
                    return Turn(text="Nothing to tidy: the files are already sorted.")
                return call("files__preview_plan", plan_id=data["plan_id"])
            if tool == "files__preview_plan":
                if not data.get("safe_to_apply"):
                    return Turn(text="The preview found problems, so I did not apply anything.")
                return call("files__apply_plan", plan_id=data["plan_id"], confirm_code=data["confirm_code"])
            if tool == "files__apply_plan":
                return Turn(text="I did not apply the plan." if denied() else f"Done: moved {data.get('moved')} files.")

        if low.startswith("undo"):
            if last is None:
                return call("files__undo_plan")
            return Turn(text="I did not undo anything." if denied() else f"Undone: restored {js().get('restored')} files.")

        m = re.match(r"add task (.+?)(?: due (\d{4}-\d\d-\d\d))?$", text, re.I)
        if m:
            if last is None:
                return call("tasks__add_task", title=m.group(1), due=m.group(2) or "")
            return Turn(text=f"Result: {last[1]}")

        if low == "agenda":
            return call("tasks__agenda", days=1) if last is None else Turn(text=f"Agenda: {last[1]}")

        m = re.match(r"delete task (\d+)$", low)
        if m:
            if last is None:
                return call("tasks__delete_task", task_id=int(m.group(1)))
            return Turn(text="I did not delete it." if denied() else f"Result: {last[1]}")

        m = re.match(r"save note (.+?) :: (.+)$", text, re.I)
        if m:
            return call("notes__add_note", title=m.group(1), content=m.group(2)) if last is None \
                else Turn(text=f"Result: {last[1]}")

        m = re.match(r"find note (.+)$", text, re.I)
        if m:
            return call("notes__search_notes", query=m.group(1)) if last is None \
                else Turn(text=f"Result: {last[1]}")

        return Turn(text=HELP)
