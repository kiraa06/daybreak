// Reads Slack through the Slack web session already signed in to this browser — no Slack app to install:
//   1. sessions: `localConfig_v2` from app.slack.com's localStorage holds every workspace you're signed in to, each with
//      its xoxc token. We use the workspaces named in settings, or all of them (up to four). If no Slack tab has it yet,
//      app.slack.com/client/ is loaded in a background tab so Slack's own client writes it (your `d` session cookie does
//      the sign-in), then the tab is closed.
//   2. calls: multipart POSTs to https://<workspace>.slack.com/api/<method> with the token, like the web client —
//      activity.feed (mentions inbox), client.counts (DMs with new messages), search.modules.messages (your own
//      messages), conversations.history / conversations.replies, and edgeapi.slack.com/cache for names.
// Read-only methods only. Tokens stay in this browser (session storage); only normalized conversations go to the
// local helper.

const READ_ONLY = new Set([
  "auth.test", "activity.feed", "activity.mentions", "client.counts", "users.conversations", "conversations.history",
  "conversations.replies", "conversations.info", "search.modules.messages", "search.messages", "users.info",
]);
const FEED_TYPES = "internal_channel_invite,at_user,at_user_group,at_channel,at_everyone,keyword,external_channel_invite,shared_workspace_invite,external_dm_invite";
const AUTH_ERRORS = ["invalid_auth", "not_authed", "token_revoked", "account_inactive"];
const MAX_WORKSPACES = 4;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// --- sessions -------------------------------------------------------------------

async function readLocalConfig(tabId) {
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId }, func: () => localStorage.getItem("localConfig_v2"),
  });
  if (!result) return null;
  const cfg = JSON.parse(result);
  return cfg?.teams && Object.keys(cfg.teams).length ? cfg : null;
}

async function localConfig() {
  for (const t of await chrome.tabs.query({ url: "https://app.slack.com/*" })) {
    const c = await readLocalConfig(t.id).catch(() => null);
    if (c) return c;
  }
  // Let Slack's own web client boot once so it writes localConfig_v2.
  const tab = await chrome.tabs.create({ url: "https://app.slack.com/client/", active: false });
  try {
    for (let i = 0; i < 40; i++) {
      await sleep(1000);
      const t = await chrome.tabs.get(tab.id).catch(() => null);
      if (!t) return null;
      if (t.url && /slack\.com\/(signin|workspace-signin|get-started|ssb\/signin)/.test(t.url)) return null;
      const c = await readLocalConfig(tab.id).catch(() => null);
      if (c) return c;
    }
    return null;
  } finally {
    chrome.tabs.remove(tab.id).catch(() => {});
  }
}

const host = (w) => String(w || "").trim().replace(/^https?:\/\//, "").replace(/\/.*$/, "").toLowerCase();

function wantedTeams(cfg, workspace) {
  const all = Object.values(cfg.teams || {}).filter((t) => t.token);
  const wants = String(workspace || "").split(",").map(host).filter(Boolean);
  const picked = wants.length
    ? all.filter((t) => wants.some((w) => host(t.url) === w || `${t.domain}.slack.com` === w || (t.name || "").toLowerCase() === w))
    : all;
  picked.sort((a, b) => (b.id === cfg.lastActiveTeamId) - (a.id === cfg.lastActiveTeamId));
  return picked.slice(0, MAX_WORKSPACES);
}

const toSession = (team) => ({
  token: team.token, url: (team.url || `https://${team.domain}.slack.com/`).replace(/\/$/, ""),
  teamId: team.id, enterpriseId: team.enterprise_id || null, name: team.name || team.domain || "",
});

async function sessions(workspace, { fresh = false } = {}) {
  const key = String(workspace || "");
  if (!fresh) {
    const { slackSessions } = await chrome.storage.session.get("slackSessions");
    if (slackSessions?.workspace === key && slackSessions.list?.length) return slackSessions.list;
  }
  const cfg = await localConfig();
  if (!cfg) return [];
  const list = wantedTeams(cfg, workspace).map(toSession);
  await chrome.storage.session.set({ slackSessions: { workspace: key, list } });
  return list;
}

// --- API ------------------------------------------------------------------------

function client(s) {
  const call = async (method, args = {}, reason = "") => {
    if (!READ_ONLY.has(method)) throw new Error(`blocked Slack method ${method}`);
    const form = new FormData();
    form.append("token", s.token);
    for (const [k, v] of Object.entries(args)) if (v !== undefined && v !== null) form.append(k, String(v));
    const q = reason ? `?_x_reason=${encodeURIComponent(reason)}&slack_route=${encodeURIComponent(s.teamId)}` : "";
    const r = await fetch(`${s.url}/api/${method}${q}`, { method: "POST", body: form, credentials: "include" });
    if (r.status === 429) throw new Error("Slack rate limit exceeded");
    return r.json();
  };
  const edge = async (path, body) => {
    const id = s.enterpriseId || s.teamId;
    const r = await fetch(`https://edgeapi.slack.com/cache/${id}/${path}`, {
      method: "POST", credentials: "include", headers: { "Content-Type": "text/plain;charset=UTF-8" },
      body: JSON.stringify({ token: s.token, ...body }),
    });
    return r.ok ? r.json() : null;
  };
  return { call, edge };
}

async function resolveNames({ call, edge }, userIds, channelIds) {
  const users = new Map();
  const channels = new Map();
  const uids = [...userIds].filter(Boolean).slice(0, 80);
  if (uids.length) {
    const res = await edge("users/info", { check_interaction: true, include_profile_only_users: true,
      updated_ids: Object.fromEntries(uids.map((u) => [u, 0])) }).catch(() => null);
    for (const u of res?.results || []) {
      users.set(u.id, u.profile?.display_name || u.profile?.real_name || u.real_name || u.name || u.id);
    }
    for (const u of uids.filter((x) => !users.has(x)).slice(0, 25)) {  // fallback for anything the edge cache missed
      const r = await call("users.info", { user: u }).catch(() => null);
      users.set(u, r?.user?.profile?.display_name || r?.user?.real_name || r?.user?.name || u);
    }
  }
  const cids = [...channelIds].filter((c) => c && !c.startsWith("D")).slice(0, 40);
  if (cids.length) {
    const res = await edge("channels/info", { check_membership: true,
      updated_channels: Object.fromEntries(cids.map((c) => [c, 0])) }).catch(() => null);
    for (const c of res?.results || []) channels.set(c.id, c.name || c.id);
    for (const c of cids.filter((x) => !channels.has(x)).slice(0, 15)) {
      const r = await call("conversations.info", { channel: c }).catch(() => null);
      if (r?.channel) channels.set(c, r.channel.is_mpim ? "group DM" : r.channel.name || c);
    }
  }
  return { users, channels };
}

const MENTION = /<@([A-Z0-9]+)(?:\|[^>]*)?>/g;
const CHANNEL = /<#[A-Z0-9]+(?:\|([^>]*))?>/g;
const LINK = /<(https?:\/\/[^>|]+)(?:\|([^>]*))?>/g;

function cleanText(text, users) {
  return String(text || "")
    .replace(MENTION, (_m, id) => "@" + (users.get(id) || id))
    .replace(CHANNEL, (_m, name) => "#" + (name || "channel"))
    .replace(LINK, (_m, url, label) => (label ? `${label} (${url})` : url))
    .slice(0, 700);
}

// --- one workspace ---------------------------------------------------------------

async function fetchWorkspace(s, oldest) {
  const api = client(s);
  const auth = await api.call("auth.test");
  if (!auth.ok) return { name: s.name, workspace: s.url, error: `auth.test: ${auth.error}`, authError: AUTH_ERRORS.includes(auth.error) };
  const me = auth.user_id;

  // Conversations worth reading: keyed by channel + thread.
  const wanted = new Map();
  const want = (channel, threadTs, reason) => {
    if (!channel) return;
    const key = `${channel}:${threadTs || ""}`;
    if (!wanted.has(key)) wanted.set(key, { channel, threadTs: threadTs || null, reasons: new Set() });
    wanted.get(key).reasons.add(reason);
  };

  const [feed, counts, mine] = await Promise.all([
    api.call("activity.feed", { types: FEED_TYPES, mode: "priority_reads_and_unreads_v1", limit: 50,
      is_activity_inbox: false }, "fetchActivityFeed").catch(() => null),
    api.call("client.counts", { thread_counts_by_channel: true, include_file_channels: true },
      "fetchClientCountsOnConnect").catch(() => null),
    api.call("search.modules.messages", { query: `from:<@${me}>`, module: "messages", count: 30, sort: "timestamp",
      sort_dir: "desc", extra_message_data: true, search_exclude_bots: true }, "search-messages").catch(() => null),
  ]);

  const diag = { feed: feed?.ok ? (feed.items || []).length : feed?.error || "failed",
                 counts: counts?.ok ? (counts.ims || []).length + (counts.mpims || []).length : counts?.error || "failed",
                 mine: mine?.ok ? (mine.items || []).length : mine?.error || "failed" };

  // 1) Mentions / keywords from the activity inbox (fallback: activity.mentions).
  let feedItems = feed?.ok ? feed.items || [] : [];
  if (!feed?.ok) {
    const m = await api.call("activity.mentions", { types: "at,channel,everyone,invite,user_group,word", count: 40 },
      "fetchActivityMentions").catch(() => null);
    feedItems = (m?.mentions || []).map((x) => ({ feed_ts: x.message?.ts, item: { type: x.type, message: x.message } }));
  }
  for (const it of feedItems) {
    const msg = it.item?.message || it.message || {};
    const ts = Number(msg.ts || it.feed_ts || 0);
    if (ts < oldest) continue;
    want(msg.channel || msg.channel_id, msg.thread_ts || msg.ts, it.item?.type || "mention");
  }

  // 2) DMs and group DMs with new messages since the cursor.
  for (const im of [...(counts?.ims || []), ...(counts?.mpims || [])]) {
    if (Number(im.latest || 0) >= oldest) want(im.id, null, "dm");
  }

  // 3) Threads I wrote in (my commitments and replies).
  const myMsgs = [];
  for (const group of mine?.items || []) {
    for (const m of group.messages || [group]) if (m?.ts) myMsgs.push({ ...m, channel: m.channel?.id || group.channel?.id || m.channel });
  }
  if (!mine?.ok) {  // fallback to the public search endpoint
    const day = new Date((oldest - 86400) * 1000).toISOString().slice(0, 10);
    const r = await api.call("search.messages", { query: `from:<@${me}> after:${day}`, count: 30, sort: "timestamp" }).catch(() => null);
    for (const m of r?.messages?.matches || []) myMsgs.push({ ...m, channel: m.channel?.id });
  }
  for (const m of myMsgs) {
    if (Number(m.ts) < oldest) continue;
    const thread = (m.permalink || "").match(/thread_ts=([\d.]+)/)?.[1] || m.thread_ts;
    want(typeof m.channel === "string" ? m.channel : m.channel?.id, thread, "mine");
  }

  // Read the most relevant conversations (bounded).
  const picks = [...wanted.values()].slice(0, 16);
  const convos = await Promise.all(picks.map(async (c) => {
    const r = c.threadTs
      ? await api.call("conversations.replies", { channel: c.channel, ts: c.threadTs, limit: 20 }, "history-api/fetchReplies").catch(() => null)
      : await api.call("conversations.history", { channel: c.channel, oldest, limit: 15 }, "history-api/fetchHistory").catch(() => null);
    const msgs = (r?.messages || []).filter((m) => !m.bot_id && m.subtype !== "bot_message" && m.subtype !== "channel_join");
    return { ...c, msgs: c.threadTs ? msgs.slice(-15) : msgs.reverse().slice(-15) };
  }));

  const userIds = new Set();
  const channelIds = new Set();
  for (const c of convos) {
    channelIds.add(c.channel);
    for (const m of c.msgs) {
      userIds.add(m.user);
      for (const x of String(m.text || "").matchAll(MENTION)) userIds.add(x[1]);
    }
  }
  const names = await resolveNames(api, userIds, channelIds);

  return {
    name: s.name, workspace: s.url, me: auth.user, diag, considered: wanted.size,
    conversations: convos.filter((c) => c.msgs.length).map((c) => {
      const isDm = c.channel.startsWith("D");
      const other = isDm ? names.users.get(c.msgs.find((m) => m.user !== me)?.user) : null;
      return {
        ref: `slack:${s.teamId}:${c.channel}:${c.threadTs || "channel"}`,
        workspace: s.name,
        channel: isDm ? `DM with ${other || "someone"}` : c.channel.startsWith("G") && !names.channels.has(c.channel)
          ? "group DM" : `#${names.channels.get(c.channel) || c.channel}`,
        why: [...c.reasons].join(", "),
        url: `${s.url}/archives/${c.channel}${c.threadTs ? "/p" + c.threadTs.replace(".", "") : ""}`,
        messages: c.msgs.map((m, i) => ({
          n: i + 1, user: m.user === me ? `${auth.user} (me)` : names.users.get(m.user) || m.user || "unknown",
          text: cleanText(m.text, names.users), at: new Date(Number(m.ts) * 1000).toISOString(),
        })),
      };
    }),
  };
}

// --- all workspaces ---------------------------------------------------------------

export async function slackFetch(params) {
  const fallback = Number(params.oldest) || Date.now() / 1000 - 12 * 3600;
  // Each workspace has its own "read up to" point; a workspace seen for the first time looks back further.
  const since = (s) => Number(params.oldest_by_workspace?.[s.url]) || Number(params.oldest_new) || fallback;
  const run = async (list) => Promise.all(list.map((s) => fetchWorkspace(s, since(s))
    .catch((e) => ({ name: s.name, workspace: s.url, error: String(e?.message || e) }))));

  let list = await sessions(params.workspace);
  if (!list.length) return { error: "Slack session not found — sign in to Slack once in this browser profile (app.slack.com)." };
  let results = await run(list);
  if (results.some((r) => r.authError)) {  // a token rotated: re-read localConfig_v2 once
    list = await sessions(params.workspace, { fresh: true });
    results = await run(list);
  }
  const ok = results.filter((r) => !r.error);
  const workspaces = results.map((r) => ({ name: r.name, url: r.workspace, ok: !r.error, error: r.error || "",
                                           conversations: (r.conversations || []).length, considered: r.considered || 0,
                                           diag: r.diag || null }));
  if (!ok.length) return { error: results.map((r) => `${r.name}: ${r.error}`).join("; "), workspaces };
  return {
    me: [...new Set(ok.map((r) => r.me))].join(", "),
    workspace: ok.map((r) => r.workspace).join(", "),
    workspaces,
    conversations: ok.flatMap((r) => r.conversations),
  };
}
