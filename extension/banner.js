// System notifications (macOS banners, Windows toasts, Linux notifications) for pop-ups. Used while you're in another
// app, where a card drawn inside a web page can't be seen, and when there's no page to draw the card on.
import { askInPanel } from "./api.js";

const LABEL = { join: "Join", prep: "Prep notes", open: "Open", ask: "Ask Daybreak" };

// Pop-ups worth reaching you in another app: meetings and the things someone is waiting on you for.
export const BANNER_KINDS = new Set(["meeting", "followup"]);

export async function browserInFront() {
  try {
    return (await chrome.windows.getLastFocused()).focused;
  } catch {
    return false;  // no browser windows open
  }
}

// The buttons a banner offers, in order (notifications allow two).
export function bannerActions(n) {
  const p = n.payload || {};
  if (n.kind === "meeting") return [p.join_url && "join", p.prep_url && "prep"].filter(Boolean);
  return [p.url && "open", p.prompt && "ask"].filter(Boolean);
}

export async function showBanner(n) {
  const p = n.payload || {};
  const id = `nudge:${n.id}`;
  const actions = bannerActions(n);
  await chrome.storage.session.set({ [id]: { ...n, actions } });
  if (n.kind === "meeting" && p.meeting_id) {
    // "Now" replaces the "in 5 min" banner for the same meeting instead of stacking up.
    const key = `meeting:${p.meeting_id}`;
    const prev = (await chrome.storage.session.get(key))[key];
    if (prev && prev !== id) clearBanner(prev);
    await chrome.storage.session.set({ [key]: id });
  }
  const people = (p.attendees || []).slice(0, 4).map((a) => String(a).replace(/@.*/, "")).join(", ");
  await chrome.notifications.create(id, {
    type: "basic", iconUrl: "icons/icon128.png", priority: 2,
    title: n.kind === "meeting" ? `${n.title} ${n.body}` : n.title,
    message: n.kind === "meeting" ? people : n.body || "",
    ...(n.kind === "followup" ? { contextMessage: "Needs you" } : {}),
    buttons: actions.map((a) => ({ title: LABEL[a] })),
  });
  return { id, actions };
}

export function clearBanner(id) {
  chrome.notifications.clear(id);
  chrome.storage.session.remove(id);
}

async function openInFront(url) {
  const tab = await chrome.tabs.create({ url });
  await chrome.windows.update(tab.windowId, { focused: true }).catch(() => {});
}

// What a click does. The banner's own body opens prep notes (never joins a call by accident) or the source.
export async function runBannerAction(n, action) {
  const p = n.payload || {};
  if (!action) {
    if (n.kind === "brief") action = "brief";
    else if (n.kind === "meeting") action = p.prep_url ? "prep" : null;
    else action = p.url ? "open" : p.prompt ? "ask" : null;
  }
  if (action === "join") await openInFront(p.join_url);
  if (action === "prep") await openInFront(p.prep_url);
  if (action === "open") await openInFront(p.url);
  if (action === "brief") await openInFront(chrome.runtime.getURL("newtab.html"));
  if (action === "ask") await askInPanel(p.prompt);
  return action;
}

export function listenForBannerClicks() {
  const handle = async (id, idx) => {
    const n = (await chrome.storage.session.get(id))[id];
    clearBanner(id);
    if (n) await runBannerAction(n, idx === undefined ? null : (n.actions || [])[idx]);
  };
  chrome.notifications.onClicked.addListener((id) => handle(id));
  chrome.notifications.onButtonClicked.addListener((id, idx) => handle(id, idx));
}
