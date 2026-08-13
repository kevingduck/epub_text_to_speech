"use strict";
const $ = (sel) => document.querySelector(sel);

const player = $("#player");
let books = [];
let current = null;        // detail object of the open book
let pollTimer = null;
let lastPosSave = 0;

async function api(path, opts = {}) {
  const r = await fetch("/api" + path, opts);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

function fmt(ms) {
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + `:${String(sec).padStart(2, "0")}`;
}

function show(view) {
  $("#view-library").hidden = view !== "library";
  $("#view-player").hidden = view !== "player";
  window.scrollTo(0, 0);
}

// --- voices -----------------------------------------------------------------

async function loadVoices() {
  const voices = await api("/voices");
  for (const sel of [$("#upload-voice"), $("#rerender-voice")]) {
    sel.innerHTML = voices
      .map((v) => `<option value="${v.id}">${v.name}</option>`)
      .join("");
  }
}

// --- library ------------------------------------------------------------------

const STATUS_LABEL = {
  queued: "Queued",
  parsing: "Parsing…",
  synthesizing: "Synthesizing",
  encoding: "Encoding…",
};

function renderLibrary() {
  const list = $("#book-list");
  if (!books.length) {
    list.innerHTML = `<p class="muted">No books yet. Tap “+ Add book” and pick an EPUB.</p>`;
    return;
  }
  list.innerHTML = books
    .map((b) => {
      let badge = "";
      if (b.status === "done") {
        badge = `<span class="badge">${fmt(b.duration_ms || 0)}</span>`;
      } else if (b.status === "failed") {
        badge = `<span class="badge failed">Failed</span>
          <div class="muted error-text">${esc(b.error || "")}</div>`;
      } else {
        const pct = Math.round((b.progress || 0) * 100);
        badge = `<span class="badge working">${STATUS_LABEL[b.status] || b.status} ${
          b.status === "synthesizing" ? pct + "%" : ""
        }</span><div class="progress"><div style="width:${pct}%"></div></div>`;
      }
      return `<div class="book" data-id="${b.id}">
        <img src="/api/books/${b.id}/cover" onerror="this.outerHTML='<div class=no-cover>📖</div>'">
        <div class="info">
          <div class="title">${esc(b.title)}</div>
          <div class="muted">${esc(b.author || "")}</div>
          ${badge}
        </div>
      </div>`;
    })
    .join("");
  list.querySelectorAll(".book").forEach((el) => {
    el.onclick = () => {
      const b = books.find((x) => x.id === el.dataset.id);
      if (b.status === "done") openBook(b.id);
      else if (b.status === "failed") alert(b.error || "Job failed");
    };
  });
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function refreshLibrary() {
  books = await api("/books");
  renderLibrary();
  clearTimeout(pollTimer);
  const active = books.some((b) =>
    ["queued", "parsing", "synthesizing", "encoding"].includes(b.status));
  if (active && !document.hidden) pollTimer = setTimeout(refreshLibrary, 3000);
}

// --- upload ---------------------------------------------------------------------

$("#btn-upload").onclick = () => ($("#upload-form").hidden = false);
$("#btn-cancel-upload").onclick = () => ($("#upload-form").hidden = true);

$("#upload-form").onsubmit = async (e) => {
  e.preventDefault();
  const file = $("#file-input").files[0];
  if (!file) return;
  const status = $("#upload-status");
  status.textContent = "Uploading…";
  const fd = new FormData();
  fd.append("file", file);
  fd.append("voice", $("#upload-voice").value);
  try {
    await api("/books", { method: "POST", body: fd });
    status.textContent = "";
    $("#upload-form").hidden = true;
    $("#file-input").value = "";
    refreshLibrary();
  } catch (err) {
    status.textContent = "Upload failed: " + err.message;
  }
};

// --- player -----------------------------------------------------------------------

async function openBook(id) {
  current = await api(`/books/${id}`);
  $("#p-title").textContent = current.title;
  $("#p-author").textContent = current.author || "";
  const cover = $("#p-cover");
  cover.hidden = false;
  cover.src = `/api/books/${id}/cover`;
  cover.onerror = () => (cover.hidden = true);

  const { position_ms } = await api(`/books/${id}/position`);
  player.src = `/api/books/${id}/audio`;
  player.addEventListener(
    "loadedmetadata",
    () => {
      // Seeking before metadata silently no-ops on iOS.
      if (position_ms > 1000) player.currentTime = position_ms / 1000;
      player.playbackRate = parseFloat(localStorage.speed || "1");
    },
    { once: true }
  );

  const chapters = current.chapters.filter((c) => c.start_ms != null);
  $("#chapter-list").innerHTML = chapters
    .map((c) => `<button class="chapter" data-idx="${c.idx}" data-start="${c.start_ms}">
        <span>${esc(c.title)}</span><span class="time">${fmt(c.start_ms)}</span>
      </button>`)
    .join("");
  document.querySelectorAll(".chapter").forEach((el) => {
    el.onclick = () => {
      player.currentTime = +el.dataset.start / 1000;
      player.play();
    };
  });

  $("#chapter-toggles").innerHTML = current.chapters
    .map((c) => `<label><input type="checkbox" data-idx="${c.idx}" ${c.included ? "checked" : ""}>
        <span>${esc(c.title)} <span class="muted">(${(c.chars || 0).toLocaleString()} chars)</span></span>
      </label>`)
    .join("");
  $("#rerender-voice").value = current.voice;
  $("#rerender-box").open = false;

  setupMediaSession();
  $("#speed").value = localStorage.speed || "1";
  show("player");
}

function setupMediaSession() {
  if (!("mediaSession" in navigator)) return;
  navigator.mediaSession.metadata = new MediaMetadata({
    title: current.title,
    artist: current.author || "",
    artwork: [{ src: `/api/books/${current.id}/cover`, sizes: "512x512", type: "image/jpeg" }],
  });
  const ms = navigator.mediaSession;
  ms.setActionHandler("play", () => player.play());
  ms.setActionHandler("pause", () => player.pause());
  ms.setActionHandler("seekbackward", () => (player.currentTime -= 15));
  ms.setActionHandler("seekforward", () => (player.currentTime += 30));
  ms.setActionHandler("seekto", (e) => {
    if (e.seekTime != null) player.currentTime = e.seekTime;
  });
}

$("#btn-back").onclick = () => {
  savePosition();
  show("library");
  refreshLibrary();
};
$("#btn-back15").onclick = () => (player.currentTime -= 15);
$("#btn-fwd30").onclick = () => (player.currentTime += 30);
$("#btn-playpause").onclick = () => (player.paused ? player.play() : player.pause());
player.addEventListener("play", () => {
  $("#btn-playpause").textContent = "❚❚";
  // iOS occasionally resets the rate when playback restarts.
  player.playbackRate = parseFloat(localStorage.speed || "1");
});
player.addEventListener("pause", () => {
  $("#btn-playpause").textContent = "▶";
  savePosition();
});

$("#speed").onchange = () => {
  localStorage.speed = $("#speed").value;
  player.playbackRate = parseFloat($("#speed").value);
};

// --- position saving (best-effort; never load-bearing) -------------------------

function savePosition(useBeacon = false) {
  if (!current || !player.duration) return;
  const payload = JSON.stringify({
    position_ms: Math.floor(player.currentTime * 1000),
  });
  const url = `/api/books/${current.id}/position`;
  if (useBeacon && navigator.sendBeacon) {
    navigator.sendBeacon(url, new Blob([payload], { type: "application/json" }));
  } else {
    fetch(url, { method: "POST", body: payload,
                 headers: { "Content-Type": "application/json" } }).catch(() => {});
  }
}

player.addEventListener("timeupdate", () => {
  const now = Date.now();
  if (now - lastPosSave > 15000) {
    lastPosSave = now;
    savePosition();
  }
  // highlight current chapter
  const t = player.currentTime * 1000;
  document.querySelectorAll(".chapter").forEach((el) => {
    const start = +el.dataset.start;
    const next = el.nextElementSibling ? +el.nextElementSibling.dataset.start : Infinity;
    el.classList.toggle("current", t >= start && t < next);
  });
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) savePosition(true);
  else if (!$("#view-library").hidden) refreshLibrary();
});
window.addEventListener("pagehide", () => savePosition(true));

// --- rerender / delete ------------------------------------------------------------

$("#btn-rerender").onclick = async () => {
  const chapters = [...document.querySelectorAll("#chapter-toggles input")].map((el) => ({
    idx: +el.dataset.idx,
    included: el.checked,
  }));
  if (!chapters.some((c) => c.included)) return alert("Select at least one chapter.");
  await api(`/books/${current.id}/rerender`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ voice: $("#rerender-voice").value, chapters }),
  });
  show("library");
  refreshLibrary();
};

$("#btn-delete").onclick = async () => {
  if (!confirm(`Delete “${current.title}”?`)) return;
  player.pause();
  player.removeAttribute("src");
  player.load();
  await api(`/books/${current.id}`, { method: "DELETE" });
  current = null;
  show("library");
  refreshLibrary();
};

// --- init ---------------------------------------------------------------------------

loadVoices();
refreshLibrary();
