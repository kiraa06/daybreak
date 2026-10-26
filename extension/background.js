// Daybreak service worker: polls the helper for pop-ups and browser jobs, shows toasts, wires up the side panel.
import { api, post, askInPanel } from "./api.js";
import { slackFetch } from "./slack.js";
import { renderToast } from "./toast.js";

const TICK = "daybreak-tick";
let lastHello = 0;

async function setup() {
  chrome.alarms.create(TICK, { periodInMinutes: 0.5 });
  if (chrome.sidePanel?.setPanelBehavior) {
    chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
  }
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: "ask-page", title: "Ask Daybreak about this page", contexts: ["page"] });
    chrome.contextMenus.create({ id: "ask-selection", title: "Ask Daybreak about “%s”", contexts: ["selection"] });
  });
}

chrome.runtime.onInstalled.addListener(setup);
chrome.runtime.onStartup.addListener(setup);
// Some browsers skip onStartup for already-installed extensions; make sure the poll timer exists either way.
chrome.alarms.get(TICK).then((a) => a || chrome.alarms.create(TICK, { periodInMinutes: 0.5 }));

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === TICK) await tick();
});

async function tick() {
  try {
    if (Date.now() - lastHello > 4 * 60_000) {
      await post("/api/hello", { version: chrome.runtime.getManifest().version, sidePanel: !!chrome.sidePanel?.open });
      lastHello = Date.now();
    }
    const nudges = await api("/api/nudges");
    for (const n of nudges) await showNudge(n);
    const job = await post("/api/bridge/next");
    if (job && job.id) runJob(job);
    chrome.action.setBadgeText({ text: "" });
  } catch (e) {
    if (e.offline) chrome.action.setBadgeText({ text: "off" });
    chrome.action.setBadgeBackgroundColor({ color: "#5a5248" });
  }
}

async function runJob(job) {
  let result;
  try {
    if (job.kind === "slack.fetch") result = await slackFetch(job.params);
    else result = { error: `unknown job ${job.kind}` };
  } catch (e) {
    result = { error: String(e?.message || e) };
  }
  await post("/api/bridge/result", { id: job.id, result }).catch(() => {});
}

// --- The Morning Brief tab ------------------------------------------------------
// Once the brief is ready, it opens as its own tab the first time you use the browser that morning.

async function maybeOpenBrief({ force = false } = {}) {
  try {
    const st = await api("/api/state");
    if (!st.brief_ready) return false;
    const { briefOpened } = await chrome.storage.local.get("briefOpened");
    if (briefOpened === st.day) return false;
    const h = new Date().getHours();
    if (!force && (h < 5 || h >= 13)) return false;
    await chrome.storage.local.set({ briefOpened: st.day });
    await chrome.tabs.create({ url: chrome.runtime.getURL("newtab.html") });
    return true;
  } catch {
    return false;
  }
}

chrome.windows.onFocusChanged.addListener((wid) => {
  if (wid !== chrome.windows.WINDOW_ID_NONE) maybeOpenBrief();
});

chrome.commands.onCommand.addListener((cmd) => {
  if (cmd === "open-brief") chrome.tabs.create({ url: chrome.runtime.getURL("newtab.html") });
});

// --- pop-ups ---------------------------------------------------------------

async function showNudge(n) {
  if (n.kind === "brief" && (await maybeOpenBrief({ force: true }))) return;
  const isWeb = (t) => t && /^https?:/.test(t.url || "") && !/chromewebstore|chrome\.google\.com\/webstore/.test(t.url);
  let [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!isWeb(tab) && !(tab?.url || "").startsWith(chrome.runtime.getURL(""))) {
    // Focus may sit on a window without a web page; use the most recently used visible web tab instead.
    const actives = (await chrome.tabs.query({ active: true })).filter(isWeb);
    tab = actives.sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0))[0] || tab;
  }
  const injectable = isWeb(tab);
  if (injectable) {
    try {
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: renderToast, args: [n] });
      return;
    } catch (e) {
      console.warn("toast injection failed; using a system notification", e);
    }
  }
  // New tab page (our brief) listens for this and renders the same toast itself.
  const delivered = await chrome.runtime.sendMessage({ type: "daybreak-toast", nudge: n }).catch(() => false);
  if (delivered) return;
  const buttons = [];
  if (n.kind === "meeting" && n.payload.join_url) buttons.push({ title: "Join" });
  if (n.kind === "meeting" && n.payload.prep_url) buttons.push({ title: "Prep notes" });
  chrome.notifications.create(`nudge:${n.id}`, {
    type: "basic", iconUrl: "icons/icon128.png", priority: 2,
    title: n.kind === "meeting" ? `${n.title} ${n.body}` : n.title,
    message: n.kind === "meeting" ? (n.payload.attendees || []).slice(0, 4).join(", ") : n.body || "",
    buttons,
  });
  await chrome.storage.session.set({ [`nudge:${n.id}`]: n });
}

chrome.notifications.onClicked.addListener(async (nid) => {
  const n = (await chrome.storage.session.get(nid))[nid];
  if (!n) return;
  const p = n.payload || {};
  if (n.kind === "brief") chrome.tabs.create({});
  else if (p.prep_url || p.url) chrome.tabs.create({ url: p.prep_url || p.url });
  else if (p.prompt) askInPanel(p.prompt);
  chrome.notifications.clear(nid);
});

chrome.notifications.onButtonClicked.addListener(async (nid, idx) => {
  const n = (await chrome.storage.session.get(nid))[nid];
  if (!n) return;
  const p = n.payload || {};
  const order = [p.join_url ? "join" : null, p.prep_url ? "prep" : null].filter(Boolean);
  const action = order[idx];
  if (action === "join") chrome.tabs.create({ url: p.join_url });
  if (action === "prep") chrome.tabs.create({ url: p.prep_url });
  chrome.notifications.clear(nid);
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.type !== "daybreak-nudge-action") return;
  const { id, action, url, prompt } = msg;
  (async () => {
    if (action === "dismiss") await post(`/api/nudges/${id}`, { action: "dismiss" }).catch(() => {});
    if (action === "open" && url) await chrome.tabs.create({ url });
    if (action === "brief") await chrome.tabs.create({});
    if (action === "ask" && prompt) await askInPanel(prompt, { tabId: sender.tab?.id });
  })();
  sendResponse(true);
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  const prompt = info.menuItemId === "ask-selection"
    ? `About this selection from "${tab?.title || "this page"}": “${info.selectionText}” — explain it and tell me what matters for my work.`
    : "Summarize this page and tell me what matters for my work.";
  askInPanel(prompt, { tabId: tab?.id });
});
