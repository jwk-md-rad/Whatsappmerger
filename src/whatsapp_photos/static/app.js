"use strict";

const grid = document.getElementById("grid");
const form = document.getElementById("filters");
const statusEl = document.getElementById("status");
const lightbox = document.getElementById("lightbox");
const fullImg = document.getElementById("full");
const meta = document.getElementById("meta");
const clearBtn = document.getElementById("clear-filters");
const browseSection = document.getElementById("browse");
document.getElementById("close-lb").addEventListener("click", () => lightbox.close());

let nextOffset = 0;
let total = 0;
let loadedHits = [];
let currentIndex = -1;
let chatJidToId = null;
let chatById = new Map();   // id (string) -> {name, photo_count}
let senderByJid = new Map(); // jid -> {name, photo_count}
let suppressUrlSync = false;

function fmtDate(unix) {
  if (!unix) return "";
  const d = new Date(unix * 1000);
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function fmtDateFull(unix) {
  if (!unix) return "";
  const d = new Date(unix * 1000);
  return d.toLocaleString(undefined, {
    year: "numeric", month: "long", day: "numeric",
    hour: "numeric", minute: "2-digit",
  });
}

function fmtRelDate(unix) {
  if (!unix) return "";
  const now = Date.now() / 1000;
  const diff = now - unix;
  const day = 86400;
  if (diff < day) return "today";
  if (diff < 2 * day) return "yesterday";
  if (diff < 7 * day) return `${Math.floor(diff / day)} days ago`;
  if (diff < 30 * day) return `${Math.floor(diff / (7 * day))} weeks ago`;
  if (diff < 365 * day) return fmtDate(unix);
  return fmtDate(unix);
}

function dateToUnix(input) {
  if (!input) return null;
  const d = new Date(input + "T00:00:00");
  return Math.floor(d.getTime() / 1000);
}

function unixToDateInput(unix) {
  if (!unix) return "";
  const d = new Date(unix * 1000);
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

function currentFilters() {
  const fd = new FormData(form);
  return {
    q: fd.get("q") || "",
    chat_id: fd.get("chat_id") || "",
    sender: fd.get("sender") || "",
    type: fd.get("type") || "",
    since: fd.get("since") || "",
    until: fd.get("until") || "",
    order: fd.get("order") || "newest",
  };
}

function hasAnyFilter(f) {
  return Boolean(f.q || f.chat_id || f.sender || f.type || f.since || f.until);
}

function buildQuery(reset) {
  const f = currentFilters();
  const params = new URLSearchParams();
  if (f.q) params.set("q", f.q);
  if (f.chat_id) params.set("chat_id", f.chat_id);
  if (f.sender) params.set("sender", f.sender);
  if (f.type) params.set("type", f.type);
  const since = dateToUnix(f.since);
  if (since) params.set("since", since);
  const until = dateToUnix(f.until);
  if (until) params.set("until", until + 86399);
  if (f.order) params.set("order", f.order);
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
  updateBrowseVisibility();
  statusEl.textContent = `${total.toLocaleString()} message${total === 1 ? "" : "s"}`;
  updateDocTitle();
  if (reset) syncUrlFromForm();
}

function renderEmptyState() {
  const existing = document.getElementById("empty-state");
  if (existing) existing.remove();
  if (total !== 0) return;
  const div = document.createElement("div");
  div.id = "empty-state";
  div.className = "empty-state";
  div.innerHTML = `
    <svg class="empty-state-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>
    </svg>
    <div>No photos match these filters.</div>
    <div style="margin-top:0.4rem"><a href="#" id="empty-clear">Clear filters</a> and try again.</div>
  `;
  div.querySelector("#empty-clear").addEventListener("click", (e) => {
    e.preventDefault();
    clearFilters();
  });
  grid.appendChild(div);
}

function renderCard(hit, index) {
  const card = document.createElement("div");
  card.className = "card card-" + hit.type;
  card.tabIndex = 0;
  // Audio / video cards play inline — no lightbox click. Image and text
  // open the lightbox as before.
  const lightboxable = hit.type === "image" || hit.type === "text";
  if (lightboxable) {
    card.addEventListener("click", (e) => {
      if (e.target.closest(".pivot")) return;
      openLightbox(index);
    });
    card.addEventListener("keypress", (e) => { if (e.key === "Enter") openLightbox(index); });
  }

  if (hit.type === "image") {
    const img = document.createElement("img");
    img.loading = "lazy";
    img.src = hit.thumb_url;
    img.alt = hit.body || `Photo from ${hit.chat_name || hit.chat_jid}`;
    card.appendChild(img);
  } else if (hit.type === "audio") {
    const wrap = document.createElement("div");
    wrap.className = "media-wrap audio-wrap";
    const audio = document.createElement("audio");
    audio.controls = true;
    audio.preload = "none";
    audio.src = hit.media_url;
    wrap.appendChild(audio);
    card.appendChild(wrap);
  } else if (hit.type === "video") {
    const wrap = document.createElement("div");
    wrap.className = "media-wrap video-wrap";
    const video = document.createElement("video");
    video.controls = true;
    video.preload = "metadata";
    video.src = hit.media_url;
    wrap.appendChild(video);
    card.appendChild(wrap);
  } else {
    // Text bubble takes the place of the image area.
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    if (hit.snippet) {
      bubble.innerHTML = safeSnippet(hit.snippet);
    } else {
      bubble.textContent = hit.body || "";
    }
    card.appendChild(bubble);
  }

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
  line2.textContent = fmtRelDate(hit.sent_at);
  line2.title = fmtDateFull(hit.sent_at);
  info.appendChild(line2);

  // For images, show caption snippet beneath the photo (text cards
  // already use the body as the bubble).
  if (hit.type === "image") {
    if (hit.snippet) {
      const cap = document.createElement("div");
      cap.className = "caption";
      cap.innerHTML = safeSnippet(hit.snippet);
      info.appendChild(cap);
    } else if (hit.body) {
      const cap = document.createElement("div");
      cap.className = "caption";
      cap.textContent = hit.body;
      info.appendChild(cap);
    }
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
  if (fieldName === "chat_id") {
    setComboboxValue("chat", value);
  } else if (fieldName === "sender") {
    setComboboxValue("sender", value);
  }
  runSearch(true);
  window.scrollTo({ top: 0, behavior: "smooth" });
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
  const date = fmtDateFull(hit.sent_at);
  const fromName = hit.is_from_me ? "Me" : (hit.sender_name || hit.sender_jid || "?");
  const chatName = hit.chat_name || hit.chat_jid;
  const stage = document.querySelector(".lb-stage");
  const textPanel = document.getElementById("lb-text");

  if (hit.type === "image") {
    fullImg.src = hit.photo_url;
    fullImg.hidden = false;
    textPanel.hidden = true;
    stage.classList.remove("text-mode");
  } else {
    fullImg.src = "";
    fullImg.hidden = true;
    textPanel.hidden = false;
    textPanel.textContent = hit.body || "";
    stage.classList.add("text-mode");
  }

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
  if (hit.type === "image") {
    sub.textContent =
      `${date} · ${hit.width || "?"}×${hit.height || "?"} · ${hit.filename}`;
  } else {
    sub.textContent = date;
  }
  meta.appendChild(sub);

  // For image messages, the body acts as caption; show it below.
  if (hit.type === "image" && hit.body) {
    const c = document.createElement("div");
    c.className = "caption-full";
    c.textContent = hit.body;
    meta.appendChild(c);
  }

  const nav = document.createElement("div");
  nav.className = "lb-nav";
  nav.textContent = `${index + 1} of ${loadedHits.length}${total > loadedHits.length ? ` (${total} total)` : ""}`;
  meta.appendChild(nav);

  const prev = document.getElementById("lb-prev");
  const next = document.getElementById("lb-next");
  prev.disabled = index === 0;
  next.disabled = index >= loadedHits.length - 1;

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

document.getElementById("lb-prev").addEventListener("click", (e) => {
  e.preventDefault();
  showNext(-1);
});
document.getElementById("lb-next").addEventListener("click", (e) => {
  e.preventDefault();
  showNext(1);
});

function clearFilters() {
  for (const input of form.querySelectorAll("input, select")) {
    if (input.type === "search" || input.type === "date" || input.type === "text") {
      input.value = "";
    } else if (input.type === "hidden") {
      input.value = "";
    }
  }
  for (const sel of form.querySelectorAll("select")) sel.selectedIndex = 0;
  for (const cb of form.querySelectorAll(".cb-input")) {
    cb.classList.remove("has-selection");
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

// ---------------------------------------------------------------------------
// Combobox: filterable dropdown that writes to a hidden form input.
// ---------------------------------------------------------------------------

const COMBOBOXES = {};   // name -> { input, hidden, list, options }

function setupCombobox(cbEl) {
  const name = cbEl.dataset.cb;
  const input = cbEl.querySelector(".cb-input");
  const hidden = cbEl.querySelector("input[type=hidden]");
  const list = cbEl.querySelector(".cb-list");
  const state = { input, hidden, list, options: [], activeIndex: -1 };
  COMBOBOXES[name] = state;

  input.addEventListener("focus", () => showList(state, ""));
  input.addEventListener("input", () => {
    hidden.value = "";
    input.classList.remove("has-selection");
    showList(state, input.value);
  });
  input.addEventListener("keydown", (e) => onComboboxKey(e, state));
  // Hide list on blur, with a delay so click handlers on list items fire.
  input.addEventListener("blur", () => setTimeout(() => hideList(state), 150));
}

function setComboboxOptions(name, options) {
  const state = COMBOBOXES[name];
  if (!state) return;
  state.options = options;
}

function showList(state, filter) {
  const f = filter.trim().toLowerCase();
  const matches = (f
    ? state.options.filter((o) => o.label.toLowerCase().includes(f))
    : state.options
  ).slice(0, 200);

  state.list.innerHTML = "";
  state.activeIndex = -1;
  if (!matches.length) {
    const li = document.createElement("li");
    li.className = "cb-empty";
    li.textContent = f ? `No matches for "${filter}"` : "(no options)";
    state.list.appendChild(li);
    state.list.hidden = false;
    return;
  }
  for (const [i, opt] of matches.entries()) {
    const li = document.createElement("li");
    li.dataset.value = opt.value;
    li.dataset.label = opt.label;
    li.setAttribute("role", "option");
    li.textContent = opt.label;
    if (opt.count !== undefined) {
      const c = document.createElement("span");
      c.className = "cb-count";
      c.textContent = `(${opt.count})`;
      li.appendChild(c);
    }
    li.addEventListener("mousedown", (e) => {
      e.preventDefault();
      selectComboboxOption(state, opt.value, opt.label);
    });
    state.list.appendChild(li);
  }
  state.list.hidden = false;
}

function hideList(state) {
  state.list.hidden = true;
}

function selectComboboxOption(state, value, label) {
  state.input.value = label;
  state.input.classList.add("has-selection");
  state.hidden.value = value;
  hideList(state);
  runSearch(true);
}

function onComboboxKey(e, state) {
  const items = Array.from(state.list.querySelectorAll("li:not(.cb-empty)"));
  if (e.key === "ArrowDown") {
    if (state.list.hidden) showList(state, state.input.value);
    state.activeIndex = Math.min(items.length - 1, state.activeIndex + 1);
    highlightActive(state, items);
    e.preventDefault();
  } else if (e.key === "ArrowUp") {
    state.activeIndex = Math.max(0, state.activeIndex - 1);
    highlightActive(state, items);
    e.preventDefault();
  } else if (e.key === "Enter") {
    if (state.activeIndex >= 0 && items[state.activeIndex]) {
      const li = items[state.activeIndex];
      selectComboboxOption(state, li.dataset.value, li.dataset.label);
      e.preventDefault();
    } else if (items.length === 1) {
      // Single match: pick it on Enter.
      const li = items[0];
      selectComboboxOption(state, li.dataset.value, li.dataset.label);
      e.preventDefault();
    }
  } else if (e.key === "Escape") {
    hideList(state);
  }
}

function highlightActive(state, items) {
  for (const [i, li] of items.entries()) {
    li.classList.toggle("active", i === state.activeIndex);
  }
  if (state.activeIndex >= 0) {
    items[state.activeIndex].scrollIntoView({ block: "nearest" });
  }
}

function setComboboxValue(name, value) {
  const state = COMBOBOXES[name];
  if (!state) return;
  const opt = state.options.find((o) => String(o.value) === String(value));
  if (!opt) return;
  state.input.value = opt.label;
  state.input.classList.add("has-selection");
  state.hidden.value = String(opt.value);
  hideList(state);
}

// ---------------------------------------------------------------------------
// Browse panel: top chats + top senders as visual tiles.
// ---------------------------------------------------------------------------

function renderBrowsePanel(chats, senders) {
  const chatRow = document.getElementById("browse-chats");
  const senderRow = document.getElementById("browse-senders");
  chatRow.innerHTML = "";
  senderRow.innerHTML = "";

  for (const c of chats.slice(0, 12)) {
    chatRow.appendChild(makeTile({
      name: c.name || c.jid,
      count: c.message_count != null ? c.message_count : c.photo_count,
      sample: c.sample_photo_id,
      // Chat tiles open the chronological thread view directly — it's
      // what users want when picking a chat to read. To filter the
      // search instead, click the chat name in any card.
      onClick: () => { window.location.href = `/thread/${c.id}`; },
    }));
  }
  for (const s of senders.slice(0, 12)) {
    if (!s.sender_jid) continue;
    senderRow.appendChild(makeTile({
      name: s.sender_name || s.sender_jid,
      count: s.photo_count,
      sample: s.sample_photo_id,
      onClick: () => pivotTo("sender", s.sender_jid),
    }));
  }
}

function makeTile({ name, count, sample, onClick }) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "tile" + (sample ? "" : " tile-placeholder");
  btn.title = `${name} · ${count} photo${count === 1 ? "" : "s"}`;
  btn.addEventListener("click", onClick);

  if (sample) {
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = "";
    img.src = `/api/thumb/${sample}?size=320`;
    btn.appendChild(img);
  }

  const overlay = document.createElement("div");
  overlay.className = "tile-overlay";
  btn.appendChild(overlay);

  const textWrap = document.createElement("div");
  textWrap.className = "tile-text";

  const nameEl = document.createElement("div");
  nameEl.className = "tile-name";
  nameEl.textContent = name;
  textWrap.appendChild(nameEl);

  const countEl = document.createElement("div");
  countEl.className = "tile-count";
  countEl.textContent = `${count} photo${count === 1 ? "" : "s"}`;
  textWrap.appendChild(countEl);

  btn.appendChild(textWrap);
  return btn;
}

function updateBrowseVisibility() {
  if (!browseSection) return;
  browseSection.hidden = hasAnyFilter(currentFilters());
}

// ---------------------------------------------------------------------------
// URL state: keep ?q=…&chat_id=…&sender=… in sync with the form.
// ---------------------------------------------------------------------------

function syncUrlFromForm() {
  if (suppressUrlSync) return;
  const f = currentFilters();
  const params = new URLSearchParams();
  if (f.q) params.set("q", f.q);
  if (f.chat_id) params.set("chat_id", f.chat_id);
  if (f.sender) params.set("sender", f.sender);
  if (f.type) params.set("type", f.type);
  if (f.since) params.set("since", f.since);
  if (f.until) params.set("until", f.until);
  if (f.order && f.order !== "newest") params.set("order", f.order);
  const qs = params.toString();
  const newUrl = qs ? `?${qs}` : window.location.pathname;
  history.replaceState({ filters: f }, "", newUrl);
}

function applyUrlToForm() {
  const url = new URL(window.location.href);
  const q = url.searchParams.get("q") || "";
  const chat_id = url.searchParams.get("chat_id") || "";
  const sender = url.searchParams.get("sender") || "";
  const type = url.searchParams.get("type") || "";
  const since = url.searchParams.get("since") || "";
  const until = url.searchParams.get("until") || "";
  const order = url.searchParams.get("order") || "newest";

  form.querySelector("input[name=q]").value = q;
  form.querySelector("input[name=since]").value = since;
  form.querySelector("input[name=until]").value = until;
  form.querySelector("select[name=order]").value = order;
  form.querySelector("select[name=type]").value = type;

  if (chat_id) setComboboxValue("chat", chat_id);
  if (sender) setComboboxValue("sender", sender);
}

function updateDocTitle() {
  const f = currentFilters();
  const parts = [];
  if (f.q) parts.push(`"${f.q}"`);
  if (f.chat_id && chatById.has(f.chat_id)) parts.push(chatById.get(f.chat_id).name);
  if (f.sender && senderByJid.has(f.sender)) parts.push(senderByJid.get(f.sender).name);
  const suffix = parts.length ? ` — ${parts.join(" · ")}` : "";
  document.title = `WhatsApp Photo Archive${suffix}`;
}

window.addEventListener("popstate", (e) => {
  suppressUrlSync = true;
  try {
    applyUrlToForm();
    runSearch(true);
  } finally {
    suppressUrlSync = false;
  }
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

async function loadFilterOptions() {
  const [chats, senders] = await Promise.all([
    fetch("/api/chats").then(r => r.json()),
    fetch("/api/senders").then(r => r.json()),
  ]);
  chatJidToId = new Map();
  for (const c of chats) {
    chatJidToId.set(c.jid, c.id);
    chatById.set(String(c.id), { name: c.name || c.jid, photo_count: c.photo_count });
  }
  for (const s of senders) {
    if (s.sender_jid) {
      senderByJid.set(s.sender_jid, { name: s.sender_name || s.sender_jid, photo_count: s.photo_count });
    }
  }

  // Combobox options.
  setComboboxOptions("chat", chats.map((c) => ({
    value: String(c.id),
    label: `${c.name || c.jid} (${c.photo_count})`,
    count: undefined,  // count is part of the label already
  })));
  setComboboxOptions("sender", senders
    .filter((s) => s.sender_jid)
    .map((s) => ({
      value: s.sender_jid,
      label: `${s.sender_name || s.sender_jid} (${s.photo_count})`,
      count: undefined,
    })));

  renderBrowsePanel(chats, senders);
}

for (const cbEl of document.querySelectorAll(".combobox")) setupCombobox(cbEl);

form.addEventListener("submit", (e) => { e.preventDefault(); runSearch(true); });

// Type filter is a regular select — submit immediately on change.
form.querySelector("select[name=type]").addEventListener("change", () => runSearch(true));
form.querySelector("select[name=order]").addEventListener("change", () => runSearch(true));

loadFilterOptions().then(() => {
  applyUrlToForm();
  runSearch(true);
});
