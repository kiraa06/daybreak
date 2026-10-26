import { api, stream } from "./api.js";
import { renderMarkdown } from "./md.js";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const TOOL_LABELS = {
  get_current_focus: "Checking your workstreams", list_meetings: "Looking at your calendar", create_artifact: "Making the document",
  schedule_prompt: "Scheduling", ToolSearch: "Finding tools", search_threads: "Searching Gmail", get_thread: "Reading an email",
  searchJiraIssuesUsingJql: "Searching Jira", searchConfluenceUsingCql: "Searching Confluence", search_files: "Searching Drive",
  read_file_content: "Reading a doc", create_draft: "Drafting an email",
};
let chatId = null;
let busy = false;

function hello() {
  const h = new Date().getHours();
  $("hello").textContent = h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
}

async function currentPage() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab || !/^https?:/.test(tab.url || "")) return null;
  try {
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => ({ url: location.href, title: document.title, selection: String(getSelection() || ""),
                     text: (document.body?.innerText || "").replace(/\n{3,}/g, "\n\n").slice(0, 20000) }),
    });
    return result;
  } catch {
    return { url: tab.url, title: tab.title, text: "", selection: "" };
  }
}

async function updatePageLabel() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  const ok = tab && /^https?:/.test(tab.url || "");
  $("use-page").disabled = !ok;
  $("page-label").textContent = ok ? `Use this page — ${tab.title || new URL(tab.url).host}` : "No page to read on this tab";
}

function addUser(text, page) {
  $("welcome")?.remove();
  const el = document.createElement("div");
  el.className = "msg user";
  el.innerHTML = `<div><div class="bubble">${esc(text)}</div>${page ? `<div class="page">📄 ${esc(page.title || page.url)}</div>` : ""}</div>`;
  $("log").appendChild(el);
  el.scrollIntoView({ block: "end" });
}

function addAI() {
  const el = document.createElement("div");
  el.className = "msg ai";
  el.innerHTML = `<div class="tools"></div><div class="body"><span class="thinking">Thinking…</span></div>`;
  $("log").appendChild(el);
  return el;
}

function decorateArtifacts(el) {
  el.querySelectorAll('a[href^="http://127.0.0.1"]').forEach((a) => {
    if (!/\/a\//.test(a.href)) return;
    a.classList.add("artifact");
    a.innerHTML = `<small>Open document</small>${a.textContent === a.href ? "View it in a new tab" : esc(a.textContent)}`;
  });
}

async function send(text) {
  text = text.trim();
  if (!text || busy) return;
  busy = true;
  $("send").disabled = true;
  const page = $("use-page").checked && !$("use-page").disabled ? await currentPage() : null;
  addUser(text, page);
  $("input").value = "";
  autosize();
  const el = addAI();
  const body = el.querySelector(".body");
  const tools = el.querySelector(".tools");
  let acc = "";
  const seen = new Set();
  try {
    await stream("/api/chat", { chat_id: chatId, message: text, page }, (ev) => {
      if (ev.type === "chat") chatId = ev.chat_id;
      if (ev.type === "text") {
        acc += ev.text;
        body.innerHTML = renderMarkdown(acc);
      }
      if (ev.type === "tool") {
        const short = ev.name.split("__").pop();
        const label = TOOL_LABELS[short] || short.replace(/_/g, " ");
        if (!seen.has(label)) {
          seen.add(label);
          tools.insertAdjacentHTML("beforeend", `<span class="tool">${esc(label)}</span>`);
        }
        if (!acc) body.innerHTML = `<span class="thinking">${esc(label)}…</span>`;
      }
      if (ev.type === "error") body.insertAdjacentHTML("beforeend", `<p class="error">${esc(ev.text)}</p>`);
      el.scrollIntoView({ block: "end" });
    });
    if (!acc && !body.querySelector(".error")) body.innerHTML = "<p class='error'>No answer came back.</p>";
    decorateArtifacts(body);
    loadHistory();
  } catch (e) {
    body.innerHTML = `<p class="error">${esc(e.offline ? "The Daybreak helper isn't running — start it with `daybreak run`." : e.message)}</p>`;
  } finally {
    busy = false;
    $("send").disabled = false;
    $("input").focus();
  }
}

async function loadHistory() {
  try {
    const chats = await api("/api/chats");
    $("history").innerHTML = `<option value="">Recent chats</option>` +
      chats.map((c) => `<option value="${esc(c.id)}" ${c.id === chatId ? "selected" : ""}>${esc(c.title.slice(0, 40))}</option>`).join("");
  } catch { /* helper offline */ }
}

async function openChat(id) {
  chatId = id;
  $("log").innerHTML = "";
  const msgs = await api(`/api/chats/${id}`);
  for (const m of msgs) {
    if (m.role === "user") addUser(m.content);
    else {
      const el = addAI();
      el.querySelector(".body").innerHTML = renderMarkdown(m.content);
      decorateArtifacts(el);
    }
  }
}

async function welcome() {
  hello();
  const chips = ["What should I focus on today?", "Summarize this page for me", "Prep me for my next meeting",
                 "What's waiting on me? Draft the replies", "Make a one-pager on my current work"];
  $("chips").innerHTML = chips.map((c) => `<button class="chip">${esc(c)}</button>`).join("");
  $("chips").querySelectorAll(".chip").forEach((b) => (b.onclick = () => send(b.textContent)));
  try {
    const st = await api("/api/state");
    if (st.me && st.me !== "the user") $("hello").textContent += `, ${st.me.split(" ")[0]}`;
    if (st.next_meeting) {
      const m = st.next_meeting;
      $("next").innerHTML = `Next: <strong>${esc(m.title)}</strong> at ${esc(m.time)}` +
        (m.prep_url ? ` · <a href="${esc(m.prep_url)}" target="_blank">prep notes</a>` : "");
    }
  } catch (e) {
    $("next").textContent = e.offline ? "The Daybreak helper isn't running — start it with `daybreak run`." : e.message;
  }
}

function autosize() {
  const t = $("input");
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 180) + "px";
}

async function takePending() {
  const { pendingPrompt } = await chrome.storage.local.get("pendingPrompt");
  if (pendingPrompt && Date.now() - pendingPrompt.at < 60_000) {
    await chrome.storage.local.remove("pendingPrompt");
    if (pendingPrompt.prompt) send(pendingPrompt.prompt);
    else $("input").focus();
  }
}

$("form").onsubmit = (e) => { e.preventDefault(); send($("input").value); };
$("input").addEventListener("input", autosize);
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send($("input").value); }
});
$("new").onclick = () => { chatId = null; location.reload(); };
$("history").onchange = (e) => e.target.value && openChat(e.target.value);
chrome.storage.onChanged.addListener((changes) => { if (changes.pendingPrompt?.newValue) takePending(); });
chrome.tabs.onActivated.addListener(updatePageLabel);
chrome.tabs.onUpdated.addListener((_id, info) => info.title && updatePageLabel());

welcome();
loadHistory();
updatePageLabel();
takePending();
