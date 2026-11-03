import { api, post, pair } from "./api.js";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function refresh() {
  try {
    const [st, settings, schedules] = await Promise.all([api("/api/state"), api("/api/settings"), api("/api/schedules")]);
    $("conn").innerHTML = `<span class="ok">Connected</span> to the helper as <strong>${esc(st.me)}</strong>. ` +
      `${st.workstreams} live workstreams. Config file: <code>${esc(settings.config_path)}</code>`;
    if (st.claude_auth && st.claude_auth.ok === false) {
      const since = new Date(st.claude_auth.since * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
      $("conn").innerHTML += `<br><br><span class="bad"><strong>Claude sign-in expired</strong> (since ${esc(since)}).</span> ` +
        `Daybreak can't read your sources until you sign in again. ` +
        esc(st.auth_fix || "Open Terminal and run `daybreak login`.").replace(/`([^`]+)`/g, "<code>$1</code>");
    }
    for (const k of ["name", "brief_time", "slack", "slack_workspace"]) $(k).value = settings[k] ?? "";
    const names = { gmail: "Gmail", atlassian: "Jira & Confluence", drive: "Google Drive", calendar: "Google Calendar", slack: "Slack", bitbucket: "Bitbucket" };
    $("sources").innerHTML = Object.entries(st.sources || {}).map(([k, v]) => {
      const note = k === "calendar" && v.unavailable ? "not connected — connect Google Calendar to your Claude account"
        : v.ok ? `ok · last read ${new Date(v.at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`
        : k === "slack" ? "couldn't read — sign in to Slack in this browser profile" : "failed on the last run";
      return `<li><strong>${names[k] || k}</strong>: <span class="${v.ok ? "ok" : "bad"}">${esc(note)}</span></li>`;
    }).join("") || "<li>Not read yet.</li>";
    const ws = st.slack_workspaces || [];
    if (ws.length) {
      $("sources").insertAdjacentHTML("beforeend", `<li><strong>Slack workspaces</strong>: ${ws.map((w) =>
        `${esc(w.name)} <span class="${w.ok ? "ok" : "bad"}">${w.ok ? "✓" : "✕ " + esc(w.error)}</span>`).join(" · ")}</li>`);
    }
    $("schedules").innerHTML = schedules.map((s) =>
      `<li><strong>${esc(s.name)}</strong> — ${esc(s.spec)} · next ${esc(s.next_run_text)} ${s.last_status ? "· " + esc(s.last_status) : ""}
       <button class="secondary" data-del="${esc(s.id)}" style="margin:0 0 0 8px;padding:3px 10px">Delete</button></li>`).join("") || "<li>None yet.</li>";
    $("schedules").querySelectorAll("[data-del]").forEach((b) => (b.onclick = async () => {
      await api(`/api/schedules/${b.dataset.del}`, { method: "DELETE" });
      refresh();
    }));
  } catch (e) {
    $("conn").innerHTML = `<span class="bad">${esc(e.offline ? "The helper isn't running. Start it with `daybreak run` (or `daybreak install`)." : e.message)}</span>`;
  }
}

// The model pickers list what the helper's Claude account offers: the family aliases (always the latest version)
// first, then specific versions. A value set by hand in config.toml that isn't on the list stays selectable.
async function loadModels() {
  const [models, settings] = await Promise.all([api("/api/models"), api("/api/settings")]);
  const fill = (select, current, blankLabel) => {
    const opt = (m) => `<option value="${esc(m.value)}">${esc(m.name)}${m.description ? " · " + esc(m.description) : ""}</option>`;
    const blank = models.find((m) => m.value === "");
    const named = models.filter((m) => m.value);
    const aliases = named.filter((m) => !m.value.startsWith("claude-"));
    const pinned = named.filter((m) => m.value.startsWith("claude-"));
    let html = `<option value="">${esc(blankLabel(blank))}</option>` + aliases.map(opt).join("");
    if (pinned.length) html += `<optgroup label="Specific versions">${pinned.map(opt).join("")}</optgroup>`;
    if (current && !named.some((m) => m.value === current)) html += `<option value="${esc(current)}">${esc(current)} · from config.toml</option>`;
    select.innerHTML = html;
    select.value = current || "";
  };
  fill($("model"), settings.model, (b) => `Claude Code default${b?.description ? " · " + b.description : ""}`);
  fill($("model_fast"), settings.model_fast, () => "Same as above");
}

$("save-model").onclick = async () => {
  const r = await post("/api/settings", { model: $("model").value, model_fast: $("model_fast").value });
  $("saved-model").textContent = r.ok === false ? r.error : "Saved. The next brief, prep and chat message use it.";
  setTimeout(() => ($("saved-model").textContent = ""), 4000);
};

$("repair").onclick = async () => {
  try { await chrome.storage.local.remove("pairing"); await pair(); } catch (e) { $("conn").innerHTML = `<span class="bad">${esc(e.message)}</span>`; return; }
  refresh();
};
$("save-manual").onclick = async () => {
  try {
    const p = JSON.parse($("manual").value);
    if (!p.token || !p.port) throw new Error("needs port and token");
    await chrome.storage.local.set({ pairing: p });
    $("manual").value = "";
    refresh();
  } catch (e) { $("conn").innerHTML = `<span class="bad">That doesn't look like \`daybreak pair\` output: ${esc(e.message)}</span>`; }
};
$("sense").onclick = async () => { await post("/api/sense"); $("conn").textContent = "Reading your sources in the background…"; };
$("save").onclick = async () => {
  await post("/api/settings", { name: $("name").value, brief_time: $("brief_time").value || "08:30", slack: $("slack").value,
                                slack_workspace: $("slack_workspace").value });
  $("saved").textContent = "Saved.";
  setTimeout(() => ($("saved").textContent = ""), 2000);
  refresh();
};

refresh();
loadModels().catch((e) => ($("model-note").textContent = `Couldn't list models: ${e.message}`));
