// Injected into the active page: a card in the bottom-right corner. Must be self-contained.
export function renderToast(n) {
  const HOST_ID = "daybreak-toast-host";
  let host = document.getElementById(HOST_ID);
  if (!host) {
    host = document.createElement("div");
    host.id = HOST_ID;
    host.style.cssText = "position:fixed;right:20px;bottom:20px;z-index:2147483647;";
    document.documentElement.appendChild(host);
    host.attachShadow({ mode: "open" }).innerHTML = `<style>
      .stack{display:flex;flex-direction:column;gap:10px;align-items:flex-end}
      .card{width:360px;background:#1b1a19;color:#f3efe6;border:1px solid #3a3733;border-radius:16px;padding:14px 16px 14px;
        box-shadow:0 18px 50px rgba(0,0,0,.45);font:14px/1.4 -apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif;
        animation:in .28s cubic-bezier(.2,.9,.3,1.2)}
      @keyframes in{from{transform:translateY(16px) scale(.98);opacity:0}to{transform:none;opacity:1}}
      .row{display:flex;align-items:center;gap:8px}
      .title{font-weight:650;font-size:16px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      .when{color:#9a958c;font-weight:500;margin-left:4px}
      .x{background:none;border:0;color:#9a958c;font-size:18px;cursor:pointer;padding:0 2px;line-height:1}
      .body{color:#cfc9bd;margin:6px 0 2px}
      .people{display:flex;align-items:center;gap:8px;margin:8px 0 2px;color:#b9b3a7;font-size:13px}
      .av{display:flex}.av span{width:22px;height:22px;border-radius:50%;background:#4a4540;color:#f3efe6;font-size:10px;font-weight:700;
        display:grid;place-items:center;margin-left:-6px;border:2px solid #1b1a19}.av span:first-child{margin-left:0}
      .names{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
      .actions{display:flex;gap:8px;margin-top:12px}
      button.b{border:0;border-radius:10px;padding:8px 14px;font:600 14px/1 inherit;cursor:pointer;background:#2c2a27;color:#f3efe6}
      button.b:hover{background:#3a3733}
      button.primary{background:#f7f3ea;color:#151413;margin-left:auto}
      button.primary:hover{background:#fff}
      .tag{font:600 10px/1 "SF Mono",Menlo,monospace;letter-spacing:.12em;text-transform:uppercase;color:#f6e04b;margin-bottom:6px}
    </style><div class="stack"></div>`;
  }
  const root = host.shadowRoot.querySelector(".stack");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const p = n.payload || {};
  const card = document.createElement("div");
  card.className = "card";
  const send = (action, extra = {}) => chrome.runtime.sendMessage({ type: "daybreak-nudge-action", id: n.id, action, ...extra });
  const close = () => { card.remove(); if (!root.children.length) host.remove(); };
  let html = "";
  if (n.kind === "meeting") {
    const people = p.attendees || [];
    const initials = people.slice(0, 3).map((a) => `<span>${esc(String(a).replace(/@.*/, "").split(/[ ._]/).map((w) => w[0] || "").join("").slice(0, 2).toUpperCase())}</span>`).join("");
    const names = people.slice(0, 3).map((a) => String(a).replace(/@.*/, "")).join(", ") + (people.length > 3 ? ` +${people.length - 3}` : "");
    html = `<div class="row"><div class="title">${esc(n.title)}<span class="when">${esc(n.body)}</span></div><button class="x" data-a="close">×</button></div>
      ${people.length ? `<div class="people"><div class="av">${initials}</div><div class="names">${esc(names)}</div></div>` : ""}
      <div class="actions"><button class="b" data-a="dismiss">Ignore</button>
      ${p.prep_url ? `<button class="b" data-a="prep">Prep notes</button>` : ""}
      ${p.join_url ? `<button class="b primary" data-a="join">Join</button>` : ""}</div>`;
  } else if (n.kind === "brief") {
    html = `<div class="tag">Daybreak</div><div class="row"><div class="title">${esc(n.title)} is ready</div><button class="x" data-a="close">×</button></div>
      <div class="body">${esc(n.body)}</div><div class="actions"><button class="b" data-a="dismiss">Later</button><button class="b primary" data-a="brief">Open brief</button></div>`;
  } else {
    html = `<div class="tag">${n.kind === "followup" ? "Needs you" : "Daybreak"}</div><div class="row"><div class="title">${esc(n.title)}</div><button class="x" data-a="close">×</button></div>
      <div class="body">${esc(n.body)}</div><div class="actions"><button class="b" data-a="dismiss">Ignore</button>
      ${p.url ? `<button class="b" data-a="open">Open</button>` : ""}
      ${p.prompt ? `<button class="b primary" data-a="ask">Ask Daybreak</button>` : ""}</div>`;
  }
  card.innerHTML = html;
  card.addEventListener("click", (e) => {
    const a = e.target?.dataset?.a;
    if (!a) return;
    if (a === "join") send("open", { url: p.join_url });
    if (a === "prep") send("open", { url: p.prep_url });
    if (a === "open") send("open", { url: p.url });
    if (a === "ask") send("ask", { prompt: p.prompt });
    if (a === "brief") send("brief");
    if (a === "dismiss") send("dismiss");
    close();
  });
  root.appendChild(card);
  const ttl = n.kind === "meeting" && n.body === "now" ? 180_000 : 60_000;
  setTimeout(close, ttl);
}

