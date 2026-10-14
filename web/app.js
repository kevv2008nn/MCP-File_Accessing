"use strict";
// All text from the model and from tools is UNTRUSTED. We only ever use textContent / createTextNode,
// never innerHTML, so nothing a file name or note contains can inject markup or script.

const $ = (id) => document.getElementById(id);
const messages = $("messages"), input = $("input"), sendBtn = $("send"), stopBtn = $("stop");
const toolCards = new Map();
let ws = null, busy = false, servers = [], enabled = new Set(), approvalMode = "auto";

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

// ---- minimal, safe markdown: **bold**, `code`, - bullets, ``` fences ----
function inline(parent, text) {
  for (const part of text.split(/(\*\*[^*]+\*\*|`[^`]+`)/)) {
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) parent.appendChild(el("strong", "", part.slice(2, -2)));
    else if (part.startsWith("`") && part.endsWith("`") && part.length > 2) parent.appendChild(el("code", "", part.slice(1, -1)));
    else parent.appendChild(document.createTextNode(part));
  }
}
function renderRich(container, text) {
  let list = null, fence = null;
  for (const line of text.split("\n")) {
    if (line.trim().startsWith("```")) {
      if (fence === null) { fence = []; list = null; } else { container.appendChild(el("pre", "", fence.join("\n"))); fence = null; }
      continue;
    }
    if (fence !== null) { fence.push(line); continue; }
    const m = line.match(/^\s*[-*]\s+(.*)$/);
    if (m) {
      if (!list) { list = el("ul"); container.appendChild(list); }
      const li = el("li"); inline(li, m[1]); list.appendChild(li);
    } else if (line.trim()) {
      list = null; const p = el("p"); inline(p, line); container.appendChild(p);
    } else list = null;
  }
  if (fence !== null) container.appendChild(el("pre", "", fence.join("\n")));
}

// ---- chat rendering ----
function scrollDown() { messages.scrollTop = messages.scrollHeight; }
function addMsg(kind, text) {
  $("empty")?.remove();
  const d = el("div", "msg " + kind);
  if (kind === "assistant") renderRich(d, text); else d.textContent = text;
  messages.appendChild(d); scrollDown(); return d;
}
function pretty(obj) { try { return JSON.stringify(obj, null, 2); } catch { return String(obj); } }

function addToolCall(ev) {
  $("empty")?.remove();
  const card = el("details", "tool-card");
  const sum = el("summary");
  sum.append(el("span", "", `${ev.server} · ${ev.tool}`), el("span", "status", "running…"));
  card.append(sum, el("pre", "", pretty(ev.args)));
  messages.appendChild(card); toolCards.set(ev.id, card); scrollDown();
}
function finishToolCall(ev) {
  const card = toolCards.get(ev.id);
  if (!card) return;
  const st = card.querySelector(".status");
  st.textContent = ev.is_error ? "error" : "done"; st.className = "status " + (ev.is_error ? "err" : "ok");
  card.appendChild(el("pre", "", ev.text)); scrollDown();
}
function addApproval(ev) {
  const box = el("div", "approval");
  box.append(el("h3", "", `Approve this action?  ${ev.server} · ${ev.tool}`), el("pre", "", pretty(ev.args)));
  const row = el("div", "row"), yes = el("button", "", "Approve"), no = el("button", "ghost", "Deny");
  no.style.marginTop = "0"; no.style.width = "auto";
  const decide = (ok) => {
    ws.send(JSON.stringify({ type: "approval", id: ev.id, approved: ok }));
    row.replaceWith(el("div", "muted small", ok ? "Approved" : "Denied"));
  };
  yes.onclick = () => decide(true); no.onclick = () => decide(false);
  row.append(yes, no); box.appendChild(row); messages.appendChild(box); scrollDown();
}

// ---- sidebar ----
function saveEnabled() { try { localStorage.setItem("enabled", JSON.stringify([...enabled])); } catch {} }
function loadEnabled() { try { return JSON.parse(localStorage.getItem("enabled")); } catch { return null; } }
function renderServers() {
  const box = $("servers"); box.replaceChildren();
  for (const s of servers) {
    const card = el("div", "server" + (s.ok ? "" : " down"));
    const label = el("label"), cb = el("input"); cb.type = "checkbox"; cb.disabled = !s.ok; cb.checked = s.ok && enabled.has(s.name);
    cb.onchange = () => { cb.checked ? enabled.add(s.name) : enabled.delete(s.name); saveEnabled(); };
    label.append(cb, el("span", "", s.name), el("span", "count", s.ok ? `${s.tools.length} tools` : "offline"));
    card.appendChild(label);
    if (!s.ok) card.appendChild(el("div", "err", s.error || "failed to start"));
    else {
      const det = el("details"); det.appendChild(el("summary", "", "show tools"));
      for (const t of s.tools) {
        const row = el("div", "tool"); row.title = t.description;
        row.append(el("span", "risk " + t.risk, t.risk), el("span", "", t.name)); det.appendChild(row);
      }
      card.appendChild(det);
    }
    box.appendChild(card);
  }
}
function applyHello(ev) {
  servers = ev.servers; approvalMode = ev.approval;
  const saved = loadEnabled(), ok = servers.filter((s) => s.ok).map((s) => s.name);
  enabled = new Set(saved ? saved.filter((n) => ok.includes(n)) : ok);
  $("provider").textContent = "Model: " + ev.provider.label;
  $("approvalNote").textContent = approvalMode === "always"
    ? "Every tool call needs your approval." : "Risky tools ask for your approval, based on each tool's MCP annotations.";
  renderServers();
}

// ---- connection ----
function setBusy(b) { busy = b; sendBtn.hidden = b; stopBtn.hidden = !b; input.disabled = b; if (!b) input.focus(); }
function setConn(on) { const c = $("conn"); c.textContent = on ? "connected" : "disconnected"; c.className = "pill " + (on ? "on" : "off"); }
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => setConn(true);
  ws.onclose = () => { setConn(false); setBusy(false); setTimeout(connect, 2000); };
  ws.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    switch (ev.type) {
      case "hello": applyHello(ev); break;
      case "assistant": addMsg("assistant", ev.text); break;
      case "tool_call": addToolCall(ev); break;
      case "tool_result": finishToolCall(ev); break;
      case "approval_request": addApproval(ev); break;
      case "error": addMsg("error", ev.text); break;
      case "done": setBusy(false); break;
      case "reset_ok": messages.replaceChildren(); toolCards.clear(); location.reload(); break;
    }
  };
}
function send(text) {
  text = text.trim();
  if (!text || busy || ws.readyState !== WebSocket.OPEN) return;
  addMsg("user", text); input.value = ""; autosize(); setBusy(true);
  ws.send(JSON.stringify({ type: "chat", text, enabled: [...enabled] }));
}
function autosize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 180) + "px"; }

sendBtn.onclick = () => send(input.value);
stopBtn.onclick = () => ws.send(JSON.stringify({ type: "stop" }));
input.addEventListener("input", autosize);
input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input.value); } });
$("newChat").onclick = () => { if (!busy) ws.send(JSON.stringify({ type: "reset" })); };
document.querySelectorAll(".chip").forEach((c) => (c.onclick = () => send(c.textContent)));
connect();
