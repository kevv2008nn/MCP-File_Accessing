// Drives the REAL web UI (app.js) in a simulated browser (jsdom) against the REAL host (scripted model).
// Setup (one time):  npm install jsdom      Run:  node tests/ui_test.js
const { JSDOM } = require("jsdom");
const { spawn } = require("child_process");
const fs = require("fs"), os = require("os"), path = require("path");

const ROOT = path.resolve(__dirname, "..");
const PY = process.env.PYTHON || "python";
const port = 20000 + Math.floor(Math.random() * 20000);
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ui_test_"));
const files = path.join(tmp, "files"); fs.mkdirSync(files);
for (const f of ["song.mp3", "notes.pdf", "pic.jpg"]) fs.writeFileSync(path.join(files, f), "x");
let fails = 0;
const check = (name, cond, extra = "") => { console.log((cond ? "PASS  " : "FAIL  ") + name + (!cond && extra ? "   -> " + extra : "")); if (!cond) fails++; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitFor(fn, what, ms = 15000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) { try { const v = fn(); if (v) return v; } catch {} await sleep(50); }
  throw new Error("timeout waiting for: " + what);
}

(async () => {
  const host = spawn(PY, ["run.py"], { cwd: ROOT, stdio: ["ignore", "ignore", "ignore"], env: { ...process.env, PORT: String(port),
    AGENT_DATA_DIR: path.join(tmp, "data"), ORGANIZER_ROOT: files, LLM_MODE: "scripted", APPROVAL_TIMEOUT: "60" } });
  let dom;
  try {
    await waitFor(async () => false, "x", 1).catch(() => {});
    for (let i = 0; i < 80; i++) { try { await fetch(`http://127.0.0.1:${port}/api/status`); break; } catch { await sleep(500); } }
    dom = await JSDOM.fromURL(`http://127.0.0.1:${port}/`, { runScripts: "dangerously", resources: "usable", pretendToBeVisual: true });
    const w = dom.window, d = w.document, $ = (s) => d.querySelector(s), $$ = (s) => [...d.querySelectorAll(s)];
    const send = async (text) => { $("#input").value = text; $("#send").click(); };
    const msgs = () => $$("#messages .msg.assistant").map((e) => e.textContent).join(" | ");

    await waitFor(() => $("#conn").textContent === "connected", "websocket connected");
    await waitFor(() => $$("#servers .server").length === 3, "3 server cards");
    check("connected pill is shown", $("#conn").className.includes("on"));
    check("provider label shown", $("#provider").textContent.includes("Scripted"));
    check("3 servers listed with tool counts", $$("#servers .count").map((e) => e.textContent).join() === "14 tools,6 tools,6 tools", $$("#servers .count").map((e) => e.textContent).join());
    check("risk badges rendered", $$("#servers .risk.destructive").length === 4 && $$("#servers .risk.read").length > 5);
    check("empty-state chips present", $$(".chip").length === 4);

    // ---- XSS: hostile text must stay text
    await send("add task <img src=x onerror=alert(1)> **boom** <script>window.pwned=1</script>");
    await waitFor(() => msgs().includes("boom") , "assistant reply to hostile task");
    await waitFor(() => !$("#input").disabled, "composer re-enabled");
    check("user bubble shown as plain text", $("#messages .msg.user").textContent.includes("<img src=x"));
    check("NO <img>/<script> element injected from tool output", !$("#messages img") && !$("#messages script") && !w.pwned);
    check("hostile text displayed literally", msgs().includes("<img src=x"));
    const card = $("#messages details.tool-card");
    check("tool card shows server · tool and 'done'", card && card.querySelector("summary").textContent.includes("tasks · add_task") && card.querySelector(".status").textContent === "done");
    check("tool card has args + result blocks", card.querySelectorAll("pre").length === 2);

    // ---- busy state + approval flow (approve)
    await send("tidy");
    const box = await waitFor(() => $(".approval"), "approval box");
    check("approval box names the risky tool", box.textContent.includes("files · apply_plan"));
    check("composer locked + Stop visible while waiting", $("#input").disabled && !$("#stop").hidden && $("#send").hidden);
    check("files not moved while waiting for approval", fs.existsSync(path.join(files, "song.mp3")));
    [...box.querySelectorAll("button")].find((b) => b.textContent === "Approve").click();
    await waitFor(() => msgs().includes("Done: moved 3 files"), "tidy finished");
    await waitFor(() => !$("#input").disabled, "composer unlocked");
    check("approved: files moved on disk", fs.existsSync(path.join(files, "Audio", "song.mp3")));
    check("approval box collapsed to 'Approved'", box.textContent.includes("Approved") && !box.querySelector("button"));
    check("Send button restored, Stop hidden", !$("#send").hidden && $("#stop").hidden);

    // ---- deny
    await send("undo");
    const box2 = await waitFor(() => $$(".approval")[1], "second approval box");
    [...box2.querySelectorAll("button")].find((b) => b.textContent === "Deny").click();
    await waitFor(() => msgs().includes("I did not undo anything"), "deny handled");
    check("denied: nothing restored", fs.existsSync(path.join(files, "Audio", "song.mp3")));
    check("approval box collapsed to 'Denied'", box2.textContent.includes("Denied"));
    const denied = $$("details.tool-card").find((c) => c.querySelector("summary").textContent.startsWith("files · undo_plan"));
    check("denied tool card marked as error", denied.querySelector(".status").className.includes("err"));

    // ---- server toggle
    await waitFor(() => !$("#input").disabled, "idle");
    const tasksCb = $$("#servers .server").find((s) => s.textContent.includes("tasks")).querySelector("input");
    tasksCb.checked = false; tasksCb.dispatchEvent(new w.Event("change"));
    await send("how many tools");
    await waitFor(() => msgs().includes("20 tools"), "20 tools after switching tasks off");
    check("switching a server off hides its tools from the model", msgs().includes("20 tools") && !msgs().includes("20 tools from servers: files, notes, tasks"));
    check("toggle persisted to localStorage", !JSON.parse(w.localStorage.getItem("enabled")).includes("tasks"));

    // ---- safe markdown renderer, directly
    const host2 = d.createElement("div");
    w.renderRich(host2, "**bold** and `code` <b>nope</b>\n- one\n- two\n```\nx<y && z\n```");
    check("markdown: bold/code/list/fence render", host2.querySelector("strong")?.textContent === "bold" && host2.querySelector("code")?.textContent === "code"
      && host2.querySelectorAll("li").length === 2 && host2.querySelector("pre")?.textContent === "x<y && z");
    check("markdown: raw HTML stays text", !host2.querySelector("b") && host2.textContent.includes("<b>nope</b>"));
    const unclosed = d.createElement("div"); w.renderRich(unclosed, "```\nnever closed");
    check("markdown: unclosed fence doesn't crash", unclosed.querySelector("pre")?.textContent === "never closed");

    // ---- chips send a message
    await waitFor(() => !$("#input").disabled, "idle");
    const before = $$("#messages .msg.user").length;
    check("empty state removed after first message", $("#empty") === null);
    check("every user message got an answer", $$("#messages .msg.user").length === before && before >= 4);
  } catch (e) { console.log("FAIL  " + e.message); fails++; }
  finally { host.kill(); dom?.window.close(); fs.rmSync(tmp, { recursive: true, force: true }); }
  console.log("\nRESULT:", fails ? fails + " FAILED" : "ALL UI TESTS PASSED");
  process.exit(fails ? 1 : 0);
})();
