/* Clicker4AI — mobile SPA. No dependencies. */
"use strict";

// ---------------------------------------------------------------- utilities

const $ = (sel, root) => (root || document).querySelector(sel);

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "html") node.innerHTML = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) node.setAttribute(k, v);
    }
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined) continue;
    node.append(c.nodeType ? c : document.createTextNode(c));
  }
  return node;
}

const esc = (s) =>
  String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

function relTime(ms) {
  if (!ms) return "";
  const d = Date.now() - ms;
  if (d < 60e3) return "now";
  if (d < 3600e3) return Math.floor(d / 60e3) + "m";
  if (d < 86400e3) return Math.floor(d / 3600e3) + "h";
  return Math.floor(d / 86400e3) + "d";
}

// rate-limit windows as the SDK names them
const USAGE_WINDOWS = {
  five_hour: "5h window",
  seven_day: "7 days",
  seven_day_opus: "7 days · opus",
  seven_day_sonnet: "7 days · sonnet",
  overage: "overage",
};

// resets_at is a unix timestamp (seconds, older payloads may send ms). Within
// a day the clock time is what you want to know; past that, add the weekday.
function fmtReset(ts) {
  if (!ts) return "";
  const d = new Date(ts < 1e12 ? ts * 1000 : ts);
  if (isNaN(d)) return "";
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const away = d - Date.now();
  if (away <= 0) return "now";
  if (away < 86400e3) return hm;
  return d.toLocaleDateString([], { weekday: "short" }) + " " + hm;
}

function fmtCost(v) {
  if (v === null || v === undefined) return "";
  return "$" + (v < 10 ? v.toFixed(2) : v.toFixed(0));
}

function fmtTokens(n) {
  if (n === null || n === undefined) return "";
  return n >= 1000 ? Math.round(n / 1000) + "k" : String(n);
}

// Minimal markdown renderer (escape-first, safe).
function md(src) {
  if (!src) return "";
  const codeBlocks = [];
  src = String(src).replace(/( *)```([^\n`]*)\n([\s\S]*?)[ \t]*```/g, (_, ind, lang, body) => {
    // a fence indented under a list item: its lines carry that indent too
    if (ind) body = body.replace(new RegExp(`^ {1,${ind.length}}`, "gm"), "");
    codeBlocks.push(`<pre><code>${esc(body.replace(/\n$/, ""))}</code></pre>`);
    return `${ind}\u0000B${codeBlocks.length - 1}\u0000`;
  });

  const inline = (text) => {
    const spans = [];
    text = text.replace(/`([^`]+)`/g, (_, c) => {
      spans.push(`<code>${esc(c)}</code>`);
      return `\u0000S${spans.length - 1}\u0000`;
    });
    text = esc(text)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener">$1</a>');
    return text.replace(/\u0000S(\d+)\u0000/g, (_, i) => spans[i]);
  };

  const lines = src.split("\n");
  const out = [];
  // open lists, outermost first: {type: "ul"|"ol", indent}; each has an
  // open <li>, so a deeper item nests inside it
  const lists = [];
  let para = [];
  const indentOf = (s) => s.match(/^[ \t]*/)[0].replace(/\t/g, "    ").length;
  const itemRe = /^([-*+]|\d+[.)])\s+(.*)$/;

  const flushPara = () => {
    if (para.length) { out.push(`<p>${inline(para.join("\n"))}</p>`); para = []; }
  };
  const closeTop = () => { out.push(`</li></${lists.pop().type}>`); };
  const flushList = () => { while (lists.length) closeTop(); };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const t = line.trim();
    const indent = indentOf(line);

    if (/^\u0000B\d+\u0000$/.test(t)) {
      flushPara();
      // an indented code block stays in its list item
      if (!(lists.length && indent > 0)) flushList();
      out.push(t); continue;
    }
    if (!t) {
      flushPara();
      // a blank line between items (or before an item's indented text)
      // keeps the list, so its numbers run on
      let j = i + 1;
      while (j < lines.length && !lines[j].trim()) j++;
      const next = lines[j] ?? "";
      if (lists.length && !(itemRe.test(next.trim()) || indentOf(next) > 0)) flushList();
      continue;
    }

    // table
    if (t.startsWith("|") && lines[i + 1] && /^\|?[\s:|-]+\|?$/.test(lines[i + 1].trim())
        && lines[i + 1].includes("-")) {
      flushPara(); flushList();
      const cells = (row) => row.replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
      let html = "<table><tr>" + cells(t).map((c) => `<th>${c}</th>`).join("") + "</tr>";
      i += 2;
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        html += "<tr>" + cells(lines[i].trim()).map((c) => `<td>${c}</td>`).join("") + "</tr>";
        i++;
      }
      i--;
      out.push(html + "</table>"); continue;
    }

    const h = t.match(/^(#{1,4})\s+(.*)$/);
    if (h) { flushPara(); flushList(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); continue; }
    if (/^(-{3,}|\*{3,})$/.test(t)) { flushPara(); flushList(); out.push("<hr>"); continue; }
    if (t.startsWith(">")) { flushPara(); flushList(); out.push(`<blockquote>${inline(t.replace(/^>\s?/, ""))}</blockquote>`); continue; }

    const item = t.match(itemRe);
    if (item) {
      flushPara();
      const type = /^\d/.test(item[1]) ? "ol" : "ul";
      while (lists.length && lists[lists.length - 1].indent > indent) closeTop();
      const top = lists[lists.length - 1];
      if (top && top.indent === indent && top.type !== type) closeTop();
      const same = lists[lists.length - 1];
      if (same && same.indent === indent) {
        out.push("</li>");
      } else {
        // a list that starts at 3 (or goes on after text) keeps its number
        const n = parseInt(item[1], 10);
        out.push(type === "ol" && n !== 1 ? `<ol start="${n}">` : `<${type}>`);
        lists.push({ type, indent });
      }
      out.push(`<li>${inline(item[2])}`);
      continue;
    }
    // text under an item: indented, or straight after it with no blank line
    const afterBlank = !lines[i - 1]?.trim();
    if (lists.length && (indent > 0 || !afterBlank)) {
      out.push(afterBlank ? `<p>${inline(t)}</p>` : `<br>${inline(t)}`);
      continue;
    }
    flushList();
    para.push(line);
  }
  flushPara(); flushList();
  return out.join("\n").replace(/\u0000B(\d+)\u0000/g, (_, i) => codeBlocks[i]);
}

// ---------------------------------------------------------------- state

const S = {
  authed: false,
  view: null,          // {name, ...params}
  sessions: [],
  tabs: [],            // sids open as tabs (insertion order)
  lastTab: null,       // sid of the most recently viewed tab
  events: {},          // sid -> [persisted events]
  seqs: {},            // sid -> last persisted seq
  meta: {},            // sid -> merged meta
  status: {},          // sid -> {state, detail}
  context: {},         // sid -> context usage
  btw: {},             // sid -> side questions (/btw), newest last
  subs: new Set(),
  wsUp: false,
  serverError: false,   // last ws close was 4503 (devices.json unreadable)
  defaults: { models: ["default", "opus", "sonnet", "haiku"], modes: ["default", "acceptEdits", "plan"] },
  home: "",
  library: null,
  libraryAt: 0,
  projects: null,
  projectsAt: 0,
  passkey: { available: false, has_credentials: false, device_passkey: false },
  assets: null,        // frontend stamp this page booted with (see checkAssets)
  locked: false,       // session asleep: the cookie is fine, a passkey wakes it
  passkeyGate: false,  // the device must register a passkey before anything else
  requirePasskey: false,
  elevatedUntil: 0,    // ms; a step-up confirmation is good until then
};

let _libPromise = null;
function loadLibrary() {
  if (S.library && Date.now() - S.libraryAt < 15000) return Promise.resolve(S.library);
  if (!_libPromise) {
    _libPromise = T.rpc("library")
      .then((lib) => { S.library = lib; S.libraryAt = Date.now(); return lib; })
      .finally(() => { _libPromise = null; });
  }
  return _libPromise;
}

let _projPromise = null;
function loadProjects() {
  if (S.projects && Date.now() - S.projectsAt < 30000) return Promise.resolve(S.projects);
  if (!_projPromise) {
    _projPromise = T.rpc("projects.recent")
      .then(({ projects }) => { S.projects = projects; S.projectsAt = Date.now(); return projects; })
      .finally(() => { _projPromise = null; });
  }
  return _projPromise;
}

const chatUI = { sid: null, byTool: {}, byReq: {}, provText: [], provThink: [], msgs: null };

// ---------------------------------------------------------------- tabs state

function loadTabs() {
  try { S.tabs = JSON.parse(localStorage.getItem("c4ai_tabs") || "[]"); } catch { S.tabs = []; }
  if (!Array.isArray(S.tabs)) S.tabs = [];
  S.lastTab = localStorage.getItem("c4ai_tab_active") || null;
}

function saveTabs() {
  localStorage.setItem("c4ai_tabs", JSON.stringify(S.tabs));
  if (S.lastTab) localStorage.setItem("c4ai_tab_active", S.lastTab);
  else localStorage.removeItem("c4ai_tab_active");
}

// drop tabs whose session no longer exists
function pruneTabs() {
  const live = new Set(S.sessions.map((s) => s.sid));
  const next = S.tabs.filter((sid) => live.has(sid));
  if (next.length !== S.tabs.length) { S.tabs = next; saveTabs(); }
}

// Unsent text per session, so switching tabs, a reload or iOS killing the
// app in the background loses nothing. Kept in localStorage — on this
// device only, erased on sign out — except an incognito chat's (or one not
// yet in the session list), which lives in memory only: nothing typed there
// stays on the device.
const DRAFTS_KEY = "c4ai_drafts";
const memDrafts = {};

function loadDrafts() {
  try {
    const d = JSON.parse(localStorage.getItem(DRAFTS_KEY) || "{}");
    return d && typeof d === "object" && !Array.isArray(d) ? d : {};
  } catch { return {}; }
}

function draftInMemory(sid) {
  return S.sessions.find((s) => s.sid === sid)?.incognito !== false;
}

function getDraft(sid) {
  return memDrafts[sid] ?? loadDrafts()[sid] ?? "";
}

function setDraft(sid, text) {
  const d = loadDrafts();
  delete memDrafts[sid];
  delete d[sid];
  if (text.trim()) {
    if (draftInMemory(sid)) memDrafts[sid] = text; else d[sid] = text;
  }
  try {
    if (Object.keys(d).length) localStorage.setItem(DRAFTS_KEY, JSON.stringify(d));
    else localStorage.removeItem(DRAFTS_KEY);
  } catch {}
}

// drop drafts of sessions this device no longer sees (deleted, ended,
// or outside its folders now)
function pruneDrafts() {
  const live = new Set(S.sessions.map((s) => s.sid));
  const d = loadDrafts();
  const keep = Object.keys(d).filter((sid) => live.has(sid));
  if (keep.length === Object.keys(d).length) return;
  if (keep.length) localStorage.setItem(DRAFTS_KEY, JSON.stringify(Object.fromEntries(keep.map((k) => [k, d[k]]))));
  else localStorage.removeItem(DRAFTS_KEY);
}

function closeTab(sid) {
  S.tabs = S.tabs.filter((t) => t !== sid);
  if (S.lastTab === sid) S.lastTab = S.tabs[S.tabs.length - 1] || null;
  saveTabs();
  unsubscribe(sid);
}

loadTabs();

// ---------------------------------------------------------------- api
// REST is only used for auth (/api/login, /api/logout).
// Everything else rides the transport as rpc over the control channel.

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  if (res.status === 401) { S.authed = false; nav({ name: "login" }); throw new Error("unauthorized"); }
  if (res.status === 423) { showLock(); throw new Error("locked"); }
  if (res.status === 428) { showPasskeyGate(); throw new Error("passkey required"); }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

// ---------------------------------------------------------------- transport

let T = null; // LocalTransport, set during boot (see transport.js)

function wsSend(obj) {
  if (T && T.send(obj)) return true;
  toast("Not connected — retrying…", true);
  return false;
}

function onTransportStatus(st) {
  S.wsUp = !!st.up;
  updateConnUI();
  if (st.locked) { showLock(); return; }
  if (st.passkeyRequired) { showPasskeyGate(); return; }
  if (st.authRequired) { S.authed = false; nav({ name: "login" }); return; }
  if (st.badOrigin) {
    // a proxy that rewrites Host or Origin; bringing the app back tries again
    toast("The server refused this page's address — open the app at its own URL", true);
    return;
  }
  if (st.serverError) {
    S.serverError = true;
    if (S.authed) renderUnreachable();   // boot shows it on its own
    return;
  }
  if (st.up && S.serverError) {
    S.serverError = false;
    if (S.authed) render();
  }
  if (st.up) {
    for (const sid of S.subs) {
      T.send({ type: "attach", session_id: sid, since_seq: S.seqs[sid] || 0 });
    }
  }
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && S.authed && T) T.connect();
});

// The page keeps running the app.js it loaded, so a server restart or a
// frontend edit is invisible until something reloads it. The server stamps
// the frontend files in every hello; a stamp that differs from the one we
// booted with means this page is stale.
function checkAssets(stamp) {
  if (!stamp) return;
  if (!S.assets) { S.assets = stamp; return; }
  if (S.assets === stamp || $("#update-pill")) return;
  // inside #app, which follows the area above the keyboard
  $("#app").append(el("button", {
    class: "update-pill", id: "update-pill",
    onclick: () => location.reload(),
  }, "new version — tap to reload"));
}

function handleWS(msg) {
  switch (msg.type) {
    case "sessions":
      for (const s2 of msg.sessions || []) {
        if (s2.state === "working" || s2.state === "starting") delete pendingSend[s2.sid];
      }
      S.sessions = msg.sessions || [];
      for (const s of S.sessions) S.status[s.sid] = { state: s.state, detail: s.detail };
      if (S.view?.name === "home") renderHome();
      if (S.view?.name === "sessions") renderSessions();
      if (S.view?.name === "tabs") renderTabs();
      if (S.view?.name === "chat") { updateChatHeader(); updateStatusLine(); }
      if (S.view?.name === "term") updateTermHeader();
      updateTabsAlert();
      break;
    case "attached": {
      const sid = msg.session_id;
      if (msg.snapshot) upsertSession(msg.snapshot);
      if (msg.meta) S.meta[sid] = { ...(S.meta[sid] || {}), ...msg.meta };
      if (msg.context) S.context[sid] = msg.context;
      if (msg.status) S.status[sid] = msg.status;
      if (msg.btw) { S.btw[sid] = msg.btw; btwUI.draw?.(sid); }
      const evs = msg.events || [];
      if (!S.events[sid]) S.events[sid] = [];
      for (const ev of evs) {
        S.events[sid].push(ev);
        if (ev.seq) S.seqs[sid] = ev.seq;
        applyEventToState(sid, ev);
      }
      const rewound = evs.some((ev) => ev.kind === "rewound");
      if (S.view?.name === "chat" && S.view.sid === sid) {
        if (evs.length && S.events[sid].length !== evs.length && !rewound) {
          for (const ev of evs) renderLiveEvent(sid, ev);
        } else {
          renderChatTranscript(sid);
        }
        updateChatHeader(); updateStatusLine();
      }
      break;
    }
    case "event": {
      const sid = msg.session_id;
      const ev = msg.ev || {};
      if (ev.seq) {
        if ((S.seqs[sid] || 0) >= ev.seq) break; // duplicate
        S.seqs[sid] = ev.seq;
        (S.events[sid] = S.events[sid] || []).push(ev);
      }
      applyEventToState(sid, ev);
      if (S.view?.name === "chat" && S.view.sid === sid) {
        if (ev.kind === "rewound") renderChatTranscript(sid);
        else renderLiveEvent(sid, ev);
        updateChatHeader();
      }
      break;
    }
    case "hello":
      checkAssets(msg.assets);
      S.serverVersion = msg.version || "";
      if (Number.isInteger(msg.rewind_max)) rewindMax = msg.rewind_max;
      // back after a restart the Agent SDK page asked for (or one it waited
      // for): say so and show the versions the new process runs
      if (S.sdkRestarting) {
        S.sdkRestarting = false;
        toast("Server restarted");
      }
      if (S.authed && S.view?.name === "sdk") renderSdk();
      break;
    // A page that stays connected never sees another hello, so the stamp also
    // rides the 25s keepalive: a frontend edit shows up within one ping.
    case "pong":
      checkAssets(msg.assets);
      break;
    case "error":
      // the server refused an action that needs a fresh confirmation
      if (msg.code === "stepup_required") {
        S.elevatedUntil = 0;
        // an "Always" the server refused: the request still waits, so the
        // card gets its buttons back
        const card = msg.ref && chatUI.byReq?.[msg.ref];
        if (card && !card.classList.contains("resolved")) {
          card._always = false;
          card.querySelectorAll(".a-buttons button").forEach((b) => { b.disabled = false; });
        }
      }
      if (msg.code === "runner_limit") {
        // The message goes back into the box at once, so neither "OK, I'll
        // wait" nor closing the sheet loses it. Stopping a session sends it.
        const r = restorePending(msg.ref);
        runnerLimitSheet(msg, () => resendPending(msg.ref, r));
        break;
      }
      if (msg.code === "untrusted") {
        // as with the limit: trusting the folder sends it
        const r = restorePending(msg.ref);
        trustSheet(msg, () => resendPending(msg.ref, r));
        break;
      }
      if (msg.code === "quiet_hours" || msg.code === "term_opening") {
        // refused before it reached Claude: the message goes back into the box
        restorePending(msg.ref);
      }
      toast(msg.message || "error", true);
      break;
  }
}

function upsertSession(snap) {
  const i = S.sessions.findIndex((s) => s.sid === snap.sid);
  if (i >= 0) S.sessions[i] = snap; else S.sessions.unshift(snap);
}

function applyEventToState(sid, ev) {
  switch (ev.kind) {
    case "status":
      S.status[sid] = { state: ev.state, detail: ev.detail };
      if (S.view?.name === "chat" && S.view.sid === sid) updateStatusLine();
      updateTabsAlert();
      break;
    case "meta": S.meta[sid] = { ...(S.meta[sid] || {}), ...ev }; break;
    case "commands": S.meta[sid] = { ...(S.meta[sid] || {}), commands: ev.commands }; break;
    case "context": S.context[sid] = ev; if (S.view?.name === "chat" && S.view.sid === sid) updateChatHeader(); break;
    case "btw": S.btw[sid] = ev.items || []; btwUI.draw?.(sid); break;
    // "edit from here": what this client holds from from_seq on is gone
    case "rewound":
      S.events[sid] = (S.events[sid] || []).filter((e) => !(e.seq >= ev.from_seq && e.seq < ev.seq));
      break;
  }
}

function subscribe(sid) {
  if (S.subs.has(sid)) return;
  S.subs.add(sid);
  // if the socket is down, T.send reconnects and onTransportStatus
  // re-attaches every S.subs entry once it is up again
  if (T) T.send({ type: "attach", session_id: sid, since_seq: S.seqs[sid] || 0 });
}

// A closed tab stops the server's stream and drops what it had here;
// opening the chat again attaches from scratch. Its status falls back to
// the session list's, which keeps coming for every session.
function unsubscribe(sid) {
  if (!S.subs.has(sid) || (isSessionView(S.view) && S.view.sid === sid)) return;
  S.subs.delete(sid);
  delete S.events[sid]; delete S.seqs[sid]; delete S.status[sid];
  T?.send({ type: "detach", session_id: sid });
}

// ---------------------------------------------------------------- router

// a session screen: its chat or its True View
const isSessionView = (v) => !!v && (v.name === "chat" || v.name === "term");

// ‹, ⌫ and the iOS edge swipe undo the last step: back goes to the previous
// screen, back again returns to where you were. History holds just those
// two entries (history.state.d: 1 = there is a previous screen under this
// one); going back swaps them. A session's chat and its True View are one
// screen. The menu, on every screen, is the way anywhere else.
const LIST_VIEWS = ["sessions", "projects", "skills", "commands", "agents", "mcp"];

S.depth = 0;
let afterRewind = null;
// The file viewer is a sheet with a history entry of its own (same URL,
// state.viewer): back closes just the viewer, the next back works as
// before. Closing it any other way drops the entry (closeSheet), and that
// history.back() must not route anywhere: popstates until this time pass.
S.viewerOpen = false;
let skipPopUntil = 0;

function viewerHistory() {
  S.depth += 1;
  history.pushState({ d: S.depth, viewer: 1 }, "");
  S.viewerOpen = true;
}

function viewHash(view) {
  return view.name === "chat" ? `#/chat/${view.sid}`
    : view.name === "term" ? `#/term/${view.sid}`
    : view.name === "project" ? `#/project?${encodeURIComponent(view.path)}`
    : view.name === "files" ? `#/files?${encodeURIComponent(view.path || "")}`
    : `#/${view.name}`;
}

function histPush(view) { S.depth += 1; history.pushState({ d: S.depth }, "", viewHash(view)); }
function histReplace(view) { history.replaceState({ d: S.depth }, "", viewHash(view)); }

// go back `steps` entries, then run `then` instead of the normal popstate
// render; the timer covers a popstate that never comes
function rewind(steps, then) {
  if (steps <= 0) { then(); return; }
  const run = () => { if (afterRewind === run) { afterRewind = null; S.depth = history.state?.d ?? 0; then(); } };
  afterRewind = run;
  history.go(-steps);
  setTimeout(run, 1000);
}

function nav(view) {
  // the viewer's entry goes first, then the usual step from the screen under it
  if (S.viewerOpen) { S.viewerOpen = false; closeSheet(); rewind(1, () => nav(view)); return; }
  kbdReset();
  closeSheet();
  closeDrawer();
  // signed out: the hash stays, so signing in again lands on the same screen
  if (view.name === "login") { S.view = view; render(); return; }
  const prev = S.view;
  const show = () => { S.view = view; render(); };
  if (isSessionView(prev) && isSessionView(view) && prev.sid === view.sid) { histReplace(view); show(); return; }
  if (location.hash === viewHash(view)) { show(); return; }
  // the screen being left is the only one kept under the new one
  rewind(S.depth, () => {
    if (prev && prev.name !== "login") histReplace(prev);
    histPush(view); show();
  });
}

// ‹ and ⌫, as the edge swipe; with nothing to go back to (first screen
// after a start), home
function goBack() {
  if (S.depth > 0) { history.back(); return; }
  if (S.view?.name !== "home") nav({ name: "home" });
}

function routeFromHash() {
  const h = location.hash || "#/home";
  if (h.startsWith("#/chat/")) return { name: "chat", sid: h.slice(7) };
  if (h.startsWith("#/term/")) return { name: "term", sid: h.slice(7) };
  if (h.startsWith("#/tabs")) return { name: "tabs" };
  if (h.startsWith("#/devices")) return { name: "devices" };
  if (h.startsWith("#/sdk")) return { name: "sdk" };
  // a hand-edited hash may not decode; such a link falls back to home
  const decode = (s) => { try { return decodeURIComponent(s); } catch { return null; } };
  if (h.startsWith("#/project?")) {
    const path = decode(h.slice(10));
    return path === null ? { name: "home" } : { name: "project", path };
  }
  if (h.startsWith("#/files?")) {
    const path = decode(h.slice(8));
    return path === null ? { name: "home" } : { name: "files", path };
  }
  for (const name of LIST_VIEWS) if (h.startsWith("#/" + name)) return { name };
  if (h.startsWith("#/new")) return { name: "projects" };
  if (h.startsWith("#/library")) return { name: "skills" };
  return { name: "home" };
}

window.addEventListener("popstate", () => {
  if (afterRewind) { afterRewind(); return; }
  if (Date.now() < skipPopUntil) { skipPopUntil = 0; return; }
  if (S.viewerOpen) {   // back on the file viewer: close it, stay put
    S.viewerOpen = false;
    S.depth = history.state?.d ?? 0;
    kbdReset(); closeSheet();
    return;
  }
  if (history.state?.viewer) {   // forward onto a closed viewer's entry
    skipPopUntil = Date.now() + 1000;
    history.back();
    return;
  }
  const left = S.view;
  let to = routeFromHash();
  S.depth = history.state?.d ?? 0;
  kbdReset(); closeSheet(); closeDrawer();
  // back into a session deleted meanwhile (here or on another device)
  if (isSessionView(to) && !S.sessions.some((s) => s.sid === to.sid)) to = { name: "sessions" };
  // swap: the screen just left becomes the one to go back to
  if (S.authed && left && left.name !== "login") { histReplace(left); histPush(to); }
  else histReplace(to);
  S.view = to;
  render();
});

// ---------------------------------------------------------------- shell

function setTopbar(...nodes) {
  const tb = $("#topbar");
  tb.replaceChildren(...nodes);
}

function backBtn() {
  return el("button", { class: "tb-btn", onclick: goBack }, "‹");
}

function menuBtn() {
  return el("button", { class: "tb-btn menu", "aria-label": "Open menu",
    onclick: () => { if (drawerOpen()) closeDrawer(); else openDrawer(); } },
    el("span", { class: "m-glyph" }, "❯"),
    S.sdkAttention ? el("span", { class: "attn-dot" }) : null);
}

// the menu button stays on top of the open drawer, turned to ❮, and closes it
function setMenuBtn(open) {
  const b = $("#topbar .menu");
  if (!b) return;
  b.classList.toggle("open", open);
  b.setAttribute("aria-label", open ? "Close menu" : "Open menu");
  b.querySelector(".m-glyph").textContent = open ? "❮" : "❯";
}

function tbTitle(text) {
  return el("div", { class: "tb-title" }, el("span", { class: "crumb" }, text));
}

// Safari-style top-right button: open-tab count in a rounded square
function tabsBtn() {
  return el("button", { class: "tb-btn tabs-btn" + (tabsAwaiting() ? " alert" : ""),
    "aria-label": "Open tabs",
    onclick: () => nav({ name: "tabs" }) },
    el("span", { class: "tabs-count" }, String(S.tabs.length)));
}

// A session open as a tab on this device waits for an approval — other
// than the one on screen, whose approval card is already in view
function tabAwaiting(sid) {
  const cur = isSessionView(S.view) ? S.view.sid : null;
  return sid !== cur && S.tabs.includes(sid) && S.status[sid]?.state === "awaiting";
}

// The Tabs button pulses for such a tab unless its session card (home,
// sessions list) is actually on screen — then the card pulses instead
function tabsAwaiting() {
  const seen = new Set([...document.querySelectorAll(".s-card[data-sid]")]
    .filter((c) => c._seen).map((c) => c.dataset.sid));
  return S.tabs.some((sid) => tabAwaiting(sid) && !seen.has(sid));
}

function updateTabsAlert() {
  for (const c of document.querySelectorAll(".s-card[data-sid]")) c.classList.toggle("alert", tabAwaiting(c.dataset.sid));
  const on = tabsAwaiting();
  for (const b of document.querySelectorAll(".tabs-btn")) b.classList.toggle("alert", on);
}

// Which session cards are on screen: scrolled into view and, in the home
// carousel, not swiped aside (the observer counts clipping by ancestors)
let cardObserver = null;
function observeCards() {
  cardObserver?.disconnect();
  cardObserver = new IntersectionObserver((entries) => {
    for (const e of entries) e.target._seen = e.isIntersecting;
    updateTabsAlert();
  }, { threshold: 0.5 });
  for (const c of document.querySelectorAll("#view .s-card[data-sid]")) cardObserver.observe(c);
}

function updateConnUI() {
  const pill = $("#conn-pill");
  if (pill) pill.style.display = S.wsUp ? "none" : "block";
  if (S.view?.name === "chat") updateStatusLine();
}

function render() {
  // the lock, gate and login screens replace an open terminal too
  if (S.locked || S.passkeyGate || !S.authed || S.view?.name !== "term") disposeTerm();
  if (S.locked) { renderLock(); return; }
  if (S.passkeyGate) { renderPasskeyGate(); return; }
  if (!S.authed) { renderLogin(); return; }
  switch (S.view?.name) {
    case "chat": renderChat(S.view.sid); break;
    case "term": renderTerm(S.view.sid); break;
    case "tabs": renderTabs(); break;
    case "project": renderProject(S.view.path); break;
    case "files": renderFiles(S.view.path); break;
    case "sessions": renderSessions(); break;
    case "projects": renderProjects(); break;
    case "devices": renderDevices(); break;
    case "sdk": renderSdk(); break;
    case "skills": case "commands": case "agents": case "mcp":
      renderLibraryList(S.view.name); break;
    default: renderHome();
  }
}

// ---------------------------------------------------------------- drawer

const NAV_TABS = [
  { name: "skills", label: "Skills", icon: "◆" },
  { name: "commands", label: "Commands", icon: "⌘" },
  { name: "agents", label: "Agents", icon: "⑃" },
  { name: "mcp", label: "MCPs", icon: "⚡" },
  { name: "sessions", label: "Sessions", icon: "❯" },
  { name: "projects", label: "Projects", icon: "▤" },
];

function navCount(name) {
  if (name === "sessions") return S.sessions.length;
  if (name === "projects") return (S.projects || []).filter((p) => p.exists).length;
  const lib = S.library;
  if (!lib) return "";
  return { skills: lib.skills.length, commands: lib.commands.length,
    agents: lib.agents.length, mcp: lib.mcp_servers.length }[name] ?? "";
}

function openDrawer() {
  closeDrawer();
  const active = S.view?.name;
  const backdrop = el("div", { class: "drawer-backdrop",
    onclick: (e) => { if (e.target === backdrop) closeDrawer(); } });
  const items = [{ name: "home", label: "Home", icon: "⌂" }, ...NAV_TABS,
    { name: "devices", label: "Devices", icon: "⎔" },
    // the SDK is the server's, so only a device that manages it sees this
    S.canManage ? { name: "sdk", label: "Agent SDK", icon: "⟳", dot: S.sdkAttention } : null]
    .filter(Boolean);
  backdrop.append(el("aside", { class: "drawer" },
    el("div", { class: "drawer-head" }, "Clicker4AI"),
    el("nav", { class: "drawer-nav" },
      items.map((t) => el("button", {
        class: "nav-item" + (active === t.name ? " on" : ""),
        onclick: () => nav({ name: t.name }),
      },
        el("span", { class: "n-icon" }, t.icon),
        el("span", { class: "n-label" }, t.label),
        t.dot ? el("span", { class: "attn-dot" }) : null,
        el("span", { class: "n-count" }, String(navCount(t.name) || ""))))),
    el("div", { class: "drawer-foot" },
      el("span", { class: S.wsUp ? "conn-ok" : "conn-bad" }, S.wsUp ? "● connected" : "○ offline"),
      S.passkeyDevice ? el("button", { class: "foot-btn", onclick: doLock }, "lock") : null,
      el("button", { class: "foot-btn", onclick: () => confirmSheet("Sign out completely?",
        S.passkeyDevice
          ? "The device is removed from the server. To keep its folders, lock it instead."
          : "The device is removed from the server; you will need a pairing code next time.",
        doLogout) }, "sign out"))));
  $("#drawer-root").append(backdrop);
  setMenuBtn(true);
  loadLibrary().then(() => {
    // fill in counts once the library arrives
    if (!$("#drawer-root").children.length) return;
    backdrop.querySelectorAll(".nav-item").forEach((btn, i) => {
      const c = btn.querySelector(".n-count");
      if (c) c.textContent = String(navCount(items[i].name) || "");
    });
  }).catch(() => {});
}

const drawerOpen = () => $("#drawer-root").children.length > 0;
function closeDrawer() { $("#drawer-root").replaceChildren(); setMenuBtn(false); }

async function doLogout() {
  try { await api("/api/logout", { method: "POST" }); } catch {}
  localStorage.removeItem("bcrc_token"); // legacy cleanup
  localStorage.removeItem(DRAFTS_KEY);
  for (const k of Object.keys(memDrafts)) delete memDrafts[k];
  S.authed = false;
  S.locked = false;
  S.passkeyGate = false;
  closeDrawer(); closeSheet();
  try { T?.close(); } catch {}
  T = null;
  renderLogin();
}


// ---------------------------------------------------------------- agent sdk
// The server's claude-agent-sdk against the `claude` CLI its sessions run
// (clicker4ai/sdk_update.py). Installing and restarting need the manage grant
// and a fresh passkey confirmation; the server re-checks both.

async function sdkCall(method, params, reason) {
  try {
    return await T.rpc(method, params);
  } catch (e) {
    if (e.code !== "stepup_required") throw e;
    S.elevatedUntil = 0;
    if (!await ensureStepUp(reason, true)) return null;
    return T.rpc(method, params);
  }
}

// Until the socket is back (hello → renderSdk) the page says what is going on.
function sdkRestartingView() {
  S.sdkRestarting = true;
  if (S.view?.name !== "sdk") return;
  $("#view").replaceChildren(el("div", { class: "pad sdk-page" },
    el("div", { class: "empty" }, "Restarting the server… this page reconnects by itself.")));
}

async function renderSdk(check) {
  if (S.sdkRestarting) { sdkRestartingView(); return; }
  setTopbar(menuBtn(), tbTitle("Agent SDK"), tabsBtn());
  const view = $("#view");
  if (!view.querySelector(".sdk-page")) {
    view.replaceChildren(el("div", { class: "pad sdk-page" }, el("div", { class: "empty" }, "Loading…")));
  }
  let st;
  try {
    st = await T.rpc("sdk.status", check ? { check: true } : {});
  } catch (e) {
    if (S.view?.name === "sdk") view.replaceChildren(el("div", { class: "pad sdk-page" }, el("div", { class: "empty" }, e.message)));
    return;
  }
  if (S.view?.name !== "sdk") return;
  const attn = st.update_available || st.cli_update_available || st.restart_pending;
  if (attn !== S.sdkAttention) {
    S.sdkAttention = attn;
    setTopbar(menuBtn(), tbTitle("Agent SDK"), tabsBtn());
  }
  const drift = st.system_cli && st.bundled_cli && st.system_cli !== st.bundled_cli;
  const li = st.last_install, lc = st.last_cli_update;
  const actions = [];
  if (st.installing || st.cli_updating) {
    actions.push(el("div", { class: "empty" },
      st.installing ? `Installing ${st.installing}…` : `Updating the CLI to ${st.cli_latest}…`));
    setTimeout(() => { if (S.view?.name === "sdk") renderSdk(); }, 2000);
  } else if (st.update_available && st.can_install) {
    actions.push(el("button", { class: "btn primary", onclick: () => confirmSheet(
      `Install SDK ${st.latest}?`,
      "pip installs it on the server and checks the server still starts; "
      + "if not, the current version is put back. Running sessions are not "
      + "touched — the new SDK is used after a restart.",
      async () => {
        const r = await sdkCall("sdk.install", { version: st.latest }, "Updating the SDK");
        if (r) renderSdk();
      }) }, `Install ${st.latest}`));
  } else if (st.update_available) {
    actions.push(el("div", { class: "empty" }, "No pip in the server's environment. Run on the server:"),
      el("pre", { class: "sdk-cmd" }, st.hint));
  }
  if (st.cli_update_available && !st.installing && !st.cli_updating) {
    actions.push(el("button", { class: "btn primary", onclick: () => confirmSheet(
      `Update the CLI to ${st.cli_latest}?`,
      "Runs `claude update` on the server. No restart: new sessions start on "
      + "the new CLI, running ones keep theirs until they stop.",
      async () => {
        const r = await sdkCall("sdk.cli_update", { version: st.cli_latest }, "Updating the claude CLI");
        if (r) renderSdk();
      }) }, `Update CLI ${st.cli_latest}`));
  }
  if (st.restart_pending && !st.installing && !st.cli_updating) {
    if (!st.can_restart) {
      actions.push(el("div", { class: "empty" },
        "Installed; the server has no supervisor (pm2/systemd) to start it again, so restart it on the host."));
    } else {
      const busy = st.busy ? `${st.busy} session${st.busy > 1 ? "s" : ""} or terminal${st.busy > 1 ? "s" : ""} busy right now. ` : "";
      actions.push(el("button", { class: "btn", onclick: () => confirmSheet("Restart the server now?",
        busy + "Every session stops and resumes on its next message (rebuilding its context "
        + "cache costs tokens). This page reconnects by itself.",
        async () => {
          const r = await sdkCall("sdk.restart", { when: "now" }, "Restarting the server");
          if (r) sdkRestartingView();
        }) }, "Restart now"));
      actions.push(st.restart_when_idle
        ? el("button", { class: "btn", onclick: async () => {
            try { await T.rpc("sdk.restart", { when: "cancel" }); } catch (e) { toast(e.message, true); }
            renderSdk();
          } }, "Cancel restart when idle")
        : el("button", { class: "btn", onclick: async () => {
            try {
              if (await sdkCall("sdk.restart", { when: "idle" }, "Restarting the server")) renderSdk();
            } catch (e) { toast(e.message, true); }
          } }, "Restart when idle"));
    }
  }
  view.replaceChildren(el("div", { class: "pad sdk-page" },
    el("div", { class: "eyebrow" }, "versions"),
    kvRow("Clicker4AI server", S.serverVersion),
    kvRow("claude CLI (sessions)", st.system_cli),
    kvRow(`newest CLI (${st.cli_channel})`, st.cli_latest),
    kvRow("SDK running", st.loaded),
    st.installed !== st.loaded ? kvRow("SDK installed", st.installed) : null,
    kvRow("SDK tested with CLI", st.bundled_cli),
    kvRow(`newest SDK ${st.range}`, st.latest),
    kvRow("checked", st.checked_at ? relTime(st.checked_at * 1000) : ""),
    st.error ? el("div", { class: "empty" }, st.error) : null,
    el("div", { class: "empty" },
      st.update_available ? `Update available: ${st.installed} → ${st.latest}. `
        : st.latest ? "The SDK is up to date. " : "",
      !drift ? ""
        : st.system_cli.localeCompare(st.bundled_cli, undefined, { numeric: true }) > 0
          ? `The CLI (${st.system_cli}) is ahead of what this SDK was tested with (${st.bundled_cli}).`
          : `The CLI (${st.system_cli}) is behind what this SDK was tested with (${st.bundled_cli}); update the CLI.`),
    li ? el("div", { class: li.ok ? "empty" : "empty sdk-err" }, li.message) : null,
    lc ? el("div", { class: lc.ok ? "empty" : "empty sdk-err" }, lc.message) : null,
    st.restart_when_idle ? el("div", { class: "empty" },
      "The server restarts once no session is in a turn and no True View is open.") : null,
    el("div", { class: "a-buttons" }, ...actions,
      el("button", { class: "btn", onclick: () => renderSdk(true) }, "Check now")),
    el("div", { class: "empty" }, "Same from the server: ", el("code", null, "c4ai sdk [update|update-cli]"), ".")));
}

// ---------------------------------------------------------------- shared components

// Large, readable list row. side may be a string or array of lines.
function bigItem({ icon, title, badge, sub, side, mono, onclick, key }) {
  const sides = side == null ? [] : (Array.isArray(side) ? side : [side]).filter(Boolean);
  return el("button", { class: "item", onclick, "data-kbd": key },
    icon ? el("span", { class: "item-icon" }, icon) : null,
    el("div", { class: "item-main" },
      el("div", { class: "item-title" + (mono ? " mono" : "") },
        el("span", { class: "item-name" }, title), badge || null),
      sub ? el("div", { class: "item-sub" }, sub) : null),
    sides.length ? el("div", { class: "item-side" }, sides.map((x) => el("div", null, x))) : null);
}

// Search input + filtered list; filters items on every keystroke.
function searchableList({ items, keys, toNode, placeholder, empty }) {
  const input = el("input", { class: "search-input", type: "search",
    placeholder: placeholder || "Search…", autocapitalize: "none", autocorrect: "off" });
  const list = el("div", { class: "item-list" });
  const draw = () => {
    const f = input.value.trim().toLowerCase();
    const vis = f
      ? items.filter((it) => keys.some((k) => String(it[k] || "").toLowerCase().includes(f)))
      : items;
    list.replaceChildren(...(vis.length ? vis.map(toNode)
      : [el("div", { class: "empty" }, empty || "Nothing found.")]));
  };
  input.addEventListener("input", draw);
  draw();
  return { node: el("div", null, input, list), input };
}

function scopeBadge(it) {
  return el("span", { class: "badge " + it.scope }, it.scope + (it.source ? ":" + it.source : ""));
}

function kvRow(k, v) {
  return el("div", { class: "kv" },
    el("span", { class: "kv-k" }, k),
    el("span", { class: "kv-v" }, v || "—"));
}

const shortPath = (p) => (p || "").replace(S.home, "~");

// ---------------------------------------------------------------- login

function renderLogin() {
  setTopbar();
  const onEnter = (e) => { if (e.key === "Enter") doLogin(); };
  $("#view").replaceChildren(
    el("div", { class: "login-box" },
      el("div", { class: "glyph" }, "❯_"),
      el("h1", null, "Clicker4AI"),
      el("p", null, "Enter the pairing code shown in the server terminal (or run ",
        el("code", null, "c4ai pair"), "), or scan its QR code."),
      el("input", { type: "text", placeholder: "XXXXX-XXXXX", id: "pair-code",
        autocapitalize: "characters", autocorrect: "off", autocomplete: "one-time-code",
        spellcheck: "false", maxlength: "16", onkeydown: onEnter }),
      el("input", { type: "text", placeholder: "Device name (optional)", id: "pair-name",
        autocorrect: "off", maxlength: "64", onkeydown: onEnter }),
      el("button", { class: "btn primary", onclick: doLogin }, "Connect"),
      el("div", { class: "login-alt", id: "login-alt" }),
      el("div", { class: "login-err", id: "login-err" })
    )
  );
  // the passkey button only appears where it can actually work
  loadPasskeyStatus().then((st) => {
    if (S.authed || !$("#login-alt")) return;
    if (!st.available || !st.has_credentials || !hasWebAuthn()) return;
    $("#login-alt").replaceChildren(
      el("div", { class: "login-or" }, "or"),
      el("button", { class: "btn", onclick: doPasskeyLogin }, "Sign in with passkey"));
  }).catch(() => {});
}

async function doPasskeyLogin() {
  try {
    await passkeyLogin($("#pair-name")?.value.trim());
    S.elevatedUntil = Date.now() + 240e3;
    await boot();
  } catch (e) {
    if (e && e.name === "NotAllowedError") return;   // cancelled on the phone
    if (/unknown passkey/i.test(e.message || "")) {
      signalUnknownPasskey(e.credentialId);
      $("#login-err").textContent =
        "This passkey was removed on the server — use a pairing code";
      return;
    }
    $("#login-err").textContent = e.message || "Passkey sign-in failed";
  }
}

// Redeem a one-time pairing code; the server sets the device cookie.
async function redeemCode(code, name) {
  const r = await fetch("/api/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, name: name || "" }),
  });
  if (r.ok) return;
  if (r.status === 429) throw new Error("Too many attempts — wait a minute");
  throw new Error("Invalid or expired code");
}

async function doLogin() {
  const code = $("#pair-code").value.trim();
  if (!code) return;
  try {
    await redeemCode(code, $("#pair-name").value.trim());
    await boot();
  } catch (e) {
    $("#login-err").textContent = e.message || "Login failed";
  }
}

// ---------------------------------------------------------------- passkeys
// WebAuthn: a second way in (login without a pairing code) and a step-up
// confirmation for risky actions. The server decides what needs one and
// answers "stepup_required"; grants never come from here.

const hasWebAuthn = () => !!(window.PublicKeyCredential && navigator.credentials);

function b64uToBuf(str) {
  const b64 = String(str).replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(b64 + "=".repeat((4 - (b64.length % 4)) % 4));
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
}

function bufToB64u(buf) {
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// The server sends ids and challenges base64url-encoded; WebAuthn wants buffers.
function decodeOptions(opts) {
  const o = { ...opts, challenge: b64uToBuf(opts.challenge) };
  if (o.user) o.user = { ...o.user, id: b64uToBuf(o.user.id) };
  for (const key of ["excludeCredentials", "allowCredentials"]) {
    if (Array.isArray(o[key])) o[key] = o[key].map((c) => ({ ...c, id: b64uToBuf(c.id) }));
  }
  return o;
}

function encodeCredential(cred) {
  const r = cred.response;
  const out = {
    id: cred.id, rawId: bufToB64u(cred.rawId), type: cred.type,
    clientExtensionResults: cred.getClientExtensionResults?.() || {},
    response: { clientDataJSON: bufToB64u(r.clientDataJSON) },
  };
  if (cred.authenticatorAttachment) out.authenticatorAttachment = cred.authenticatorAttachment;
  if (r.attestationObject) {
    out.response.attestationObject = bufToB64u(r.attestationObject);
    if (r.getTransports) out.response.transports = r.getTransports();
  } else {
    out.response.authenticatorData = bufToB64u(r.authenticatorData);
    out.response.signature = bufToB64u(r.signature);
    out.response.userHandle = r.userHandle ? bufToB64u(r.userHandle) : null;
  }
  return out;
}

async function loadPasskeyStatus() {
  try {
    S.passkey = await (await fetch("/api/passkey")).json();
  } catch { S.passkey = { available: false, has_credentials: false }; }
  return S.passkey;
}

// A server cannot delete a passkey from the keychain — it never holds the
// private key. The Signal API is the way back: we hand the system the ids
// we still accept and it drops the rest. Safari 18.4+ and Chrome 132+;
// older browsers simply lack the method and keep the manual route
// (iOS Settings -> Passwords).
async function signalAcceptedPasskeys() {
  if (!hasWebAuthn() || !PublicKeyCredential.signalAllAcceptedCredentials) return;
  try {
    const p = await api("/api/passkeys/signal");
    if (!p.rp_id || !p.user_id) return;     // nothing was ever registered
    await PublicKeyCredential.signalAllAcceptedCredentials({
      rpId: p.rp_id, userId: p.user_id,
      allAcceptedCredentialIds: p.credential_ids,
    });
  } catch {}   // best effort: never let housekeeping break a screen
}

// The counterpart for the login screen, where we are not signed in: the
// phone offered a passkey the server no longer knows, so retire just that one.
async function signalUnknownPasskey(credentialId) {
  if (!hasWebAuthn() || !PublicKeyCredential.signalUnknownCredential || !credentialId) return;
  try {
    const st = S.passkey.rp_id ? S.passkey : await loadPasskeyStatus();
    if (!st.rp_id) return;
    await PublicKeyCredential.signalUnknownCredential({
      rpId: st.rp_id, credentialId,
    });
  } catch {}
}

// Face ID / Touch ID prompts are user gestures: every caller runs inside
// a click handler, and a cancelled prompt just returns false.
async function passkeyLogin(name) {
  const opts = await api("/api/passkey/login/begin", { method: "POST", body: "{}" });
  const cred = await navigator.credentials.get({ publicKey: decodeOptions(opts) });
  if (!cred) throw new Error("No passkey chosen");
  try {
    await api("/api/passkey/login/finish", { method: "POST",
      body: JSON.stringify({ credential: encodeCredential(cred), name: name || "" }) });
  } catch (e) {
    e.credentialId = cred.id;   // so the caller can retire an orphan
    throw e;
  }
}

async function passkeyRegister(name) {
  const opts = await api("/api/passkey/register/begin", { method: "POST", body: "{}" });
  const cred = await navigator.credentials.create({ publicKey: decodeOptions(opts) });
  if (!cred) throw new Error("Passkey not created");
  const r = await api("/api/passkey/register/finish", { method: "POST",
    body: JSON.stringify({ credential: encodeCredential(cred), name: name || "" }) });
  S.elevatedUntil = Date.now() + 240e3;
  S.passkey.device_passkey = true;
  S.passkeyDevice = true;
  return r;
}

const elevated = () => Date.now() < S.elevatedUntil;

// Ask for Face ID unless a recent confirmation still counts. Returns
// false when the user cancels, so callers can quietly drop the action.
// `force`: ask on a device without a passkey of its own too (a key synced
// from another device confirms there as well) — the SDK update does.
// `fresh`: ask even when a recent confirmation still counts — starting a
// True View terminal does.
async function ensureStepUp(reason, force, fresh) {
  if (!S.passkeyDevice && !force) return true;
  if (elevated() && !fresh) return true;
  if (!hasWebAuthn()) { toast("This browser cannot confirm passkeys", true); return false; }
  try {
    const opts = await api("/api/passkey/stepup/begin", { method: "POST", body: "{}" });
    const cred = await navigator.credentials.get({ publicKey: decodeOptions(opts) });
    if (!cred) return false;
    await api("/api/passkey/stepup/finish", { method: "POST",
      body: JSON.stringify({ credential: encodeCredential(cred) }) });
    // keep a small margin under the server's window
    S.elevatedUntil = Date.now() + 240e3;
    return true;
  } catch (e) {
    if (e && e.name === "NotAllowedError") return false;   // cancelled
    toast(reason ? `${reason} needs a passkey: ${e.message}` : e.message, true);
    return false;
  }
}

// rpc that survives a "stepup_required": confirm once, then retry.
async function rpcConfirmed(method, params, reason) {
  try {
    return await T.rpc(method, params);
  } catch (e) {
    if (e.code !== "stepup_required") throw e;
    S.elevatedUntil = 0;
    if (!await ensureStepUp(reason)) throw new Error("Not confirmed");
    return T.rpc(method, params);
  }
}

async function addPasskeyFlow() {
  if (!hasWebAuthn()) { toast("This browser has no passkey support", true); return; }
  if (!await ensureStepUp("Adding a passkey")) return;
  try {
    await passkeyRegister("");
    toast("Passkey added");
  } catch (e) {
    if (e && e.name === "NotAllowedError") return;   // cancelled
    toast(e.message || "Could not add the passkey", true);
  }
  renderDevices();
}

// ---------------------------------------------------------------- lock
// "Lock" keeps the cookie and the device's grants; only a passkey wakes
// it. Getting back in therefore needs the cookie *and* the owner, and the
// passkey grants nothing it did not already have.

function showLock() {
  if (S.locked) return;
  S.locked = true;
  closeDrawer(); closeSheet();
  try { T?.close(); } catch {}
  renderLock();
}

function renderLock() {
  setTopbar();
  $("#view").replaceChildren(
    el("div", { class: "login-box" },
      el("div", { class: "glyph" }, "⚿"),
      el("h1", null, "Locked"),
      el("p", null, "This device keeps its folders and stays paired. "
        + "Unlock with your passkey to carry on."),
      el("button", { class: "btn primary", onclick: doUnlock }, "Unlock"),
      el("div", { class: "login-or" }, "or"),
      signOutButton(),
      el("div", { class: "login-err", id: "login-err" }),
      el("p", { class: "quiet", id: "lock-help" })));
  lostKeyHelp("lock-help", (dev) => [
    "Passkey gone from this phone? On the server run ",
    el("code", null, "c4ai passkeys"), " to find it, ",
    el("code", null, "c4ai passkeys remove <id>"), " and ",
    el("code", null, `c4ai devices unlock ${dev}`),
    ", then add a new passkey here. The device keeps its folders."]);
}

function signOutButton() {
  return el("button", { class: "btn", onclick: () => confirmSheet("Sign out completely?",
    "The device is removed from the server; you will need a pairing code "
    + "and new folders next time.", doLogout) }, "Sign out completely");
}

// Name the device in CLI hints; the name works wherever an id does.
function lostKeyHelp(id, parts) {
  loadPasskeyStatus().then((st) => {
    const box = $("#" + id);
    if (!box) return;
    const d = st.device || {};
    const ref = d.name ? (/\s/.test(d.name) ? `"${d.name}"` : d.name) : (d.id || "<device>");
    box.replaceChildren(...parts(ref));
  }).catch(() => {});
}

// ---------------------------------------------------------------- passkey gate
// A device paired with require_passkey (the default) may do nothing but
// register a passkey until it has one; the server refuses the rest (428,
// ws close 4428). Only the CLI can lift the requirement.

function showPasskeyGate() {
  if (S.passkeyGate || S.locked) return;
  S.passkeyGate = true;
  closeDrawer(); closeSheet();
  try { T?.close(); } catch {}
  renderPasskeyGate();
}

function renderPasskeyGate() {
  setTopbar();
  const box = el("div", { class: "login-box" },
    el("div", { class: "glyph" }, "⚿"),
    el("h1", null, "Add a passkey"),
    el("p", null, "This device needs a passkey before it can be used. "
      + "It then confirms True View and other risky actions, and unlocks "
      + "the app after a while away."),
    el("div", { id: "gate-action" }),
    el("div", { class: "login-or" }, "or"),
    signOutButton(),
    el("div", { class: "login-err", id: "login-err" }),
    el("p", { class: "quiet", id: "gate-help" }));
  $("#view").replaceChildren(box);
  loadPasskeyStatus().then((st) => {
    const slot = $("#gate-action");
    if (!slot) return;
    if (!st.available) {
      slot.replaceChildren(el("p", { class: "login-err" },
        "Passkeys need the server's https address — this one cannot register them."));
    } else if (!hasWebAuthn()) {
      slot.replaceChildren(el("p", { class: "login-err" },
        "This browser cannot create passkeys."));
    } else {
      slot.replaceChildren(el("button", { class: "btn primary", onclick: doGateRegister },
        "Add a passkey"));
    }
  }).catch(() => {});
  lostKeyHelp("gate-help", (dev) => [
    "No passkey possible on this device? The requirement is lifted on the server: ",
    el("code", null, `c4ai devices set ${dev} --no-passkey`), "."]);
}

async function doGateRegister() {
  try {
    await passkeyRegister("");
    S.passkeyGate = false;
    toast("Passkey added");
    await boot();
  } catch (e) {
    if (e && e.name === "NotAllowedError") return;   // cancelled
    const err = $("#login-err");
    if (err) err.textContent = e.message || "Could not add the passkey";
  }
}

async function doUnlock() {
  if (!hasWebAuthn()) { toast("This browser cannot use passkeys", true); return; }
  try {
    const opts = await api("/api/unlock/begin", { method: "POST", body: "{}" });
    const cred = await navigator.credentials.get({ publicKey: decodeOptions(opts) });
    if (!cred) return;
    await api("/api/unlock/finish", { method: "POST",
      body: JSON.stringify({ credential: encodeCredential(cred) }) });
    S.locked = false;
    S.elevatedUntil = Date.now() + 240e3;
    await boot();
  } catch (e) {
    if (e && e.name === "NotAllowedError") return;   // cancelled
    const err = $("#login-err");
    if (err) err.textContent = e.message || "Unlock failed";
  }
}

async function doLock() {
  try {
    await api("/api/lock", { method: "POST", body: "{}" });
    showLock();
  } catch (e) { toast(e.message || "Could not lock", true); }
}

// ---------------------------------------------------------------- devices

async function renderDevices() {
  setTopbar(menuBtn(), tbTitle("Devices"), tabsBtn());
  const view = $("#view");
  view.replaceChildren(el("div", { class: "pad" }, el("div", { class: "empty" }, "Loading…")));
  let res;
  try {
    res = await T.rpc("devices.list");
  } catch (e) { view.replaceChildren(el("div", { class: "pad" }, el("div", { class: "empty" }, e.message))); return; }
  if (S.view?.name !== "devices") return;
  const devs = res.devices || [];
  devs.sort((a, b) => (b.current - a.current) || (b.last_seen - a.last_seen));
  view.replaceChildren(el("div", { class: "pad" },
    el("div", { class: "eyebrow" }, res.can_manage ? "signed-in devices" : "this device"),
    el("div", { class: "item-list" }, devs.map((d) => bigItem({
      icon: "⎔",
      title: d.name || "Unknown device",
      badge: [d.current ? el("span", { class: "badge" }, "this device") : null,
        d.manage_devices ? el("span", { class: "badge" }, "manager") : null,
        d.terminal ? el("span", { class: "badge" }, "terminal") : null,
        d.files_upload ? el("span", { class: "badge" }, "files + upload")
          : d.files ? el("span", { class: "badge" }, "files") : null,
        d.require_passkey ? el("span", { class: "badge" }, "passkey required") : null],
      sub: `folders: ${!d.roots ? "all" : d.roots.length ? d.roots.map(shortPath).join(", ") : "none"}`
        + ` · paired ${new Date(d.created_at * 1000).toLocaleDateString()} · id ${d.id}`,
      side: relTime(d.last_seen * 1000),
      onclick: () => deviceMenu(d),
    }))),
    el("div", { class: "empty" }, res.can_manage ? "" : "Other devices are managed on the server. ",
      "Folders and permissions are set on the server: ",
      el("code", null, "c4ai devices"), "."),
    el("div", { class: "pk-section", id: "pk-section" })));
  renderPasskeys();
}

// Passkeys live under the device list: they sign this phone back in
// without a pairing code, and confirm True View, "always allow" and
// signing out another device.
async function renderPasskeys() {
  const box = $("#pk-section");
  if (!box) return;
  const st = await loadPasskeyStatus();
  if (!box.isConnected) return;
  if (!st.available) {
    box.replaceChildren(el("div", { class: "eyebrow" }, "passkeys"),
      el("div", { class: "empty" }, "Passkeys need an https address for this server."));
    return;
  }
  let keys = [];
  try { keys = (await api("/api/passkeys")).passkeys || []; } catch {}
  if (!box.isConnected) return;
  box.replaceChildren(
    el("div", { class: "eyebrow" }, "passkeys"),
    el("div", { class: "item-list" }, keys.map((k) => bigItem({
      icon: "⚿",
      title: k.name || "Passkey",
      badge: [k.backed_up ? el("span", { class: "badge" }, "synced") : null],
      sub: `added ${new Date(k.created_at * 1000).toLocaleDateString()}`
        + ` · last used ${relTime(k.last_used * 1000) || "never"}`,
      onclick: () => passkeyMenu(k, keys),
    }))),
    hasWebAuthn()
      ? el("button", { class: "btn", onclick: addPasskeyFlow },
          keys.length ? "Add another passkey" : "Add a passkey")
      : null,
    el("div", { class: "empty" },
      "A passkey signs you in without a pairing code, but grants nothing: "
      + "a new device starts with no folders until the server gives it some. "
      + "Risky actions on a device with a passkey ask for it first."));
}

function passkeyMenu(k, keys) {
  const name = k.name || "passkey";
  // the last key of a device that must have one: it stops working until
  // a new one is added
  const lastHere = S.requirePasskey && k.current_device
    && keys.filter((x) => x.current_device).length === 1;
  sheet(name, [
    { icon: "✎", label: "Rename", sub: "Only here — the keychain keeps “Clicker4AI”",
      action: () => renamePasskey(k) },
    { icon: "✕", label: "Remove", sub: "It can no longer sign in or confirm",
      danger: true,
      action: () => confirmSheet(`Remove “${name}”?`,
        (lastHere
          ? "This device requires a passkey: without this one it cannot be "
            + "used until you add a new one. "
          : "This device stays signed in; pairing codes still work. ")
        + "The key is removed from the phone's keychain too.",
        async () => {
          if (!await ensureStepUp("Removing a passkey")) return;
          try {
            await api(`/api/passkeys/${encodeURIComponent(k.id)}`, { method: "DELETE" });
            await signalAcceptedPasskeys();   // drop it from the keychain too
          } catch (e) { toast(e.message || "Could not remove", true); }
          renderDevices();
        }) },
  ]);
}

// The label is the server's (it starts as the registering device's name,
// misleading once the key syncs to other devices). Another device's key
// needs the manage grant and a passkey confirmation, as a device rename.
async function renamePasskey(k) {
  const name = await textSheet("Passkey name", { value: k.name || "", maxlength: 64 });
  if (!name || name === k.name) return;
  if (!k.current_device && !await ensureStepUp("Renaming another device's passkey")) return;
  try {
    await api(`/api/passkeys/${encodeURIComponent(k.id)}`, { method: "PATCH",
      body: JSON.stringify({ name }) });
    toast("Renamed");
  } catch (e) { toast(e.message || "Could not rename", true); }
  renderDevices();
}

function deviceMenu(d) {
  const name = d.name || "device";
  sheet(name, [
    d.current && !d.require_passkey ? { icon: "⚿", label: "Require a passkey",
      sub: "Only the server can turn this off again",
      action: () => confirmSheet("Require a passkey on this device?",
        "Without one it can do nothing but add one. Turning this off again "
        + "needs the server: c4ai devices set " + name + " --no-passkey.",
        requirePasskeyHere) } : null,
    { icon: "✎", label: "Rename",
      sub: d.current ? "This device" : "Needs the manage grant",
      action: () => renameDevice(d) },
    { icon: "✕", label: d.current ? "Revoke this device" : "Revoke",
      sub: d.current ? "Logs you out here" : "Logged out on its next request", danger: true,
      action: () => confirmSheet(`Revoke “${name}”?`,
        d.current ? "This device will be logged out." : "It will need a new pairing code to log in again.",
        async () => {
          const r = await rpcConfirmed("devices.revoke", { id: d.id },
            "Signing out a device");
          if (r.current) { await doLogout(); return; }
          toast("Revoked");
          renderDevices();
        }) },
  ]);
}

async function requirePasskeyHere() {
  try {
    const r = await api("/api/passkey/require", { method: "POST", body: "{}" });
    S.requirePasskey = true;
    if (r.passkey_required) { showPasskeyGate(); return; }
    toast("Passkey required on this device");
  } catch (e) { toast(e.message || "Could not change it", true); }
  renderDevices();
}

// Names are unique server-side, so a clash comes back as a plain error.
async function renameDevice(d) {
  const name = await textSheet("Device name", { value: d.name || "", maxlength: 64 });
  if (!name || name === d.name) return;
  try {
    await rpcConfirmed("devices.rename", { id: d.id, name },
      "Renaming another device");
    toast("Renamed");
  } catch (e) {
    toast(e.message || "Could not rename", true);
  }
  renderDevices();
}

// Too many sessions hold a live process. The server says which ones can
// be stopped; the choice is the user's, because stopping a long session
// means paying to rebuild its context when they come back to it.
// Tokens alone would mislead: the same context costs far more to rebuild
// on Opus than on Haiku, so the model is always named next to them.
function fmtReload(c) {
  const model = (c.model || "default").replace(/^claude-/, "");
  if (!c.resume_tokens) return `reloads on ${model}`;
  const n = c.resume_tokens;
  const size = n >= 1000 ? `~${Math.round(n / 1000)}k` : `~${n}`;
  return `${size} tokens to reload on ${model}`;
}

function runnerLimitSheet(err, retry) {
  const list = (err.data && err.data.candidates) || [];
  if (!list.length) { toast(err.message, true); return; }
  const limit = err.data && err.data.limit;
  sheet(limit ? `${limit} sessions already running` : "Too many sessions running", list.map((c) => ({
    icon: "■",
    label: c.title,
    // a True View terminal may be mid-turn: the server cannot see it
    sub: `${shortPath(c.cwd)} · ${c.terminal ? "True View open" : `idle ${relTime(c.last_active)}`} · ${fmtReload(c)}`,
    action: async () => {
      try {
        await T.rpc("sessions.stop", { sid: c.sid });
        toast("Stopped — it resumes when you write to it");
        if (retry) await retry();
      } catch (e) { toast(e.message || "Could not stop it", true); }
    },
  })), [
    el("button", { class: "btn primary", style: "width:100%", onclick: closeSheet }, "OK, I'll wait"),
    el("div", { class: "si-sub", style: "margin:12px 0 4px" }, "…or stop one of these to make room:"),
  ]);
}

// The terminal asks "Do you trust the files in this folder?" on a first
// run; the chat's `claude` does not, so the server asks through this
// (trust.py). Items: what the folder would run, one summary line each.
function trustSheet(err, retry) {
  const d = err.data || {};
  const items = d.items || [];
  if (!d.cwd || !items.length) { toast(err.message || "error", true); return; }
  rawSheet(el("div", { class: "sheet-body" },
    el("div", { class: "sheet-title" }, "Trust this folder?"),
    el("div", { class: "a-desc", style: "margin-bottom:10px" }, shortPath(d.cwd)),
    // each folder is trusted on its own (trust.py); say why it asks anyway
    d.trusted_parent ? el("div", { class: "a-desc", style: "margin-bottom:10px" },
      `${shortPath(d.trusted_parent.path)} is trusted (in the `
      + `${d.trusted_parent.where === "app" ? "app" : "terminal"}), but that does `
      + "not cover the folders inside it.") : null,
    el("div", { class: "a-desc", style: "margin-bottom:14px" },
      "Claude Code loads these from the folder and runs them on its own: hooks "
      + "start without asking, allow rules skip the approval card, commands run "
      + "their shell lines when typed. Trust only a folder whose files you know."),
    ...items.map((it) => el("div", { style: "margin:0 0 10px" },
      el("div", { style: "font-family:var(--mono)" }, it.path),
      el("div", { class: "si-sub" }, it.note))),
    el("div", { class: "a-buttons", style: "margin-top:14px" },
      el("button", { class: "btn primary", onclick: async () => {
        try {
          await rpcConfirmed("trust.add", { cwd: d.cwd }, "Trusting a folder");
        } catch (e) { toast(e.message || "Could not trust it", true); return; }
        closeSheet();
        toast("Folder trusted");
        if (retry) await retry();
      } }, "Trust and continue"),
      el("button", { class: "btn", onclick: closeSheet }, "Cancel"))));
}

// ---------------------------------------------------------------- home

function stateRank(s) {
  if (s.terminal) return 3;   // True View open: in use, like a running idle session
  return { awaiting: 0, working: 1, compacting: 1, starting: 2, idle: 3, error: 4, detached: 5 }[s.state] ?? 6;
}

function sortedSessions() {
  return [...S.sessions].sort((a, b) => stateRank(a) - stateRank(b) || b.last_active - a.last_active);
}

async function renderHome() {
  if (S.view?.name !== "home") return;
  setTopbar(menuBtn(), el("div", { class: "tb-spacer" }), tabsBtn());
  const view = $("#view");
  const prevScroll = $(".carousel")?.scrollLeft ?? 0;
  const wrap = el("div", { class: "pad home" });
  wrap.append(el("div", { class: "conn-pill", id: "conn-pill", style: S.wsUp ? "display:none" : "" }, "reconnecting…"));

  // a passkey login starts with no folders — say so instead of showing
  // empty lists that look like a bug
  if (!S.hasRoots) {
    wrap.append(el("div", { class: "card home-empty" },
      el("strong", null, "This device has no folders yet."),
      el("div", null, "Signing in with a passkey proves who you are, "
        + "it does not grant access. On the server run "),
      el("code", null, "c4ai devices set <id> --root <folder>"),
      el("div", { class: "quiet" }, "Your id is in menu → Devices.")));
  }

  if (S.canIncognito) {
    wrap.append(el("button", { class: "btn incognito-btn", onclick: openIncognito },
      sortedSessions().some((s) => s.incognito) ? "◐ Back to incognito chat" : "◐ Incognito chat"));
  }

  // 1 — last session, swipeable
  wrap.append(el("h2", { class: "home-h home-link", onclick: () => nav({ name: "sessions" }) },
    "Last Session", el("span", { class: "home-more" }, " ›")));
  // the incognito chat has its own button above
  const recent = sortedSessions().filter((s) => !s.incognito).slice(0, 10);
  let carousel = null;
  if (recent.length) {
    carousel = el("div", { class: "carousel" }, recent.map((s) => sessionCard(s)));
    wrap.append(carousel);
  } else {
    wrap.append(el("div", { class: "card home-empty" },
      "No sessions yet. Pick a project below to start one."));
  }

  // 2 — projects, VS Code style
  wrap.append(el("h2", { class: "home-h home-link", onclick: () => nav({ name: "projects" }) },
    "Projects", el("span", { class: "home-more" }, " ›")));
  const plist = el("div", { class: "vs-list" }, el("div", { class: "notice" }, "loading…"));
  wrap.append(plist, el("div", { style: "height:40px" }));
  view.replaceChildren(wrap);
  if (carousel) carousel.scrollLeft = prevScroll;
  observeCards();

  try {
    const projects = await loadProjects();
    if (S.view?.name !== "home") return;
    const existing = projects.filter((p) => p.exists);
    const rows = existing.slice(0, 10).map(vsRow);
    if (existing.length > 10) {
      rows.push(el("button", { class: "vs-row", onclick: projectQuickOpen },
        el("span", { class: "vs-name" }, "More…")));
    }
    plist.replaceChildren(...rows);
    if (!existing.length) plist.replaceChildren(el("div", { class: "notice" }, "No projects found."));
  } catch (e) {
    plist.replaceChildren(el("div", { class: "notice error" }, "Could not load projects: " + e.message));
  }
}

// VS Code style recent row: accent name + dimmed full path (the parent alone
// is ambiguous for short names like "1", "2")
function vsRow(p) {
  return el("button", { class: "vs-row", "data-kbd": p.path, onclick: () => nav({ name: "project", path: p.path }) },
    el("span", { class: "vs-name" }, p.name),
    el("span", { class: "vs-path" }, shortPath(p.path)));
}

// quick-open search over all projects (the "More…" behavior)
function projectQuickOpen() {
  const { node, input } = searchableList({
    items: (S.projects || []).filter((p) => p.exists),
    keys: ["name", "path"],
    placeholder: "Search projects…",
    toNode: vsRow,
    empty: "No matching projects.",
  });
  const panel = el("div", { class: "qo-panel" }, node);
  const backdrop = el("div", { class: "qo-backdrop",
    onclick: (e) => { if (e.target === backdrop) closeSheet(); } }, panel);
  closeSheet();
  $("#sheet-root").append(backdrop);
  setTimeout(() => input.focus(), 60);
}

// ---------------------------------------------------------------- sessions list

function renderSessions() {
  if (S.view?.name !== "sessions") return;
  setTopbar(menuBtn(), tbTitle("Sessions"), tabsBtn());
  const view = $("#view");
  const wrap = el("div", { class: "pad" });
  wrap.append(el("div", { class: "conn-pill", id: "conn-pill", style: S.wsUp ? "display:none" : "" }, "reconnecting…"));

  // a passkey login starts with no folders — say so instead of showing
  // empty lists that look like a bug
  if (!S.hasRoots) {
    wrap.append(el("div", { class: "card home-empty" },
      el("strong", null, "This device has no folders yet."),
      el("div", null, "Signing in with a passkey proves who you are, "
        + "it does not grant access. On the server run "),
      el("code", null, "c4ai devices set <id> --root <folder>"),
      el("div", { class: "quiet" }, "Your id is in menu → Devices.")));
  }

  const sorted = sortedSessions();
  const live = sorted.filter((s) => s.live || ["working", "awaiting", "starting", "compacting"].includes(s.state));
  const rest = sorted.filter((s) => !live.includes(s));

  if (!sorted.length) {
    wrap.append(el("div", { class: "empty" },
      el("span", { class: "glyph" }, "❯_"),
      "No sessions yet.", el("br"), "Pick a project to start one."));
  }
  // runners whose folder was deleted or moved can never start again; offer
  // to clear them in one go (the Claude transcripts on disk are kept)
  const missing = sorted.filter((s) => s.cwd_missing);
  if (missing.length) {
    wrap.append(el("button", { class: "btn danger small", style: "margin-bottom:12px",
      onclick: () => confirmSheet(`Delete ${missing.length} session${missing.length > 1 ? "s" : ""} with missing folders?`,
        "Their folders no longer exist, so they cannot be resumed here. This removes them "
        + "from Clicker4AI; the Claude Code transcripts on disk are kept.",
        async () => {
          // one failure (a dropped connection) must not stop the rest;
          // what is left stays listed, so the button can be pressed again
          let done = 0, err = null;
          for (const s of missing) {
            try {
              await T.rpc("sessions.delete", { sid: s.sid });
            } catch (e) { err = err || e; continue; }
            delete S.events[s.sid]; S.subs.delete(s.sid); closeTab(s.sid);
            done++;
          }
          if (err) toast(`Deleted ${done} of ${missing.length} — ${err.message}`, true);
          else toast("Deleted " + done);
        }) },
      `Delete ${missing.length} session${missing.length > 1 ? "s" : ""} with missing folders`));
  }
  if (live.length) {
    wrap.append(el("div", { class: "eyebrow" }, "active"));
    live.forEach((s) => wrap.append(sessionCard(s)));
  }
  if (rest.length) {
    wrap.append(el("div", { class: "eyebrow" }, "recent"));
    rest.forEach((s) => wrap.append(sessionCard(s)));
  }
  wrap.append(el("div", { style: "height:90px" }));
  view.replaceChildren(wrap);
  view.append(el("button", { class: "fab", onclick: () => nav({ name: "projects" }) }, "+"));
  observeCards();
}

function sessionCard(s) {
  const st = S.status[s.sid] || { state: s.state, detail: s.detail };
  const busy = ["working", "awaiting", "starting", "compacting"].includes(st.state);

  // body: live ticker while busy, otherwise the last exchanged message
  let body;
  if (s.incognito) {
    body = el("div", { class: "ticker quiet" },
      el("span", { class: "caret" }, "❯"),
      el("span", { class: "t-text" }, incognitoPreview(st, busy)));
  } else if (busy) {
    body = el("div", { class: "ticker" },
      el("span", { class: "caret" }, "❯"),
      el("span", { class: "t-text" }, st.detail || st.state));
  } else if (s.last_msg?.text) {
    body = el("div", { class: "last-msg " + s.last_msg.role },
      el("span", { class: "lm-who" }, s.last_msg.role === "user" ? "you" : "claude"),
      el("span", { class: "lm-text" }, s.last_msg.text));
  } else {
    body = el("div", { class: "ticker quiet" },
      el("span", { class: "caret" }, "❯"),
      el("span", { class: "t-text" }, st.detail || "no messages yet"));
  }

  const metaBits = [procLabel(s, st), ...sizeBits(s, true)];
  metaBits.push(el("span", null, relTime(s.last_active)));

  return el("button", { class: "card s-card" + (tabAwaiting(s.sid) ? " alert" : ""),
    "data-kbd": s.sid, "data-sid": s.sid, onclick: () => openSession(s) },
    el("div", { class: "row1" },
      el("span", { class: "dot " + st.state }),
      el("span", { class: "title" }, s.title || "(new session)"),
      el("span", { class: "menu-btn", onclick: (e) => { e.stopPropagation(); sessionMenu(s); } }, "⋯")),
    el("div", { class: "proj" }, s.cwd_short || s.project, missingMark(s),
      s.pending_permissions ? "  ·  ⚠ needs approval" : null),
    body,
    el("div", { class: "meta-row" }, metaBits)
  );
}

// What a card shows instead of an incognito chat's content: its state only,
// never the last message or the running tool's detail (a search query), so
// the switcher and the session list can be opened in company
function incognitoPreview(st, busy) {
  return busy ? st.state + " — content hidden" : "incognito — content hidden";
}

function missingMark(s) {
  return s.cwd_missing ? el("span", { class: "missing" }, "  ·  folder missing") : null;
}

// Model, (cost) and context size for session and tab cards. The size is
// shown for stopped sessions too: it is what a resume has to load into the
// prompt cache again.
function sizeBits(s, withCost) {
  const bits = [];
  if (s.model && s.model !== "default") bits.push(el("span", null, shortModel(s.model)));
  if (withCost && s.cost_usd) bits.push(el("span", null, fmtCost(s.cost_usd)));
  const size = fmtTokens(s.context_tokens);
  if (s.context_pct != null) bits.push(el("span", null, `ctx ${Math.round(s.context_pct)}%` + (size ? ` · ${size}` : "")));
  else if (size) bits.push(el("span", null, `ctx ${size}`));
  return bits;
}

function shortModel(m) {
  if (!m || m === "default") return "default";
  return m.replace(/^claude-/, "").replace(/-\d{8}$/, "");
}

// Stop the session's claude process (it resumes on the next message); when
// stopped from inside the chat, return to the session list. The tab stays.
async function stopSession(sid) {
  await T.rpc("sessions.stop", { sid });
  toast("Stopped");
  if (S.view?.name === "chat" && S.view.sid === sid) nav({ name: "sessions" });
}

// The title is the first line ever sent, so renaming is the only way to
// label a session by what it turned into. The field starts with the current
// name, cursor at the end, so it can be amended rather than retyped.
function renameSession(s) {
  const old = s.title && s.title !== "(new session)" ? s.title : "";
  const input = el("input", { type: "text", value: old, maxlength: 64,
    autocapitalize: "sentences", enterkeyhint: "done" });
  const save = () => { closeSheet(); saveSessionName(s, input.value); };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); save(); } });
  rawSheet(el("div", null,
    el("div", { class: "sheet-title" }, "Session name"),
    input,
    el("div", { class: "a-buttons" },
      el("button", { class: "btn primary", onclick: save }, "Save"),
      el("button", { class: "btn", onclick: closeSheet }, "Cancel"))));
  input.focus();
  input.setSelectionRange(old.length, old.length);
}

async function saveSessionName(s, next) {
  const title = next.trim();
  if (!title || title === s.title) return;
  try {
    await T.rpc("sessions.rename", { sid: s.sid, title });
    upsertSession({ ...s, title });
    toast("Renamed");
    // the server's own sessions push follows; this just avoids the lag
    if (S.view?.name === "chat") updateChatHeader(); else render();
  } catch (e) { toast(e.message, true); }
}

// "Resume as fork" of the project screen, for a transcript that is already a
// session: a new session branching off its current conversation
// A fork's title: one " (fork)" however deep, and it fits the server's 64
function forkTitle(title) {
  return title.trim().replace(/( \(fork\))+$/, "").slice(0, 57).trimEnd() + " (fork)";
}

function forkItem(s) {
  if (s.incognito || !s.claude_session_id) return null;
  return { icon: "⑂", label: "Fork", sub: "New branch — this session untouched",
    action: () => startSession(s.cwd, { model: "default", mode: "default",
      resume: s.claude_session_id, fork: true, title: forkTitle(s.title || "(session)") }) };
}

function sessionMenu(s) {
  sheet(s.title || "(session)", [
    { icon: "▶", label: "Open chat", action: () => openChat(s.sid) },
    !s.incognito && { icon: "✎", label: "Rename", action: () => renameSession(s) },
    forkItem(s),
    { icon: "■", label: "Stop process", sub: "Keeps history; resumes on next message",
      action: () => stopSession(s.sid) },
    S.canFiles && !s.incognito && { icon: "▤", label: "Files", sub: shortPath(s.cwd),
      action: () => nav({ name: "files", path: s.cwd }) },
    deleteItem(s),
  ]);
}

// ---------------------------------------------------------------- tabs view

const TABS_CLOSE_DELAY = 800;   // ms

function renderTabs() {
  if (S.view?.name !== "tabs") return;
  pruneTabs();
  const bar = [
    menuBtn(), backBtn(),
    tbTitle(S.tabs.length ? `Tabs · ${S.tabs.length}` : "Tabs"),
  ];
  // "close all" wakes up a moment after Tabs opens: a double tap on the
  // Tabs button must not land on it
  if (S.tabsView !== S.view) { S.tabsView = S.view; S.tabsSince = Date.now(); }
  if (S.tabs.length) {
    const wait = S.tabsSince + TABS_CLOSE_DELAY - Date.now();
    const btn = el("button", { class: "tb-btn tb-text", onclick: closeIdleTabs }, "close all");
    if (wait > 0) { btn.disabled = true; setTimeout(() => { btn.disabled = false; }, wait); }
    bar.push(btn);
  }
  setTopbar(...bar);

  const view = $("#view");
  const wrap = el("div", { class: "pad" });
  // S.tabs ends with the most recently entered tab; show it first
  const open = S.tabs.map((sid) => S.sessions.find((s) => s.sid === sid)).filter(Boolean).reverse();
  if (open.length) {
    wrap.append(el("div", { class: "tabs-grid" }, open.map(tabCard)));
  } else {
    wrap.append(el("div", { class: "empty" },
      el("span", { class: "glyph" }, "▦"),
      "No open tabs.", el("br"), "Tap + to start a session."));
  }
  wrap.append(el("div", { style: "height:90px" }));
  view.replaceChildren(wrap);
  view.append(el("button", { class: "fab", onclick: () => nav({ name: "projects" }) }, "+"));
}

// Process state label for session/tab cards: working (turn in progress),
// running (process alive, waiting), stopped (resumes on the next message).
// An open True View stays named while its turn runs: "true view · working".
function procLabel(s, st) {
  const tv = s.terminal ? "true view · " : "";
  if (["working", "awaiting", "starting", "compacting"].includes(st.state))
    return el("span", { class: "active" }, tv + "working");
  if (st.state === "error") return el("span", { class: "error" }, tv + "error");
  if (s.terminal) return el("span", { class: "running" }, "true view");
  return s.live ? el("span", { class: "running" }, "running")
    : el("span", { class: "stopped" }, "stopped");
}

function tabCard(s) {
  const st = S.status[s.sid] || { state: s.state, detail: s.detail };
  const busy = ["working", "awaiting", "starting", "compacting"].includes(st.state);
  const body = s.incognito ? incognitoPreview(st, busy)
    : busy ? (st.detail || st.state) : (s.last_msg?.text || "no messages yet");
  return el("button", { class: "tab-card" + (s.sid === S.lastTab ? " on" : ""),
    "data-kbd": s.sid, onclick: () => openSession(s) },
    el("div", { class: "tc-head" },
      el("span", { class: "dot " + st.state }),
      el("span", { class: "tc-title" }, s.title || "(new session)"),
      el("span", { class: "tc-menu", role: "button", "aria-label": "Tab actions",
        onclick: (e) => { e.stopPropagation(); tabMenu(s); } }, "⋯"),
      el("span", { class: "tc-close", role: "button", "aria-label": "Close tab",
        onclick: (e) => { e.stopPropagation(); confirmCloseTab(s); } }, "✕")),
    el("div", { class: "tc-proj" }, s.cwd_short || s.project, missingMark(s)),
    el("div", { class: "tc-body" + (busy ? " busy" : "") }, body),
    el("div", { class: "tc-meta" },
      procLabel(s, st),
      ...sizeBits(s, false),
      el("span", null, relTime(s.last_active))));
}

// A busy tab is one whose turn is still running or waiting for an approval:
// closed, it drops out of the Tabs pulse and is easy to forget
function tabBusy(sid) {
  return ["working", "awaiting", "starting", "compacting"].includes(S.status[sid]?.state);
}

// "close all" keeps busy tabs and says how many stayed
function closeIdleTabs() {
  const idle = S.tabs.filter((sid) => !tabBusy(sid));
  const kept = S.tabs.length - idle.length;
  const keptText = `${kept} busy tab${kept > 1 ? "s" : ""}`;
  if (!idle.length) { toast(`All tabs are busy — ${keptText} kept`); return; }
  confirmSheet(kept ? `Close ${idle.length} idle tab${idle.length > 1 ? "s" : ""}?` : "Close all tabs?",
    (kept ? `${keptText} (working or awaiting approval) stay${kept > 1 ? "" : "s"} open. ` : "")
    + "Sessions keep running — this only empties the tab switcher.",
    () => {
      for (const sid of idle) closeTab(sid);
      renderTabs();
      if (kept) toast(`Closed ${idle.length} · ${keptText} kept`);
    });
}

// Closing a tab looks destructive but only forgets the session here, so say
// what it does and does not touch before the card disappears; a busy one
// gets a warning and a red button. fromMenu: an idle tab closes at once.
function confirmCloseTab(s, fromMenu) {
  const done = () => { closeTab(s.sid); renderTabs(); };
  const keep = "The session and its process are untouched — reopen it from Sessions.";
  if (tabBusy(s.sid)) {
    const what = S.status[s.sid].state === "awaiting" ? "is waiting for your approval" : "is still working";
    confirmSheet(`“${s.title || "session"}” ${what}`,
      "Closing the tab hides it from the tab switcher and the Tabs alert. " + keep,
      done, "Close anyway");
  } else if (fromMenu) {
    done();
  } else {
    confirmSheet(`Close “${s.title || "session"}”?`,
      "This removes the session from the tab switcher only. " + keep, done);
  }
}

// Same actions as the session list's ⋯ menu, so a process can be stopped from
// the tab switcher without opening the chat.
function tabMenu(s) {
  sheet(s.title || "(session)", [
    { icon: "▶", label: "Open chat", action: () => openChat(s.sid) },
    !s.incognito && { icon: "✎", label: "Rename", action: () => renameSession(s) },
    forkItem(s),
    s.live && { icon: "■", label: "Stop process", sub: "Keeps history; resumes on next message",
      action: async () => { await stopSession(s.sid); renderTabs(); } },
    S.canFiles && !s.incognito && { icon: "▤", label: "Files", sub: shortPath(s.cwd),
      action: () => nav({ name: "files", path: s.cwd }) },
    { icon: "✕", label: "Close tab", sub: "Keeps the session; removes it from this switcher",
      action: () => confirmCloseTab(s, true) },
    deleteItem(s, renderTabs),
  ]);
}

// ---------------------------------------------------------------- projects list

async function renderProjects() {
  if (S.view?.name !== "projects") return;
  setTopbar(menuBtn(), tbTitle("Projects"), tabsBtn());
  const view = $("#view");
  view.replaceChildren(el("div", { class: "pad" }, el("div", { class: "empty" }, "Loading projects…")));
  let projects;
  try {
    projects = await loadProjects();
  } catch (e) { view.replaceChildren(el("div", { class: "pad" }, el("div", { class: "empty" }, "Could not load projects: " + e.message))); return; }
  if (S.view?.name !== "projects") return;

  const { node } = searchableList({
    items: projects.filter((p) => p.exists),
    keys: ["name", "path"],
    placeholder: "Search projects…",
    empty: "No matching projects.",
    toNode: (p) => bigItem({
      icon: "❯",
      title: p.name,
      sub: shortPath(p.path),
      side: [relTime(p.last_active_ms), p.session_count ? p.session_count + " sessions" : ""],
      onclick: () => nav({ name: "project", path: p.path }),
      key: p.path,
    }),
  });
  view.replaceChildren(el("div", { class: "pad" },
    bigItem({ icon: "▸", title: "Browse folders…", sub: "start a session in any directory",
      onclick: () => browseSheet((path) => nav({ name: "project", path })) }),
    node));
}

async function browseSheet(pick) {
  let current = null;
  const body = el("div");
  const load = async (path) => {
    const data = await T.rpc("browse", path ? { path } : {});
    current = data.path; // null = list of allowed folders (several roots)
    // parent: path, "" = back to the allowed-folders list, null = top
    const up = data.parent != null;
    // replaceChildren, unlike el(), turns null into the text "null"
    body.replaceChildren(...[
      el("div", { class: "sheet-title", style: "word-break:break-all" },
        current ? current.replace(S.home, "~") : "Allowed folders"),
      el("div", { class: "a-buttons", style: "margin-bottom:10px" },
        up ? el("button", { class: "btn small", onclick: () => load(data.parent) }, "‹ up") : null,
        current ? el("button", { class: "btn small primary", onclick: () => { closeSheet(); pick(current); } }, "Use this folder") : null,
        current ? el("button", { class: "btn small", onclick: () => newFolder(current) }, "+ New folder") : null),
      data.dirs.length ? null : el("div", { class: "empty" }, "No folders available."),
      ...data.dirs.map((d) => el("button", { class: "sheet-item", onclick: () => load(d.path) },
        el("span", { class: "si-icon" }, d.is_project ? "❯" : "▸"),
        el("div", { class: "si-main" }, d.name))),
    ].filter(Boolean));
  };
  // Created folders open at once: from there "Use this folder" or another
  // level down. The name sheet replaces this one, so put it back afterwards.
  const newFolder = async (parent) => {
    const name = await textSheet("New folder name", { plain: true, maxlength: 255 });
    rawSheet(body);
    if (!name) return;
    try {
      const r = await T.rpc("mkdir", { parent, name });
      await load(r.path);
    } catch (e) {
      toast(e.message || "Could not create the folder", true);
    }
  };
  rawSheet(body);
  await load(null);
}

// ---------------------------------------------------------------- project screen

async function renderProject(path) {
  setTopbar(
    menuBtn(), backBtn(),
    el("div", { class: "tb-title" },
      el("span", { class: "crumb" }, path.split("/").pop())),
    tabsBtn()
  );
  const view = $("#view");
  const opts = { model: "default", mode: "default" };

  const modelRow = el("div", { class: "a-buttons", style: "margin-bottom:14px" });
  const renderModels = () => {
    modelRow.replaceChildren(...S.defaults.models.map((m) =>
      el("button", { class: "btn small" + (opts.model === m ? " primary" : ""),
        onclick: () => { opts.model = m; renderModels(); } }, m)));
  };
  renderModels();

  const modeRow = el("div", { class: "a-buttons", style: "margin-bottom:16px" });
  const renderModes = () => {
    modeRow.replaceChildren(...["default", "acceptEdits", "plan"].map((m) =>
      el("button", { class: "btn small" + (opts.mode === m ? " primary" : ""),
        onclick: () => { opts.mode = m; renderModes(); } }, m === "acceptEdits" ? "accept edits" : m)));
  };
  renderModes();

  const wrap = el("div", { class: "pad" },
    el("div", { class: "card" },
      el("div", { class: "eyebrow", style: "margin-top:0" }, "model"), modelRow,
      el("div", { class: "eyebrow" }, "permissions"), modeRow,
      el("button", { class: "btn primary", style: "width:100%; padding:13px",
        onclick: () => startSession(path, opts) }, "Start new session")),
    S.canFiles ? bigItem({ icon: "▤", title: "Files", sub: "browse, view and download"
      + (S.canUpload ? ", upload" : ""), onclick: () => nav({ name: "files", path }) }) : null,
    el("div", { class: "eyebrow" }, "resume a past session"),
    el("div", { id: "past-list" }, el("div", { class: "notice" }, "loading…")));
  view.replaceChildren(wrap);

  try {
    const { sessions } = await T.rpc("projects.sessions", { cwd: path });
    const list = $("#past-list");
    if (!list) return;
    list.replaceChildren();
    if (!sessions.length) list.append(el("div", { class: "notice" }, "No past sessions in this project."));
    for (const ps of sessions) {
      // already a session here: open it as the session list does (fork and
      // rename are in its ⋯ menu), rather than offering to resume it again
      const held = ps.runner_sid;
      list.append(bigItem({
        title: ps.summary,
        badge: held ? el("span", { class: "badge" }, "in sessions") : null,
        sub: [ps.git_branch, pastSize(ps), (ps.session_id || "").slice(0, 8)].filter(Boolean).join(" · "),
        side: relTime(ps.last_modified_ms),
        onclick: () => {
          if (!held) { pastSessionSheet(path, ps, opts); return; }
          const s = S.sessions.find((x) => x.sid === held);
          if (s) openSession(s); else openChat(held);
        },
        key: ps.session_id,
      }));
    }
  } catch (e) {
    const list = $("#past-list");
    if (list) list.replaceChildren(el("div", { class: "notice error" }, "Could not list sessions: " + e.message));
  }
}

// ---------------------------------------------------------------- files screen
// Plain file access, no Claude (clicker4ai/files.py). Going into a folder
// replaces this screen in history, so ‹ leaves the files, not one level.

function fmtBytes(n) {
  if (n == null) return "";
  if (n < 1024) return n + " B";
  const u = ["KB", "MB", "GB"];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return (n < 10 ? n.toFixed(1) : Math.round(n)) + " " + u[i];
}

const fileUrl = (path, download) =>
  "/api/files/raw?path=" + encodeURIComponent(path) + (download ? "&download=1" : "");

function filesGo(path) {
  S.view = { name: "files", path: path || "" };
  histReplace(S.view);
  renderFiles(S.view.path);
}

async function renderFiles(path) {
  const view = $("#view");
  const title = (p) => p ? p.split("/").pop() || "/" : "Files";
  setTopbar(menuBtn(), backBtn(), tbTitle(title(path)), tabsBtn());
  if (!S.canFiles) {
    view.replaceChildren(el("div", { class: "pad" }, el("div", { class: "empty" },
      "This device may not browse files. On the server: ", el("code", null, "c4ai devices set <device> --files"), ".")));
    return;
  }
  view.replaceChildren(el("div", { class: "pad" }, el("div", { class: "empty" }, "Loading…")));
  let data;
  try {
    data = await T.rpc("files.list", path ? { path } : {});
  } catch (e) {
    if (S.view?.name !== "files" || S.view.path !== path) return;
    view.replaceChildren(el("div", { class: "pad" }, el("div", { class: "empty" }, "Could not list files: " + e.message)));
    return;
  }
  if (S.view?.name !== "files" || S.view.path !== path) return;
  const dir = data.path;
  if (dir !== path && dir) { S.view.path = dir; histReplace(S.view); setTopbar(menuBtn(), backBtn(), tbTitle(title(dir)), tabsBtn()); }
  const { node } = searchableList({
    items: data.entries,
    keys: ["name"],
    placeholder: "Filter…",
    empty: data.entries.length ? "No matching files." : "Empty folder.",
    toNode: (f) => bigItem({
      icon: f.dir ? "▸" : f.image ? "◩" : "▤",
      title: f.name,
      mono: true,
      side: f.dir ? null : [fmtBytes(f.size), relTime(f.mtime)],
      onclick: () => f.dir ? filesGo(f.path) : fileSheet(f, data.entries.filter((x) => !x.dir)),
      key: f.path,
    }),
  });
  view.replaceChildren(el("div", { class: "pad" },
    dir ? el("div", { class: "notice", style: "margin-top:0" }, shortPath(dir)) : null,
    el("div", { class: "a-buttons", style: "margin-bottom:10px" },
      data.parent != null ? el("button", { class: "btn small", onclick: () => filesGo(data.parent) }, "‹ up") : null,
      dir && S.canUpload ? el("button", { class: "btn small", onclick: () => pickUpload(dir) }, "Upload…") : null),
    node));
}

// A file on a sheet: images as <img>, text as plain text, anything else
// just its size; every one can be downloaded.
// Keys: ↑ ↓ j k scroll, space / PgUp / PgDn a page, ← → the previous or
// next file of the folder, d downloads, ⏎ and esc close. Close holds the
// focus, so a stray ⏎ never starts a download.
// A file named in a tool card (Read, Edit, Write…), in the file viewer with
// its folder's files for ← →; the server checks it is in the device's folders.
async function openFileAt(path, cwd) {
  if (!path.startsWith("/") && cwd) path = cwd.replace(/\/$/, "") + "/" + path;
  const cut = path.lastIndexOf("/");
  const name = path.slice(cut + 1);
  let data;
  try {
    data = await T.rpc("files.list", { path: path.slice(0, cut) || "/" });
  } catch (e) { toast(e.message || "Could not open the file", true); return; }
  const files = data.entries.filter((x) => !x.dir);
  const f = files.find((x) => x.name === name);
  if (!f) { toast("No such file any more — moved or deleted", true); return; }
  fileSheet(f, files);
}

function openFiles(sid) {
  const s = S.sessions.find((x) => x.sid === sid);
  if (s?.cwd) nav({ name: "files", path: s.cwd });
}

// An image at most 1:1 with the screen's pixels (a 1179 px iPhone
// screenshot is 590 CSS px on a 2x iPad, not the whole width) and whole on
// the sheet; a tap switches to 1:1 with scrolling and back. SVGs have no
// pixels of their own and keep their CSS size.
function imageView(f, body) {
  const img = el("img", { class: "file-img", src: fileUrl(f.path), alt: f.name });
  const wrap = el("div", { class: "file-img-wrap" }, img);
  const info = el("div", { class: "notice" }, "loading…");
  let fitW = 0, fullW = 0, fit = true;
  const apply = () => {
    img.style.width = (fit ? fitW : fullW) + "px";
    wrap.classList.toggle("full", !fit);
    wrap.classList.toggle("zoomable", fitW < fullW);
    info.textContent = `${img.naturalWidth}×${img.naturalHeight} · `
      + (fitW >= fullW ? "1:1" : fit ? "fitted — tap for 1:1" : "1:1 — tap to fit");
  };
  img.addEventListener("load", () => {
    if (!img.naturalWidth) { info.textContent = ""; return; }
    const dpr = /\.svg$/i.test(f.name) ? 1 : (window.devicePixelRatio || 1);
    fullW = img.naturalWidth / dpr;
    const fullH = img.naturalHeight / dpr;
    img.style.width = "0px";   // measure the sheet without the image
    const panel = body.closest(".sheet");
    const availH = (panel ? window.innerHeight * 0.82 - panel.offsetHeight : fullH) - 8;
    const s = Math.min(1, wrap.clientWidth / fullW, Math.max(availH, 80) / fullH);
    fitW = Math.max(1, Math.floor(fullW * s));
    apply();
  });
  img.addEventListener("error", () => { info.textContent = "Could not load the image."; });
  img.addEventListener("click", () => { if (fitW < fullW) { fit = !fit; apply(); } });
  return [wrap, info];
}

async function fileSheet(f, siblings = []) {
  const body = el("div", { class: "sheet-body" });
  const dl = el("a", { class: "btn", href: fileUrl(f.path, true), download: f.name }, "Download", kHint("d"));
  const i = siblings.findIndex((x) => x.path === f.path);
  const head = [
    el("div", { class: "sheet-title", style: "word-break:break-all" }, f.name),
    el("div", { class: "notice", style: "margin-top:0" }, `${fmtBytes(f.size)} · ${relTime(f.mtime)}`
      + (i >= 0 && siblings.length > 1 ? ` · ${i + 1} of ${siblings.length}` : "")),
  ];
  const show = (...nodes) => body.replaceChildren(...head, ...nodes.filter(Boolean));
  const close = el("button", { class: "btn primary", onclick: closeSheet }, "Close", kHint("⏎"));
  const foot = el("div", { class: "a-buttons" }, dl, close);
  show(...(f.image ? imageView(f, body) : [el("div", { class: "notice" }, "loading…")]));
  // ← → to a sibling replaces the sheet but keeps the viewer's history entry
  const was = S.viewerOpen;
  S.viewerOpen = false;
  rawSheet(body, foot);
  if (was) S.viewerOpen = true;
  else viewerHistory();
  document.activeElement?.blur();
  kbd.cur = close;
  if (finePointer()) close.focus({ preventScroll: true });
  const step = (d) => { const n = siblings[i + d]; if (i >= 0 && n) fileSheet(n, siblings); };
  const page = () => Math.max(40, body.clientHeight - 40);
  S.sheetKeys = (key) => {
    const act = {
      ArrowDown: () => body.scrollBy(0, 48), j: () => body.scrollBy(0, 48),
      ArrowUp: () => body.scrollBy(0, -48), k: () => body.scrollBy(0, -48),
      " ": () => body.scrollBy(0, page()), PageDown: () => body.scrollBy(0, page()),
      PageUp: () => body.scrollBy(0, -page()),
      Home: () => body.scrollTo(0, 0), End: () => body.scrollTo(0, body.scrollHeight),
      ArrowRight: () => step(1), ArrowLeft: () => step(-1),
      d: () => dl.click(), Enter: closeSheet,
    }[key];
    if (act) act();
    return !!act;
  };
  if (f.image) return;
  try {
    const r = await T.rpc("files.text", { path: f.path });
    if (!body.isConnected) return;
    if (r.binary) { show(el("div", { class: "notice" }, "Not a text file — download it to open.")); return; }
    show(el("pre", { class: "file-text" }, r.text),
      r.truncated ? el("div", { class: "notice warn" }, `First ${fmtBytes(r.text.length)} only — download for the rest.`) : null);
  } catch (e) {
    if (body.isConnected) show(el("div", { class: "notice error" }, e.message));
  }
}

function pickUpload(dir) {
  const input = el("input", { type: "file", style: "display:none" });
  input.addEventListener("change", () => {
    const file = input.files?.[0];
    input.remove();
    if (file) uploadFile(dir, file, file.name);
  });
  document.body.append(input);
  input.click();
}

// An existing name is never overwritten: the server says 409 before
// reading the body, and the file goes up again under a new name or not at all
async function uploadFile(dir, file, name) {
  const q = `?dir=${encodeURIComponent(dir)}&name=${encodeURIComponent(name)}`;
  toast(`Uploading ${name}…`);
  let res;
  try {
    res = await fetch("/api/files/upload" + q, { method: "PUT", body: file,
      headers: { "Content-Type": "application/octet-stream", "X-C4AI-Upload": "1" } });
  } catch (e) { toast("Upload failed: " + e.message, true); return; }
  if (res.status === 409) {
    const next = await textSheet(`"${name}" already exists — new name`, { value: name, plain: true, maxlength: 255 });
    if (next && next !== name) await uploadFile(dir, file, next);
    else if (next === name) toast("That name is taken", true);
    return;
  }
  if (res.status === 423) { showLock(); return; }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    toast("Upload failed: " + detail, true);
    return;
  }
  toast(`Uploaded ${name}`);
  if (S.view?.name === "files" && S.view.path === dir) renderFiles(dir);
}

// "84k · opus-5": what resuming loads into the prompt cache again, and the
// model it was on (the same tokens cost several times more on Opus)
function pastSize(ps) {
  if (!ps.context_tokens) return "";
  return `ctx ${fmtTokens(ps.context_tokens)}` + (ps.model ? " · " + shortModel(ps.model) : "");
}

// Name a past session without resuming it: the server appends the title to
// its transcript, as /rename in the terminal does
function renamePastSession(path, ps) {
  const old = ps.summary && ps.summary !== "(untitled)" ? ps.summary : "";
  const input = el("input", { type: "text", value: old, maxlength: 64,
    autocapitalize: "sentences", enterkeyhint: "done" });
  const save = async () => {
    closeSheet();
    const title = input.value.trim();
    if (!title || title === ps.summary) return;
    try {
      await T.rpc("projects.rename", { session_id: ps.session_id, cwd: path, title });
      toast("Renamed");
      if (S.view?.name === "project" && S.view.path === path) renderProject(path);
    } catch (e) { toast(e.message, true); }
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); save(); } });
  rawSheet(el("div", null,
    el("div", { class: "sheet-title" }, "Session name"),
    input,
    el("div", { class: "a-buttons" },
      el("button", { class: "btn primary", onclick: save }, "Save"),
      el("button", { class: "btn", onclick: closeSheet }, "Cancel"))));
  input.focus();
  input.setSelectionRange(old.length, old.length);
}

function pastSessionSheet(path, ps, opts) {
  const body = el("div");
  body.append(
    el("div", { class: "sheet-title" }, ps.summary),
    el("div", { style: "font-family:var(--mono);font-size:11px;color:var(--dim);margin:-6px 0 8px" },
      [ps.cwd_short, pastSize(ps)].filter(Boolean).join(" · ")),
    el("button", { class: "sheet-item", onclick: async () => {
      closeSheet(); await startSession(path, { ...opts, resume: ps.session_id, title: ps.summary });
    } }, el("span", { class: "si-icon" }, "▶"), el("div", { class: "si-main" }, "Resume",
      el("div", { class: "si-sub" }, "continue this conversation"))),
    el("button", { class: "sheet-item", onclick: async () => {
      closeSheet(); await startSession(path, { ...opts, resume: ps.session_id, fork: true, title: forkTitle(ps.summary || "(session)") });
    } }, el("span", { class: "si-icon" }, "⑂"), el("div", { class: "si-main" }, "Resume as fork",
      el("div", { class: "si-sub" }, "new branch — original session untouched"))),
    el("button", { class: "sheet-item", onclick: () => renamePastSession(path, ps) },
      el("span", { class: "si-icon" }, "✎"), el("div", { class: "si-main" }, "Rename",
      el("div", { class: "si-sub" }, "without resuming — nothing is loaded"))),
    el("div", { id: "peek", class: "sheet-body", style: "margin-top:10px" },
      el("div", { class: "notice" }, "loading preview…"))
  );
  rawSheet(body);
  T.rpc("projects.preview", { session_id: ps.session_id, cwd: path })
    .then(({ messages }) => {
      const peek = $("#peek");
      if (!peek) return;
      peek.replaceChildren(...(messages.length ? messages : []).map((m) =>
        el("div", { class: "notice", style: m.role === "user" ? "color:var(--text)" : "" },
          (m.role === "user" ? "you: " : "claude: ") + m.text.slice(0, 200))));
      if (!messages.length) peek.replaceChildren(el("div", { class: "notice" }, "(no preview available)"));
    }).catch(() => {});
}

async function startSession(cwd, opts) {
  try {
    const params = {
      cwd, model: opts.model, mode: opts.mode,
      resume: opts.resume || null, fork: !!opts.fork, title: opts.title || "",
    };
    const started = (snap) => { upsertSession(snap); openChat(snap.sid); };
    try {
      started(await T.rpc("sessions.create", params));
    } catch (e) {
      if (e.code === "untrusted") { trustSheet(e, () => startSession(cwd, opts)); return; }
      if (e.code !== "runner_limit") throw e;
      // offer to stop one, then start this session for real
      runnerLimitSheet(e, async () => started(await T.rpc("sessions.create", params)));
    }
  } catch (e) { toast("Start failed: " + e.message, true); }
}

// This device's incognito chat: the one it has, else a new one. The server
// keeps it to one per device and erases it after 24 h idle (sessions.py).
function openIncognito() {
  const mine = S.sessions.find((s) => s.incognito);
  if (mine) { openChat(mine.sid); return; }
  const models = S.defaults?.models || ["default"];
  // a web question rarely needs more: the cheapest model is the suggestion
  const cheap = models.find((m) => /haiku/i.test(m)) || "default";
  const start = async (model) => {
    closeSheet();
    const started = (snap) => { upsertSession(snap); openChat(snap.sid); };
    try {
      try {
        started(await T.rpc("sessions.incognito", { model }));
      } catch (e) {
        if (e.code !== "runner_limit") throw e;
        runnerLimitSheet(e, async () => started(await T.rpc("sessions.incognito", { model })));
      }
    } catch (e) { toast("Start failed: " + e.message, true); }
  };
  rawSheet(el("div", null,
    el("div", { class: "sheet-title" }, "Incognito chat"),
    el("div", { class: "a-desc", style: "margin-bottom:14px" },
      "Web search and fetch only — no files, no shell, no MCP, no True View. "
      + "Erased with its transcript when you end it, sign out, or after 24 h without a message."),
    el("div", { class: "eyebrow", style: "margin-top:0" }, "model"),
    el("div", { class: "a-buttons" }, models.map((m) =>
      el("button", { class: "btn small" + (m === cheap ? " primary" : ""), onclick: () => start(m) }, m)))));
}

// Delete, or for an incognito chat End & erase, which takes its transcripts
// and folder with it (SessionManager.delete on the server)
function deleteItem(s, after) {
  const inc = !!s.incognito;
  return { icon: "✕", label: inc ? "End & erase" : "Delete session", danger: true,
    sub: inc ? "Deletes the conversation from the server for good" : null,
    action: () => confirmSheet(
      inc ? "End and erase this incognito chat?" : `Delete “${s.title || "session"}”?`,
      inc ? "The conversation, its transcript and its folder are deleted. This cannot be undone."
        : "This removes it from Clicker4AI. The Claude Code transcript on disk is kept.",
      async () => {
        await T.rpc("sessions.delete", { sid: s.sid });
        delete S.events[s.sid]; S.subs.delete(s.sid); closeTab(s.sid);
        toast(inc ? "Erased" : "Deleted");
        if (S.view?.name === "chat" && S.view.sid === s.sid) nav({ name: "home" });
        after?.();
      }) };
}

// ---------------------------------------------------------------- chat

function enterTab(sid) {
  // most recently entered last; the Tabs view shows it first
  S.tabs = S.tabs.filter((t) => t !== sid);
  S.tabs.push(sid);
  S.lastTab = sid;
  saveTabs();
}

function openChat(sid) {
  enterTab(sid);
  nav({ name: "chat", sid });
}

// A card opens where the session is being driven: its True View while a
// terminal is open, the chat otherwise ("Open chat" in the ⋯ menu)
function openSession(s) {
  if (s.terminal && S.canTerminal && !s.incognito) openTrueView(s.sid);
  else openChat(s.sid);
}

function currentSession() {
  return S.sessions.find((s) => s.sid === S.view?.sid);
}

function renderChat(sid) {
  const s = S.sessions.find((x) => x.sid === sid);
  chatUI.sid = sid;
  chatUI.byTool = {}; chatUI.byReq = {}; chatUI.provText = []; chatUI.provThink = [];

  setTopbar(
    menuBtn(), backBtn(),
    el("div", { class: "tb-title", onclick: () => infoSheet(sid), style: "cursor:pointer" },
      el("span", { class: "dot " + ((S.status[sid] || {}).state || "detached"), id: "hdr-dot" }),
      el("div", { style: "min-width:0" },
        el("div", { class: "crumb", id: "hdr-title" }, s?.title || "session"),
        el("div", { class: "tb-sub", id: "hdr-sub" }, s?.project || ""))),
    el("div", { class: "tb-chips" },
      // True View only with the device's terminal grant (pair --terminal),
      // never in an incognito chat (the server refuses it too)
      S.canTerminal && !s?.incognito ? el("button", { class: "tb-btn tb-text" + (s?.terminal ? " tv-on" : ""), id: "hdr-tv",
        "aria-label": "True View (terminal)", onclick: () => openTrueView(sid) }, ...tvBtnContent(s)) : null,
      tabsBtn(),
      el("button", { class: "tb-btn", onclick: () => infoSheet(sid) }, "ⓘ"))
  );

  const msgs = el("div", { id: "msgs" });
  chatUI.msgs = msgs;
  const statusLine = el("div", { id: "status-line", class: "quiet" },
    el("span", { class: "caret" }, "❯"), el("span", { class: "s-text" }, ""),
    el("span", { class: "s-usage" }, ""));
  const input = el("textarea", { class: "c-input", id: "chat-input", rows: 1,
    placeholder: "Message Claude…", enterkeyhint: "send" });
  const sendBtn = el("button", { class: "c-btn send", id: "send-btn", onclick: () => sendOrStop() }, "↑");

  input.value = getDraft(sid);
  input.addEventListener("input", () => {
    setDraft(sid, input.value);
    kbd.typedAt = Date.now();
    input.style.height = "auto";
    // border-box: the height includes the border, scrollHeight does not —
    // without it the text is 2 px short and a scrollbar shows at once
    const border = input.offsetHeight - input.clientHeight;
    input.style.height = Math.min(input.scrollHeight + border, 132) + "px";
  });
  input.addEventListener("keydown", (e) => {
    if (homeEnd(input, e)) return;
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey || (finePointer() && !e.shiftKey))) {
      e.preventDefault(); sendOrStop(true);
    }
  });

  const composer = el("div", { id: "composer" },
    el("button", { class: "c-btn", onclick: () => palette(sid) }, "/"),
    input, sendBtn);

  $("#view").replaceChildren(el("div", { id: "chat-wrap" }, msgs, statusLine, composer));
  if (input.value) input.dispatchEvent(new Event("input"));   // a restored draft: size the box

  if (S.events[sid]) renderChatTranscript(sid);
  else msgs.append(el("div", { class: "notice", id: "chat-loading" }, "loading transcript…"));
  subscribe(sid);
  updateChatHeader();
  updateStatusLine();
}

function sendOrStop(fromKeyboard) {
  const sid = chatUI.sid;
  const st = (S.status[sid] || {}).state;
  const input = $("#chat-input");
  const text = input.value.trim();
  if (!text && ["working", "compacting", "starting"].includes(st)) {
    wsSend({ type: "interrupt", session_id: sid });
    return;
  }
  if (!text) return;
  if (handleLocalCommand(sid, text)) { input.value = ""; input.style.height = "auto"; setDraft(sid, ""); return; }
  if (wsSend({ type: "send", session_id: sid, text })) {
    pendingSend[sid] = text;   // dropped as soon as the server accepts it
    input.value = ""; input.style.height = "auto"; setDraft(sid, "");
    if (!fromKeyboard) input.blur();
  }
}

// Text the server has not acknowledged yet, keyed by session. The runner
// limit is the one refusal that arrives after the message left the box,
// so the app can put it back rather than lose what was typed.
const pendingSend = {};

// A refused message goes back into the box if that session is on screen and
// its box is empty. Returns what resendPending needs.
function restorePending(sid) {
  const text = pendingSend[sid];
  delete pendingSend[sid];
  const box = chatUI.sid === sid ? $("#chat-input") : null;
  const restored = !!(text && box && !box.value.trim());
  if (restored) { box.value = text; box.dispatchEvent(new Event("input")); setDraft(sid, text); }
  return { text, restored };
}

// Sends it once the obstacle is gone: the box's text if it was put back
// there (it may have been edited since), else the original.
function resendPending(sid, { text, restored }) {
  const input = restored && chatUI.sid === sid ? $("#chat-input") : null;
  const t = (input && input.value.trim()) || text;
  if (t && wsSend({ type: "send", session_id: sid, text: t })) {
    pendingSend[sid] = t;
    if (input) { input.value = ""; input.style.height = "auto"; setDraft(sid, ""); }
  }
}

// Palette entries an incognito chat leaves out: its mode stays default (plan
// mode would wait for ExitPlanMode, a tool it does not have), its name stays
// "Incognito", and skills, agents and MCP do not exist in it.
const INCOGNITO_HIDDEN = new Set(["mode", "permissions", "rename", "library", "skills", "files"]);
// CLI commands that still make sense there; the rest are skills or need files
// (not /fast: twice the price per token, for a chat that gains little from speed)
const INCOGNITO_CLI = new Set(["effort", "usage", "recap"]);

// slash commands the app itself owns (instant feedback, no guessing)
function handleLocalCommand(sid, text) {
  const m = text.match(/^\/([a-z-]+)\s*(.*)$/i);
  if (!m) return false;
  const [, cmd, rest] = m;
  if (INCOGNITO_HIDDEN.has(cmd.toLowerCase())
      && S.sessions.find((x) => x.sid === sid)?.incognito) {
    toast(`/${cmd} is not available in an incognito chat`, true);
    return true;
  }
  switch (cmd.toLowerCase()) {
    case "clear": confirmClear(sid); return true;
    case "compact": confirmCompact(sid, rest); return true;
    case "btw": btwSheet(sid, rest); return true;
    case "model": modelSheet(sid); return true;
    case "mode": case "permissions": modeSheet(sid); return true;
    case "info": case "context": infoSheet(sid); return true;
    case "term": case "terminal": case "trueview": openTrueView(sid); return true;
    case "library": case "skills": nav({ name: "skills" }); return true;
    // without the grant a project's own /files command still goes through
    case "files": if (S.canFiles) { openFiles(sid); return true; } break;
  }
  return false;
}

// >_ in the chat header, lit (with a dot) while the session's True View
// terminal runs — the chat alone would not show that it is driven there
function tvBtnContent(s) {
  const on = !!s?.terminal;
  const tv = $("#hdr-tv");
  if (tv) tv.classList.toggle("tv-on", on);
  return on ? [">_", el("span", { class: "attn-dot" })] : [">_"];
}

function updateChatHeader() {
  const s = currentSession();
  if (!s) return;
  const dot = $("#hdr-dot"); const title = $("#hdr-title"); const sub = $("#hdr-sub");
  const st = S.status[s.sid] || {};
  if (dot) dot.className = "dot " + (st.state || "detached");
  if (title) title.textContent = s.title || "session";
  const tv = $("#hdr-tv");
  if (tv) tv.replaceChildren(...tvBtnContent(s));
  if (sub) {
    const ctx = S.context[s.sid];
    const bits = [s.project, shortModel((S.meta[s.sid] || {}).model || s.model)];
    if (ctx?.percentage != null) bits.push(`ctx ${Math.round(ctx.percentage)}%`
      + (ctx.total_tokens ? ` · ${fmtTokens(ctx.total_tokens)}` : ""));
    if (s.cost_usd) bits.push(fmtCost(s.cost_usd));
    sub.textContent = bits.filter(Boolean).join(" · ");
  }
}

function updateStatusLine() {
  const line = $("#status-line");
  if (!line || S.view?.name !== "chat") return;
  const sid = S.view.sid;
  const st = S.status[sid] || { state: "detached", detail: "" };
  const busy = ["working", "awaiting", "starting", "compacting"].includes(st.state);
  line.classList.toggle("quiet", !busy);
  const textEl = line.querySelector(".s-text");
  if (!S.wsUp) {
    textEl.replaceChildren(el("span", { class: "s-conn" }, "reconnecting…"));
  } else {
    textEl.textContent = st.detail || (st.state === "detached" ? "stopped — will resume on next message" : st.state);
  }
  const usageEl = line.querySelector(".s-usage");
  if (usageEl) usageEl.replaceChildren(...usageBits());
  const btn = $("#send-btn");
  if (btn) {
    const input = $("#chat-input");
    const showStop = busy && !(input && input.value.trim());
    btn.textContent = showStop ? "■" : "↑";
    btn.className = "c-btn " + (showStop ? "stop" : "send");
  }
}

// ---------------------------------------------------------------- true view (real claude TUI)

// The server runs `claude --resume <session>` in a PTY and streams raw bytes
// here; xterm.js renders the actual terminal UI, so this view mirrors Claude
// Code exactly. The PTY survives leaving the view — reattach replays scrollback.

const termUI = { sid: null, ws: null, term: null, cleanup: null, ending: false, gen: 0, slowAt: 0 };

const _assets = {};
function loadAsset(url, kind) {
  if (!_assets[url]) {
    _assets[url] = new Promise((resolve, reject) => {
      const node = kind === "css"
        ? el("link", { rel: "stylesheet", href: url })
        : el("script", { src: url });
      node.onload = resolve;
      node.onerror = () => { delete _assets[url]; reject(new Error("failed to load " + url)); };
      document.head.append(node);
    });
  }
  return _assets[url];
}

function ensureXterm() {
  return Promise.all([
    loadAsset("/vendor/xterm/xterm.css", "css"),
    loadAsset("/vendor/xterm/xterm.js"),
    loadAsset("/vendor/xterm/addon-fit.js"),
  ]);
}

function disposeTerm() {
  if (termUI.cleanup) termUI.cleanup();
  if (termUI.ws) { termUI.ws.onclose = null; termUI.ws.onmessage = null; termUI.ws.close(); }
  if (termUI.term) termUI.term.dispose();
  termUI.sid = null; termUI.ws = null; termUI.term = null; termUI.cleanup = null;
  termUI.ending = false;
}

// True View is a real shell, so a passkey device confirms every time it
// starts a new terminal process; joining one that already runs (switching
// between open terminals) asks nothing. The server enforces the same.
function trueViewStepUp(s) {
  return s?.terminal ? true : ensureStepUp("True View", false, true);
}

async function openTrueView(sid) {
  const s = S.sessions.find((x) => x.sid === sid);
  if (s?.incognito) {
    toast("True View is not available in an incognito chat", true);
    return;
  }
  if (!await trueViewStepUp(s)) return;
  enterTab(sid);
  nav({ name: "term", sid });
}

const TERM_KEYS = [
  ["esc", "\u001b"], ["tab", "\t"], ["\u21e7\u21e5", "\u001b[Z"], ["^C", "\u0003"],
  ["\u2190", "\u001b[D"], ["\u2191", "\u001b[A"], ["\u2193", "\u001b[B"], ["\u2192", "\u001b[C"],
  ["\u23ce", "\r"],
];

function termKeyBar() {
  return el("div", { id: "term-keys" },
    TERM_KEYS.map(([label, seq]) => el("button", {
      class: "term-key",
      // pointerdown + preventDefault: send the key without stealing focus
      // from xterm (which would close the mobile keyboard)
      onpointerdown: (e) => {
        e.preventDefault();
        if (termUI.ws && termUI.ws.readyState === 1) {
          termUI.ws.send(JSON.stringify({ type: "input", data: seq }));
        }
      },
    }, label)));
}

// "True View · project · model · ctx 41% · 89k", the chat header's line without
// the cost (a terminal does not report it). The context comes from the
// transcript while the terminal drives the session (Runner.snapshot).
function termSub(s) {
  const bits = ["True View", s?.project, s?.model && shortModel(s.model)];
  if (s?.context_pct != null) bits.push(`ctx ${Math.round(s.context_pct)}%`
    + (s.context_tokens ? ` · ${fmtTokens(s.context_tokens)}` : ""));
  else if (s?.context_tokens) bits.push(`ctx ${fmtTokens(s.context_tokens)}`);
  return bits.filter(Boolean).join(" · ");
}

function updateTermHeader() {
  const s = S.sessions.find((x) => x.sid === S.view.sid);
  const sub = $("#term-sub");
  if (sub) sub.textContent = termSub(s);
}

function renderTerm(sid) {
  disposeTerm();
  const s = S.sessions.find((x) => x.sid === sid);
  setTopbar(
    // one level up, as from the chat; the chat itself is where "end" leads
    menuBtn(), backBtn(),
    // as in the chat: the session's name, tap for its info sheet
    el("div", { class: "tb-title", onclick: () => infoSheet(sid), style: "cursor:pointer" },
      el("div", { style: "min-width:0" },
        el("div", { class: "crumb" }, s?.title || "session"),
        el("div", { class: "tb-sub", id: "term-sub" }, termSub(s)))),
    el("div", { class: "tb-chips" },
      el("button", { class: "tb-btn tb-text", onclick: () => {
        confirmSheet("End terminal?", "The Claude TUI process stops. The session stays resumable from the chat.", () => {
          // confirmed: straight to the chat, no "terminal ended" overlay
          termUI.ending = true;
          if (termUI.ws && termUI.ws.readyState === 1) termUI.ws.send('{"type":"kill"}');
          else openChat(sid);
        });
      } }, "end"),
      tabsBtn(),
      el("button", { class: "tb-btn", onclick: () => infoSheet(sid) }, "ⓘ")),
  );

  const mount = el("div", { id: "term" });
  const overlay = el("div", { id: "term-overlay", class: "hidden" });
  $("#view").replaceChildren(el("div", { id: "term-wrap" },
    el("div", { id: "term-host" }, mount, overlay), termKeyBar()));

  termUI.sid = sid;
  // two renders while xterm loads (a double tap on >_): only the last opens
  const gen = ++termUI.gen;
  overlay.classList.remove("hidden");
  overlay.replaceChildren(el("div", { class: "term-msg" }, "connecting…"));
  ensureXterm()
    .then(() => {
      if (gen !== termUI.gen || termUI.sid !== sid || S.view?.name !== "term") return;
      openTerm(sid, mount, overlay);
    })
    .catch(() => showTermOverlay(overlay, sid, "Could not load terminal assets"));
}

function openTerm(sid, mount, overlay) {
  const term = new Terminal({
    fontFamily: 'ui-monospace, "SF Mono", SFMono-Regular, Menlo, monospace',
    fontSize: 13,
    cursorBlink: true,
    scrollback: 4000,
    theme: {
      background: "#141210", foreground: "#ede6de",
      cursor: "#e8825e", cursorAccent: "#141210",
      selectionBackground: "#3a2a22",
    },
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(mount);
  fit.fit();
  termUI.term = term;

  const ws = T.openTerm(sid);
  ws.binaryType = "arraybuffer";
  termUI.ws = ws;

  const sendResize = () => {
    if (ws.readyState === 1) ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
  };
  ws.onopen = () => {
    overlay.classList.add("hidden");
    fit.fit(); sendResize(); term.focus();
  };
  ws.onmessage = (e) => {
    if (typeof e.data === "string") {
      let msg; try { msg = JSON.parse(e.data); } catch { return; }
      if (msg.type === "exit") {
        if (termUI.ending) { openChat(sid); return; }
        if (/passkey/i.test(msg.reason || "")) S.elevatedUntil = 0;
        showTermOverlay(overlay, sid, msg.reason || "Terminal ended");
      }
      return;
    }
    term.write(new Uint8Array(e.data));
  };
  ws.onclose = (e) => {
    if (termUI.ws !== ws) return;
    if (termUI.ending) { openChat(sid); return; }
    if (e.code === 4423) { showLock(); return; }
    if (e.code === 4428) { showPasskeyGate(); return; }
    if (e.code === 4401) { S.authed = false; nav({ name: "login" }); return; }
    if (e.code === 4413) {   // the link fell behind: reopen from the scrollback,
      const again = Date.now() - termUI.slowAt < 30000;   // once per 30 s
      termUI.slowAt = Date.now();
      if (again) showTermOverlay(overlay, sid, "Connection too slow");
      else renderTerm(sid);
      return;
    }
    if (overlay.classList.contains("hidden")) showTermOverlay(overlay, sid, "Disconnected");
  };
  term.onData((d) => {
    if (ws.readyState === 1) ws.send(JSON.stringify({ type: "input", data: d }));
  });

  let fitTimer = null;
  const refit = () => {
    clearTimeout(fitTimer);
    fitTimer = setTimeout(() => { fit.fit(); sendResize(); }, 120);
  };
  window.addEventListener("resize", refit);
  const vv = window.visualViewport;
  if (vv) vv.addEventListener("resize", refit);
  const ping = setInterval(() => { if (ws.readyState === 1) ws.send('{"type":"ping"}'); }, 25000);

  termUI.cleanup = () => {
    clearTimeout(fitTimer);
    clearInterval(ping);
    window.removeEventListener("resize", refit);
    if (vv) vv.removeEventListener("resize", refit);
  };
}

function showTermOverlay(overlay, sid, reason) {
  overlay.classList.remove("hidden");
  overlay.replaceChildren(
    el("div", { class: "term-msg" }, reason),
    el("div", { class: "term-actions" },
      el("button", { onclick: async () => {
        if (await trueViewStepUp(S.sessions.find((x) => x.sid === sid))) renderTerm(sid);
      } }, "reopen"),
      el("button", { onclick: () => nav({ name: "chat", sid }) }, "back to chat")));
}

// ---------------------------------------------------------------- transcript rendering

function renderChatTranscript(sid) {
  const msgs = chatUI.msgs;
  if (!msgs) return;
  chatUI.byTool = {}; chatUI.byReq = {}; chatUI.provText = []; chatUI.provThink = [];
  msgs.replaceChildren();
  for (const ev of S.events[sid] || []) renderEvent(sid, ev, false);
  scrollBottom(true);
  kbdCards();
}

function renderLiveEvent(sid, ev) {
  const loading = $("#chat-loading");
  if (loading) loading.remove();
  renderEvent(sid, ev, true);
  kbdCards();
}

function nearBottom() {
  const m = chatUI.msgs;
  return m ? m.scrollHeight - m.scrollTop - m.clientHeight < 140 : false;
}

function scrollBottom(force) {
  const m = chatUI.msgs;
  if (m && (force || nearBottom())) m.scrollTop = m.scrollHeight;
}

function appendMsg(node, parentToolId) {
  if (parentToolId && chatUI.byTool[parentToolId]) {
    let nest = chatUI.byTool[parentToolId].querySelector(".nested-tools");
    if (!nest) {
      nest = el("div", { class: "nested-tools" });
      chatUI.byTool[parentToolId].append(nest);
    }
    nest.append(node);
  } else {
    chatUI.msgs.append(node);
  }
  scrollBottom(false);
}

function renderEvent(sid, ev, live) {
  switch (ev.kind) {
    case "user_text":
      finalizeProvisionals();
      // "edit from here" sits on a ✎ in the corner, not on the bubble: a
      // tap on the text would take over the long press that selects it
      appendMsg(el("div", { class: ev.external ? "msg m-user external" : "msg m-user",
        "data-seq": ev.seq || "" },
        ev.seq ? el("button", { class: "m-edit", "aria-label": "Edit from here",
          onclick: () => editFromHere(sid, ev.seq) }, "✎") : null,
        ev.text));
      markEditable();
      if (live) scrollBottom(true);
      break;

    case "delta": {
      let blk = chatUI.provText[chatUI.provText.length - 1];
      if (!blk || blk.dataset.done) {
        blk = el("div", { class: "msg m-assist streaming" });
        blk._buf = "";
        chatUI.provText.push(blk);
        appendMsg(blk);
      }
      blk._buf += ev.text || "";
      blk.textContent = blk._buf;
      scrollBottom(false);
      break;
    }
    case "text_open": {
      const blk = el("div", { class: "msg m-assist streaming" });
      blk._buf = "";
      chatUI.provText.push(blk);
      appendMsg(blk);
      break;
    }
    case "text": {
      const prov = chatUI.provText.find((b) => !b.dataset.replaced);
      const html = md(ev.text);
      if (prov) {
        prov.dataset.replaced = "1";
        prov.className = "msg m-assist";
        prov.innerHTML = html;
      } else {
        appendMsg(el("div", { class: "msg m-assist", html }));
      }
      scrollBottom(false);
      break;
    }

    case "thinking_open": case "thinking_delta": {
      let blk = chatUI.provThink[chatUI.provThink.length - 1];
      if (!blk || blk.dataset.done) {
        blk = el("details", { class: "m-think" },
          el("summary", null, "◈ thinking"),
          el("div", { class: "think-body" }, ""));
        chatUI.provThink.push(blk);
        appendMsg(blk);
      }
      if (ev.kind === "thinking_delta") {
        const body = blk.querySelector(".think-body");
        body.textContent += ev.text || "";
      }
      break;
    }
    case "thinking": {
      const prov = chatUI.provThink.find((b) => !b.dataset.replaced);
      if (prov) {
        prov.dataset.replaced = "1";
        prov.querySelector(".think-body").textContent = ev.text;
        prov.querySelector("summary").textContent = "◈ thought";
      } else {
        appendMsg(el("details", { class: "m-think" },
          el("summary", null, "◈ thought"),
          el("div", { class: "think-body" }, ev.text)));
      }
      break;
    }
    case "block_stop": {
      const t = chatUI.provText[chatUI.provText.length - 1];
      if (t && !t.dataset.replaced) t.dataset.done = "1";
      const th = chatUI.provThink[chatUI.provThink.length - 1];
      if (th && !th.dataset.replaced) th.dataset.done = "1";
      break;
    }

    case "tool_start": appendMsg(toolCard(ev), ev.parent_tool_use_id); break;
    case "tool_result": fillToolResult(ev); break;

    case "permission": appendMsg(permissionCard(sid, ev)); if (live) scrollBottom(true); break;
    case "question": appendMsg(questionCard(sid, ev)); if (live) scrollBottom(true); break;
    case "plan_approval": appendMsg(planCard(sid, ev)); if (live) scrollBottom(true); break;
    case "permission_resolved": resolveActionCard(ev); break;

    case "cleared":
      finalizeProvisionals();
      appendMsg(el("div", { class: "divider" }, "conversation cleared"));
      break;
    case "rewound":
      finalizeProvisionals();
      appendMsg(el("div", { class: "divider" },
        `rewound — ${ev.prompts} prompt${ev.prompts === 1 ? "" : "s"} dropped`));
      break;
    case "resumed": {
      const skipped = (ev.total || 0) - (ev.shown || 0);
      appendMsg(el("div", { class: "divider" },
        "resumed session — continuing below"
        + (skipped > 0 ? ` · ${skipped} older messages not shown` : "")));
      if (live) scrollBottom(true);
      break;
    }
    case "compact":
      appendMsg(el("div", { class: "divider" },
        "compacted" + (ev.pre_tokens ? ` · was ${fmtTokens(ev.pre_tokens)} tokens` : "")
        + (ev.trigger === "auto" ? " (auto)" : "")
        + (ev.trigger === "past" ? " (earlier)" : "")));
      break;

    case "result": {
      finalizeProvisionals();
      const bits = [];
      if (ev.duration_ms) bits.push((ev.duration_ms / 1000).toFixed(0) + "s");
      if (ev.cost_usd != null) bits.push(fmtCost(ev.cost_usd) + " total");
      if (ev.is_error) bits.push("⚠ " + (ev.subtype || "error"));
      appendMsg(el("div", { class: "turn-result" }, bits.join(" · ")));
      break;
    }

    case "notice":
      appendMsg(el("div", { class: "notice " + (ev.level || "") }, ev.text
        + (ev.rate_limit === "rejected" && ev.resets_at ? ` — resets ${fmtReset(ev.resets_at)}` : "")));
      break;

    case "meta": case "commands": case "context": case "status": case "system": case "debug":
      break; // state-only
  }
}

function finalizeProvisionals() {
  for (const b of chatUI.provText) if (!b.dataset.replaced) { b.dataset.replaced = "1"; b.className = "msg m-assist"; b.innerHTML = md(b._buf || b.textContent); }
  for (const b of chatUI.provThink) if (!b.dataset.replaced) b.dataset.replaced = "1";
  chatUI.provText = []; chatUI.provThink = [];
}

// ---------- tool cards ----------

const TOOL_ICONS = {
  Bash: "❯", Read: "▤", Edit: "✎", Write: "✎", Grep: "⌕", Glob: "⌕",
  Task: "⑃", WebFetch: "⇩", WebSearch: "⌕", TodoWrite: "☑", Skill: "◆",
  NotebookEdit: "✎", ExitPlanMode: "▤", KillShell: "■", BashOutput: "❯",
};

function toolIcon(name) {
  if (name.startsWith("mcp__")) return "⚡";
  return TOOL_ICONS[name] || "⚙";
}

const FILE_TOOLS = ["Read", "Edit", "MultiEdit", "Write", "NotebookEdit"];

function toolCard(ev) {
  const body = el("div", { class: "tool-body" });
  const inputPre = toolInputView(ev.tool, ev.input || {});
  if (inputPre) body.append(inputPre);
  const state = el("span", { class: "t-state spin" }, "●");
  // ↗ opens the file in the viewer (devices with --files)
  const fp = FILE_TOOLS.includes(ev.tool) && (ev.input?.file_path || ev.input?.notebook_path);
  const cwd = S.sessions.find((x) => x.sid === chatUI.sid)?.cwd;
  const open = fp && S.canFiles ? el("button", { class: "t-open", "aria-label": "Open the file",
    onclick: () => openFileAt(String(fp), cwd) }, "↗") : null;
  const card = el("div", { class: "tool-card", "data-tool-id": ev.tool_use_id },
    el("div", { class: "tool-row" },
      el("button", { class: "tool-head", onclick: () => card.classList.toggle("open") },
        el("span", { class: "t-icon" }, toolIcon(ev.tool)),
        el("span", { class: "t-name" }, ev.tool.replace(/^mcp__/, "")),
        el("span", { class: "t-sum" }, ev.summary || ""),
        state),
      open),
    body);
  card._state = state;
  chatUI.byTool[ev.tool_use_id] = card;
  return card;
}

function toolInputView(tool, input) {
  if (tool === "Bash" && input.command) {
    return el("div", null, el("div", { class: "out-label" }, "command"),
      el("pre", null, input.command));
  }
  if (tool === "Edit" && (input.old_string || input.new_string)) {
    const wrap = el("div", null, el("div", { class: "out-label" }, input.file_path || ""));
    const pre = el("pre");
    if (input.old_string) pre.append(el("span", { class: "diff-old" },
      input.old_string.split("\n").map((l) => "- " + l).join("\n") + "\n"));
    if (input.new_string) pre.append(el("span", { class: "diff-new" },
      input.new_string.split("\n").map((l) => "+ " + l).join("\n")));
    wrap.append(pre);
    return wrap;
  }
  if (tool === "Write" && input.content) {
    return el("div", null, el("div", { class: "out-label" }, input.file_path || "content"),
      el("pre", null, input.content.length > 3000 ? input.content.slice(0, 3000) + "\n…" : input.content));
  }
  if (tool === "TodoWrite" && Array.isArray(input.todos)) {
    return el("ul", { class: "todo-list" }, input.todos.map((t) =>
      el("li", { class: t.status === "completed" ? "td-done" : t.status === "in_progress" ? "td-active" : "" },
        (t.status === "completed" ? "☑ " : t.status === "in_progress" ? "◐ " : "☐ ") + (t.content || ""))));
  }
  const keys = Object.keys(input || {});
  if (!keys.length) return null;
  const compact = keys.map((k) => {
    let v = input[k];
    if (typeof v === "object") v = JSON.stringify(v);
    v = String(v);
    return k + ": " + (v.length > 400 ? v.slice(0, 400) + "…" : v);
  }).join("\n");
  return el("pre", null, compact);
}

function fillToolResult(ev) {
  const card = chatUI.byTool[ev.tool_use_id];
  if (!card) return;
  card._state.className = "t-state " + (ev.is_error ? "err" : "ok");
  card._state.textContent = ev.is_error ? "✕" : "✓";
  const body = card.querySelector(".tool-body");
  if (ev.text) {
    body.append(el("div", { class: "out-label" }, ev.is_error ? "error" : "output"),
      el("pre", null, ev.text));
    if (ev.is_error) card.classList.add("open");
  }
}

// ---------- permission / question / plan cards ----------

function permissionCard(sid, ev) {
  const btns = el("div", { class: "a-buttons" });
  const card = el("div", { class: "action-card", "data-req": ev.request_id },
    el("div", { class: "a-head" }, "⚠ approval needed"),
    el("div", { class: "a-title" }, ev.title || `${ev.tool}${ev.summary ? " — " + ev.summary : ""}`),
    ev.description ? el("div", { class: "a-desc" }, ev.description) : null,
    ev.reason ? el("div", { class: "a-desc" }, ev.reason) : null,
    permInputPreview(ev),
    ev.suggestions?.length
      ? el("div", { class: "a-desc" }, "Always: " + ev.suggestions.map(describeSuggestion).join("; "))
      : null,
    btns);
  chatUI.byReq[ev.request_id] = card;

  const respond = (payload, label) => {
    if (wsSend({ type: "permission", session_id: sid, request_id: ev.request_id, ...payload })) {
      markResolved(card, label);
    }
  };
  // DOM append, not el(): a null child would print as "null"
  btns.append(...[
    el("button", { class: "btn primary", "data-k": "1 y", onclick: () => respond({ behavior: "allow" }, "allowed") }, "Allow", kHint("1")),
    ev.suggestions?.length
      ? el("button", { class: "btn", "data-k": "2", onclick: async () => {
          // "Always" widens what runs without asking again — confirm it
          if (!await ensureStepUp("Always allow")) return;
          // the server may still want a fresh passkey (stepup_required):
          // wait for permission_resolved instead of closing the card now
          if (wsSend({ type: "permission", session_id: sid, request_id: ev.request_id,
                       behavior: "allow", apply_suggestions: ev.suggestions.map((_, i) => i) })) {
            card._always = true;
            btns.querySelectorAll("button").forEach((b) => { b.disabled = true; });
          }
        } }, "Always", kHint("2"))
      : null,
    el("button", { class: "btn danger", "data-k": "3 n", onclick: () => respond({ behavior: "deny", message: "User denied this action" }, "denied") }, "Deny", kHint("3")),
    el("button", { class: "btn small a-fill", "data-k": "4", onclick: async () => {
      const note = await textSheet("Tell Claude why / what to do instead");
      if (note !== null) respond({ behavior: "deny", message: note || "User denied" }, "denied");
    } }, "Deny with note…", kHint("4")),
  ].filter(Boolean));
  return card;
}

// "Edit from here" on one of the latest prompts: the conversation goes back
// to before it, and its text returns to the box for editing. Only the
// conversation — files stay as they are. How many: the server's REWIND_MAX
// (sessions.py), sent in hello; 5 until then.
let rewindMax = 5;

// the ✎ shows on the last rewindMax prompts only — older ones cannot be
// rewound from here
function markEditable() {
  const us = [...(chatUI.msgs?.querySelectorAll(".m-user") || [])];
  us.forEach((u, i) => u.classList.toggle("can-edit", i >= us.length - rewindMax));
}

async function editFromHere(sid, seq) {
  const evs = S.events[sid] || [];
  const later = evs.filter((e) => e.kind === "user_text" && e.seq >= seq).length;
  if (!seq || later > rewindMax) return;   // older prompts: True View (Esc Esc)
  let info;
  try {
    info = await T.rpc("sessions.rewind", { sid, seq, dry: true });
  } catch (e) { toast(e.message, true); return; }
  const n = info.prompts;
  const files = info.files || [];
  confirmSheet("Edit from here?",
    `Drops ${n === 1 ? "this prompt" : `this and ${n - 1} later prompt${n === 2 ? "" : "s"}`} `
    + "and the answers; the prompt comes back to the box for editing."
    + (files.length ? ` Files changed in these turns stay as they are: ${files.map(shortPath).join(", ")}.` : ""),
    async () => {
      const r = await T.rpc("sessions.rewind", { sid, seq });
      const input = $("#chat-input");
      if (input && chatUI.sid === sid) {
        input.value = r.text;
        input.dispatchEvent(new Event("input"));
        input.focus();
      }
    });
}

// What "Always" grants, the way the terminal words it ("always allow
// access to X for this session") — the card's button alone does not say.
function describeSuggestion(s) {
  const where = { session: "for this session", localSettings: "in this project (local settings)",
    projectSettings: "in this project", userSettings: "in all projects" }[s.destination] || "for this session";
  if (s.type === "setMode") return `switch to ${s.mode}`;
  if (s.type === "addDirectories") return `access to ${(s.directories || []).join(", ")} ${where}`;
  if (s.type === "addRules" || s.type === "replaceRules") {
    const rules = (s.rules || []).map((r) => r.ruleContent ? `${r.toolName}(${r.ruleContent})` : r.toolName);
    return `${s.behavior || "allow"} ${rules.join(", ")} ${where}`;
  }
  return s.type;
}

function permInputPreview(ev) {
  const input = ev.input || {};
  if (ev.tool === "Bash" && input.command) return el("pre", null, input.command);
  if (ev.tool === "Edit" || ev.tool === "Write") {
    const view = toolInputView(ev.tool, input);
    return view || null;
  }
  const keys = Object.keys(input);
  if (!keys.length) return null;
  const txt = keys.map((k) => {
    let v = input[k];
    if (typeof v === "object") v = JSON.stringify(v, null, 1);
    return `${k}: ${String(v).slice(0, 500)}`;
  }).join("\n");
  return el("pre", null, txt.slice(0, 1200));
}

function questionCard(sid, ev) {
  const questions = ev.questions || [];
  const answers = {};
  const card = el("div", { class: "action-card", "data-req": ev.request_id },
    el("div", { class: "a-head" }, "? claude asks"));
  chatUI.byReq[ev.request_id] = card;

  const done = () => {
    if (wsSend({ type: "permission", session_id: sid, request_id: ev.request_id, behavior: "allow", answers })) {
      markResolved(card, Object.values(answers).join(" · ").slice(0, 80) || "answered");
    }
  };

  const single = questions.length === 1 && !questions[0].multiSelect;
  let n = 0;   // option keys 1–9 run across all questions of the card
  questions.forEach((q) => {
    card.append(el("div", { class: "a-title" }, q.question));
    const sel = new Set();
    const optWrap = el("div");
    (q.options || []).forEach((o) => {
      const label = typeof o === "string" ? o : o.label;
      const k = ++n <= 9 ? String(n) : null;
      const btn = el("button", { class: "q-option", "data-k": k, onclick: () => {
        if (q.multiSelect) {
          sel.has(label) ? sel.delete(label) : sel.add(label);
          answers[q.question] = [...sel].join(", ");
          btn.classList.toggle("sel");
        } else {
          answers[q.question] = label;
          if (single) done();
          else {
            optWrap.querySelectorAll(".q-option").forEach((b) => b.classList.remove("sel"));
            btn.classList.add("sel");
          }
        }
      } },
        el("div", { class: "q-label" }, k ? kHint(k) : null, label),
        (typeof o === "object" && o.description) ? el("div", { class: "q-desc" }, o.description) : null);
      optWrap.append(btn);
    });
    const other = el("input", { class: "q-other-input", placeholder: "Other…" });
    other.addEventListener("input", () => { answers[q.question] = other.value; });
    other.addEventListener("keydown", (e) => { if (e.key === "Enter" && single && other.value.trim()) done(); });
    optWrap.append(other);
    card.append(optWrap);
  });

  if (!single) {
    card.append(el("div", { class: "a-buttons" },
      el("button", { class: "btn primary", "data-k": "enter", onclick: done }, "Send answers", kHint("⏎"))));
  }
  return card;
}

function planCard(sid, ev) {
  const card = el("div", { class: "action-card", "data-req": ev.request_id },
    el("div", { class: "a-head" }, "▤ plan ready for review"),
    el("div", { class: "msg m-assist", html: md(ev.plan || "") }));
  chatUI.byReq[ev.request_id] = card;
  const respond = (payload, label, mode) => {
    if (wsSend({ type: "permission", session_id: sid, request_id: ev.request_id, ...payload })) {
      if (mode) wsSend({ type: "set_mode", session_id: sid, mode });
      markResolved(card, label);
    }
  };
  card.append(el("div", { class: "a-buttons" },
    el("button", { class: "btn primary", "data-k": "1", onclick: () => respond({ behavior: "allow" }, "approved — auto-accepting edits", "acceptEdits") }, "Approve, accept edits", kHint("1")),
    el("button", { class: "btn", "data-k": "2", onclick: () => respond({ behavior: "allow" }, "approved", "default") }, "Approve, ask each time", kHint("2")),
    el("button", { class: "btn danger", "data-k": "3", onclick: async () => {
      const note = await textSheet("What should change in the plan?");
      if (note !== null) respond({ behavior: "deny", message: note || "Keep planning" }, "sent back");
    } }, "Keep planning", kHint("3"))));
  return card;
}

function markResolved(card, label) {
  card.classList.add("resolved");
  card.querySelectorAll(".a-buttons, .q-option, .q-other-input").forEach((n) => n.remove());
  card.append(el("div", { class: "a-outcome" }, "→ " + label));
  kbdCards();
}

function resolveActionCard(ev) {
  const card = chatUI.byReq[ev.request_id];
  if (card && !card.classList.contains("resolved")) {
    markResolved(card, ev.behavior === "allow" ? (card._always ? "always allowed" : "allowed")
      : "denied" + (ev.note ? ": " + ev.note.slice(0, 60) : ""));
  }
}

// ---------------------------------------------------------------- palette & sheets

function palette(sid) {
  const meta = S.meta[sid] || {};
  const incognito = !!S.sessions.find((x) => x.sid === sid)?.incognito;
  // your own commands (~/.claude/commands) stay; the CLI list does not say
  // which of its entries are skills, so everything else is filtered by name
  const own = new Set((S.library?.commands || []).filter((c) => c.scope === "user").map((c) => c.name));
  const appCmds = [
    { name: "clear", desc: "Start fresh context in this chat", act: () => confirmClear(sid) },
    { name: "compact", desc: "Summarize old messages to free context", act: () => confirmCompact(sid) },
    { name: "btw", desc: "Side question, kept out of the conversation", act: () => btwSheet(sid) },
    { name: "model", desc: "Change model", act: () => modelSheet(sid) },
    { name: "mode", desc: "Change permission mode", act: () => modeSheet(sid) },
    { name: "info", desc: "Session info, context, MCP status", act: () => infoSheet(sid) },
    { name: "rename", desc: "Rename this session",
      act: () => renameSession(S.sessions.find((x) => x.sid === sid) || { sid }) },
    { name: "library", desc: "Skills, commands, agents, MCP", act: () => nav({ name: "skills" }) },
    S.canFiles && { name: "files", desc: "Browse this project's files", act: () => openFiles(sid) },
  ].filter((c) => c && (!incognito || !INCOGNITO_HIDDEN.has(c.name)));
  const skip = new Set(["clear", "compact", "btw", "model", "info", "context", "library",
    "login", "logout", "quit", "exit", "resume", "help", "doctor", "ide", "vim",
    "terminal-setup", "install-github-app", "rc", "remote-control"]);
  const cliCmds = (meta.commands || [])
    .filter((c) => !skip.has(c.name.replace(/^\//, "")))
    .filter((c) => !incognito || INCOGNITO_CLI.has(c.name.replace(/^\//, ""))
      || own.has(c.name.replace(/^\//, "")))
    .map((c) => ({
      name: c.name.replace(/^\//, ""),
      desc: c.description || "",
      hint: c.argument_hint || "",
      insert: true,
    }));

  const body = el("div");
  const search = el("input", { type: "search", placeholder: "Filter commands…" });
  const list = el("div", { class: "sheet-body" });

  const draw = (filter) => {
    const f = (filter || "").toLowerCase();
    list.replaceChildren();
    const add = (title, items) => {
      const vis = items.filter((c) => !f || c.name.toLowerCase().includes(f) || c.desc.toLowerCase().includes(f));
      if (!vis.length) return;
      list.append(el("div", { class: "eyebrow" }, title));
      for (const c of vis) {
        list.append(el("button", { class: "sheet-item", onclick: () => {
          closeSheet();
          if (c.act) c.act();
          else {
            const input = $("#chat-input");
            input.value = "/" + c.name + " ";
            input.focus();
            input.dispatchEvent(new Event("input"));
          }
        } },
          el("div", { class: "si-main" },
            el("div", { style: "font-family:var(--mono); font-size:14px" }, "/" + c.name + (c.hint ? " " + c.hint : "")),
            c.desc ? el("div", { class: "si-sub" }, c.desc.slice(0, 90)) : null)));
      }
    };
    add("app", appCmds);
    add("commands", cliCmds);
  };
  search.addEventListener("input", () => draw(search.value));
  body.append(search, list);
  draw("");
  rawSheet(body);
}

function confirmClear(sid) {
  confirmSheet("Clear conversation?", "Context resets to zero. The transcript stays visible above the divider.", () => {
    wsSend({ type: "clear", session_id: sid });
  });
}

// compacting replaces the conversation with a summary for good, so a
// stray tap in the palette must not do it on its own
function confirmCompact(sid, instructions = "") {
  confirmSheet("Compact conversation?", "Older messages are replaced by a summary. This cannot be undone.", () => {
    wsSend({ type: "compact", session_id: sid, instructions });
  });
}

// /btw: a side question answered from the conversation so far and kept out
// of it (the server forks the transcript into a one-off process with no
// tools). The sheet shows one answer at a time, the earlier ones dimmed
// below; tapping one brings it up. The server keeps the list in memory.
const btwUI = { draw: null };

function btwSheet(sid, question = "") {
  if (question) wsSend({ type: "btw", session_id: sid, question });
  let shown = null;   // id of the exchange shown in full; null = the newest
  const main = el("div");
  const input = el("textarea", { class: "btw-input", rows: 1, placeholder: "Side question…" });
  const ask = el("button", { class: "btn primary", onclick: () => send() }, "Ask");
  const clear = el("button", { class: "btn", onclick: () => {
    wsSend({ type: "btw_clear", session_id: sid });
  } }, "Clear");
  const send = () => {
    const q = input.value.trim();
    if (!q) return;
    if (wsSend({ type: "btw", session_id: sid, question: q })) {
      input.value = ""; shown = null;
      if (!finePointer()) input.blur();
    }
  };
  input.addEventListener("keydown", (e) => {
    if (homeEnd(input, e)) return;
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  const copy = (text) => navigator.clipboard?.writeText(text)
    .then(() => toast("Copied"), () => toast("Copy failed", true))
    ?? toast("Copy needs https", true);

  btwUI.draw = (changed) => {
    if (changed !== sid) return;
    if (!main.isConnected) { btwUI.draw = null; return; }
    const items = S.btw[sid] || [];
    const cur = items.find((x) => x.id === shown) || items[items.length - 1];
    const busy = items.some((x) => x.state === "asking");
    ask.disabled = busy;
    clear.disabled = !items.length;
    main.replaceChildren(el("div", { class: "sheet-title" }, "Side question"));
    if (!cur) {
      main.append(el("div", { class: "a-desc" },
        "Ask about anything already in this conversation. Neither the question nor the answer is added to it, and no tools run."));
      return;
    }
    // oldest first, as in the chat: the newest sits by the box
    const full = (x) => el("div", { class: "btw-cur" },
      el("div", { class: "btw-q" }, x.q),
      x.state === "asking" ? el("div", { class: "btw-a dim" }, "thinking…") : [
        el("div", { class: "msg m-assist btw-a" + (x.state === "error" ? " err" : ""), html: md(x.a) }),
        el("div", { class: "btw-meta" },
          // a side question costs fractions of a cent; fmtCost would say $0.00
          x.cost != null ? "$" + x.cost.toFixed(x.cost < 0.1 ? 3 : 2) : "",
          x.state === "done" ? el("button", { class: "btn small", onclick: () => copy(x.a) }, "Copy") : null)]);
    const folded = (x) => el("button", { class: "btw-old", onclick: () => { shown = x.id; btwUI.draw(sid); } },
      el("div", { class: "btw-q" }, x.q),
      el("div", { class: "btw-snip" }, (x.a || "…").slice(0, 140)));
    const open = full(cur);
    main.append(el("div", { class: "btw-list" }, items.map((x) => x === cur ? open : folded(x))));
    // the newest: scroll to the bottom; an earlier one: bring it into view
    if (cur === items[items.length - 1]) body.scrollTop = body.scrollHeight;
    else open.scrollIntoView({ block: "nearest" });
  };
  const body = el("div", { class: "sheet-body" }, main);
  rawSheet(body,
    el("div", { class: "btw-foot" }, input, el("div", { class: "a-buttons" }, ask, clear)));
  btwUI.draw(sid);
  if (finePointer()) input.focus();
}

function modelSheet(sid) {
  const cur = (S.sessions.find((s) => s.sid === sid) || {}).model || "default";
  const items = S.defaults.models.map((m) => ({
    icon: shortModel(cur) === m || cur === m ? "✓" : " ",
    label: m,
    action: () => wsSend({ type: "set_model", session_id: sid, model: m }),
  }));
  items.push({ icon: "✎", label: "Custom model id…", action: async () => {
    const m = await textSheet("Model id", { placeholder: "e.g. claude-opus-4-8", plain: true, maxlength: 100 });
    if (m) wsSend({ type: "set_model", session_id: sid, model: m });
  } });
  sheet("Model", items);
}

const MODE_INFO = {
  default: "Ask before risky tools",
  acceptEdits: "Auto-accept file edits",
  plan: "Plan first — no execution",
  dontAsk: "Deny anything not pre-allowed",
  bypassPermissions: "Run everything without asking",
};

function modeSheet(sid) {
  const cur = (S.sessions.find((s) => s.sid === sid) || {}).mode;
  sheet("Permission mode", S.defaults.modes.map((m) => ({
    icon: cur === m ? "✓" : " ",
    label: m,
    sub: MODE_INFO[m] || "",
    danger: m === "bypassPermissions",
    action: () => wsSend({ type: "set_mode", session_id: sid, mode: m }),
  })));
}

// Plan usage for the chat's status line: "21% (5h) · 82% (7d)". The account's
// numbers, not the session's, so any snapshot carries them; empty until some
// session has made a request since the server started.
const STATUS_WINDOWS = [["five_hour", "5h"], ["seven_day", "7d"]];

function planUsage() {
  return S.sessions.find((x) => x.usage)?.usage || {};
}

function usageLevel(pct) {
  return pct >= 80 ? "high" : pct >= 55 ? "mid" : "";
}

function usageBits() {
  const usage = planUsage();
  const out = [];
  for (const [key, short] of STATUS_WINDOWS) {
    const u = usage[key];
    if (!u) continue;
    const pct = Math.round((u.utilization || 0) * 100);
    if (out.length) out.push(" · ");
    out.push(el("span", { class: usageLevel(pct), title: u.resets_at ? `resets ${fmtReset(u.resets_at)}` : "" },
      `${pct}% (${short})`));
  }
  return out;
}

function infoSheet(sid) {
  const s = S.sessions.find((x) => x.sid === sid) || {};
  const meta = S.meta[sid] || {};
  // with True View open the terminal drives the session: the SDK's context
  // figure and the chat's cost and plan usage fall behind, so the context
  // comes from the snapshot (the transcript) and the rest from /usage
  const tv = !!s.terminal;
  const live = S.context[sid];
  const ctx = !tv ? live : s.context_tokens ? {
    total_tokens: s.context_tokens, max_tokens: live?.max_tokens, percentage: s.context_pct,
  } : null;
  const body = el("div", { class: "sheet-body" });

  body.append(el("div", { class: "sheet-title" }, s.title || "session"));

  body.append(
    kvRow("project", s.incognito ? s.cwd_short : shortPath(s.cwd)),
    kvRow("model", meta.model || s.model),
    kvRow("mode", s.mode),
    ...(tv ? [] : [kvRow("cost", s.cost_usd != null ? "$" + s.cost_usd.toFixed(3) : "—")]),
    kvRow("claude session", (s.claude_session_id || "").slice(0, 13)),
    kvRow("process", s.terminal ? "true view" : s.live ? "running" : "stopped"),
  );

  if (ctx) {
    const pct = Math.round(ctx.percentage || 0);
    const known = ctx.percentage != null && ctx.max_tokens;
    body.append(el("div", { style: "padding:10px 0 4px" },
      el("div", { style: "display:flex; justify-content:space-between; font-size:12px; font-family:var(--mono); color:var(--dim)" },
        el("span", null, "context"),
        el("span", null, known
          ? `${fmtTokens(ctx.total_tokens)} / ${fmtTokens(ctx.max_tokens)} · ${pct}%`
          : fmtTokens(ctx.total_tokens))),
      el("div", { class: "ctx-bar" },
        el("div", { class: "fill" + (pct > 80 ? " high" : pct > 55 ? " mid" : ""), style: `width:${pct}%` }))));
  }

  const windows = tv ? [] : Object.entries(planUsage());
  if (tv) body.append(kvRow("cost, plan usage", "/usage in the terminal"));
  if (windows.length) {
    body.append(el("div", { class: "eyebrow" }, "plan usage"));
    for (const [type, u] of windows) {
      const pct = Math.round((u.utilization || 0) * 100);
      body.append(el("div", { class: "usage-row" },
        kvRow(USAGE_WINDOWS[type] || type,
          `${pct}%` + (u.resets_at ? ` · resets ${fmtReset(u.resets_at)}` : "")
          + (u.status === "rejected" ? " · rejected" : "")),
        el("div", { class: "ctx-bar" },
          el("div", { class: "fill " + usageLevel(pct), style: `width:${Math.min(pct, 100)}%` }))));
    }
  }

  if (meta.mcp_servers?.length) {
    body.append(el("div", { class: "eyebrow" }, "mcp servers"));
    for (const m of meta.mcp_servers) {
      body.append(kvRow(m.name, m.status || "?"));
    }
  }

  // the buttons stay in view below the scrolling part: the sheet grows
  // past its height with plan usage and MCP servers, and a cut at the
  // bottom looked like the sheet's end, hiding the second row
  const foot = el("div", { class: "a-buttons" },
    s.incognito ? null : el("button", { class: "btn", onclick: () => { closeSheet(); renameSession(s); } }, "Rename"),
    forkItem(s) ? el("button", { class: "btn", onclick: () => { closeSheet(); forkItem(s).action(); } }, "Fork") : null,
    el("button", { class: "btn", onclick: () => { closeSheet(); confirmCompact(sid); } }, "Compact"),
    el("button", { class: "btn", onclick: () => { closeSheet(); confirmClear(sid); } }, "Clear"),
    el("button", { class: "btn", onclick: () => { closeSheet(); wsSend({ type: "refresh_context", session_id: sid }); } }, "Refresh"),
    el("button", { class: "btn danger", onclick: async () => {
      closeSheet();
      try { await stopSession(sid); } catch (e) { toast("Stop failed: " + e.message, true); }
    } }, "Stop process"),
    s.incognito ? el("button", { class: "btn danger", onclick: () => deleteItem(s).action() }, "End & erase") : null);
  rawSheet(body, foot);
}

// generic sheets

// footNode, if given, sits below bodyNode and does not scroll with it
function rawSheet(bodyNode, footNode) {
  closeSheet();
  const panel = el("div", { class: "sheet" }, el("div", { class: "grab" }), bodyNode,
    footNode ? el("div", { class: "sheet-foot" }, footNode) : null);
  const backdrop = el("div", { class: "sheet-backdrop", onclick: (e) => { if (e.target === backdrop) closeSheet(); } }, panel);
  sheetSwipe(panel);
  $("#sheet-root").append(backdrop);
}

// Pull a sheet down to close it, as a tap outside does. The drag starts
// only downwards and only where nothing under the finger is scrolled away
// from its top, so the sheet's own scrolling and taps work as before.
function sheetSwipe(panel) {
  let y0 = 0, x0 = 0, t0 = 0, dy = 0, state = "";   // "" | "drag" | "off"
  const scrolledAway = (node) => {
    for (let n = node; n && n !== panel; n = n.parentElement) if (n.scrollTop > 0) return true;
    return false;
  };
  panel.addEventListener("touchstart", (e) => {
    if (e.touches.length !== 1) { state = "off"; return; }
    y0 = e.touches[0].clientY; x0 = e.touches[0].clientX; t0 = Date.now(); dy = 0;
    state = scrolledAway(e.target) ? "off" : "";
  }, { passive: true });
  panel.addEventListener("touchmove", (e) => {
    if (state === "off") return;
    const d = e.touches[0].clientY - y0;
    if (state === "") {
      if (Math.abs(d) < 8 && Math.abs(e.touches[0].clientX - x0) < 8) return;
      if (d <= 0 || Math.abs(e.touches[0].clientX - x0) > d) { state = "off"; return; }
      state = "drag";
      panel.style.transition = "none";
    }
    e.preventDefault();   // no scroll, no page bounce under the drag
    dy = Math.max(0, d);
    panel.style.transform = `translateY(${dy}px)`;
  }, { passive: false });
  panel.addEventListener("touchend", () => {
    if (state !== "drag") return;
    state = "";
    const fast = dy / Math.max(1, Date.now() - t0) > 0.5;
    panel.style.transition = "transform 0.18s ease";
    if (dy > 90 || (fast && dy > 30)) {
      panel.style.transform = "translateY(100%)";
      setTimeout(() => { if (panel.isConnected) closeSheet(); }, 180);
    } else {
      panel.style.transform = "";
    }
  });
  // the system took the touch over: back into place
  panel.addEventListener("touchcancel", () => {
    if (state !== "drag") return;
    state = "";
    panel.style.transition = "transform 0.18s ease";
    panel.style.transform = "";
  });
}

// One text field in a sheet, instead of the browser's prompt(), which the
// system draws white whatever the theme. Resolves with the trimmed text, or
// null on Cancel (a tap outside or Esc closes it without resolving).
function textSheet(title, { value = "", placeholder = "", maxlength = 500, plain = false } = {}) {
  return new Promise((resolve) => {
    const input = el("input", { type: "text", value, placeholder, maxlength,
      autocapitalize: plain ? "off" : "sentences", autocorrect: plain ? "off" : "on",
      spellcheck: plain ? "false" : "true", enterkeyhint: "done" });
    const finish = (v) => { closeSheet(); resolve(v); };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); finish(input.value.trim()); }
    });
    rawSheet(el("div", null,
      el("div", { class: "sheet-title" }, title),
      input,
      el("div", { class: "a-buttons" },
        el("button", { class: "btn primary", onclick: () => finish(input.value.trim()) }, "OK"),
        el("button", { class: "btn", onclick: () => finish(null) }, "Cancel"))));
    input.focus();
    input.setSelectionRange(value.length, value.length);
  });
}

function sheet(title, items, head) {
  const body = el("div", { class: "sheet-body" });
  if (title) body.append(el("div", { class: "sheet-title" }, title));
  if (head) body.append(...head);
  for (const it of items) {
    if (!it) continue;
    body.append(el("button", { class: "sheet-item" + (it.danger ? " danger" : ""), onclick: async () => {
      closeSheet();
      try { await it.action?.(); } catch (e) { toast(e.message, true); }
    } },
      el("span", { class: "si-icon" }, it.icon || ""),
      el("div", { class: "si-main" }, it.label, it.sub ? el("div", { class: "si-sub" }, it.sub) : null)));
  }
  rawSheet(body);
}

// With a keyboard, Confirm takes the focus after the approval cards' grace
// period (⏎ confirms, esc cancels), so the ⏎ that sent /clear cannot also
// confirm it. Not on a phone: leaving the message box drops its keyboard.
// danger: a red button with its own label; ⏎ goes to Cancel instead, and at
// once, so neither a stray Enter nor the button under the sheet that still
// holds the focus (a tab card after its ✕) carries anything out
function confirmSheet(title, subtitle, action, danger) {
  const body = el("div");
  const ok = el("button", { class: danger ? "btn danger" : "btn primary", onclick: async () => { closeSheet(); try { await action(); } catch (e) { toast(e.message, true); } } },
    danger || "Confirm", danger ? null : kHint("⏎"));
  const cancel = el("button", { class: "btn", onclick: closeSheet }, "Cancel", danger ? kHint("⏎") : null);
  body.append(
    el("div", { class: "sheet-title" }, title),
    el("div", { class: "a-desc", style: "margin-bottom:14px" }, subtitle),
    el("div", { class: "a-buttons" }, ok, cancel));
  rawSheet(body);
  if (danger) {
    document.activeElement?.blur();
    kbd.cur = cancel;
    if (finePointer()) cancel.focus();
    return;
  }
  if (!finePointer()) return;
  setTimeout(() => {
    const a = document.activeElement;
    if (!ok.isConnected || (isTyping(a) && a.value)) return;
    kbd.cur = ok;
    ok.focus();
  }, KBD_GRACE);
}

function closeSheet() {
  S.sheetKeys = null;
  $("#sheet-root").replaceChildren();
  if (S.viewerOpen) {   // closed by Close, esc, a tap outside, a swipe or another sheet
    S.viewerOpen = false;
    S.depth -= 1;
    skipPopUntil = Date.now() + 1000;
    history.back();
  }
}

function toast(text, isErr) {
  const t = el("div", { class: "toast" + (isErr ? " error" : "") }, text);
  $("#toast-root").append(t);
  setTimeout(() => t.remove(), 3200);
}

// ---------------------------------------------------------------- library lists

const LIB_KINDS = {
  skills: { title: "Skills", key: "skills", icon: "◆" },
  commands: { title: "Commands", key: "commands", icon: "⌘" },
  agents: { title: "Agents", key: "agents", icon: "⑃" },
  mcp: { title: "MCPs", key: "mcp_servers", icon: "⚡" },
};

async function renderLibraryList(kind) {
  const cfg = LIB_KINDS[kind];
  setTopbar(menuBtn(), tbTitle(cfg.title), tabsBtn());
  const view = $("#view");
  view.replaceChildren(el("div", { class: "pad" }, el("div", { class: "empty" }, "Loading…")));
  let lib;
  try {
    lib = await loadLibrary();
  } catch (e) { view.replaceChildren(el("div", { class: "pad" }, el("div", { class: "empty" }, e.message))); return; }
  if (S.view?.name !== kind) return;

  const items = lib[cfg.key] || [];
  const { node } = searchableList({
    items,
    keys: ["name", "description", "detail", "source"],
    placeholder: `Search ${items.length} ${cfg.title.toLowerCase()}…`,
    // claude.ai connectors belong to the account, not to a config file
    // this list reads; only a running claude reports them
    empty: kind === "mcp" && !items.length
      ? "No MCP servers in config files. claude.ai connectors are listed in a session's info (ⓘ)."
      : "Nothing here.",
    toNode: (it) => bigItem({
      mono: kind === "commands" || kind === "mcp",
      title: kind === "commands" ? "/" + it.name : it.name,
      badge: scopeBadge(it),
      sub: kind === "mcp"
        ? it.transport + (it.detail ? " · " + it.detail : "")
        : (it.description || ""),
      onclick: () => libDetailSheet(kind, it),
      key: (it.scope || "") + ":" + it.name,
    }),
  });
  view.replaceChildren(el("div", { class: "pad" }, node));
}

function libDetailSheet(kind, it) {
  const body = el("div", { class: "sheet-body" });
  body.append(el("div", { class: "sheet-title", style: "font-size:16px" },
    (kind === "commands" ? "/" : "") + it.name, " ", scopeBadge(it)));
  if (it.description) body.append(el("div", { class: "lib-detail-desc" }, it.description));
  if (it.argument_hint) body.append(kvRow("arguments", it.argument_hint));
  if (it.model) body.append(kvRow("model", it.model));
  if (it.tools) body.append(kvRow("tools", it.tools));
  if (it.transport) body.append(kvRow("transport", it.transport));
  if (it.detail) body.append(kvRow("endpoint", it.detail));
  if (it.path) body.append(kvRow("path", shortPath(it.path)));
  rawSheet(body);
}

// ---------------------------------------------------------------- physical keyboard

// Shortcuts never fire while a text field has the focus; Esc leaves it. On a
// phone without a keyboard nothing changes (hints show only with a fine pointer).
// "any-pointer", not "pointer": an iPad with a trackpad keyboard still reports
// the touch screen as its primary pointer.
function finePointer() { return matchMedia("(any-pointer: fine)").matches; }

// Home / End in a prompt box go to the start / end of the line, as on a PC
// (macOS scrolls the page and leaves the caret); ⇧ extends the selection.
// A line here is a line of text up to ⏎, not a visually wrapped row.
function homeEnd(input, e) {
  if (!["Home", "End"].includes(e.key) || e.metaKey || e.ctrlKey || e.altKey
      || e.isComposing) return false;
  const v = input.value;
  const back = input.selectionDirection === "backward";
  const anchor = back ? input.selectionEnd : input.selectionStart;
  const focus = back ? input.selectionStart : input.selectionEnd;
  let to;
  // lastIndexOf clamps a -1 start to 0, so a caret at 0 would find a ⏎ there
  if (e.key === "Home") to = focus > 0 ? v.lastIndexOf("\n", focus - 1) + 1 : 0;
  else { to = v.indexOf("\n", focus); if (to < 0) to = v.length; }
  e.preventDefault();
  if (!e.shiftKey) input.setSelectionRange(to, to);
  else if (to < anchor) input.setSelectionRange(to, anchor, "backward");
  else input.setSelectionRange(anchor, to, "forward");
  return true;
}
const KBD_GRACE = 700;    // ms a new approval card ignores keys (no double-⏎)
const KBD_IDLE = 2000;    // ms without typing before an approval takes the focus
const kbd = { card: null, since: 0, typedAt: 0, timer: 0, key: null, g: 0, cur: null };

function kHint(k) { return el("kbd", { class: "k-hint" }, k); }

const isTyping = (t) => !!t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
const overlayOpen = () => $("#sheet-root").children.length || $("#drawer-root").children.length;

function kbdReset() {
  clearTimeout(kbd.timer);
  kbd.card = null; kbd.key = null; kbd.g = 0;
}

// Keys act on the oldest unresolved approval card. When it changes, its
// Allow takes the focus (⏎ approves, like the terminal) — but only after the
// grace period, and only if the message box is empty and idle.
function kbdCards() {
  const card = S.view?.name === "chat" ? chatUI.msgs?.querySelector(".action-card:not(.resolved)") || null : null;
  if (card === kbd.card) return;
  const prev = kbd.card;
  prev?.classList.remove("kbd-target");
  clearTimeout(kbd.timer);
  kbd.card = card;
  kbd.since = Date.now();
  if (!card) {
    // the last card is answered and its buttons are gone (focus fell to the
    // body): give the message box the focus so a reply needs no click — only
    // with a keyboard, a phone would pop its on-screen one
    const a = document.activeElement;
    if (prev?.classList.contains("resolved") && finePointer() && !overlayOpen()
        && (!a || a === document.body || chatUI.msgs?.contains(a))) {
      $("#chat-input")?.focus({ preventScroll: true });
    }
    return;
  }
  card.classList.add("kbd-target");
  const tryFocus = () => {
    if (kbd.card !== card || card.classList.contains("resolved") || overlayOpen()) return;
    const a = document.activeElement;
    const input = $("#chat-input");
    if (a === input) {
      // moving the focus would drop a phone's on-screen keyboard
      if (input.value || !finePointer()) return;
      const wait = kbd.typedAt + KBD_IDLE - Date.now();
      if (wait > 0) { kbd.timer = setTimeout(tryFocus, wait); return; }
    } else if (a && a !== document.body && !chatUI.msgs?.contains(a)) return;
    card.querySelector('[data-k~="1"]')?.focus({ preventScroll: !nearBottom() });
  };
  kbd.timer = setTimeout(tryFocus, KBD_GRACE);
}

// what ↑/↓ walk through: the open sheet or drawer, else the current list
function kbdItems() {
  const root = $("#sheet-root").children.length ? $("#sheet-root")
    : $("#drawer-root").children.length ? $("#drawer-root") : $("#view");
  const sel = root.id === "view" ? ".s-card, .tab-card, .vs-row, .item, .btn:not(.fab)" : "button";
  return [...root.querySelectorAll(sel)].filter((n) => n.offsetParent !== null);
}

// kbd.cur: the item the keys last moved to. Safari (also as a Dock web app)
// does not always report a focused button as document.activeElement, and
// the walk then restarted at the end, so every move took two presses.
function kbdMove(dir) {
  const items = kbdItems();
  if (!items.length) return;
  let i = items.indexOf(document.activeElement);
  if (i < 0) i = items.indexOf(kbd.cur);
  const next = items[i < 0 ? (dir > 0 ? 0 : items.length - 1)
    : Math.min(items.length - 1, Math.max(0, i + dir))];
  kbd.cur = next;
  next.focus();
  next.scrollIntoView({ block: "nearest", inline: "nearest" });
  kbd.key = next.dataset.kbd || null;
}

function keysSheet() {
  const rows = [
    ["move in lists, menus, sheets", "↑ ↓  j k"],
    ["open / press", "⏎"],
    ["item menu (⋯)", "."],
    ["search · message box in a chat", "/"],
    ["message box", "i"],
    ["in it: start · end of the line (⇧ selects)", "home · end"],
    ["close sheet, leave a text field", "esc"],
    ["back", "⌫  ["],
    ["Home · Sessions · Projects · Tabs", "g h · g s · g p · g t"],
    ["Allow · Always · Deny · Deny with note", "1 y · 2 · 3 n · 4"],
    ["answer a question · plan choice", "1–9"],
    ["file: scroll · page · prev/next · download · close", "↑ ↓ · space · ← → · d · ⏎"],
  ];
  rawSheet(el("div", { class: "sheet-body" },
    el("div", { class: "sheet-title" }, "Keyboard shortcuts"),
    rows.map(([k, v]) => kvRow(k, v)),
    el("div", { class: "a-desc", style: "margin-top:12px" },
      "A new approval takes the focus when the message box is empty and "
      + "you have not typed for 2 s; ⏎ then allows. Keys wait 0.7 s after a "
      + "card appears, so a second ⏎ does not approve the next one. Once the "
      + "last card is answered, the message box has the focus again.")));
}

document.addEventListener("keydown", (e) => {
  const t = e.target;
  if (t?.closest?.(".xterm")) return;   // True View: every key belongs to the terminal
  if (e.key === "Escape") {
    if (overlayOpen()) { closeSheet(); closeDrawer(); }
    else if (isTyping(t)) t.blur();
    return;
  }
  if (e.defaultPrevented || e.isComposing || e.ctrlKey || e.metaKey || e.altKey) return;
  if (isTyping(t)) {
    // ↓ from a search box goes on to the list under it
    if (e.key === "ArrowDown" && t.type === "search") { e.preventDefault(); kbdMove(1); }
    return;
  }
  if (!S.authed || S.locked || S.passkeyGate || S.view?.name === "term") return;

  const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  const go = (fn) => { e.preventDefault(); fn(); };
  // a sheet with keys of its own (the file viewer)
  if (S.sheetKeys && $("#sheet-root").children.length && S.sheetKeys(key)) { e.preventDefault(); return; }
  if (kbd.g) {
    const fresh = Date.now() - kbd.g < 1500;
    kbd.g = 0;
    const to = { h: "home", s: "sessions", p: "projects", t: "tabs" }[key];
    if (fresh && to) return go(() => nav({ name: to }));
  }
  if (key === "ArrowDown" || key === "ArrowRight" || key === "j") {
    if (S.view?.name !== "chat" || overlayOpen()) go(() => kbdMove(1));
    return;
  }
  if (key === "ArrowUp" || key === "ArrowLeft" || key === "k") {
    if (S.view?.name !== "chat" || overlayOpen()) go(() => kbdMove(-1));
    return;
  }
  if (overlayOpen()) {
    // ⏎ on a focused button presses it natively; where Safari lost track of
    // the focus, press the item the arrows moved to
    const cur = kbd.cur;
    if (key === "Enter" && t?.tagName !== "BUTTON" && cur?.isConnected
        && ($("#sheet-root").contains(cur) || $("#drawer-root").contains(cur))) go(() => cur.click());
    return;
  }
  if (key === "?") return go(keysSheet);
  if (key === "g") { kbd.g = Date.now(); return; }
  if (key === "Backspace" || key === "[") return go(goBack);   // lists too, where there is no ‹

  if (S.view?.name === "chat") {
    const card = kbd.card;
    if (card && !card.classList.contains("resolved") && Date.now() - kbd.since >= KBD_GRACE) {
      // ⏎ on a focused button presses it natively; unfocused, the card's default
      const k = key === "Enter" ? (t?.tagName === "BUTTON" ? null : "enter") : key;
      const btn = k && card.querySelector(`[data-k~="${CSS.escape(k)}"]`);
      if (btn) return go(() => btn.click());
    }
    if (key === "/" || key === "i") return go(() => $("#chat-input")?.focus());
    return;
  }
  if (key === ".") {
    const m = document.activeElement?.querySelector?.(".menu-btn, .tc-menu");
    if (m) go(() => m.click());
    return;
  }
  if (key === "/") {
    const search = $("#view .search-input");
    if (search) return go(() => search.focus());
    if (S.view?.name === "home") return go(projectQuickOpen);
  }
});

document.addEventListener("pointerdown", () => { kbd.key = null; }, true);

// Home, Sessions and Tabs re-render on every status update; put the keyboard
// focus back on the same item (by its data-kbd key) when that happens.
new MutationObserver(() => {
  if (!kbd.key || (document.activeElement && document.activeElement !== document.body)) return;
  const n = [...$("#view").querySelectorAll("[data-kbd]")].find((x) => x.dataset.kbd === kbd.key);
  n?.focus({ preventScroll: true });
}).observe($("#view"), { childList: true, subtree: true });

// ---------------------------------------------------------------- iOS keyboard

if (window.visualViewport) {
  const vv = window.visualViewport;
  const root = document.documentElement;
  // the height with no keyboard: the web view's own
  const full = document.createElement("div");
  full.style.cssText = "position:fixed;top:0;bottom:0;width:0;visibility:hidden";
  document.body.append(full);
  // iOS either shrinks the visual viewport and leaves the page, or scrolls
  // the page as well; both ways the visual viewport is what is left above
  // the keyboard, so #app is fitted to it. When iOS scrolls, it animates
  // the scroll but reports only its end, so the top bar blinks out while
  // the keyboard slides in; focusing the box from the top of the screen to
  // avoid the scroll left the chat unscrollable above the keyboard.
  // WebKit bug 301857 (iOS 26, home-screen apps): after the keyboard the
  // web view can stay short by the status bar, with black below it, until
  // the app is killed (re-measure tricks did not bring it back). The black
  // band already keeps the box off the home indicator, so its inset and
  // the composer's bottom gap go. Touch only: an installed desktop app
  // window is always shorter than the screen and would lose its gap too.
  const standalone = navigator.maxTouchPoints > 0
    && (navigator.standalone || matchMedia("(display-mode: standalone)").matches);
  const short = () => {
    const tall = matchMedia("(orientation: portrait)").matches ? screen.height : screen.width;
    return standalone && window.innerHeight < tall - 20;
  };
  let open = false;
  const update = () => {
    // the chat keeps what sat at its bottom edge there, like a native app:
    // the reply being read stays above the keyboard instead of under it
    const m = chatUI.msgs?.isConnected ? chatUI.msgs : null;
    const fromBottom = m ? m.scrollHeight - m.scrollTop - m.clientHeight : 0;
    open = full.offsetHeight - vv.height > 150;
    if (open) {
      root.style.setProperty("--app-h", vv.height + "px");
      root.style.setProperty("--app-top", vv.offsetTop + "px");
      root.style.setProperty("--sab", "0px");
    } else {
      for (const p of ["--app-h", "--app-top", "--sab", "--c-gap"]) root.style.removeProperty(p);
      if (short()) { root.style.setProperty("--sab", "0px"); root.style.setProperty("--c-gap", "0px"); }
    }
    // only when the height changed: a write would stop a running fling
    const want = m ? m.scrollHeight - m.clientHeight - fromBottom : 0;
    if (m && Math.abs(m.scrollTop - want) > 1) m.scrollTop = want;
  };
  vv.addEventListener("resize", update);
  vv.addEventListener("scroll", update);
  update();   // a web view already short at launch
}

// ---------------------------------------------------------------- boot

async function boot() {
  if (!T) {
    T = new LocalTransport();
    T.onMessage(handleWS);
    T.onStatus(onTransportStatus);
  }

  // one-time pairing code from the QR link — fragment only, never query strings
  const mLogin = (location.hash || "").match(/^#login=([A-Za-z0-9-]+)$/);
  if (mLogin) {
    history.replaceState(null, "", location.pathname);
    try { await redeemCode(mLogin[1]); } catch {}
  }

  await finishBoot();
}

let bootRetry = 0, bootTimer = null;

async function finishBoot() {
  clearTimeout(bootTimer);
  try {
    const state = await T.rpc("state");
    S.authed = true;
    S.sessions = state.sessions;
    S.defaults = state.defaults;
    S.canTerminal = !!state.can_terminal;
    S.passkeyDevice = !!state.passkey_device;
    S.requirePasskey = !!state.require_passkey;
    S.hasRoots = state.has_roots !== false;
    S.canIncognito = !!state.can_incognito;
    S.canManage = !!state.can_manage;
    S.canFiles = !!state.can_files;
    S.canUpload = !!state.can_upload;
    S.sdkAttention = !!state.sdk_attention;
    S.home = state.home;
    for (const s of S.sessions) S.status[s.sid] = { state: s.state, detail: s.detail };
    pruneTabs();
    pruneDrafts();
    // warm caches so home and the drawer paint instantly
    loadProjects().then(() => { if (S.view?.name === "home") renderHome(); }).catch(() => {});
    loadLibrary().catch(() => {});
    signalAcceptedPasskeys();   // prune keys removed while the app was closed
    // a reload keeps the entries and their depths; a fresh start has none
    if (history.state?.d == null) history.replaceState({ d: 0 }, "");
    S.depth = history.state.d;
    if (history.state.viewer) {   // reloaded with the file viewer open
      S.depth -= 1;
      skipPopUntil = Date.now() + 1000;
      history.back();
    }
    S.view = routeFromHash();
    bootRetry = 0;
    render();
  } catch (e) {
    if (S.locked || S.passkeyGate) { render(); return; }
    S.authed = false;
    // only a refused cookie (ws close 4401) means signing in again; a
    // server that is down, restarting or cannot read its device list
    // (4503) keeps the cookie good, so wait for it
    if (e?.message === "unauthorized") { renderLogin(); return; }
    renderUnreachable();
    bootTimer = setTimeout(finishBoot, Math.min(8000, 1000 * 2 ** bootRetry++));
  }
}

function renderUnreachable() {
  setTopbar();
  $("#view").replaceChildren(
    el("div", { class: "login-box" },
      el("div", { class: "glyph" }, "❯_"),
      el("h1", null, "Clicker4AI"),
      el("p", null, S.serverError
        ? "Server error — the device list can't be read. Contact the server administrator. Retrying…"
        : "Can't reach the server — retrying…")));
}

boot();
