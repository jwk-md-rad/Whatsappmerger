"use strict";

const listEl = document.getElementById("chats-list");
const loadingEl = document.getElementById("chats-loading");
const emptyEl = document.getElementById("chats-empty");
const filterEl = document.getElementById("chat-filter");

const SENDER_PALETTE = [
  "#ef6e6e", "#f59e6b", "#e0bd4d", "#a3d067",
  "#5bd1a8", "#5cc5e6", "#7c8aff", "#c084f5",
];

function chatColor(jid) {
  if (!jid) return "var(--fg-secondary)";
  let h = 0;
  for (let i = 0; i < jid.length; i++) h = (h * 31 + jid.charCodeAt(i)) | 0;
  return SENDER_PALETTE[Math.abs(h) % SENDER_PALETTE.length];
}

function initials(name) {
  if (!name) return "?";
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  const first = parts[0][0] || "";
  const second = parts.length > 1 ? parts[parts.length - 1][0] : "";
  return (first + second).toUpperCase();
}

function fmtRelative(unix) {
  if (!unix) return "";
  const d = new Date(unix * 1000);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const yesterday = new Date(today); yesterday.setDate(yesterday.getDate() - 1);
  const dayStart = new Date(d); dayStart.setHours(0, 0, 0, 0);
  if (dayStart.getTime() === today.getTime()) {
    return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  }
  if (dayStart.getTime() === yesterday.getTime()) return "Yesterday";
  const diffDays = Math.floor((today - dayStart) / 86400000);
  if (diffDays > 0 && diffDays < 7) {
    return d.toLocaleDateString(undefined, { weekday: "long" });
  }
  if (d.getFullYear() === today.getFullYear()) {
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function fmtFull(unix) {
  if (!unix) return "";
  return new Date(unix * 1000).toLocaleString(undefined, {
    year: "numeric", month: "long", day: "numeric",
    hour: "numeric", minute: "2-digit",
  });
}

const TYPE_LABEL = {
  image: "Photo",
  video: "Video",
  audio: "Voice note",
};

function previewText(chat) {
  const t = chat.last_message_type;
  const body = (chat.last_message_body || "").replace(/\s+/g, " ").trim();
  const mediaLabel = TYPE_LABEL[t];
  if (mediaLabel) {
    return body ? `${mediaLabel} · ${body}` : mediaLabel;
  }
  return body;
}

function previewPrefix(chat) {
  if (chat.last_message_from_me) return "You: ";
  if (chat.is_group && chat.last_message_sender) {
    return `${chat.last_message_sender}: `;
  }
  return "";
}

function renderRow(chat) {
  const a = document.createElement("a");
  a.className = "chat-row";
  a.href = `/thread/${chat.id}`;
  // Search key: chat name + every distinct sender name. Lets typing
  // someone's name surface group chats they're in, not just chats
  // literally titled with their name.
  const searchParts = [chat.name || "", chat.jid || "", chat.sender_names || ""];
  // toLocaleLowerCase rather than toLowerCase so non-Latin scripts
  // (e.g. Turkish İ vs i) fold the way the user's locale expects.
  a.dataset.searchKey = searchParts.join(" ").toLocaleLowerCase();

  const avatar = document.createElement("div");
  avatar.className = "chat-avatar";
  avatar.style.background = chatColor(chat.jid);
  avatar.textContent = initials(chat.name || chat.jid);
  if (chat.is_group) avatar.classList.add("is-group");
  a.appendChild(avatar);

  const body = document.createElement("div");
  body.className = "chat-body";

  const topRow = document.createElement("div");
  topRow.className = "chat-top-row";
  const name = document.createElement("div");
  name.className = "chat-name";
  name.textContent = chat.name || chat.jid || "(no name)";
  topRow.appendChild(name);

  const time = document.createElement("div");
  time.className = "chat-time";
  time.textContent = fmtRelative(chat.last_message_at);
  if (chat.last_message_at) time.title = fmtFull(chat.last_message_at);
  topRow.appendChild(time);
  body.appendChild(topRow);

  const bottomRow = document.createElement("div");
  bottomRow.className = "chat-bottom-row";
  const preview = document.createElement("div");
  preview.className = "chat-preview";
  const prefix = previewPrefix(chat);
  const text = previewText(chat);
  if (prefix || text) {
    if (prefix) {
      const p = document.createElement("span");
      p.className = "chat-preview-prefix";
      p.textContent = prefix;
      preview.appendChild(p);
    }
    preview.appendChild(document.createTextNode(text || ""));
  } else {
    preview.classList.add("muted");
    const n = chat.message_count;
    preview.textContent = `${n.toLocaleString()} message${n === 1 ? "" : "s"}`;
  }
  bottomRow.appendChild(preview);

  if (chat.message_count) {
    const count = document.createElement("div");
    count.className = "chat-count";
    count.textContent = chat.message_count.toLocaleString();
    count.title = `${chat.message_count.toLocaleString()} messages in this chat`;
    bottomRow.appendChild(count);
  }
  body.appendChild(bottomRow);

  a.appendChild(body);
  return a;
}

function applyFilter() {
  const q = filterEl.value.trim().toLocaleLowerCase();
  const rows = listEl.querySelectorAll(".chat-row");
  let visible = 0;
  for (const row of rows) {
    const match = !q || row.dataset.searchKey.includes(q);
    row.hidden = !match;
    if (match) visible++;
  }
  emptyEl.hidden = visible !== 0 || rows.length === 0;
  if (visible === 0 && rows.length > 0) {
    emptyEl.hidden = false;
    emptyEl.textContent = `No chats match “${filterEl.value}”.`;
  }
}

(async () => {
  try {
    const r = await fetch("/api/chats");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const chats = await r.json();
    loadingEl.remove();
    listEl.removeAttribute("aria-busy");
    if (!chats.length) {
      emptyEl.hidden = false;
      return;
    }
    const frag = document.createDocumentFragment();
    for (const c of chats) frag.appendChild(renderRow(c));
    listEl.appendChild(frag);
  } catch (e) {
    loadingEl.textContent = "Failed to load chats. Check the server log.";
  }
})();

filterEl.addEventListener("input", applyFilter);
