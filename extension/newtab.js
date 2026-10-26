import { api, post, blobUrl, askInPanel } from "./api.js";
import { renderToast } from "./toast.js";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const slugify = (s) => String(s || "").toLowerCase().replace(/[^a-z]+/g, "-").replace(/^-|-$/g, "");
let pollTimer = null;

function showEmpty(title, text, actions = []) {
  $("brief").hidden = true;
  $("empty").hidden = false;
  $("empty-title").textContent = title;
  $("empty-text").innerHTML = text;
  const box = $("empty-actions");
  box.innerHTML = "";
  for (const a of actions) {
    const b = document.createElement("button");
    b.className = "btn" + (a.secondary ? " secondary" : "");
    b.textContent = a.label;
    b.onclick = a.onClick;
    box.appendChild(b);
  }
}

function tickClock() {
  const now = new Date();
  $("clock").textContent = now.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }).toUpperCase();
  $("date").textContent = now.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }).toUpperCase();
}

const ask = (prompt) => prompt && askInPanel(prompt);

function facepile(people = []) {
  const list = people.filter(Boolean).slice(0, 4);
  if (!list.length) return "";
  const initials = (n) => String(n).replace(/@.*/, "").split(/[\s._-]+/).map((w) => w[0] || "").join("").slice(0, 2).toUpperCase();
  const names = list.slice(0, 3).map((n) => String(n).replace(/@.*/, "")).join(", ") + (people.length > 3 ? ` +${people.length - 3}` : "");
  return `<span class="facepile">${list.slice(0, 3).map((n) => `<span class="av" title="${esc(n)}">${esc(initials(n))}</span>`).join("")}<span class="names">${esc(names)}</span></span>`;
}

const sourceLink = (s) => (s && s.url && /^https?:/.test(s.url))
  ? `<a class="src" href="${esc(s.url)}" target="_blank" rel="noreferrer">${esc(s.label || "Open")}</a>` : "";

function statusLine(state) {
  const src = state.sources || {};
  const label = { gmail: "Gmail", atlassian: "Jira", drive: "Drive", calendar: "Calendar", slack: "Slack", bitbucket: "Bitbucket" };
  const parts = Object.entries(src).map(([k, v]) => {
    if (k === "calendar" && v.unavailable) return "Calendar: not connected";
    if (k === "slack" && !v.ok) return state.extension_online ? "Slack: sign in to Slack in this browser" : "Slack: waiting for the browser";
    return `${label[k] || k} ${v.ok ? "✓" : "✕"}`;
  });
  const when = state.last_sense ? new Date(state.last_sense * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "never";
  const busy = state.activity?.length ? ` · ${state.activity.join(", ")}…` : "";
  return `Updated ${when} · ${parts.join(" · ")}${busy}`;
}

function renderPush(b) {
  const push = b.push_forward || [];
  $("push-card").hidden = !push.length;
  $("push").innerHTML = push.map((it, i) => `
    <div class="item" data-i="${i}">
      <h3>${esc(it.headline)}</h3>
      <p>${esc(it.body)}</p>
    </div>`).join("");
  $("push").querySelectorAll(".item h3").forEach((h) => {
    h.onclick = () => ask(push[Number(h.parentElement.dataset.i)].prompt || h.textContent);
  });
  $("sticker").onclick = () => ask(push[0]?.prompt || push[0]?.headline);
}

function renderTodos(b) {
  const todos = b.todos || [];
  $("todo-row").hidden = !todos.length;
  const draw = () => {
    const done = todos.filter((t) => t.done).length;
    $("todo-progress").textContent = `${done} / ${todos.length} completed`;
    $("todos").innerHTML = todos.map((t) => `
      <li class="${t.done ? "done" : ""}" data-id="${esc(t.id)}">
        <input type="checkbox" ${t.done ? "checked" : ""} aria-label="Mark done">
        <div>
          <div class="t">${esc(t.text)}</div>
          ${t.detail ? `<div class="d">${esc(t.detail)}</div>` : ""}
          <div class="acts">
            ${t.prompt ? `<button class="ask-btn"><span>Ask Daybreak</span><span class="arr">→</span></button>` : ""}
            ${sourceLink(t.source)}
          </div>
        </div>
      </li>`).join("") + (todos.length && done === todos.length ? `<li class="all-done">You completed all your to-dos!</li>` : "");
    $("todos").querySelectorAll("li[data-id]").forEach((li) => {
      const t = todos.find((x) => x.id === li.dataset.id);
      li.querySelector("input").onchange = async (e) => {
        t.done = e.target.checked ? 1 : 0;
        draw();
        await post(`/api/todos/${t.id}`, { done: !!t.done }).catch(() => {});
      };
      const btn = li.querySelector(".ask-btn");
      if (btn) btn.onclick = () => ask(t.prompt);
    });
  };
  draw();
}

function renderUpdates(b) {
  const ups = b.updates || [];
  $("updates-row").hidden = !ups.length;
  $("updates").innerHTML = ups.map((u, i) => `
    <li class="update">
      <span class="num">${String(i + 1).padStart(2, "0")}</span>
      <div>
        <div class="u-head"><h3>${esc(u.title || u.workstream)}</h3>${u.tag ? `<span class="tag ${slugify(u.tag)}">${esc(u.tag)}</span>` : ""}</div>
        <p>${esc(u.text || u.summary)}</p>
        <div class="u-foot">${sourceLink(u.source)}${facepile(u.people)}</div>
      </div>
    </li>`).join("");
}

function renderDay(b, meetings) {
  const sched = b.schedule || [];
  const live = Object.fromEntries((meetings || []).map((m) => [m.id, m]));
  const yd = b.your_day && !Array.isArray(b.your_day) ? b.your_day : {};
  const now = Date.now() / 1000;
  if (!sched.length && b.calendar_connected === false) {
    $("schedule").innerHTML = `<li style="grid-template-columns:1fr"><p class="hint">Connect Google Calendar to your Claude account
      (claude.ai → Settings → Connectors) to see your day, get prep notes, and pop-ups before meetings.</p></li>`;
  } else if (!sched.length) {
    $("schedule").innerHTML = `<li style="grid-template-columns:1fr"><p class="hint">No meetings today.</p></li>`;
  } else {
    const next = sched.find((m) => m.end > now);
    $("schedule").innerHTML = sched.map((m) => `
      <li class="${m.end < now ? "past" : ""} ${next && m.id === next.id ? "next" : ""}">
        <span class="time">${esc(m.time)}</span>
        <span class="ev" title="${esc(yd.notes?.[m.id] || "")}">${esc(m.title)}</span>
        ${m.join_url && m.end > now ? `<button class="join" data-url="${esc(m.join_url)}">Join</button>` : "<span></span>"}
      </li>`).join("");
    $("schedule").querySelectorAll(".join").forEach((j) => (j.onclick = () => chrome.tabs.create({ url: j.dataset.url })));
  }
  $("day-summary").textContent = yd.summary || (sched.length ? "" : "A clear calendar — good time for focused work.");
  const prepId = yd.prep_meeting_id && live[yd.prep_meeting_id] ? yd.prep_meeting_id : "";
  const prepMeeting = sched.find((m) => m.id === prepId);
  $("day-people").innerHTML = prepMeeting ? facepile(prepMeeting.attendees) : "";
  const btn = $("prep-me");
  btn.hidden = !prepId;
  if (prepId) {
    const m = live[prepId];
    btn.onclick = async () => {
      if (m.prep_url) return chrome.tabs.create({ url: m.prep_url });
      btn.innerHTML = `<span class="star">✦</span> Preparing…`;
      btn.disabled = true;
      const r = await post(`/api/meetings/${encodeURIComponent(prepId)}/prep`).catch(() => ({}));
      btn.disabled = false;
      btn.innerHTML = `<span class="star">✦</span> ${r.url ? "Open prep" : "Prep failed — retry"}`;
      if (r.url) { m.prep_url = r.url; chrome.tabs.create({ url: r.url }); }
    };
  }
}

function renderAhead(b) {
  const la = b.looking_ahead && !Array.isArray(b.looking_ahead) ? b.looking_ahead : null;
  $("ahead-row").hidden = !la;
  if (!la) return;
  $("ahead-intro").textContent = la.intro || "";
  const opts = la.options || [];
  const chosen = b.focus_choice?.label;
  const save = async (option) => {
    await post("/api/brief/focus", { day: b.day, option }).catch(() => {});
    $("ahead-saved").hidden = false;
  };
  $("ahead-options").innerHTML = opts.map((o, i) => `
    <li data-i="${i}" class="${chosen === o.label ? "chosen" : ""}"><span class="radio"></span>
      <div><div class="t">${esc(o.label)}</div>${o.detail ? `<div class="d">${esc(o.detail)}</div>` : ""}</div></li>`).join("");
  $("ahead-saved").hidden = !chosen;
  $("ahead-options").querySelectorAll("li").forEach((li) => (li.onclick = () => {
    $("ahead-options").querySelectorAll("li").forEach((x) => x.classList.remove("chosen"));
    li.classList.add("chosen");
    save(opts[Number(li.dataset.i)]);
  }));
  $("ahead-custom").onkeydown = (e) => {
    if (e.key === "Enter" && e.target.value.trim()) {
      $("ahead-options").querySelectorAll("li").forEach((x) => x.classList.remove("chosen"));
      save({ label: e.target.value.trim(), detail: "", prompt: e.target.value.trim() });
    }
  };
}

async function renderBrief(b, state, meetings) {
  $("empty").hidden = true;
  $("brief").hidden = false;
  document.title = b.title || "New Tab";
  $("title-day").textContent = (b.title || "The Brief").replace(/^The\s+/, "");
  $("caption").textContent = b.caption || "";

  const p = b.painting || {};
  if (p.file) {
    $("credit").textContent = [p.title, p.artist, p.date, p.medium].filter(Boolean).join(", ");
    $("credit").href = p.source || "#";
    try {
      const img = $("painting");
      img.onload = () => img.classList.add("loaded");
      img.src = await blobUrl(`/api/painting/${encodeURIComponent(p.file)}`);
    } catch { document.querySelector(".painting").classList.add("no-image"); }
  } else {
    document.querySelector(".painting").classList.add("no-image");
  }

  renderPush(b);
  renderTodos(b);
  renderUpdates(b);
  renderDay(b, meetings);
  renderAhead(b);
  $("status").textContent = statusLine(state);
}

async function load(day) {
  tickClock();
  let b;
  try {
    b = await api(day ? `/api/brief?day=${day}` : "/api/brief");
  } catch (e) {
    if (e.offline) {
      return showEmpty("Daybreak is asleep",
        "The helper on this Mac isn't running.\nStart it with <code>daybreak run</code>, or have it start at login with <code>daybreak install</code>.",
        [{ label: "Try again", onClick: () => load() }]);
    }
    return showEmpty("Let's connect Daybreak", esc(e.message), [
      { label: "Open settings", onClick: () => chrome.runtime.openOptionsPage() },
      { label: "Try again", secondary: true, onClick: () => load() }]);
  }
  if (b.missing) {
    const st = b.state || {};
    const busy = (st.activity || []).length;
    clearTimeout(pollTimer);
    if (busy) pollTimer = setTimeout(() => load(), 5000);
    const err = st.brief_error && !busy ? `\n\nLast attempt failed: ${esc(st.brief_error.error)}` : "";
    return showEmpty(busy ? "Writing your brief…" : "Your brief isn't ready yet",
      busy ? `${esc(st.activity.join(", "))}. This takes a minute or two.` :
        `Daybreak writes it every weekday morning, ready by ${esc(st.brief_time || "your brief time")}.${err}`,
      busy ? [] : [{ label: "Write today's brief now", onClick: async () => { await post("/api/brief/generate"); setTimeout(() => load(), 1500); } },
                   ...(b.last_day ? [{ label: "Show last brief", secondary: true, onClick: () => load(b.last_day) }] : [])]);
  }
  const [state, meetings] = await Promise.all([api("/api/state"), api("/api/meetings?hours=24").catch(() => [])]);
  await renderBrief(b, state, meetings);
}

$("refresh").onclick = async () => {
  await post("/api/brief/generate").catch(() => {});
  showEmpty("Writing your brief…", "Reading your sources and writing a fresh brief. This takes a minute or two.");
  setTimeout(() => load(), 3000);
};
$("open-chat").onclick = () => askInPanel("");
$("settings").onclick = () => chrome.runtime.openOptionsPage();
$("logo").onclick = () => askInPanel("");

// The service worker hands us pop-ups while the brief is the active tab.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "daybreak-toast" && document.visibilityState === "visible") {
    renderToast(msg.nudge);
    sendResponse(true);
  }
});

setInterval(tickClock, 15_000);
load();
