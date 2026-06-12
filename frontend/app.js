/* repo-knowledge frontend
   - Up to 5 chat tabs per session
   - Each tab is a distinct repo. Knowledge is never shared across tabs.
   - Per-tab state persists in sessionStorage.
*/

const MAX_TABS = 5;
const STORAGE_KEY = "repo_knowledge_session_v1";
const GEMINI_KEY_LS = "repo_knowledge_gemini_key_v1";
const PREFS_LS = "repo_knowledge_user_prefs_v1";
const POLL_MS = 3000;

/* ---- User-scoped settings (localStorage, survives reloads) ---- */
function getGeminiKey() {
  try { return localStorage.getItem(GEMINI_KEY_LS) || ""; } catch { return ""; }
}
function setGeminiKey(v) {
  try {
    if (v) localStorage.setItem(GEMINI_KEY_LS, v);
    else localStorage.removeItem(GEMINI_KEY_LS);
  } catch {}
}
function getPrefs() {
  try {
    const raw = localStorage.getItem(PREFS_LS);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr.filter((p) => typeof p === "string" && p.trim()) : [];
  } catch { return []; }
}
function setPrefs(list) {
  try { localStorage.setItem(PREFS_LS, JSON.stringify(list)); } catch {}
}
function addPref(text) {
  const t = (text || "").trim();
  if (!t) return false;
  const list = getPrefs();
  if (list.some((p) => p.toLowerCase() === t.toLowerCase())) return false;
  list.push(t);
  setPrefs(list.slice(0, 30));
  return true;
}
function removePref(matcher) {
  const m = (matcher || "").trim().toLowerCase();
  if (!m) return 0;
  const before = getPrefs();
  const after = before.filter((p) => !p.toLowerCase().includes(m));
  setPrefs(after);
  return before.length - after.length;
}
function clearPrefs() {
  const n = getPrefs().length;
  setPrefs([]);
  return n;
}

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function escapeHtml(s) {
  return (s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function renderMarkdownish(text) {
  // Minimal: escape, then turn fenced code blocks into <pre>, inline ` into <code>, preserve line breaks.
  const escaped = escapeHtml(text);
  const withFences = escaped.replace(/```([\s\S]*?)```/g, (_, body) => `<pre>${body}</pre>`);
  const withInline = withFences.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  return withInline.replace(/\n/g, "<br/>");
}

function uid() {
  return "chat_" + Math.random().toString(36).slice(2, 10);
}

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (opts.body && !(opts.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const key = getGeminiKey();
  if (key) headers["X-Gemini-Key"] = key;
  const res = await fetch(path, { ...opts, headers });
  if (!res.ok) {
    let detail = null;
    let raw = "";
    try {
      const data = await res.json();
      detail = data && data.detail !== undefined ? data.detail : data;
    } catch {
      try { raw = await res.text(); } catch {}
    }
    const message =
      (detail && typeof detail === "object" && detail.message) ||
      (typeof detail === "string" ? detail : null) ||
      raw ||
      `${res.status} ${res.statusText}`;
    const err = new Error(message);
    err.status = res.status;
    err.detail = detail;
    err.code = detail && typeof detail === "object" ? detail.code : null;
    throw err;
  }
  return res.json();
}

/* ---- Session state ---- */
const state = {
  chats: [],         // array of chat objects
  activeId: null,    // id of active chat
  pollTimer: null,
  fileCache: {},     // repoId -> { files: [{file, chunks}], fetchedAt }
};

const FILE_CACHE_TTL = 60_000;

async function getRepoFiles(repoId) {
  if (!repoId) return [];
  const cached = state.fileCache[repoId];
  if (cached && Date.now() - cached.fetchedAt < FILE_CACHE_TTL) {
    return cached.files;
  }
  try {
    const res = await api(`/api/repos/${repoId}/files?limit=500`);
    const files = res.files || [];
    state.fileCache[repoId] = { files, fetchedAt: Date.now() };
    return files;
  } catch (e) {
    return cached?.files || [];
  }
}

function invalidateFileCache(repoId) {
  if (repoId) delete state.fileCache[repoId];
}

/**
 * Parse a raw composer string into structured request fields.
 *   "/plan how should I @backend/main.py harden auth?" =>
 *     { mode: "plan", filePaths: ["backend/main.py"], question: "how should I @backend/main.py harden auth?" }
 *
 * We INTENTIONALLY keep the @-tokens in the question so the LLM still sees
 * them in context; we just also extract them for retrieval scoping.
 */
const MENTION_RE = /@([A-Za-z0-9_./\-]+)/g;
function parseComposer(raw) {
  const trimmed = (raw || "").trim();
  let mode = "strict";
  let body = trimmed;
  if (/^\/plan\b/i.test(body)) {
    mode = "plan";
    body = body.replace(/^\/plan\b\s*/i, "");
  }
  const filePaths = [];
  let m;
  MENTION_RE.lastIndex = 0;
  while ((m = MENTION_RE.exec(body)) !== null) {
    const p = m[1].replace(/[.,;:!?)]+$/, "");
    if (p && !filePaths.includes(p)) filePaths.push(p);
  }
  return { mode, filePaths, question: body.trim() };
}

function newChat() {
  return {
    id: uid(),
    repoId: null,
    label: "New chat",
    sub: "",
    status: "onboarding", // onboarding | indexing | ready | error
    statusMsg: "",
    counts: { code_chunks: 0, feature_skills: 0 },
    lastRun: null,
    messages: [],         // [{role, content, sources?, ts}]
    source: null,         // 'github' | 'upload'
    commitScope: null,    // optional commit SHA (prefix ok) to restrict retrieval
    createdAt: Date.now(),
  };
}

function persist() {
  const payload = {
    activeId: state.activeId,
    chats: state.chats,
  };
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
  } catch (e) {
    // ignore
  }
}

function restore() {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    const data = JSON.parse(raw);
    if (Array.isArray(data.chats)) state.chats = data.chats;
    if (data.activeId) state.activeId = data.activeId;
  } catch (e) {}
}

function activeChat() {
  return state.chats.find((c) => c.id === state.activeId) || null;
}

function setActive(id) {
  state.activeId = id;
  persist();
  renderTabs();
  renderMain();
}

function addChat() {
  if (state.chats.length >= MAX_TABS) {
    alert(`Limit is ${MAX_TABS} chats per session. Close one to start a new one.`);
    return;
  }
  const c = newChat();
  state.chats.push(c);
  setActive(c.id);
}

function closeChat(id) {
  const idx = state.chats.findIndex((c) => c.id === id);
  if (idx === -1) return;
  const chat = state.chats[idx];
  state.chats.splice(idx, 1);
  if (state.activeId === id) {
    state.activeId = state.chats.length ? state.chats[Math.max(0, idx - 1)].id : null;
  }
  // Always purge this chat's conversation memory from the vector store.
  fetch(`/api/chats/${encodeURIComponent(chat.id)}`, { method: "DELETE" }).catch(() => {});
  // Uploaded zips don't need to outlive their tab.
  if (chat.repoId && chat.source === "upload") {
    fetch(`/api/repos/${chat.repoId}`, { method: "DELETE" }).catch(() => {});
    invalidateFileCache(chat.repoId);
  }
  persist();
  renderTabs();
  renderMain();
}

/* ---- Rendering ---- */
function renderTabs() {
  const list = $("#tablist");
  list.innerHTML = "";
  for (const c of state.chats) {
    const el = document.createElement("div");
    el.className = `tab ${c.status} ${c.id === state.activeId ? "active" : ""}`;
    el.innerHTML = `
      <div class="tab-dot" title="${escapeHtml(c.status)}"></div>
      <div class="tab-label" title="${escapeHtml(c.label)}">${escapeHtml(c.label || "New chat")}</div>
      <button class="tab-close" title="Close">×</button>
    `;
    el.addEventListener("click", (e) => {
      if (e.target.classList.contains("tab-close")) return;
      setActive(c.id);
    });
    el.querySelector(".tab-close").addEventListener("click", (e) => {
      e.stopPropagation();
      closeChat(c.id);
    });
    list.appendChild(el);
  }
  $("#new-chat").disabled = state.chats.length >= MAX_TABS;
}

function renderMain() {
  const main = $("#main");
  
  // Save input state if active
  let activeElementId = null;
  let activeElementValue = null;
  let activeElementSelectionStart = null;
  let activeElementSelectionEnd = null;
  
  const activeEl = document.activeElement;
  if (activeEl && (activeEl.classList.contains("composer-input") || activeEl.classList.contains("scope-input"))) {
    activeElementId = activeEl.classList.contains("composer-input") ? "composer-input" : "scope-input";
    activeElementValue = activeEl.value;
    activeElementSelectionStart = activeEl.selectionStart;
    activeElementSelectionEnd = activeEl.selectionEnd;
  }

  main.innerHTML = "";
  const chat = activeChat();
  if (!chat) {
    main.innerHTML = `
      <div class="empty">
        <div class="card">
          <h1>No chats yet</h1>
          <p class="muted">Click <b>+ New chat</b> to start. Each chat learns one repo. Knowledge is never shared across chats.</p>
        </div>
      </div>`;
    return;
  }
  if (chat.status === "onboarding") {
    renderOnboarding(main, chat);
  } else if (chat.status === "indexing") {
    renderIndexing(main, chat);
  } else if (chat.status === "ready" || chat.status === "error") {
    renderChat(main, chat);
    
    // Restore input state
    if (activeElementId) {
      const newEl = activeElementId === "composer-input" 
        ? main.querySelector(".composer-input") 
        : main.querySelector(".scope-input");
      if (newEl) {
        newEl.value = activeElementValue;
        newEl.focus();
        try {
          newEl.setSelectionRange(activeElementSelectionStart, activeElementSelectionEnd);
        } catch (e) {}
      }
    }
  }
}

function renderOnboarding(main, chat) {
  const tpl = $("#tpl-onboarding").content.cloneNode(true);
  const errEl = tpl.querySelector(".onb-error");
  function showErr(msg) {
    if (!msg) { errEl.hidden = true; errEl.textContent = ""; return; }
    errEl.hidden = false; errEl.textContent = msg;
  }

  // Tab switching
  const tabs = $$(".mode-tab", tpl);
  const sections = $$(".onboarding-section", tpl);
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      tabs.forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      const mode = tab.dataset.mode;
      sections.forEach((sec) => {
        if (sec.id === `sec-${mode}`) {
          sec.hidden = false;
        } else {
          sec.hidden = true;
        }
      });
    });
  });

  // PurnaOS form
  const purnaForm = tpl.querySelector('form[data-action="purna"]');
  purnaForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    showErr(null);
    const workspaceId = purnaForm.workspace_id.value.trim();
    if (!workspaceId) return;
    
    const btn = purnaForm.querySelector('button[type="submit"]');
    btn.disabled = true; btn.textContent = "Connecting...";
    
    try {
      const res = await api(`/api/purna/workspaces/${workspaceId}`);
      chat.repoId = res.repo_id;
      chat.workspaceId = res.workspace_id;
      chat.source = "purna_workspace";
      chat.label = `${res.owner}/${res.repo_name} (PurnaOS)`;
      chat.sub = `workspace: ${res.workspace_id.slice(0, 8)}`;
      chat.status = "ready";
      persist();
      renderTabs();
      renderMain();
    } catch (err) {
      btn.disabled = false; btn.textContent = "Connect";
      showErr(err.message || String(err));
    }
  });

  const ghForm = tpl.querySelector('form[data-action="github"]');
  const tokenRow = ghForm.querySelector(".token-row");
  const tokenInput = tokenRow.querySelector('input[name="token"]');
  tokenRow.hidden = false;
  tokenInput.required = true;

  ghForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    showErr(null);
    if (!getGeminiKey()) {
      showErr("Gemini API key is not configured. Open the sidebar Settings panel, paste your Google AI Studio key, then try again.");
      const details = document.querySelector("#settings-key");
      if (details) details.open = true;
      setTimeout(() => document.querySelector("#settings-key-input")?.focus(), 0);
      return;
    }
    const url = ghForm.url.value.trim();
    const token = (tokenInput.value || "").trim();
    if (!url) return;
    if (!token) {
      showErr("GitHub token is required.");
      tokenInput.focus();
      return;
    }
    const btn = ghForm.querySelector('button[type="submit"]');
    btn.disabled = true; btn.textContent = "Indexing...";
    const body = { url };
    if (token) body.token = token;
    try {
      const res = await api("/api/repos", { method: "POST", body: JSON.stringify(body) });
      chat.repoId = res.repo_id;
      chat.source = "github";
      chat.label = `${res.owner}/${res.name}`;
      chat.sub = `branch: ${res.default_branch} · ${res.visibility}`;
      chat.status = "indexing";
      persist();
      renderTabs();
      renderMain();
    } catch (err) {
      btn.disabled = false; btn.textContent = "Index";
      if (err.code === "gemini_key_required") {
        showErr(err.message || "Add your Gemini API key in the sidebar Settings panel first.");
        const details = document.querySelector("#settings-key");
        if (details) details.open = true;
        setTimeout(() => document.querySelector("#settings-key-input")?.focus(), 0);
      } else if (err.code === "auth_required" || err.code === "token_required") {
        showErr(err.message || "GitHub access token is required.");
        setTimeout(() => tokenInput.focus(), 0);
      } else if (err.code === "invalid_token") {
        tokenInput.value = "";
        showErr(err.message || "That token didn't work. Try one with 'repo' scope.");
        setTimeout(() => tokenInput.focus(), 0);
      } else {
        showErr(err.message || String(err));
      }
    }
  });

  const dropzone = tpl.querySelector('.dropzone[data-action="upload"]');
  const fileInput = dropzone.querySelector('input[type="file"]');
  async function handleFile(file) {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".zip")) {
      showErr("Please upload a .zip file."); return;
    }
    if (!getGeminiKey()) {
      showErr("Gemini API key is not configured. Open the sidebar Settings panel, paste your Google AI Studio key, then try again.");
      const details = document.querySelector("#settings-key");
      if (details) details.open = true;
      setTimeout(() => document.querySelector("#settings-key-input")?.focus(), 0);
      return;
    }
    showErr(null);
    dropzone.querySelector(".dz-title").textContent = `Uploading ${file.name}...`;
    const fd = new FormData();
    fd.append("file", file);
    try {
      const res = await api("/api/repos/upload", { method: "POST", body: fd });
      chat.repoId = res.repo_id;
      chat.source = "upload";
      chat.label = res.label || res.name;
      chat.sub = "uploaded zip";
      chat.status = "indexing";
      persist();
      renderTabs();
      renderMain();
    } catch (e) {
      showErr(e.message || String(e));
      dropzone.querySelector(".dz-title").textContent = "Drop a .zip here or click to choose";
    }
  }
  dropzone.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => handleFile(fileInput.files[0]));
  ["dragenter", "dragover"].forEach((ev) =>
    dropzone.addEventListener(ev, (e) => {
      e.preventDefault(); dropzone.classList.add("drag");
    })
  );
  ["dragleave", "drop"].forEach((ev) =>
    dropzone.addEventListener(ev, (e) => {
      e.preventDefault(); dropzone.classList.remove("drag");
    })
  );
  dropzone.addEventListener("drop", (e) => {
    const f = e.dataTransfer?.files?.[0];
    if (f) handleFile(f);
  });

  main.appendChild(tpl);
}

function renderIndexing(main, chat) {
  const tpl = $("#tpl-indexing").content.cloneNode(true);
  tpl.querySelector('[data-stat="code_chunks"]').textContent = chat.counts.code_chunks;
  tpl.querySelector('[data-stat="feature_skills"]').textContent = chat.counts.feature_skills;
  tpl.querySelector('[data-stat="last_run"]').textContent = chat.lastRun || "—";
  const enterBtn = tpl.querySelector('[data-action="enter-chat"]');
  enterBtn.disabled = chat.counts.code_chunks === 0;
  enterBtn.addEventListener("click", () => {
    chat.status = "ready";
    persist();
    renderTabs();
    renderMain();
  });
  main.appendChild(tpl);
}

function renderChat(main, chat) {
  const tpl = $("#tpl-chat").content.cloneNode(true);
  const labelEl = tpl.querySelector(".chat-label");
  const subEl = tpl.querySelector(".chat-sub");
  labelEl.textContent = chat.label;
  const exchanges = chat.messages.filter(
    (m) => (m.role === "user" || m.role === "assistant") && !m.previewing
  ).length;
  const scopeLabel = chat.commitScope ? `scope: @${chat.commitScope.slice(0, 7)}` : null;
  // Prefer the authoritative count from the vector DB (chat_turns), which
  // includes turns from earlier sessions on this chat_id.
  const memoryLabel = chat.dbTurns != null
    ? `memory: ${chat.dbTurns} turn${chat.dbTurns === 1 ? "" : "s"} in vec-DB`
    : `memory: ${exchanges} turn${exchanges === 1 ? "" : "s"}`;
  subEl.textContent = [
    chat.sub,
    `code: ${chat.counts.code_chunks}`,
    `skills: ${chat.counts.feature_skills}`,
    memoryLabel,
    scopeLabel,
  ].filter(Boolean).join(" · ");

  const messagesEl = tpl.querySelector(".messages");
  for (const m of chat.messages) appendMessageDom(messagesEl, m);

  tpl.querySelector('[data-action="status"]').addEventListener("click", () => {
    refreshStatus(chat).then(() => renderMain());
  });
  const syncBtn = tpl.querySelector('[data-action="sync"]');
  if (chat.source !== "github") { syncBtn.disabled = true; syncBtn.title = "Sync is only for GitHub repos"; }
  syncBtn.addEventListener("click", async () => {
    try {
      await api(`/api/repos/${chat.repoId}/sync`, { method: "POST" });
      pushSystem(chat, "Sync queued.");
    } catch (e) {
      pushSystem(chat, `Sync failed: ${e.message}`);
    }
    renderMain();
  });

  // Scope-to-commit pill state
  const scopeWrap = tpl.querySelector(".scope-pill");
  const scopeInput = tpl.querySelector(".scope-input");
  const scopeClear = tpl.querySelector(".scope-clear");
  const scopeHint = tpl.querySelector(".scope-hint");
  function applyScopeUi() {
    const v = (chat.commitScope || "").trim();
    scopeInput.value = v;
    scopeWrap.classList.toggle("active", !!v);
    scopeClear.hidden = !v;
    scopeHint.textContent = v
      ? `Retrieval is restricted to chunks indexed at commits starting with ${v.slice(0, 12)}.`
      : "Optional. Restrict retrieval to chunks indexed at a specific commit.";
  }
  applyScopeUi();
  scopeInput.addEventListener("change", () => {
    chat.commitScope = (scopeInput.value || "").trim() || null;
    applyScopeUi();
    persist();
    renderTabs();
    const m = $(".chat-sub", main);
    if (m) renderMain(); // refresh subline scope badge
  });
  scopeClear.addEventListener("click", () => {
    chat.commitScope = null;
    applyScopeUi();
    persist();
    renderMain();
  });

  // Render Agent Decisions Log
  const agentLogPanel = tpl.querySelector("#agent-log-panel");
  const toggleLogBtn = tpl.querySelector('[data-action="toggle-log"]');
  
  if (chat.source === "purna_workspace") {
    toggleLogBtn.hidden = false;
    toggleLogBtn.textContent = chat.agentLogClosed ? "Show Log" : "Hide Log";
    toggleLogBtn.addEventListener("click", () => {
      chat.agentLogClosed = !chat.agentLogClosed;
      persist();
      renderMain();
    });

    if (chat.agentLogClosed) {
      agentLogPanel.hidden = true;
    } else {
      agentLogPanel.hidden = false;
      const logList = tpl.querySelector("#agent-log-list");
      const logStatus = tpl.querySelector("#agent-log-status");
      logList.innerHTML = "";
      
      const decisions = chat.decisions || [];
      if (decisions.length === 0) {
        logList.innerHTML = `<div class="muted small" style="padding: 10px;">No decisions logged yet. Save a file to trigger the sync agent.</div>`;
        logStatus.textContent = "idle";
      } else {
        const last = decisions[0];
        logStatus.textContent = `last check: ${last.action} (${last.reason})`;
        
        for (const d of decisions) {
          const item = document.createElement("div");
          item.className = "agent-log-item";
          const dateStr = d.created_at ? new Date(d.created_at).toLocaleTimeString() : "";
          item.innerHTML = `
            <div class="agent-log-meta">
              <span class="agent-action ${d.action}">${d.action}</span>
              <span class="muted small">${dateStr}</span>
            </div>
            <div class="agent-log-reason">${escapeHtml(d.reason)}</div>
          `;
          logList.appendChild(item);
        }
      }

      const closeLogBtn = tpl.querySelector("#agent-log-close");
      if (closeLogBtn) {
        closeLogBtn.addEventListener("click", () => {
          chat.agentLogClosed = true;
          persist();
          renderMain();
        });
      }
    }
  } else {
    agentLogPanel.hidden = true;
    if (toggleLogBtn) toggleLogBtn.hidden = true;
  }

  const composerWrap = tpl.querySelector(".composer-wrap");
  const composerHint = tpl.querySelector(".composer-hint");
  const hintModeEl = tpl.querySelector(".hint-mode");
  const tagsEl = tpl.querySelector(".composer-tags");
  const popup = tpl.querySelector(".mention-popup");
  const form = tpl.querySelector("form.composer");
  const input = tpl.querySelector(".composer-input");

  input.value = chat.inputValue || "";

  // Prefetch file list so the popup pops instantly on first '@'.
  getRepoFiles(chat.repoId);

  function renderTags(parsed) {
    tagsEl.innerHTML = "";
    if (parsed.mode === "plan") {
      const p = document.createElement("span");
      p.className = "composer-tag plan";
      p.innerHTML = `/plan`;
      tagsEl.appendChild(p);
    }
    for (const f of parsed.filePaths) {
      const t = document.createElement("span");
      t.className = "composer-tag";
      t.innerHTML = `@${escapeHtml(f)}`;
      tagsEl.appendChild(t);
    }
    composerHint.classList.toggle("plan", parsed.mode === "plan");
    hintModeEl.textContent = parsed.mode === "plan"
      ? "PLAN mode · repo + general knowledge + (web if available)"
      : "Strict mode · only your repo";
  }
  renderTags(parseComposer(input.value));

  /* ---- @ mention popup ---- */
  let mention = { active: false, start: -1, query: "", items: [], cursor: 0 };

  function hidePopup() {
    mention.active = false;
    popup.hidden = true;
    popup.innerHTML = "";
  }

  function refreshPopupDom() {
    if (!mention.active) return hidePopup();
    if (mention.items.length === 0) {
      popup.innerHTML = `
        <div class="mention-header">Files in this repo</div>
        <div class="mention-empty">No files matching "${escapeHtml(mention.query)}".</div>`;
      popup.hidden = false;
      return;
    }
    const rows = mention.items.slice(0, 8).map((f, i) => `
      <div class="mention-item${i === mention.cursor ? " active" : ""}" data-idx="${i}">
        <span class="mention-path">${escapeHtml(f.file)}</span>
        <span class="mention-chunks">${f.chunks}</span>
      </div>`).join("");
    popup.innerHTML = `
      <div class="mention-header">Files in this repo · ↑↓ to navigate, ⏎ or Tab to insert, Esc to close</div>
      ${rows}`;
    popup.hidden = false;
    popup.querySelectorAll(".mention-item").forEach((el) => {
      el.addEventListener("mousedown", (e) => {
        // mousedown (not click) so the input doesn't blur first
        e.preventDefault();
        const idx = parseInt(el.dataset.idx, 10);
        selectMention(idx);
      });
    });
  }

  async function maybeOpenMention() {
    const val = input.value;
    const cursor = input.selectionStart ?? val.length;
    // Find the last '@' before the cursor that starts a fresh token.
    const upto = val.slice(0, cursor);
    const m = upto.match(/(?:^|\s)@([A-Za-z0-9_./\-]*)$/);
    if (!m) return hidePopup();
    mention.active = true;
    mention.start = cursor - m[1].length - 1; // index of '@'
    mention.query = m[1];
    const files = await getRepoFiles(chat.repoId);
    const q = mention.query.toLowerCase();
    mention.items = q
      ? files.filter((f) => f.file.toLowerCase().includes(q))
      : files;
    if (mention.cursor >= mention.items.length) mention.cursor = 0;
    refreshPopupDom();
  }

  function selectMention(idx) {
    if (!mention.active) return;
    const item = mention.items[idx];
    if (!item) return hidePopup();
    const before = input.value.slice(0, mention.start);
    const after = input.value.slice(input.selectionStart ?? input.value.length);
    const insert = "@" + item.file + " ";
    input.value = before + insert + after;
    chat.inputValue = input.value;
    const newPos = (before + insert).length;
    input.setSelectionRange(newPos, newPos);
    hidePopup();
    renderTags(parseComposer(input.value));
    input.focus();
  }

  input.addEventListener("input", () => {
    chat.inputValue = input.value;
    renderTags(parseComposer(input.value));
    maybeOpenMention();
  });
  input.addEventListener("keydown", (e) => {
    if (mention.active && !popup.hidden) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        mention.cursor = Math.min(mention.cursor + 1, mention.items.length - 1);
        refreshPopupDom();
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        mention.cursor = Math.max(mention.cursor - 1, 0);
        refreshPopupDom();
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        if (mention.items.length > 0) {
          e.preventDefault();
          selectMention(mention.cursor);
          return;
        }
      }
      if (e.key === "Escape") {
        e.preventDefault();
        hidePopup();
        return;
      }
    }
  });
  input.addEventListener("blur", () => setTimeout(hidePopup, 120));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const raw = (input.value || "").trim();
    if (!raw) return;

    // Slash commands that never hit the backend - they mutate local prefs only.
    if (/^\/remember(\s|$)/i.test(raw)) {
      const body = raw.replace(/^\/remember\s*/i, "").trim();
      if (!body) {
        const cur = getPrefs();
        pushSystem(chat, cur.length
          ? `Remembered preferences (${cur.length}):\n${cur.map((p, i) => `  ${i + 1}. ${p}`).join("\n")}`
          : "No remembered preferences yet. Try: /remember always cite line numbers");
      } else if (addPref(body)) {
        pushSystem(chat, `Saved preference: "${body}". Applied to all chats from now on.`);
      } else {
        pushSystem(chat, `Already remembered: "${body}".`);
      }
      renderSidebarSettings();
      chat.inputValue = "";
      input.value = "";
      renderTags({ mode: "strict", filePaths: [] });
      hidePopup();
      renderMain();
      return;
    }
    if (/^\/forget(\s|$)/i.test(raw)) {
      const body = raw.replace(/^\/forget\s*/i, "").trim();
      if (body.toLowerCase() === "all") {
        const n = clearPrefs();
        pushSystem(chat, n ? `Cleared all ${n} remembered preference${n === 1 ? "" : "s"}.` : "Nothing to forget.");
      } else if (!body) {
        pushSystem(chat, "Usage: /forget <text matching a preference>, or /forget all.");
      } else {
        const n = removePref(body);
        pushSystem(chat, n ? `Forgot ${n} preference${n === 1 ? "" : "s"} matching "${body}".` : `No preference matched "${body}".`);
      }
      renderSidebarSettings();
      chat.inputValue = "";
      input.value = "";
      renderTags({ mode: "strict", filePaths: [] });
      hidePopup();
      renderMain();
      return;
    }

    const parsed = parseComposer(raw);
    if (!parsed.question) return;
    chat.commitScope = (scopeInput.value || "").trim() || null;
    persist();
    chat.inputValue = "";
    input.value = "";
    renderTags({ mode: "strict", filePaths: [] });
    hidePopup();
    await ask(chat, parsed);
  });

  main.appendChild(tpl);
  requestAnimationFrame(() => {
    const m = $(".messages", main);
    if (m) m.scrollTop = m.scrollHeight;
  });

  // Refresh authoritative DB turn count for this chat, async, no UI block.
  fetch(`/api/chats/${encodeURIComponent(chat.id)}/turns/count`)
    .then((r) => r.json())
    .then((j) => {
      if (typeof j.turns === "number" && j.turns !== chat.dbTurns) {
        chat.dbTurns = j.turns;
        const sub = $(".chat-sub", main);
        if (sub) sub.textContent = sub.textContent.replace(
          /memory: \d+ turns?[^·]*/,
          `memory: ${j.turns} turn${j.turns === 1 ? "" : "s"} in vec-DB`,
        );
        persist();
      }
    })
    .catch(() => {});
}

function appendMessageDom(container, m) {
  const div = document.createElement("div");
  const refusal = m.role === "assistant" && m.refusal ? " refusal" : "";
  div.className = `msg ${m.role}${refusal}`;
  const modeBadge = m.role === "assistant" && m.mode === "plan"
    ? `<div class="mode-badge plan">plan mode</div>`
    : "";
  const turnBadge = m.turnIndex
    ? `<div class="turn-badge" title="Turn ${m.turnIndex} in this chat's vector memory">turn ${m.turnIndex}</div>`
    : "";
  if (m.role === "system") {
    div.textContent = m.content;
  } else if (m.role === "assistant" && m.previewing) {
    const previewing = m.mode === "plan"
      ? "Pulling repo context + general knowledge"
      : "Reading repo context";
    const headline = m.previewSources
      ? `${previewing} · ${m.previewSources.code.length} code chunks, ${m.previewSources.skills.length} PR notes`
      : `${previewing}...`;
    div.innerHTML = `
      ${modeBadge}
      <div class="thinking">${escapeHtml(headline)}</div>
      ${renderPreviewHtml(m.previewSources)}
    `;
  } else if (m.role === "assistant") {
    let html = modeBadge + turnBadge + `<div>${renderMarkdownish(m.content)}</div>`;
    if (m.sources) html += renderSourcesHtml(m.sources);
    div.innerHTML = html;
  } else {
    div.innerHTML = turnBadge + `<div>${renderMarkdownish(m.content)}</div>`;
  }
  container.appendChild(div);
}

function renderPreviewHtml(p) {
  if (!p) {
    return `<div class="preview-panel"><div class="preview-row muted small">Looking up relevant code and PR notes for this repo only...</div></div>`;
  }
  const code = p.code || [];
  const skills = p.skills || [];
  if (code.length === 0 && skills.length === 0) {
    return `<div class="preview-panel empty"><div class="preview-row muted small">No matching context found in this repo. Bot will refuse.</div></div>`;
  }
  const codeItems = code.map((c) => {
    const lines = c.start_line && c.end_line ? ` L${c.start_line}-L${c.end_line}` : "";
    const sym = c.symbol ? `:${escapeHtml(c.symbol)}` : "";
    const sha = c.commit_sha ? ` <span class="preview-sha">@${escapeHtml(c.commit_sha.slice(0, 7))}</span>` : "";
    const snippet = (c.content || "").split("\n").slice(0, 3).join("\n");
    return `
      <div class="preview-row">
        <div class="preview-row-head">
          <span class="preview-kind">code</span>
          <span class="preview-loc">${escapeHtml(c.file)}${sym}${lines}${sha}</span>
          <span class="preview-score">${c.score.toFixed(2)}</span>
        </div>
        <pre class="preview-snippet">${escapeHtml(snippet)}</pre>
      </div>`;
  }).join("");
  const skillItems = skills.map((s) => {
    const summary = (s.summary || "").split("\n").slice(0, 2).join(" · ");
    return `
      <div class="preview-row">
        <div class="preview-row-head">
          <span class="preview-kind skill">PR</span>
          <span class="preview-loc">#${s.pr_number} ${escapeHtml(s.title || "")}</span>
          <span class="preview-score">${s.score.toFixed(2)}</span>
        </div>
        <div class="preview-snippet">${escapeHtml(summary)}</div>
      </div>`;
  }).join("");
  return `<div class="preview-panel">${codeItems}${skillItems}</div>`;
}

function renderSourcesHtml(sources) {
  const code = sources.code || [];
  const skills = sources.skills || [];
  if (code.length === 0 && skills.length === 0) return "";
  const codeItems = code.map((c) => {
    const lines = c.start_line && c.end_line ? ` L${c.start_line}-L${c.end_line}` : "";
    const sym = c.symbol ? `:${escapeHtml(c.symbol)}` : "";
    const sha = c.commit_sha ? ` <span class="src-sha">@${escapeHtml(c.commit_sha.slice(0, 7))}</span>` : "";
    return `<div class="src-item">code · ${escapeHtml(c.file)}${sym}${lines}${sha} (${c.score.toFixed(2)})</div>`;
  }).join("");
  const skillItems = skills.map((s) => {
    return `<div class="src-item">PR #${s.pr_number} · ${escapeHtml(s.title)} (${s.score.toFixed(2)})</div>`;
  }).join("");
  return `<div class="sources"><b>Sources</b>${codeItems}${skillItems}</div>`;
}

function pushSystem(chat, msg) {
  chat.messages.push({ role: "system", content: msg, ts: Date.now() });
  persist();
}

async function ask(chat, parsed) {
  if (!chat.repoId) return;
  // parsed is either the legacy raw string (back-compat) or {mode, filePaths, question}
  if (typeof parsed === "string") parsed = parseComposer(parsed);
  const { mode, filePaths, question } = parsed;

  // Surface what the user actually typed (including @files and /plan-stripped body)
  // by reconstructing a friendly display string.
  const displayPrefix = mode === "plan" ? "/plan " : "";
  chat.messages.push({
    role: "user",
    content: displayPrefix + question,
    ts: Date.now(),
    mode,
    filePaths,
  });
  const placeholderIdx = chat.messages.push({
    role: "assistant",
    content: "",
    previewing: true,
    previewSources: null,
    mode,
    filePaths,
    ts: Date.now(),
  }) - 1;
  persist();
  renderMain();

  // STRICT ISOLATION: server retrieves prior turns by chat_id only.
  // We never send history in-band — keeps prompt bounded, scales to long chats.
  const prefs = getPrefs();
  const body = JSON.stringify({
    repo_id: chat.repoId,
    chat_id: chat.id,
    question,
    commit_sha: chat.commitScope || null,
    file_paths: filePaths.length ? filePaths : null,
    mode,
    user_preferences: prefs.length ? prefs : null,
  });

  const previewP = api("/api/chat/preview", { method: "POST", body })
    .then((p) => {
      const cur = chat.messages[placeholderIdx];
      if (!cur || !cur.previewing) return;
      cur.previewSources = p;
      persist();
      renderMain();
    })
    .catch(() => { /* preview is best-effort */ });

  try {
    const res = await api("/api/chat", { method: "POST", body });
    const text = (res.answer || "").trim();
    // Plan mode is allowed to answer with no repo context, so don't flag it as refusal.
    const refusal =
      mode !== "plan" &&
      (!res.grounded || text.toLowerCase().includes("don't have enough information"));
    chat.messages[placeholderIdx] = {
      role: "assistant",
      content: text,
      sources: res.sources,
      refusal,
      mode: res.mode || mode,
      filePaths,
      turnIndex: res.assistant_turn_index,
      ts: Date.now(),
    };
    // Update the user's previous message with its server-assigned turn_index.
    const userMsg = chat.messages[placeholderIdx - 1];
    if (userMsg && userMsg.role === "user") {
      userMsg.turnIndex = res.user_turn_index;
    }
    if (typeof res.history_turns_total === "number") {
      chat.dbTurns = res.history_turns_total;
    }
  } catch (e) {
    const msg = e.code === "gemini_key_required"
      ? `${e.message || "Gemini API key missing."} Open the sidebar Settings panel, paste your key, hit Save, then try again.`
      : `Error: ${e.message}`;
    chat.messages[placeholderIdx] = {
      role: "assistant",
      content: msg,
      refusal: true,
      mode,
      filePaths,
      ts: Date.now(),
    };
    if (e.code === "gemini_key_required") {
      const details = document.querySelector("#settings-key");
      if (details) details.open = true;
    }
  }
  await previewP;
  persist();
  renderMain();
}

/* ---- Status polling ---- */
async function refreshStatus(chat) {
  if (!chat.repoId) return;
  try {
    const s = await api(`/api/repos/${chat.repoId}/status`);
    chat.counts = s.counts || { code_chunks: 0, feature_skills: 0 };
    const lr = s.last_run;
    chat.lastRun = lr ? `${lr.kind} · ${lr.status}` : null;
    chat.sub = chat.source === "upload"
      ? `uploaded zip · branch (zip)`
      : chat.source === "purna_workspace"
        ? `workspace: ${chat.workspaceId.slice(0, 8)}`
        : `branch: ${s.default_branch} · ${s.visibility} · sha ${(s.last_indexed_sha || "—").slice(0, 10)}`;
    chat.label = s.label || `${s.owner}/${s.name}`;
    if (lr && lr.status === "error") {
      chat.status = "error";
      pushSystem(chat, `Indexing error: ${lr.error || "unknown"}`);
    } else if (chat.counts.code_chunks > 0 && chat.status === "indexing") {
      // chunks are populating; keep "indexing" until user clicks "Open chat" OR auto-advance after first sync run finishes
      if (lr && lr.status === "success") {
        chat.status = "ready";
      }
    }

    // Fetch workspace decisions if PurnaOS workspace
    if (chat.source === "purna_workspace" && chat.workspaceId) {
      try {
        const decisions = await api(`/api/purna/workspaces/${chat.workspaceId}/decisions`);
        chat.decisions = decisions || [];
      } catch (e) {
        // ignore
      }
    }
  } catch (e) {
    // ignore transient
  }
  persist();
  renderTabs();
}

function startPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    let needRender = false;
    for (const c of state.chats) {
      if (!c.repoId) continue;
      if (c.status === "indexing" || c.status === "ready" || c.status === "error") {
        const before = JSON.stringify(c.counts);
        await refreshStatus(c);
        if (JSON.stringify(c.counts) !== before) {
          // file list might have changed - bust cache so @ autocomplete refetches
          invalidateFileCache(c.repoId);
          if (c.id === state.activeId) needRender = true;
        }
      }
    }
    if (needRender) renderMain();
  }, POLL_MS);
}

/* ---- Sidebar settings ---- */
function renderSidebarSettings() {
  const keyStatus = $("#settings-key-status");
  const keyInput = $("#settings-key-input");
  const keyDetails = $("#settings-key");
  if (keyStatus && keyInput) {
    const k = getGeminiKey();
    if (k) {
      keyStatus.textContent = `set · ${k.slice(0, 4)}…${k.slice(-4)}`;
      keyStatus.classList.add("ok");
      keyStatus.classList.remove("warn");
    } else {
      keyStatus.textContent = "not configured";
      keyStatus.classList.add("warn");
      keyStatus.classList.remove("ok");
      if (keyDetails) keyDetails.open = true;
    }
    keyInput.value = k;
  }
  const prefs = getPrefs();
  const cntEl = $("#settings-prefs-count");
  if (cntEl) {
    cntEl.textContent = String(prefs.length);
    cntEl.classList.toggle("ok", prefs.length > 0);
  }
  const list = $("#prefs-list");
  if (list) {
    list.innerHTML = "";
    prefs.forEach((p, idx) => {
      const li = document.createElement("li");
      li.innerHTML = `<span class="prefs-text"></span><button class="prefs-del" title="Forget">×</button>`;
      li.querySelector(".prefs-text").textContent = p;
      li.querySelector(".prefs-del").addEventListener("click", () => {
        const cur = getPrefs();
        cur.splice(idx, 1);
        setPrefs(cur);
        renderSidebarSettings();
      });
      list.appendChild(li);
    });
  }
}

function wireSidebarSettings() {
  const saveBtn = $("#settings-key-save");
  const clearBtn = $("#settings-key-clear");
  const keyInput = $("#settings-key-input");
  if (saveBtn && keyInput) {
    saveBtn.addEventListener("click", () => {
      setGeminiKey((keyInput.value || "").trim());
      renderSidebarSettings();
    });
    keyInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); saveBtn.click(); }
    });
  }
  if (clearBtn && keyInput) {
    clearBtn.addEventListener("click", () => {
      setGeminiKey("");
      keyInput.value = "";
      renderSidebarSettings();
    });
  }
  const addBtn = $("#prefs-add-btn");
  const prefInput = $("#prefs-input");
  if (addBtn && prefInput) {
    const doAdd = () => {
      if (addPref(prefInput.value)) {
        prefInput.value = "";
        renderSidebarSettings();
      }
    };
    addBtn.addEventListener("click", doAdd);
    prefInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); doAdd(); }
    });
  }
}

/* ---- Boot ---- */
$("#new-chat").addEventListener("click", addChat);

(function init() {
  restore();
  if (state.chats.length === 0) addChat();
  if (!activeChat() && state.chats.length > 0) state.activeId = state.chats[0].id;
  wireSidebarSettings();
  renderSidebarSettings();
  renderTabs();
  renderMain();
  startPolling();
})();
