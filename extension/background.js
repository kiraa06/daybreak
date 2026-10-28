// Daybreak service worker: polls the helper for pop-ups and browser jobs, shows toasts, wires up the side panel.
import { api, post, askInPanel } from "./api.js";
import { slackFetch } from "./slack.js";
import { renderToast } from "./toast.js";
import { BANNER_KINDS, browserInFront, clearBanner, listenForBannerClicks, showBanner } from "./banner.js";

const TICK = "daybreak-tick";
let lastHello = 0;

// `daybreak install` stamps build.json; when the stamp on disk changes, reload so the new code takes over.
async function diskBuild() {
  try {
    return (await (await fetch(chrome.runtime.getURL("build.json"), { cache: "no-store" })).json()).build;
  } catch {
    return null;  // running from a checkout without a stamp
  }
}
let loadedBuild = null;
diskBuild().then((b) => (loadedBuild = b));

async function setup() {
  chrome.alarms.create(TICK, { periodInMinutes: 0.5 });
  if (chrome.sidePanel?.setPanelBehavior) {
    chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
  }
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: "ask-page", title: "Ask Daybreak about this page", contexts: ["page"] });
    chrome.contextMenus.create({ id: "ask-selection", title: "Ask Daybreak about “%s”", contexts: ["selection"] });
    chrome.contextMenus.create({ id: "open-brief", title: "Open today's brief", contexts: ["action"] });
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
  const b = await diskBuild();
  if (loadedBuild && b && b !== loadedBuild) return chrome.runtime.reload();
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
  // In another app you'd never see a card inside a page, so meetings and "needs you" also go out as a system banner.
  // The card is still drawn, so it's waiting when you come back.
  const inFront = await browserInFront();
  const away = BANNER_KINDS.has(n.kind) && !inFront;
  const via = [];
  if (away) await showBanner(n).then(() => via.push("banner"), (e) => console.warn("banner failed", e));
  const card = await drawCard(n);
  if (card) via.push(card);
  else if (!away) await showBanner(n).then(() => via.push("banner"), (e) => console.warn("banner failed", e));
  post(`/api/nudges/${n.id}`, { action: "shown", via, in_front: inFront }).catch(() => {});  // for the helper's log
}

// Draws the pop-up card on the page you're on (or on the brief); says where, or null if there was nowhere to draw it.
async function drawCard(n) {
  const isWeb = (t) => t && /^https?:/.test(t.url || "") && !/chromewebstore|chrome\.google\.com\/webstore/.test(t.url);
  let [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!isWeb(tab) && !(tab?.url || "").startsWith(chrome.runtime.getURL(""))) {
    // Focus may sit on a window without a web page; use the most recently used visible web tab instead.
    const actives = (await chrome.tabs.query({ active: true })).filter(isWeb);
    tab = actives.sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0))[0] || tab;
  }
  if (isWeb(tab)) {
    try {
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: renderToast, args: [n] });
      return "page";
    } catch (e) {
      console.warn("toast injection failed; using a system notification", e);
    }
  }
  // New tab page (our brief) listens for this and renders the same toast itself.
  const delivered = await chrome.runtime.sendMessage({ type: "daybreak-toast", nudge: n }).catch(() => false);
  return delivered ? "brief" : null;
}

listenForBannerClicks();

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.type !== "daybreak-nudge-action") return;
  const { id, action, url, prompt } = msg;
  (async () => {
    clearBanner(`nudge:${id}`);  // acted on in the page, so the matching banner can go
    if (action === "dismiss") await post(`/api/nudges/${id}`, { action: "dismiss" }).catch(() => {});
    if (action === "open" && url) await chrome.tabs.create({ url });
    if (action === "brief") await chrome.tabs.create({});
    if (action === "ask" && prompt) await askInPanel(prompt, { tabId: sender.tab?.id });
  })();
  sendResponse(true);
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "open-brief") return chrome.tabs.create({ url: chrome.runtime.getURL("newtab.html") });
  const prompt = info.menuItemId === "ask-selection"
    ? `About this selection from "${tab?.title || "this page"}": “${info.selectionText}” — explain it and tell me what matters for my work.`
    : "Summarize this page and tell me what matters for my work.";
  askInPanel(prompt, { tabId: tab?.id });
});
