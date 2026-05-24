"use strict";

const CHAT_ID = window.CHAT_ID;
const IS_GROUP = window.IS_GROUP;
const TOTAL_MESSAGES = window.MESSAGE_COUNT;

const stage = document.getElementById("thread-stage");
const messagesEl = document.getElementById("thread-messages");
const topLoader = document.getElementById("thread-top-loader");
const bottomLoader = document.getElementById("thread-bottom-loader");

const lightbox = document.getElementById("lightbox");
const fullImg = document.getElementById("full");
const lbText = document.getElementById("lb-text");
const meta = document.getElementById("meta");
document.getElementById("close-lb").addEventListener("click", () => lightbox.close());
document.getElementById("lb-prev").addEventListener("click", (e) => { e.preventDefault(); lbStep(-1); });
document.getElementById("lb-next").addEventListener("click", (e) => { e.preventDefault(); lbStep(1); });

const BATCH = 100;

let oldestOffset = 0;     // next offset to fetch (newest-first paging)
let hasMore = true;
let loadingOlder = false;
// Loaded messages, kept oldest-first so DOM order matches array order.
let loaded = [];
// Index in `loaded` of image messages, for the lightbox nav.
let lbImageIndex = -1;

// ---------------------------------------------------------------------------
// Loading
// ---------------------------------------------------------------------------

async function loadOlder() {
  if (!hasMore || loadingOlder) return;
  loadingOlder = true;
  topLoader.hidden = false;
  topLoader.textContent = "Loading older messages…";

  const heightBefore = stage.scrollHeight;
  const scrollTopBefore = stage.scrollTop;

  try {
    const params = new URLSearchParams();
    params.set("chat_id", CHAT_ID);
    params.set("order", "newest");
    params.set("offset", oldestOffset);
    params.set("limit", BATCH);
    const r = await fetch("/api/search?" + params.toString());
    if (!r.ok) { topLoader.textContent = "Failed to load."; return; }
    const data = await r.json();
    if (data.next_offset === null) hasMore = false;
    oldestOffset += data.results.length;

    // API returns newest -> oldest. Reverse so the batch is oldest -> newest;
    // these are all older than what we already have, so prepend.
    const ordered = [...data.results].reverse();
    if (ordered.length) {
      loaded = ordered.concat(loaded);
      prependBatch(ordered);
    }

    // Preserve scroll: keep the user's currently visible message in view.
    const heightAfter = stage.scrollHeight;
    stage.scrollTop = scrollTopBefore + (heightAfter - heightBefore);

    if (!hasMore) {
      topLoader.textContent = `Beginning of conversation · ${loaded.length.toLocaleString()} messages`;
    } else {
      topLoader.hidden = true;
    }
  } finally {
    loadingOlder = false;
  }
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function prependBatch(batch) {
  // Build a fragment in oldest -> newest order.
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

  // De-dupe: if the existing first bubble's day matches the batch's last day,
  // the next separator (already present at top of existing content) is
  // redundant.
  const existingFirst = messagesEl.firstElementChild;
  const batchLastDay = batch.length ? dayKey(batch[batch.length - 1].sent_at) : null;
  if (existingFirst && existingFirst.classList.contains("date-separator")) {
    const existingDay = existingFirst.dataset.day;
    if (existingDay === batchLastDay) {
      existingFirst.remove();
    }
  }

  messagesEl.insertBefore(frag, messagesEl.firstChild);
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
// Lightbox
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
// Infinite scroll: load older when near the top
// ---------------------------------------------------------------------------

stage.addEventListener("scroll", () => {
  if (stage.scrollTop < 200) loadOlder();
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

bottomLoader.textContent = "Loading messages…";
loadOlder().then(() => {
  bottomLoader.textContent = "";
  // Initial scroll: newest at the bottom, like WhatsApp.
  stage.scrollTop = stage.scrollHeight;
});
