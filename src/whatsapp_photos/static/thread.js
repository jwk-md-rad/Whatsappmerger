"use strict";

const CHAT_ID = window.CHAT_ID;
const IS_GROUP = window.IS_GROUP;
const TOTAL_MESSAGES = window.MESSAGE_COUNT;

const stage = document.getElementById("thread-stage");
const messagesEl = document.getElementById("thread-messages");
const topLoader = document.getElementById("thread-top-loader");
const bottomLoader = document.getElementById("thread-bottom-loader");
const histWrap = document.getElementById("histogram-wrap");
const histEl = document.getElementById("histogram");
const histAxis = document.getElementById("histogram-axis");
const jumpDate = document.getElementById("jump-date");

const lightbox = document.getElementById("lightbox");
const fullImg = document.getElementById("full");
const lbText = document.getElementById("lb-text");
const meta = document.getElementById("meta");
document.getElementById("close-lb").addEventListener("click", () => lightbox.close());
document.getElementById("lb-prev").addEventListener("click", (e) => { e.preventDefault(); lbStep(-1); });
document.getElementById("lb-next").addEventListener("click", (e) => { e.preventDefault(); lbStep(1); });

const BATCH = 100;

// Cursor-based state. `loaded` is always oldest-first.
let loaded = [];
let hasMoreOlder = true;
let hasMoreNewer = false;   // initial load is the latest, so nothing newer
let loadingOlder = false;
let loadingNewer = false;
let lbImageIndex = -1;
let highlightTargetId = null; // bubble id to flash after a jump

// ---------------------------------------------------------------------------
// Loading
// ---------------------------------------------------------------------------

async function fetchThread(params) {
  const qs = new URLSearchParams(params).toString();
  const r = await fetch(`/api/thread/${CHAT_ID}?${qs}`);
  if (!r.ok) throw new Error(`thread fetch failed: ${r.status}`);
  return r.json();
}

async function loadOlder() {
  if (!hasMoreOlder || loadingOlder) return;
  loadingOlder = true;
  topLoader.hidden = false;
  topLoader.textContent = "Loading older messages…";

  const heightBefore = stage.scrollHeight;
  const scrollTopBefore = stage.scrollTop;

  try {
    const params = { limit: BATCH };
    if (loaded.length) params.before = loaded[0].sent_at;
    const data = await fetchThread(params);
    const batch = data.messages;
    hasMoreOlder = batch.length === BATCH;
    if (batch.length) {
      loaded = batch.concat(loaded);
      prependBatch(batch);
      const heightAfter = stage.scrollHeight;
      stage.scrollTop = scrollTopBefore + (heightAfter - heightBefore);
    }
    if (!hasMoreOlder) {
      topLoader.textContent = `Beginning of conversation · ${loaded.length.toLocaleString()} messages loaded`;
    } else {
      topLoader.hidden = true;
    }
  } catch (e) {
    topLoader.textContent = "Failed to load older.";
  } finally {
    loadingOlder = false;
  }
}

async function loadNewer() {
  if (!hasMoreNewer || loadingNewer) return;
  loadingNewer = true;
  bottomLoader.textContent = "Loading newer messages…";
  try {
    const params = { limit: BATCH, after: loaded[loaded.length - 1].sent_at };
    const data = await fetchThread(params);
    const batch = data.messages;
    hasMoreNewer = batch.length === BATCH;
    if (batch.length) {
      loaded = loaded.concat(batch);
      appendBatch(batch);
    }
    bottomLoader.textContent = hasMoreNewer ? "" : "";
  } catch (e) {
    bottomLoader.textContent = "Failed to load newer.";
  } finally {
    loadingNewer = false;
  }
}

async function jumpToUnix(unix) {
  // Wipe and reload around `unix`.
  loaded = [];
  hasMoreOlder = true;
  hasMoreNewer = true;
  messagesEl.innerHTML = "";
  topLoader.hidden = true;
  bottomLoader.textContent = "Loading…";

  const data = await fetchThread({ limit: BATCH, around: unix });
  const batch = data.messages;
  const beforeCount = data.before_count || 0;
  const afterCount = batch.length - beforeCount;
  const half = Math.floor(BATCH / 2);
  const tail = BATCH - half;
  hasMoreOlder = beforeCount === half;
  hasMoreNewer = afterCount === tail;

  loaded = batch;
  // Find the anchor: the first message at or after `unix`.
  let anchorIdx = batch.findIndex((m) => m.sent_at >= unix);
  if (anchorIdx === -1) anchorIdx = batch.length - 1;
  highlightTargetId = batch[anchorIdx] ? batch[anchorIdx].id : null;

  renderAll(batch);

  // Scroll the anchor into view.
  if (highlightTargetId != null) {
    const el = messagesEl.querySelector(`[data-message-id="${highlightTargetId}"]`);
    if (el) {
      el.scrollIntoView({ block: "center", behavior: "auto" });
      el.classList.add("bubble-highlight");
      setTimeout(() => el.classList.remove("bubble-highlight"), 2000);
    }
  }

  bottomLoader.textContent = "";
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function renderAll(batch) {
  // Build all bubbles fresh from a (oldest-first) batch.
  messagesEl.innerHTML = "";
  appendBatch(batch);
}

function buildFragment(batch) {
  // Build a fragment with date separators and bubbles in oldest-first order.
  const frag = document.createDocumentFragment();
  let prevDay = null;
  let prevSender = null;
  for (const m of batch) {
    const day = dayKey(m.sent_at);
    if (day !== prevDay) {
      frag.appendChild(renderDateSeparator(m.sent_at));
      prevDay = day;
      prevSender = null;
    }
    const sameSender = m.sender_jid != null && m.sender_jid === prevSender && !m.is_from_me;
    frag.appendChild(renderBubble(m, !sameSender));
    prevSender = m.is_from_me ? "__me__" : m.sender_jid;
  }
  return frag;
}

function prependBatch(batch) {
  const frag = buildFragment(batch);
  const existingFirst = messagesEl.firstElementChild;
  const batchLastDay = batch.length ? dayKey(batch[batch.length - 1].sent_at) : null;
  if (existingFirst && existingFirst.classList.contains("date-separator")) {
    const existingDay = existingFirst.dataset.day;
    if (existingDay === batchLastDay) existingFirst.remove();
  }
  messagesEl.insertBefore(frag, messagesEl.firstChild);
}

function appendBatch(batch) {
  // If the new batch's first day matches the last existing day, skip the
  // duplicate date separator at the start of the fragment.
  const existingLast = messagesEl.lastElementChild;
  let prevDay = null;
  if (existingLast) {
    // Walk back to find the previous bubble's day.
    let el = existingLast;
    while (el && !el.classList.contains("bubble-row")) el = el.previousElementSibling;
    if (el) {
      const messageId = el.dataset.messageId;
      const found = loaded.find((m) => String(m.id) === messageId);
      if (found) prevDay = dayKey(found.sent_at);
    }
  }
  const frag = document.createDocumentFragment();
  let prevSender = null;
  for (const m of batch) {
    const day = dayKey(m.sent_at);
    if (day !== prevDay) {
      frag.appendChild(renderDateSeparator(m.sent_at));
      prevDay = day;
      prevSender = null;
    }
    const sameSender = m.sender_jid != null && m.sender_jid === prevSender && !m.is_from_me;
    frag.appendChild(renderBubble(m, !sameSender));
    prevSender = m.is_from_me ? "__me__" : m.sender_jid;
  }
  messagesEl.appendChild(frag);
}

function renderDateSeparator(unix) {
  const div = document.createElement("div");
  div.className = "date-separator";
  div.dataset.day = dayKey(unix);
  const span = document.createElement("span");
  span.textContent = humanDay(unix);
  div.appendChild(span);
  return div;
}

function renderBubble(m, showSender) {
  const row = document.createElement("div");
  row.className = "bubble-row " + (m.is_from_me ? "mine" : "theirs");
  row.dataset.messageId = String(m.id);

  const bubble = document.createElement("div");
  bubble.className = "thread-bubble";

  if (IS_GROUP && showSender && !m.is_from_me) {
    const s = document.createElement("div");
    s.className = "sender-name";
    s.textContent = m.sender_name || m.sender_jid || "?";
    s.style.color = senderColor(m.sender_jid);
    bubble.appendChild(s);
  }

  if (m.type === "image" && m.thumb_url) {
    const img = document.createElement("img");
    img.className = "bubble-image";
    img.loading = "lazy";
    img.src = m.thumb_url + "?size=480";
    img.alt = m.body || "";
    img.addEventListener("click", () => openLightboxFor(m.id));
    bubble.appendChild(img);
    if (m.body) {
      const cap = document.createElement("div");
      cap.className = "bubble-text";
      cap.textContent = m.body;
      bubble.appendChild(cap);
    }
  } else if (m.type === "audio" && m.media_url) {
    const audio = document.createElement("audio");
    audio.className = "bubble-audio";
    audio.controls = true;
    audio.preload = "none";
    audio.src = m.media_url;
    bubble.appendChild(audio);
    if (m.body) {
      const cap = document.createElement("div");
      cap.className = "bubble-text";
      cap.textContent = m.body;
      bubble.appendChild(cap);
    }
  } else if (m.type === "video" && m.media_url) {
    const video = document.createElement("video");
    video.className = "bubble-video";
    video.controls = true;
    video.preload = "metadata";
    video.src = m.media_url;
    bubble.appendChild(video);
    if (m.body) {
      const cap = document.createElement("div");
      cap.className = "bubble-text";
      cap.textContent = m.body;
      bubble.appendChild(cap);
    }
  } else {
    const text = document.createElement("div");
    text.className = "bubble-text";
    text.textContent = m.body || "";
    bubble.appendChild(text);
  }

  const time = document.createElement("div");
  time.className = "bubble-time";
  time.textContent = fmtTime(m.sent_at);
  time.title = fmtFull(m.sent_at);
  bubble.appendChild(time);

  row.appendChild(bubble);
  return row;
}

// ---------------------------------------------------------------------------
// Histogram (per-month message counts) — clickable scrubber.
// ---------------------------------------------------------------------------

let histBuckets = [];

async function loadHistogram() {
  try {
    const r = await fetch(`/api/histogram/${CHAT_ID}`);
    if (!r.ok) return;
    histBuckets = await r.json();
    if (!histBuckets.length) return;
    renderHistogram();
    histWrap.hidden = false;
    jumpDate.min = isoDate(histBuckets[0].start_unix);
    jumpDate.max = isoDate(histBuckets[histBuckets.length - 1].end_unix);
  } catch (e) {
    // Silent — histogram is a nice-to-have.
  }
}

function renderHistogram() {
  histEl.innerHTML = "";
  histAxis.innerHTML = "";
  const maxCount = Math.max(...histBuckets.map((b) => b.count));
  let lastYear = null;
  for (const b of histBuckets) {
    const bar = document.createElement("button");
    bar.type = "button";
    bar.className = "hist-bar";
    const ratio = b.count / maxCount;
    bar.style.height = `${Math.max(4, ratio * 100)}%`;
    bar.title = `${b.bucket} · ${b.count.toLocaleString()} messages`;
    bar.addEventListener("click", () => jumpToUnix(b.start_unix));
    histEl.appendChild(bar);

    const label = document.createElement("span");
    label.className = "hist-tick";
    const year = b.bucket.slice(0, 4);
    label.textContent = year !== lastYear ? year : "";
    histAxis.appendChild(label);
    lastYear = year;
  }
}

function isoDate(unix) {
  const d = new Date(unix * 1000);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

jumpDate.addEventListener("change", () => {
  const v = jumpDate.value;
  if (!v) return;
  const d = new Date(v + "T12:00:00");
  jumpToUnix(Math.floor(d.getTime() / 1000));
});

// ---------------------------------------------------------------------------
// Date helpers
// ---------------------------------------------------------------------------

function dayKey(unix) {
  const d = new Date(unix * 1000);
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

function humanDay(unix) {
  const d = new Date(unix * 1000);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const yesterday = new Date(today); yesterday.setDate(yesterday.getDate() - 1);
  const sameDay = (a, b) =>
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate();
  if (sameDay(d, today)) return "Today";
  if (sameDay(d, yesterday)) return "Yesterday";
  const diffDays = Math.floor((today - d) / 86400000);
  if (diffDays > 0 && diffDays < 7) {
    return d.toLocaleDateString(undefined, { weekday: "long" });
  }
  if (d.getFullYear() === today.getFullYear()) {
    return d.toLocaleDateString(undefined, { weekday: "short", month: "long", day: "numeric" });
  }
  return d.toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });
}

function fmtTime(unix) {
  return new Date(unix * 1000).toLocaleTimeString(undefined, {
    hour: "numeric", minute: "2-digit",
  });
}

function fmtFull(unix) {
  return new Date(unix * 1000).toLocaleString(undefined, {
    year: "numeric", month: "long", day: "numeric",
    hour: "numeric", minute: "2-digit",
  });
}

// ---------------------------------------------------------------------------
// Sender colors (group chats)
// ---------------------------------------------------------------------------

const SENDER_PALETTE = [
  "#ef6e6e", "#f59e6b", "#e0bd4d", "#a3d067",
  "#5bd1a8", "#5cc5e6", "#7c8aff", "#c084f5",
];

function senderColor(jid) {
  if (!jid) return "var(--fg-secondary)";
  let h = 0;
  for (let i = 0; i < jid.length; i++) h = (h * 31 + jid.charCodeAt(i)) | 0;
  return SENDER_PALETTE[Math.abs(h) % SENDER_PALETTE.length];
}

// ---------------------------------------------------------------------------
// Lightbox (image only — audio/video play inline)
// ---------------------------------------------------------------------------

function imageMessages() {
  return loaded.filter((m) => m.type === "image");
}

function openLightboxFor(messageId) {
  const imgs = imageMessages();
  lbImageIndex = imgs.findIndex((m) => m.id === messageId);
  if (lbImageIndex < 0) return;
  renderLightbox();
  if (!lightbox.open) lightbox.showModal();
}

function lbStep(delta) {
  const imgs = imageMessages();
  const next = lbImageIndex + delta;
  if (next < 0 || next >= imgs.length) return;
  lbImageIndex = next;
  renderLightbox();
}

function renderLightbox() {
  const imgs = imageMessages();
  const m = imgs[lbImageIndex];
  if (!m) return;
  fullImg.src = m.photo_url;
  fullImg.hidden = false;
  lbText.hidden = true;

  meta.innerHTML = "";
  const head = document.createElement("div");
  const fromName = m.is_from_me ? "Me" : (m.sender_name || m.sender_jid || "?");
  const strong = document.createElement("strong");
  strong.textContent = fromName;
  head.appendChild(strong);
  head.appendChild(document.createTextNode(` in ${window.CHAT_NAME}`));
  meta.appendChild(head);

  const sub = document.createElement("div");
  sub.textContent =
    `${fmtFull(m.sent_at)} · ${m.width || "?"}×${m.height || "?"} · ${m.filename}`;
  meta.appendChild(sub);

  if (m.body) {
    const c = document.createElement("div");
    c.className = "caption-full";
    c.textContent = m.body;
    meta.appendChild(c);
  }

  const nav = document.createElement("div");
  nav.className = "lb-nav";
  nav.textContent = `${lbImageIndex + 1} of ${imgs.length} images loaded`;
  meta.appendChild(nav);

  document.getElementById("lb-prev").disabled = lbImageIndex === 0;
  document.getElementById("lb-next").disabled = lbImageIndex >= imgs.length - 1;
}

lightbox.addEventListener("keydown", (e) => {
  if (e.key === "ArrowRight") { e.preventDefault(); lbStep(1); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); lbStep(-1); }
});

// ---------------------------------------------------------------------------
// Infinite scroll: top loads older, bottom loads newer (after a jump)
// ---------------------------------------------------------------------------

stage.addEventListener("scroll", () => {
  if (stage.scrollTop < 200) loadOlder();
  const distFromBottom = stage.scrollHeight - stage.scrollTop - stage.clientHeight;
  if (distFromBottom < 200) loadNewer();
});

// ---------------------------------------------------------------------------
// Boot — accept ?at=<unix> for deep-linked jump
// ---------------------------------------------------------------------------

(async () => {
  await loadHistogram();
  const url = new URL(window.location.href);
  const at = url.searchParams.get("at");
  if (at) {
    await jumpToUnix(parseInt(at, 10));
  } else {
    bottomLoader.textContent = "Loading messages…";
    await loadOlder();
    bottomLoader.textContent = "";
    stage.scrollTop = stage.scrollHeight;
  }
})();
