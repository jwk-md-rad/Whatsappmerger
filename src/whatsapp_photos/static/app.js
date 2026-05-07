"use strict";

const grid = document.getElementById("grid");
const form = document.getElementById("filters");
const statusEl = document.getElementById("status");
const lightbox = document.getElementById("lightbox");
const fullImg = document.getElementById("full");
const meta = document.getElementById("meta");
document.getElementById("close-lb").addEventListener("click", () => lightbox.close());

let nextOffset = 0;
let lastQuery = {};
let total = 0;

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
  const r = await fetch("/api/search?" + params.toString());
  if (!r.ok) {
    statusEl.textContent = "Search failed: " + r.status;
    return;
  }
  const data = await r.json();
  if (reset) grid.innerHTML = "";
  total = data.total;
  for (const hit of data.results) {
    grid.appendChild(renderCard(hit));
  }
  if (data.next_offset !== null) {
    nextOffset = data.next_offset;
    appendLoadMore();
  } else {
    nextOffset = total;
  }
  statusEl.textContent = `${total} photo${total === 1 ? "" : "s"}`;
}

function renderCard(hit) {
  const card = document.createElement("div");
  card.className = "card";
  card.tabIndex = 0;
  card.addEventListener("click", () => openLightbox(hit));
  card.addEventListener("keypress", (e) => { if (e.key === "Enter") openLightbox(hit); });

  const img = document.createElement("img");
  img.loading = "lazy";
  img.src = hit.thumb_url;
  img.alt = hit.caption || hit.filename;
  card.appendChild(img);

  const info = document.createElement("div");
  info.className = "info";
  const from = hit.is_from_me ? "Me" : (hit.sender_name || hit.sender_jid || "?");
  const where = hit.is_group ? `${hit.chat_name || hit.chat_jid}` : (hit.chat_name || hit.chat_jid);
  info.innerHTML = `<div><span class="from">${escapeHtml(from)}</span> · ${escapeHtml(where)}</div>` +
                   `<div>${escapeHtml(fmtDate(hit.taken_at))}</div>`;
  if (hit.snippet) {
    const cap = document.createElement("div");
    cap.className = "caption";
    cap.innerHTML = hit.snippet;
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

function appendLoadMore() {
  const old = document.getElementById("load-more");
  if (old) old.remove();
  const btn = document.createElement("button");
  btn.id = "load-more";
  btn.textContent = "Load more";
  btn.addEventListener("click", () => { btn.remove(); runSearch(false); });
  grid.appendChild(btn);
}

function openLightbox(hit) {
  fullImg.src = hit.photo_url;
  const date = fmtDate(hit.taken_at);
  const from = hit.is_from_me ? "Me" : (hit.sender_name || hit.sender_jid || "?");
  meta.innerHTML =
    `<strong>${escapeHtml(from)}</strong> in ${escapeHtml(hit.chat_name || hit.chat_jid)}<br>` +
    `${escapeHtml(date)} · ${hit.width || "?"}×${hit.height || "?"} · ${escapeHtml(hit.filename)}`;
  if (hit.caption) {
    const c = document.createElement("div");
    c.className = "caption-full";
    c.textContent = hit.caption;
    meta.appendChild(c);
  }
  lightbox.showModal();
}

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
  for (const c of chats) {
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
