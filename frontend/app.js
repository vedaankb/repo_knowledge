/* repo-knowledge frontend
   - Up to 5 chat tabs per session
   - Each tab is a distinct repo. Knowledge is never shared across tabs.
   - Per-tab state persists in sessionStorage.
*/

const MAX_TABS = 5;
const STORAGE_KEY = "repo_knowledge_session_v1";
const POLL_MS = 3000;

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
  const res = await fetch(path, {
    headers: opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : undefined,
    ...opts,
  });
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
};

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
  }
}

function renderOnboarding(main, chat) {
  const tpl = $("#tpl-onboarding").content.cloneNode(true);
  const errEl = tpl.querySelector(".onb-error");
  function showErr(msg) {
    if (!msg) { errEl.hidden = true; errEl.textContent = ""; return; }
    errEl.hidden = false; errEl.textContent = msg;
  }

  const ghForm = tpl.querySelector('form[data-action="github"]');
  const tokenRow = ghForm.querySelector(".token-row");
  const tokenInput = tokenRow.querySelector('input[name="token"]');

  ghForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    showErr(null);
    const url = ghForm.url.value.trim();
    const token = (tokenInput.value || "").trim();
    if (!url) return;
    // Token is required only once the token field has been revealed.
    if (!tokenRow.hidden && !token) {
      showErr("Token is required for this private repo.");
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
      if (err.code === "auth_required") {
        tokenRow.hidden = false;
        tokenInput.required = true;
        showErr(err.message || "Private repo: a GitHub access token is required.");
        setTimeout(() => tokenInput.focus(), 0);
      } else if (err.code === "invalid_token") {
        tokenRow.hidden = false;
        tokenInput.required = true;
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
  subEl.textContent = [
    chat.sub,
    `code: ${chat.counts.code_chunks}`,
    `skills: ${chat.counts.feature_skills}`,
    `memory: ${exchanges} turn${exchanges === 1 ? "" : "s"}`,
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

  const form = tpl.querySelector("form.composer");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = form.question.value.trim();
    if (!q) return;
    // commit the scope input even if user didn't blur the field
    chat.commitScope = (scopeInput.value || "").trim() || null;
    persist();
    form.question.value = "";
    await ask(chat, q);
  });

  main.appendChild(tpl);
  // scroll to bottom
  requestAnimationFrame(() => {
    const m = $(".messages", main);
    if (m) m.scrollTop = m.scrollHeight;
  });
}

function appendMessageDom(container, m) {
  const div = document.createElement("div");
  const refusal = m.role === "assistant" && m.refusal ? " refusal" : "";
  div.className = `msg ${m.role}${refusal}`;
  if (m.role === "system") {
    div.textContent = m.content;
  } else if (m.role === "assistant" && m.previewing) {
    const headline = m.previewSources
      ? `Reading repo context · ${m.previewSources.code.length} code chunks, ${m.previewSources.skills.length} PR notes`
      : "Reading repo context...";
    div.innerHTML = `
      <div class="thinking">${escapeHtml(headline)}</div>
      ${renderPreviewHtml(m.previewSources)}
    `;
  } else {
    let html = `<div>${renderMarkdownish(m.content)}</div>`;
    if (m.sources) html += renderSourcesHtml(m.sources);
    div.innerHTML = html;
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

async function ask(chat, question) {
  if (!chat.repoId) return;
  chat.messages.push({ role: "user", content: question, ts: Date.now() });
  const placeholderIdx = chat.messages.push({
    role: "assistant",
    content: "",
    previewing: true,
    previewSources: null,
    ts: Date.now(),
  }) - 1;
  persist();
  renderMain();

  // STRICT ISOLATION: server retrieves prior turns by chat_id only.
  // We never send history in-band — keeps prompt bounded, scales to long chats.
  const body = JSON.stringify({
    repo_id: chat.repoId,
    chat_id: chat.id,
    question,
    commit_sha: chat.commitScope || null,
  });

  const previewP = api("/api/chat/preview", { method: "POST", body })
    .then((p) => {
      // If the final answer already replaced the placeholder, skip.
      const cur = chat.messages[placeholderIdx];
      if (!cur || !cur.previewing) return;
      cur.previewSources = p;
      persist();
      renderMain();
    })
    .catch(() => { /* ignore preview failure; final answer still works */ });

  try {
    const res = await api("/api/chat", { method: "POST", body });
    const text = (res.answer || "").trim();
    const refusal = !res.grounded || text.toLowerCase().includes("don't have enough information");
    chat.messages[placeholderIdx] = {
      role: "assistant",
      content: text,
      sources: res.sources,
      refusal,
      ts: Date.now(),
    };
  } catch (e) {
    chat.messages[placeholderIdx] = {
      role: "assistant",
      content: `Error: ${e.message}`,
      refusal: true,
      ts: Date.now(),
    };
  }
  await previewP; // settle, even though we ignore its result now
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
        if (JSON.stringify(c.counts) !== before && c.id === state.activeId) needRender = true;
      }
    }
    if (needRender) renderMain();
  }, POLL_MS);
}

/* ---- Boot ---- */
$("#new-chat").addEventListener("click", addChat);

(function init() {
  restore();
  if (state.chats.length === 0) addChat();
  if (!activeChat() && state.chats.length > 0) state.activeId = state.chats[0].id;
  renderTabs();
  renderMain();
  startPolling();
})();
