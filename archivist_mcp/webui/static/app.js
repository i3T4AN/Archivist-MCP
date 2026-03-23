const app = document.getElementById("app");
const projectInput = document.getElementById("projectId");

function qs(path, params = {}) {
  const url = new URL(path, window.location.origin);
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
  });
  return fetch(url.toString()).then((r) => r.json());
}

function postJson(path, body) {
  return fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then(async (r) => {
    const payload = await r.json();
    return { status: r.status, payload };
  });
}

function projectId() {
  return projectInput.value.trim() || "proj-1";
}

function stateBadge(state) {
  const cls = ["badge", state].join(" ");
  return `<span class="${cls}">${state}</span>`;
}

function setActiveTab() {
  const route = (window.location.hash || "#/search").slice(1);
  document.querySelectorAll(".tabs a").forEach((a) => {
    a.classList.toggle("active", a.getAttribute("href") === `#${route}`);
  });
}

function explainWhy(result) {
  const p = result.provenance || {};
  return `confidence=${(result.confidence || 0).toFixed(3)} | fts=${(p.fts || 0).toFixed(3)} vector=${(p.vector || 0).toFixed(3)} graph=${(p.graph || 0).toFixed(3)} recency=${(p.recency || 0).toFixed(3)}`;
}

async function renderSearch() {
  const query = new URLSearchParams(window.location.search).get("q") || "";
  app.innerHTML = `
    <div class="grid fade-in">
      <section class="card">
        <h3>Search</h3>
        <div style="display:flex; gap:8px; margin-bottom:10px;">
          <input id="searchQuery" placeholder="Find decisions, incidents, entities" value="${query.replace(/"/g, "&quot;")}" style="flex:1;" />
          <button id="searchBtn">Run</button>
        </div>
        <div id="searchResults" class="list"></div>
      </section>
      <aside class="card">
        <h3>Result Inspector</h3>
        <div id="inspector" class="meta">Pick a result to inspect provenance and context.</div>
      </aside>
    </div>
  `;

  const searchResults = document.getElementById("searchResults");
  const inspector = document.getElementById("inspector");

  async function runSearch() {
    const q = document.getElementById("searchQuery").value.trim();
    if (!q) {
      searchResults.innerHTML = '<p class="meta">Enter a query.</p>';
      return;
    }
    const res = await qs("/api/search", { project_id: projectId(), q, limit: 8 });
    const rows = res.data?.results || [];
    searchResults.innerHTML = rows.map((r, i) => `
      <article class="item" data-idx="${i}">
        <strong>${r.title || r.node_id}</strong>
        <p class="meta">${r.type || "Node"} • ${r.node_id}</p>
        <p>${(r.content || "").slice(0, 140)}</p>
        <div class="badges">${stateBadge(r.state || "active")}</div>
      </article>
    `).join("") || '<p class="meta">No matches.</p>';

    searchResults.querySelectorAll(".item").forEach((el) => {
      el.addEventListener("click", () => {
        const row = rows[Number(el.dataset.idx)];
        inspector.innerHTML = `
          <div class="kv">
            <div>Node</div><div>${row.node_id}</div>
            <div>Type</div><div>${row.type || "unknown"}</div>
            <div>State</div><div>${row.state || "active"}</div>
            <div>Why</div><div>${explainWhy(row)}</div>
          </div>
          <h4>Explain-Why Panel</h4>
          <pre>${JSON.stringify(row.provenance || {}, null, 2)}</pre>
        `;
      });
    });
  }

  document.getElementById("searchBtn").addEventListener("click", runSearch);
  if (query) runSearch();
}

async function renderGraph() {
  const data = await qs("/api/graph", { project_id: projectId() });
  const nodes = data.nodes || [];
  const edges = data.edges || [];
  app.innerHTML = `
    <div class="grid fade-in">
      <section class="card">
        <h3>Graph Explorer</h3>
        <p class="meta">Nodes: ${nodes.length} | Edges: ${edges.length}</p>
        <div class="list">
          ${nodes.map((n) => `
            <article class="item">
              <strong>${n.title}</strong>
              <p class="meta">${n.type} • ${n.node_id}</p>
              <div class="badges">${stateBadge(n.state)}</div>
            </article>
          `).join("")}
        </div>
      </section>
      <aside class="card">
        <h3>Latest Edges</h3>
        <div class="list">
          ${edges.slice(0, 40).map((e) => `
            <article class="item">
              <strong>${e.type}</strong>
              <p class="meta">${e.from_node_id} → ${e.to_node_id}</p>
              <p class="meta">weight=${(e.weight ?? 0).toFixed(2)}</p>
            </article>
          `).join("")}
        </div>
      </aside>
    </div>
  `;
}

async function renderDecisions() {
  const res = await qs("/api/decisions", { project_id: projectId() });
  const rows = res.decisions || [];
  app.innerHTML = `
    <section class="card fade-in">
      <h3>Decision Timeline</h3>
      <div class="list">
        ${rows.map((d) => `
          <article class="item">
            <strong>${d.title}</strong>
            <p class="meta">v${d.version} • ${d.updated_at}</p>
            <p>${(d.content || "").slice(0, 220)}</p>
            <div class="badges">${stateBadge(d.state)}</div>
          </article>
        `).join("")}
      </div>
    </section>
  `;
}

async function renderIncidents() {
  const res = await qs("/api/incidents", { project_id: projectId() });
  const rows = res.incidents || [];
  app.innerHTML = `
    <section class="card fade-in">
      <h3>Incident Timeline</h3>
      <div class="list">
        ${rows.map((i) => `
          <article class="item">
            <strong>${i.title}</strong>
            <p class="meta">${i.updated_at}</p>
            <p>${(i.content || "").slice(0, 220)}</p>
            <p class="meta">Resolved By: ${i.resolved_by_title || "-"} ${i.resolved_by_id ? `(${i.resolved_by_id})` : ""}</p>
            <div class="badges">${stateBadge(i.state)}</div>
          </article>
        `).join("")}
      </div>
    </section>
  `;
}

async function renderConflicts() {
  const res = await qs("/api/conflicts", { project_id: projectId() });
  const rows = res.conflicts || [];
  app.innerHTML = `
    <section class="card fade-in">
      <h3>Conflict Inbox (Read-only)</h3>
      <div class="list">
        ${rows.map((c) => `
          <article class="item">
            <strong>${c.action}</strong>
            <p class="meta">event=${c.event_id} • actor=${c.actor_id || "-"} • ${c.created_at}</p>
            <p class="meta">target=${c.target_id}</p>
            <pre>${JSON.stringify(c.details || {}, null, 2)}</pre>
          </article>
        `).join("") || '<p class="meta">No conflicts found.</p>'}
      </div>
    </section>
  `;
}

async function renderControls() {
  app.innerHTML = `
    <div class="grid fade-in">
      <section class="card">
        <h3>Rule Controls</h3>
        <p class="meta">Create/update/deprecate Rule nodes.</p>
        <div class="list">
          <article class="item">
            <h4>Create Rule</h4>
            <input id="ruleCreateTitle" placeholder="Title" />
            <input id="ruleCreateContent" placeholder="Content" />
            <input id="ruleCreateSeverity" placeholder="severity (info|warning|error|critical)" />
            <input id="ruleCreateEnforcement" placeholder="enforcement (advisory|gate_on_write|gate_on_recall)" />
            <button id="ruleCreateBtn">Create</button>
          </article>
          <article class="item">
            <h4>Update Rule</h4>
            <input id="ruleUpdateNodeId" placeholder="node_id" />
            <input id="ruleUpdateVersion" placeholder="expected_version" />
            <input id="ruleUpdateTitle" placeholder="New title" />
            <input id="ruleUpdateContent" placeholder="New content" />
            <button id="ruleUpdateBtn">Update</button>
          </article>
          <article class="item">
            <h4>Deprecate Rule</h4>
            <input id="ruleDepNodeId" placeholder="node_id" />
            <input id="ruleDepVersion" placeholder="expected_version" />
            <label><input type="checkbox" id="ruleDepConfirm" /> confirm high-impact action</label>
            <button id="ruleDepBtn">Deprecate</button>
          </article>
        </div>
      </section>
      <aside class="card">
        <h3>Conflict & Repair</h3>
        <div class="list">
          <article class="item">
            <h4>Resolve Conflict</h4>
            <input id="resolveEventId" placeholder="conflict_event_id" />
            <input id="resolveNote" placeholder="resolution note" />
            <label><input type="checkbox" id="resolveConfirm" /> confirm</label>
            <button id="resolveBtn">Resolve</button>
          </article>
          <article class="item">
            <h4>Promote Branch Record</h4>
            <input id="promoteNodeId" placeholder="node_id" />
            <input id="promoteVersion" placeholder="expected_version" />
            <input id="promoteNote" placeholder="resolution note" />
            <label><input type="checkbox" id="promoteConfirm" /> confirm</label>
            <button id="promoteBtn">Promote Scope</button>
          </article>
          <article class="item">
            <h4>Invalidate Stale Memory</h4>
            <input id="invalidateNodeId" placeholder="node_id" />
            <input id="invalidateVersion" placeholder="expected_version" />
            <input id="invalidateReason" placeholder="reason" />
            <input id="invalidateCorrected" placeholder="corrected_node_id (optional)" />
            <label><input type="checkbox" id="invalidateConfirm" /> confirm</label>
            <button id="invalidateBtn">Invalidate</button>
          </article>
        </div>
        <h4>Write Output</h4>
        <pre id="writeOutput">{}</pre>
      </aside>
    </div>
  `;

  const out = document.getElementById("writeOutput");
  function show(result) {
    out.textContent = JSON.stringify(result, null, 2);
  }

  document.getElementById("ruleCreateBtn").addEventListener("click", async () => {
    const result = await postJson("/api/rules", {
      project_id: projectId(),
      action: "create",
      title: document.getElementById("ruleCreateTitle").value,
      content: document.getElementById("ruleCreateContent").value,
      severity: document.getElementById("ruleCreateSeverity").value,
      enforcement: document.getElementById("ruleCreateEnforcement").value,
    });
    show(result);
  });

  document.getElementById("ruleUpdateBtn").addEventListener("click", async () => {
    const result = await postJson("/api/rules", {
      project_id: projectId(),
      action: "update",
      node_id: document.getElementById("ruleUpdateNodeId").value,
      expected_version: Number(document.getElementById("ruleUpdateVersion").value),
      title: document.getElementById("ruleUpdateTitle").value,
      content: document.getElementById("ruleUpdateContent").value,
    });
    show(result);
  });

  document.getElementById("ruleDepBtn").addEventListener("click", async () => {
    const result = await postJson("/api/rules", {
      project_id: projectId(),
      action: "deprecate",
      node_id: document.getElementById("ruleDepNodeId").value,
      expected_version: Number(document.getElementById("ruleDepVersion").value),
      confirm: document.getElementById("ruleDepConfirm").checked,
    });
    show(result);
  });

  document.getElementById("resolveBtn").addEventListener("click", async () => {
    const result = await postJson("/api/conflicts/resolve", {
      project_id: projectId(),
      conflict_event_id: Number(document.getElementById("resolveEventId").value),
      resolution_note: document.getElementById("resolveNote").value,
      confirm: document.getElementById("resolveConfirm").checked,
    });
    show(result);
  });

  document.getElementById("promoteBtn").addEventListener("click", async () => {
    const result = await postJson("/api/promote_scope", {
      project_id: projectId(),
      node_id: document.getElementById("promoteNodeId").value,
      expected_version: Number(document.getElementById("promoteVersion").value),
      resolution_note: document.getElementById("promoteNote").value,
      confirm: document.getElementById("promoteConfirm").checked,
    });
    show(result);
  });

  document.getElementById("invalidateBtn").addEventListener("click", async () => {
    const result = await postJson("/api/invalidate", {
      project_id: projectId(),
      node_id: document.getElementById("invalidateNodeId").value,
      expected_version: Number(document.getElementById("invalidateVersion").value),
      reason: document.getElementById("invalidateReason").value,
      corrected_node_id: document.getElementById("invalidateCorrected").value || null,
      confirm: document.getElementById("invalidateConfirm").checked,
    });
    show(result);
  });
}

async function route() {
  setActiveTab();
  const path = (window.location.hash || "#/search").replace("#", "");
  if (path === "/search") return renderSearch();
  if (path === "/graph") return renderGraph();
  if (path === "/decisions") return renderDecisions();
  if (path === "/incidents") return renderIncidents();
  if (path === "/conflicts") return renderConflicts();
  if (path === "/controls") return renderControls();
  window.location.hash = "#/search";
}

window.addEventListener("hashchange", route);
projectInput.addEventListener("change", route);
route();
