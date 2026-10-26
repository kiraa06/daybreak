// Talks to the local Daybreak helper. Pairing (port + token) comes from the native messaging host,
// or from a pasted `daybreak pair` blob on the options page.

const HOST = "com.daybreak.helper";

export async function pair() {
  let resp;
  try {
    resp = await chrome.runtime.sendNativeMessage(HOST, { type: "pair" });
  } catch (e) {
    throw new Error("Can't reach the Daybreak helper's native host. Run `daybreak install`, or paste the output of `daybreak pair` in Daybreak's options.");
  }
  if (!resp || !resp.token) throw new Error(resp?.error || "The helper returned no pairing details.");
  await chrome.storage.local.set({ pairing: resp });
  return resp;
}

export async function getPairing() {
  const { pairing } = await chrome.storage.local.get("pairing");
  return pairing || pair();
}

export async function base() {
  const p = await getPairing();
  return `http://127.0.0.1:${p.port}`;
}

export async function api(path, opts = {}, retried = false) {
  const p = await getPairing();
  let res;
  try {
    res = await fetch(`http://127.0.0.1:${p.port}${path}`, {
      ...opts,
      headers: { "X-Daybreak-Token": p.token, "Content-Type": "application/json", ...(opts.headers || {}) },
    });
  } catch (e) {
    const err = new Error("The Daybreak helper isn't running.");
    err.offline = true;
    throw err;
  }
  if (res.status === 401 && !retried) {
    await chrome.storage.local.remove("pairing");
    await pair();
    return api(path, opts, true);
  }
  if (!res.ok) throw new Error(`${path}: ${res.status} ${await res.text()}`);
  return res.json();
}

export const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });

// Authenticated image → object URL (the painting is served behind the token).
export async function blobUrl(path) {
  const p = await getPairing();
  const res = await fetch(`http://127.0.0.1:${p.port}${path}`, { headers: { "X-Daybreak-Token": p.token } });
  if (!res.ok) throw new Error(`image ${res.status}`);
  return URL.createObjectURL(await res.blob());
}

// Server-sent events over POST (EventSource can't send headers or a body).
export async function stream(path, body, onEvent) {
  const p = await getPairing();
  const res = await fetch(`http://127.0.0.1:${p.port}${path}`, {
    method: "POST",
    headers: { "X-Daybreak-Token": p.token, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok || !res.body) throw new Error(`${path}: ${res.status}`);
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) !== -1) {
      const chunk = buf.slice(0, i);
      buf = buf.slice(i + 2);
      if (chunk.startsWith("data: ")) {
        try { onEvent(JSON.parse(chunk.slice(6))); } catch { /* ignore partial */ }
      }
    }
  }
}

// Ask the side panel to start a chat with `prompt` (used by the brief, pop-ups and context menu).
export async function askInPanel(prompt, { windowId, tabId } = {}) {
  await chrome.storage.local.set({ pendingPrompt: { prompt, at: Date.now() } });
  try {
    if (chrome.sidePanel?.open) {
      if (tabId) await chrome.sidePanel.open({ tabId });
      else await chrome.sidePanel.open({ windowId: windowId ?? (await chrome.windows.getLastFocused()).id });
      return;
    }
  } catch (e) {
    console.warn("sidePanel.open failed, falling back to a window", e);
  }
  await chrome.windows.create({ url: chrome.runtime.getURL("sidepanel.html?window=1"), type: "popup", width: 460, height: 760 });
}
