"use strict";

const $ = (sel) => document.querySelector(sel);
const SOURCE_LABELS = { youtube: "YouTube", reddit: "Reddit", hackernews: "Hacker News", devto: "DEV", medium: "Medium" };
const state = { feedType: "", libOffset: 0, topics: [], current: null };

// --- utilities ---------------------------------------------------------------

/** Build a DOM element. Children that are strings become text nodes (never parsed as HTML). */
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "class") el.className = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: options.body ? { "Content-Type": "application/json" } : {},
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const data = await response.json();
      detail = typeof data.detail === "string" ? data.detail : data.detail?.[0]?.msg || detail;
    } catch { /* not JSON */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

let toastTimer;
function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
}

function fmtDuration(seconds) {
  if (!seconds) return null;
  if (seconds < 3600) {
    const m = Math.floor(seconds / 60), s = seconds % 60;
    return `${m}:${String(s).padStart(2, "0")}`;
  }
  return `${Math.floor(seconds / 3600)}h ${Math.round((seconds % 3600) / 60)}m`;
}

function lengthLabel(item) {
  if (!item.duration_seconds) return item.content_type === "video" ? "video" : "? min";
  return item.content_type === "video"
    ? fmtDuration(item.duration_seconds)
    : `${Math.round(item.duration_seconds / 60)} min read`;
}

function timeAgo(iso) {
  if (!iso) return "";
  const diff = (Date.now() - new Date(iso)) / 1000;
  const units = [[31536000, "y"], [2592000, "mo"], [86400, "d"], [3600, "h"], [60, "m"]];
  for (const [secs, label] of units) if (diff >= secs) return `${Math.floor(diff / secs)}${label} ago`;
  return "just now";
}

const prio = (p) => h("span", { class: `prio prio-${p}` }, `P${p}`);

/** A "a · b · c" line; empty parts are skipped. */
const metaLine = (...parts) => h("div", { class: "meta" },
  ...parts.filter((p) => p != null && p !== "" && p !== false)
    .flatMap((p, i) => [i ? h("span", { "aria-hidden": "true" }, "·") : null, p instanceof Node ? p : h("span", {}, p)]));

// --- cards -------------------------------------------------------------------

function card(item, onChange) {
  const thumb = h("button", { class: "thumb", onclick: () => openItem(item, onChange), "aria-label": `Open ${item.title}` },
    item.content_type === "video" ? "▶" : "📖",
    item.thumbnail_url && h("img", { src: item.thumbnail_url, alt: "", loading: "lazy", referrerpolicy: "no-referrer",
      onerror: (e) => e.target.remove() }),
    h("span", { class: "badge" }, lengthLabel(item)));

  const toggle = async (field, okMessage) => {
    try {
      const updated = await api(`/api/content/${item.id}`, { method: "PATCH", body: { [field]: !item[field] } });
      Object.assign(item, updated);
      toast(item[field] ? okMessage : "Undone");
      onChange?.(item, field);
    } catch (err) { toast(err.message); }
  };

  return h("article", { class: `card${item.completed ? " done" : ""}` },
    thumb,
    h("div", { class: "card-body" },
      h("div", { class: "meta" }, prio(item.topic_priority), metaLine(item.topic_name, SOURCE_LABELS[item.source] || item.source)),
      h("h3", { class: "card-title", onclick: () => openItem(item, onChange) }, item.title),
      item.description && h("p", { class: "desc" }, item.description),
      metaLine(item.author, item.published_at && timeAgo(item.published_at),
        item.view_count ? `viewed ${item.view_count}×` : null)),
    h("div", { class: "card-actions" },
      h("button", { class: `icon-btn${item.completed ? " on" : ""}`, title: "Mark done", onclick: () => toggle("completed", "Marked done ✓") }, "✓ Done"),
      h("button", { class: `icon-btn${item.bookmarked ? " on" : ""}`, title: "Save for later", onclick: () => toggle("bookmarked", "Saved ☆") }, item.bookmarked ? "★ Saved" : "☆ Save"),
      h("span", { class: "spacer" }),
      h("button", { class: "icon-btn", title: item.dismissed ? "Unhide" : "Not interested", onclick: () => toggle("dismissed", "Hidden from your feed") }, item.dismissed ? "↺" : "✕")));
}

function emptyState(title, text, action) {
  return h("div", { class: "empty" }, h("h3", {}, title), h("p", {}, text), action);
}

// --- player ------------------------------------------------------------------

async function openItem(item, onChange) {
  const dialog = $("#player");
  state.current = { item, onChange };
  $("#player-title").textContent = item.title;
  $("#player-meta").replaceChildren(prio(item.topic_priority),
    metaLine(item.topic_name, SOURCE_LABELS[item.source] || item.source, lengthLabel(item), item.author));
  $("#player-open").href = item.url;
  syncPlayerButtons();

  const body = $("#player-body");
  if (item.content_type === "video" && item.source === "youtube") {
    body.replaceChildren(h("iframe", {
      src: `https://www.youtube-nocookie.com/embed/${encodeURIComponent(item.external_id)}?autoplay=1&rel=0`,
      allow: "autoplay; encrypted-media; picture-in-picture; fullscreen", allowfullscreen: true, title: item.title,
    }));
  } else {
    body.replaceChildren(h("p", { class: "muted" }, "Loading…"));
  }
  dialog.showModal();

  try {
    const updated = await api(`/api/content/${item.id}/view`, { method: "POST" });
    Object.assign(item, updated);
  } catch { /* viewing still works if recording fails */ }

  if (item.content_type !== "video") {
    const detail = await api(`/api/content/${item.id}`).catch(() => null);
    if (detail?.body) {
      const reader = h("div", { class: "reader" });
      reader.innerHTML = detail.body; // sanitized server-side with an allowlist (nh3)
      reader.querySelectorAll("a").forEach((a) => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
      body.replaceChildren(reader);
    } else {
      body.replaceChildren(h("div", { class: "external" },
        item.thumbnail_url && h("img", { src: item.thumbnail_url, alt: "", referrerpolicy: "no-referrer", onerror: (e) => e.target.remove() }),
        item.description && h("p", {}, item.description),
        h("a", { class: "btn primary", href: item.url, target: "_blank", rel: "noopener noreferrer" },
          `Read on ${SOURCE_LABELS[item.source] || "the web"} ↗`)));
    }
  }
}

function syncPlayerButtons() {
  const { item } = state.current;
  $("#player-save").textContent = item.bookmarked ? "★ Saved" : "☆ Save";
  $("#player-done").textContent = item.completed ? "↺ Not done" : "✓ Mark done";
}

async function playerToggle(field) {
  const { item, onChange } = state.current;
  try {
    Object.assign(item, await api(`/api/content/${item.id}`, { method: "PATCH", body: { [field]: !item[field] } }));
    syncPlayerButtons();
    onChange?.(item, field);
    if (field === "completed" && item.completed) { toast("Nice — marked done ✓"); $("#player").close(); }
  } catch (err) { toast(err.message); }
}

$("#player-close").onclick = () => $("#player").close();
$("#player-save").onclick = () => playerToggle("bookmarked");
$("#player-done").onclick = () => playerToggle("completed");
$("#player").addEventListener("close", () => $("#player-body").replaceChildren()); // stops video playback
$("#player").addEventListener("click", (e) => { if (e.target.id === "player") e.target.close(); });

// --- feed --------------------------------------------------------------------

async function loadFeed() {
  const grid = $("#feed-grid");
  const params = new URLSearchParams(state.feedType ? { content_type: state.feedType } : {});
  const [items, prefs] = await Promise.all([api(`/api/feed?${params}`), api("/api/preferences")]);
  $("#feed-limits").textContent = `≤ ${prefs.max_video_minutes} min videos · ≤ ${prefs.max_reading_minutes} min reads`;

  // Remove cards that left the feed (done / hidden) without reshuffling the rest.
  const onChange = (item, field) => {
    if ((field === "completed" && item.completed) || (field === "dismissed" && item.dismissed)) {
      cards.get(item.id)?.remove();
      cards.delete(item.id);
      if (!cards.size) loadFeed();
    } else {
      const fresh = card(item, onChange);
      cards.get(item.id)?.replaceWith(fresh);
      cards.set(item.id, fresh);
    }
  };
  const cards = new Map(items.map((item) => [item.id, card(item, onChange)]));

  if (items.length) {
    grid.replaceChildren(...cards.values());
  } else if (!state.topics.length) {
    grid.replaceChildren(emptyState("Pick what you want to learn",
      "Add a few topics and give each a priority — the crawler will find short videos and readings for you.",
      h("button", { class: "btn primary", onclick: () => show("topics") }, "Add topics")));
  } else {
    grid.replaceChildren(emptyState("Nothing to show right now",
      "Crawls may still be running, or nothing fits your length limits. Try again in a moment, re-crawl, or raise the limits in Settings.",
      h("button", { class: "btn", onclick: loadFeed }, "↻ Refresh")));
  }
}

document.querySelectorAll("#feed-type .chip").forEach((chip) => {
  chip.onclick = () => {
    document.querySelectorAll("#feed-type .chip").forEach((c) => c.classList.toggle("active", c === chip));
    state.feedType = chip.dataset.type;
    loadFeed();
  };
});
$("#feed-shuffle").onclick = loadFeed;

// --- topics ------------------------------------------------------------------

const PRIORITY_NAMES = { 1: "P1 · Highest", 2: "P2 · High", 3: "P3 · Medium", 4: "P4 · Low", 5: "P5 · Lowest" };
const priorityOptions = (selected) =>
  Object.entries(PRIORITY_NAMES).map(([v, label]) => h("option", { value: v, selected: Number(v) === selected }, label));
$("#topic-priority").replaceChildren(...priorityOptions(1));

function crawlSummary(topic) {
  if (!topic.last_crawled_at) return "Not crawled yet";
  const failed = (topic.last_crawl_summary?.sources || []).filter((s) => s.error).map((s) => SOURCE_LABELS[s.source]);
  return `Crawled ${timeAgo(topic.last_crawled_at)}${failed.length ? ` · failed: ${failed.join(", ")}` : ""}`;
}

async function loadTopics() {
  state.topics = await api("/api/topics");
  const list = $("#topic-list");
  if (!state.topics.length) {
    list.replaceChildren(h("p", { class: "muted" }, "No topics yet. Try Kubernetes (P1), Helm (P2), LLM (P3)…"));
  } else {
    list.replaceChildren(...state.topics.map(topicRow));
  }
  const libTopic = $("#lib-topic"), current = libTopic.value;
  libTopic.replaceChildren(h("option", { value: "" }, "All topics"),
    ...state.topics.map((t) => h("option", { value: t.id, selected: String(t.id) === current }, t.name)));
}

function topicRow(topic) {
  const update = async (body) => {
    try { await api(`/api/topics/${topic.id}`, { method: "PATCH", body }); await loadTopics(); }
    catch (err) { toast(err.message); }
  };
  const crawlBtn = h("button", { class: "btn", onclick: async () => {
    crawlBtn.disabled = true; crawlBtn.textContent = "Crawling…";
    try {
      const result = await api(`/api/topics/${topic.id}/crawl`, { method: "POST" });
      toast(`${topic.name}: ${result.added} new, ${result.updated} refreshed`);
    } catch (err) { toast(err.message); }
    await loadTopics();
  } }, "↻ Crawl");

  return h("div", { class: `topic${topic.active ? "" : " inactive"}` },
    h("div", {},
      h("div", { class: "topic-name" }, prio(topic.priority), topic.name),
      h("div", { class: "topic-stats" },
        `${topic.item_count} items · ${topic.unseen_count} unseen · `,
        topic.active ? `${Math.round(topic.feed_share * 100)}% of feed` : "paused",
        ` · ${crawlSummary(topic)}`)),
    h("div", { class: "topic-controls" },
      h("select", { "aria-label": "Priority", onchange: (e) => update({ priority: Number(e.target.value) }) },
        ...priorityOptions(topic.priority)),
      h("label", { class: "switch" },
        h("input", { type: "checkbox", checked: topic.active, onchange: (e) => update({ active: e.target.checked }) }), "Active"),
      crawlBtn,
      h("button", { class: "icon-btn", title: "Delete topic", onclick: async () => {
        if (!confirm(`Delete "${topic.name}" and its ${topic.item_count} saved items?`)) return;
        try { await api(`/api/topics/${topic.id}`, { method: "DELETE" }); await loadTopics(); }
        catch (err) { toast(err.message); }
      } }, "🗑")),
    h("div", { class: "share" }, h("div", { style: `width:${topic.active ? topic.feed_share * 100 : 0}%` })));
}

$("#topic-form").onsubmit = async (e) => {
  e.preventDefault();
  const name = $("#topic-name").value.trim();
  if (!name) return;
  try {
    await api("/api/topics", { method: "POST", body: { name, priority: Number($("#topic-priority").value) } });
    $("#topic-name").value = "";
    toast(`Added "${name}" — crawling in the background…`);
    await loadTopics();
    setTimeout(loadTopics, 15000); // pick up the crawl results
  } catch (err) { toast(err.message); }
};

async function loadSources() {
  const sources = await api("/api/sources");
  $("#source-list").replaceChildren(...sources.map((s) => h("li", {},
    h("span", { class: `dot ${s.ready ? "ok" : "off"}` }),
    h("b", {}, SOURCE_LABELS[s.name] || s.name),
    h("span", { class: "muted small" }, s.ready ? "ready" : `${s.reason} (configure in .env)`))));
}

$("#crawl-all").onclick = async () => {
  await api("/api/crawl", { method: "POST" });
  toast("Re-crawling all active topics in the background…");
};

// --- library -----------------------------------------------------------------

async function loadLibrary(append = false) {
  if (!append) state.libOffset = 0;
  const params = new URLSearchParams({ status: $("#lib-status").value, limit: 30, offset: state.libOffset });
  if ($("#lib-q").value.trim()) params.set("q", $("#lib-q").value.trim());
  if ($("#lib-topic").value) params.set("topic_id", $("#lib-topic").value);
  if ($("#lib-type").value) params.set("content_type", $("#lib-type").value);

  const page = await api(`/api/content?${params}`);
  const grid = $("#lib-grid");
  const onChange = (item) => {
    const old = grid.querySelector(`[data-id="${item.id}"]`);
    const fresh = card(item, onChange); fresh.dataset.id = item.id; old?.replaceWith(fresh);
  };
  const cards = page.items.map((item) => { const c = card(item, onChange); c.dataset.id = item.id; return c; });
  if (append) grid.append(...cards);
  else grid.replaceChildren(...(cards.length ? cards : [emptyState("No matching content", "Try a different filter or search.")]));

  state.libOffset += page.items.length;
  $("#lib-count").textContent = `${page.total} item${page.total === 1 ? "" : "s"}`;
  $("#lib-more").hidden = state.libOffset >= page.total;
}

let searchTimer;
$("#lib-q").oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => loadLibrary(), 250); };
["#lib-topic", "#lib-type", "#lib-status"].forEach((sel) => { $(sel).onchange = () => loadLibrary(); });
$("#lib-more").onclick = () => loadLibrary(true);

// --- history -----------------------------------------------------------------

async function loadHistory() {
  const entries = await api("/api/history?limit=200");
  const list = $("#history-list");
  if (!entries.length) {
    list.replaceChildren(emptyState("No history yet", "Everything you open is kept here so you can replay it later."));
    return;
  }
  const rows = [];
  let lastDay = null;
  for (const entry of entries) {
    const when = new Date(entry.viewed_at);
    const day = when.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
    if (day !== lastDay) { rows.push(h("div", { class: "history-day" }, day)); lastDay = day; }
    const item = entry.item;
    rows.push(h("div", { class: "history-row" },
      h("span", { class: "when" }, when.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })),
      h("div", {},
        h("div", { class: "title" }, item.title),
        h("div", { class: "meta" }, prio(item.topic_priority), metaLine(item.topic_name,
          SOURCE_LABELS[item.source] || item.source, lengthLabel(item), item.completed && "✓ done"))),
      h("button", { class: "btn", onclick: () => openItem(item, () => loadHistory()) },
        item.content_type === "video" ? "▶ Replay" : "📖 Re-read")));
  }
  list.replaceChildren(...rows);
}

// --- settings ----------------------------------------------------------------

async function loadSettings() {
  const prefs = await api("/api/preferences");
  $("#max-video").value = prefs.max_video_minutes;
  $("#max-reading").value = prefs.max_reading_minutes;
  $("#inc-video").checked = prefs.include_videos;
  $("#inc-reading").checked = prefs.include_reading;
  $("#inc-unknown").checked = prefs.include_unknown_length;
  syncRangeLabels();
}

function syncRangeLabels() {
  $("#v-out").textContent = `${$("#max-video").value} min`;
  $("#r-out").textContent = `${$("#max-reading").value} min`;
}
$("#max-video").oninput = syncRangeLabels;
$("#max-reading").oninput = syncRangeLabels;

$("#prefs-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/preferences", { method: "PUT", body: {
      max_video_minutes: Number($("#max-video").value),
      max_reading_minutes: Number($("#max-reading").value),
      include_videos: $("#inc-video").checked,
      include_reading: $("#inc-reading").checked,
      include_unknown_length: $("#inc-unknown").checked,
    } });
    toast("Settings saved");
  } catch (err) { toast(err.message); }
};

// --- navigation --------------------------------------------------------------

const loaders = {
  feed: loadFeed,
  topics: () => Promise.all([loadTopics(), loadSources()]),
  library: () => loadLibrary(),
  history: loadHistory,
  settings: loadSettings,
};

function show(view) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  if (location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
  loaders[view]().catch((err) => toast(`Could not load: ${err.message}`));
}

document.querySelectorAll(".tab").forEach((tab) => { tab.onclick = () => show(tab.dataset.view); });

(async () => {
  await loadTopics().catch(() => {});
  const initial = location.hash.slice(1);
  show(loaders[initial] ? initial : "feed");
})();
