"use strict";

const grid = document.getElementById("grid");
const form = document.getElementById("filters");
const statusEl = document.getElementById("status");
const lightbox = document.getElementById("lightbox");
const fullImg = document.getElementById("full");
const meta = document.getElementById("meta");
const clearBtn = document.getElementById("clear-filters");
document.getElementById("close-lb").addEventListener("click", () => lightbox.close());

let nextOffset = 0;
let total = 0;
let loadedHits = [];
let currentIndex = -1;
let chatJidToId = null;

function fmtDate(unix) {
  if (!unix) return "";
  const d = new Date(unix * 1000);
  return d.toLocaleString();
}

function dateToUnix(input) {
  if (!input) return null;
  const d = new Date(input + "T00:00:00");
  return Math.floor(d.getTime() / 1000);
}

function buildQuery(reset) {
  const fd = new FormData(form);
  const params = new URLSearchParams();
  if (fd.get("q")) params.set("q", fd.get("q"));
  if (fd.get("chat_id")) params.set("chat_id", fd.get("chat_id"));
  if (fd.get("sender")) params.set("sender", fd.get("sender"));
  const since = dateToUnix(fd.get("since"));
  if (since) params.set("since", since);
  const until = dateToUnix(fd.get("until"));
  if (until) params.set("until", until + 86399);
  if (fd.get("order")) params.set("order", fd.get("order"));
  if (reset) nextOffset = 0;
  params.set("offset", nextOffset);
  params.set("limit", 60);
  return params;
}

async function runSearch(reset) {
  const params = buildQuery(reset);
  statusEl.textContent = "Searching…";
  const r = await fetch("/api/search?" + params.toString());
  if (!r.ok) {
    statusEl.textContent = "Search failed: " + r.status;
    return;
  }
  const data = await r.json();
  if (reset) {
    grid.innerHTML = "";
    loadedHits = [];
  }
  total = data.total;
  for (const hit of data.results) {
    if (chatJidToId) hit._chat_id = chatJidToId.get(hit.chat_jid);
    loadedHits.push(hit);
    grid.appendChild(renderCard(hit, loadedHits.length - 1));
  }
  if (data.next_offset !== null) {
    nextOffset = data.next_offset;
    appendLoadMore();
  } else {
    nextOffset = total;
  }
  renderEmptyState();
  statusEl.textContent = `${total} photo${total === 1 ? "" : "s"}`;
}

function renderEmptyState() {
  const existing = document.getElementById("empty-state");
  if (existing) existing.remove();
  if (total !== 0) return;
  const div = document.createElement("div");
  div.id = "empty-state";
  div.className = "empty-state";
  div.innerHTML = `No photos match these filters. <a href="#" id="empty-clear">Clear filters</a> and try again.`;
  div.querySelector("#empty-clear").addEventListener("click", (e) => {
    e.preventDefault();
    clearFilters();
  });
  grid.appendChild(div);
}

function renderCard(hit, index) {
  const card = document.createElement("div");
  card.className = "card";
  card.tabIndex = 0;
  card.addEventListener("click", (e) => {
    if (e.target.closest(".pivot")) return;
    openLightbox(index);
  });
  card.addEventListener("keypress", (e) => { if (e.key === "Enter") openLightbox(index); });

  const img = document.createElement("img");
  img.loading = "lazy";
  img.src = hit.thumb_url;
  img.alt = hit.caption || `Photo from ${hit.chat_name || hit.chat_jid}`;
  card.appendChild(img);

  const info = document.createElement("div");
  info.className = "info";
  const fromName = hit.is_from_me ? "Me" : (hit.sender_name || hit.sender_jid || "?");
  const chatName = hit.chat_name || hit.chat_jid;

  const line1 = document.createElement("div");
  const fromEl = makePivot(fromName, "sender", hit.sender_jid, hit.is_from_me);
  fromEl.classList.add("from");
  line1.appendChild(fromEl);
  line1.appendChild(document.createTextNode(" · "));
  line1.appendChild(makePivot(chatName, "chat_id", String(hit._chat_id || ""), false));
  info.appendChild(line1);

  const line2 = document.createElement("div");
  line2.textContent = fmtDate(hit.taken_at);
  info.appendChild(line2);

  if (hit.snippet) {
    const cap = document.createElement("div");
    cap.className = "caption";
    cap.innerHTML = safeSnippet(hit.snippet);
    info.appendChild(cap);
  } else if (hit.caption) {
    const cap = document.createElement("div");
    cap.className = "caption";
    cap.textContent = hit.caption;
    info.appendChild(cap);
  }
  card.appendChild(info);
  return card;
}

function makePivot(label, fieldName, value, disabled) {
  const span = document.createElement("span");
  span.textContent = label;
  if (disabled || !value) return span;
  span.className = "pivot";
  span.title = `Show only items where ${fieldName === "sender" ? "sender" : "chat"} = ${label}`;
  span.tabIndex = 0;
  const activate = (e) => {
    e.stopPropagation();
    e.preventDefault();
    pivotTo(fieldName, value);
  };
  span.addEventListener("click", activate);
  span.addEventListener("keypress", (e) => { if (e.key === "Enter") activate(e); });
  return span;
}

function pivotTo(fieldName, value) {
  const select = form.querySelector(`select[name=${fieldName}]`);
  if (!select) return;
  if ([...select.options].some((o) => o.value === value)) {
    select.value = value;
    runSearch(true);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
}

function safeSnippet(raw) {
  // SQLite snippet() returns text with raw <mark> markers but does not
  // escape anything else. Escape the whole string, then restore the
  // mark tags we control.
  return escapeHtml(raw)
    .replace(/&lt;mark&gt;/g, "<mark>")
    .replace(/&lt;\/mark&gt;/g, "</mark>");
}

function appendLoadMore() {
  const old = document.getElementById("load-more");
  if (old) old.remove();
  const btn = document.createElement("button");
  btn.id = "load-more";
  btn.textContent = "Load more";
  btn.addEventListener("click", () => { btn.remove(); runSearch(false); });
  grid.appendChild(btn);
}

function openLightbox(index) {
  currentIndex = index;
  const hit = loadedHits[index];
  fullImg.src = hit.photo_url;
  const date = fmtDate(hit.taken_at);
  const fromName = hit.is_from_me ? "Me" : (hit.sender_name || hit.sender_jid || "?");
  const chatName = hit.chat_name || hit.chat_jid;

  meta.innerHTML = "";
  const head = document.createElement("div");
  const fromEl = makePivot(fromName, "sender", hit.sender_jid, hit.is_from_me);
  fromEl.classList.add("from");
  const strong = document.createElement("strong");
  strong.appendChild(fromEl);
  head.appendChild(strong);
  head.appendChild(document.createTextNode(" in "));
  head.appendChild(makePivot(chatName, "chat_id", String(hit._chat_id || ""), false));
  meta.appendChild(head);

  const sub = document.createElement("div");
  sub.textContent =
    `${date} · ${hit.width || "?"}×${hit.height || "?"} · ${hit.filename}`;
  meta.appendChild(sub);

  if (hit.caption) {
    const c = document.createElement("div");
    c.className = "caption-full";
    c.textContent = hit.caption;
    meta.appendChild(c);
  }

  const nav = document.createElement("div");
  nav.className = "lb-nav";
  nav.textContent = `${index + 1} of ${loadedHits.length}${total > loadedHits.length ? ` (${total} total)` : ""}`;
  meta.appendChild(nav);

  if (!lightbox.open) lightbox.showModal();
}

function showNext(delta) {
  if (currentIndex < 0) return;
  const next = currentIndex + delta;
  if (next < 0 || next >= loadedHits.length) return;
  openLightbox(next);
}

lightbox.addEventListener("keydown", (e) => {
  if (e.key === "ArrowRight") { e.preventDefault(); showNext(1); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); showNext(-1); }
});

function clearFilters() {
  form.reset();
  // Selects don't always reset to first option on form.reset() if a
  // value was set programmatically; force it.
  for (const sel of form.querySelectorAll("select")) sel.selectedIndex = 0;
  for (const input of form.querySelectorAll("input")) {
    if (input.type === "search" || input.type === "date") input.value = "";
  }
  runSearch(true);
}

clearBtn.addEventListener("click", clearFilters);

function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

async function loadFilterOptions() {
  const [chats, senders] = await Promise.all([
    fetch("/api/chats").then(r => r.json()),
    fetch("/api/senders").then(r => r.json()),
  ]);
  const chatSel = form.querySelector("select[name=chat_id]");
  // Map chat_jid → chat id so the pivot UI in cards (which only sees
  // jid) can populate the chat_id filter dropdown.
  chatJidToId = new Map();
  for (const c of chats) {
    chatJidToId.set(c.jid, c.id);
    const o = document.createElement("option");
    o.value = c.id;
    o.textContent = `${c.name || c.jid} (${c.photo_count})`;
    chatSel.appendChild(o);
  }
  const senderSel = form.querySelector("select[name=sender]");
  for (const s of senders) {
    if (!s.sender_jid) continue;
    const o = document.createElement("option");
    o.value = s.sender_jid;
    o.textContent = `${s.sender_name || s.sender_jid} (${s.photo_count})`;
    senderSel.appendChild(o);
  }
}

form.addEventListener("submit", (e) => { e.preventDefault(); runSearch(true); });

loadFilterOptions().then(() => runSearch(true));
