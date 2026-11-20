/* board.js — the static product board page for dot_swarm.
 *
 * `swarm board sync` copies this file into .swarm/board/ and writes
 * board-data.js next to it. board-data.js sets window.BOARD to the text of
 * .swarm/PRODUCTS.md. Every 30 seconds the page loads board-data.js again and
 * draws the board again when the text changed. No server is necessary: the
 * page works from a file:// URL.
 *
 * Node can also load this file (require) for parseBoard. The Python port in
 * dot_swarm/board.py must read the format the same way.
 */
(function (root) {
  "use strict";

  const STATE = { " ": "open", ">": "claimed", "/": "in progress", "!": "blocked", x: "done", X: "done" };

  /** Parse the part of PRODUCTS.md between the board markers. */
  function parseBoard(input) {
    const text = String(input).replace(/\r\n/g, "\n");
    const s = text.indexOf("<!-- board:start -->");
    const e = text.indexOf("<!-- board:end -->");
    if (s < 0 || e < s) throw new Error("PRODUCTS.md: board markers not found");
    const body = text.slice(s, e);
    const header = (rx) => ((text.match(rx) || [])[1] || "").trim();
    const board = {
      updated: header(/^Board updated:\s*(.+)$/m),
      editor: header(/^Board editor:\s*(.+)$/m),
      operator: header(/^Operator:\s*(.+)$/m),
      products: [],
    };
    for (const chunk of body.split(/^## /m).slice(1)) {
      const lines = chunk.split("\n");
      const p = { name: lines[0].trim(), fields: {}, tasks: [], decisions: [] };
      let mode = "fields";
      for (const raw of lines.slice(1)) {
        const line = raw.trimEnd();
        if (/^Tasks:\s*$/.test(line)) { mode = "tasks"; continue; }
        if (/^Decisions:\s*$/.test(line)) { mode = "decisions"; continue; }
        if (!line.trim() || line.startsWith("<!--")) continue;
        if (mode === "fields") {
          const m = line.match(/^- ([a-z][a-z0-9_-]*):\s*(.*)$/);
          if (m) p.fields[m[1]] = m[2];
        } else if (mode === "tasks") {
          const m = line.match(/^- \[(.)\]\s*(.*)$/);
          if (!m) continue;
          const parts = m[2].split(" | ").map((x) => x.trim());
          const t = { state: STATE[m[1]] || "open", id: parts[0], owner: "unassigned", status: "", title: "" };
          for (const part of parts.slice(1)) {
            const kv = part.match(/^(owner|status):\s*(.*)$/);
            if (kv) t[kv[1]] = kv[2];
            else t.title = t.title ? `${t.title} | ${part}` : part;
          }
          p.tasks.push(t);
        } else {
          const m = line.match(/^- (T[123])\s*\|\s*(.*)$/);
          if (!m) continue;
          const parts = m[2].split(" | ").map((x) => x.trim());
          const d = { tier: m[1], title: parts[0], context: "", ref: "" };
          for (const part of parts.slice(1)) {
            const kv = part.match(/^ref:\s*(.*)$/);
            if (kv) d.ref = kv[1];
            else d.context = d.context ? `${d.context} ${part}` : part;
          }
          p.decisions.push(d);
        }
      }
      board.products.push(p);
    }
    return board;
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { parseBoard, STATE };
    return;
  }

  // ---------------------------------------------------------------- browser
  const DATA_FILE = "board-data.js";
  const POLL_MS = 30000;
  const STAGES = ["idea", "design", "building", "beta", "live"];
  const PTIER = { P1: "Launch focus", P2: "Keep moving", P3: "Parked" };
  const DTIER = {
    T1: { name: "Tier 1", hint: "Do first, also in a low-capacity week." },
    T2: { name: "Tier 2", hint: "Do when you have more time." },
    T3: { name: "Tier 3", hint: "Do on a high-capacity day." },
  };
  const CAPACITY = {
    low: { label: "Low capacity", tiers: ["T1"] },
    medium: { label: "Medium", tiers: ["T1", "T2"] },
    high: { label: "High", tiers: ["T1", "T2", "T3"] },
  };
  const GROUP_LABEL = { fleet: "Fleet bots", harness: "Other harness", unassigned: "Not assigned" };

  const store = {
    get(k, d) { try { return root.localStorage.getItem(k) || d; } catch { return d; } },
    set(k, v) { try { root.localStorage.setItem(k, v); } catch { /* storage blocked: keep the value in memory only */ } },
  };

  let lastMd = null;
  let board = null;
  let capacity = CAPACITY[store.get("board.capacity", "low")] ? store.get("board.capacity", "low") : "low";
  let owner = store.get("board.owner", "all");

  const $ = (sel) => document.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const code = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>");
  const slug = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  const time = (d) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  // An owner tag is "group" or "group:name" (for example "fleet:builder").
  const ownerGroup = (o) => (String(o || "").split(":")[0].trim() || "unassigned");
  const operatorLabel = () => (board && board.operator) || "the operator";
  const groupLabel = (g) => (g === "operator" ? cap(operatorLabel()) : GROUP_LABEL[g] || g);
  const ownerName = (o) => (o.includes(":") ? o.slice(o.indexOf(":") + 1) : groupLabel(o));
  const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

  function groupOrder(groups) {
    const editor = board && board.editor ? ownerGroup(board.editor) : "";
    const rank = (g) => (g === "operator" ? 0 : g === editor ? 1 : g === "unassigned" ? 3 : 2);
    return [...groups].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
  }

  function stage(s) {
    const i = STAGES.indexOf((s || "").split(/\s/)[0].toLowerCase());
    return `<span class="stage" title="Stage: ${esc(STAGES[i] || s || "unknown")}">${STAGES.map(
      (x, k) => `<i class="${k < i ? "past" : k === i ? "now" : ""}"></i>`,
    ).join("")}<b>${esc(STAGES[i] || s || "?")}</b></span>`;
  }

  function sortedProducts() {
    const order = { P1: 0, P2: 1, P3: 2 };
    return [...board.products].sort((a, b) => (order[a.fields.tier] ?? 9) - (order[b.fields.tier] ?? 9));
  }

  function renderDecisions(prods) {
    const all = prods.flatMap((p) => p.decisions.map((d) => ({ ...d, product: p.name })));
    const counts = { T1: 0, T2: 0, T3: 0 };
    for (const d of all) counts[d.tier]++;
    $("#h-dec").textContent = `Decisions for ${operatorLabel()}`;
    $("#capacity").innerHTML = Object.entries(CAPACITY).map(([k, c]) =>
      `<button type="button" data-cap="${k}" aria-pressed="${k === capacity}">${c.label}<span>${c.tiers.reduce((n, t) => n + counts[t], 0)}</span></button>`,
    ).join("");
    $("#decisions").innerHTML = CAPACITY[capacity].tiers.map((tier) => {
      const items = all.filter((d) => d.tier === tier);
      return `<section class="tier" data-tier="${tier}">
  <h3>${DTIER[tier].name} <span class="count">${items.length}</span> <small>${DTIER[tier].hint}</small></h3>
  <ul>${items.map((d) => `<li><a class="tag" href="#${slug(d.product)}">${esc(d.product)}</a> <strong>${code(d.title)}</strong>${d.context ? `<span class="ctx">${code(d.context)}</span>` : ""}${d.ref ? `<span class="ref">${code(d.ref)}</span>` : ""}</li>`).join("") || `<li class="none">Nothing in this tier.</li>`}</ul>
</section>`;
    }).join("");
  }

  function renderProducts(prods) {
    const open = new Set([...document.querySelectorAll("#products details[open]")].map((d) => d.id));
    $("#products").innerHTML = ["P1", "P2", "P3"].map((tier) => {
      const list = prods.filter((p) => p.fields.tier === tier);
      if (!list.length) return "";
      return `<h3>${tier} · ${PTIER[tier]}</h3>${list.map((p) => {
        const f = p.fields;
        const id = slug(p.name);
        const openTasks = p.tasks.filter((t) => t.state !== "done");
        const blocked = openTasks.filter((t) => t.state === "blocked").length;
        return `<details id="${id}"${open.has(id) ? " open" : ""}>
  <summary><span class="name">${esc(p.name)}</span>${stage(f.stage)}<span class="meta">${openTasks.length} open${blocked ? ` · <em>${blocked} blocked</em>` : ""}${p.decisions.length ? ` · ${p.decisions.length} to decide` : ""}</span></summary>
  <div class="body">
    ${f.goal ? `<p><b>Goal.</b> ${code(f.goal)}</p>` : ""}
    ${f.now ? `<p><b>Now.</b> ${code(f.now)}</p>` : ""}
    ${f.next ? `<p><b>Next.</b> ${code(f.next)}</p>` : ""}
    <p class="small">${[f.epic && `Epic ${code(f.epic)}`, f.repos && `Repos: ${code(f.repos)}`, f.updated && `Updated ${esc(f.updated)}`].filter(Boolean).join(" · ")}</p>
    ${p.tasks.length ? `<ul class="tasks">${p.tasks.map((t) => `<li data-state="${esc(t.state)}"><span class="st">${esc(t.state)}</span> <code>${esc(t.id)}</code> <span class="who">${esc(ownerName(t.owner))}</span> ${code(t.title)}${t.status ? ` <span class="ctx">(${code(t.status)})</span>` : ""}</li>`).join("")}</ul>` : ""}
  </div>
</details>`;
      }).join("")}`;
    }).join("");
  }

  function renderOwners(prods) {
    const tasks = prods.flatMap((p) => p.tasks.filter((t) => t.state !== "done").map((t) => ({ ...t, product: p.name })));
    const names = groupOrder(new Set(tasks.map((t) => ownerGroup(t.owner))));
    const groups = names.map((g) => ({ g, items: tasks.filter((t) => ownerGroup(t.owner) === g) }));
    if (owner !== "all" && !groups.some((x) => x.g === owner)) owner = "all";
    $("#owner").innerHTML = `<option value="all">Everyone (${tasks.length})</option>` +
      groups.map((x) => `<option value="${esc(x.g)}"${x.g === owner ? " selected" : ""}>${esc(groupLabel(x.g))} (${x.items.length})</option>`).join("");
    $("#owners").innerHTML = groups.filter((x) => owner === "all" || x.g === owner).map((x) =>
      `<section><h3>${esc(groupLabel(x.g))} <span class="count">${x.items.length}</span></h3><ul class="tasks">${x.items.map((t) =>
        `<li data-state="${esc(t.state)}"><span class="st">${esc(t.state)}</span> <a class="tag" href="#${slug(t.product)}">${esc(t.product)}</a>${t.owner.includes(":") ? ` <span class="who">${esc(ownerName(t.owner))}</span>` : ""} ${code(t.title)}${t.status ? ` <span class="ctx">(${code(t.status)})</span>` : ""}</li>`,
      ).join("")}</ul></section>`,
    ).join("") || `<p class="none">No open tasks.</p>`;
  }

  function render() {
    if (!board) return;
    const prods = sortedProducts();
    $("#updated").textContent = [board.updated && `Board updated ${board.updated}`, board.editor && `Board editor: ${board.editor}`].filter(Boolean).join(" · ");
    renderDecisions(prods);
    renderProducts(prods);
    renderOwners(prods);
  }

  function status(kind, text) {
    const el = $("#status");
    el.dataset.kind = kind;
    el.textContent = text;
  }

  function onData() {
    const b = root.BOARD;
    if (!b || typeof b.md !== "string") { status("error", "board-data.js has no board. Run: swarm board sync"); return; }
    const synced = b.synced ? new Date(b.synced) : null;
    if (b.md !== lastMd) {
      try {
        board = parseBoard(b.md);
        lastMd = b.md;
        render();
      } catch (err) {
        status("error", `${err.message}. The page still shows the last good board.`);
        return;
      }
    }
    status("ok", `File read ${synced ? `${synced.toLocaleDateString()} ${time(synced)}` : "(unknown)"} · checked ${time(new Date())} · next check in ${POLL_MS / 1000} s`);
  }

  function loadData() {
    const old = document.getElementById("board-data");
    const s = document.createElement("script");
    s.id = "board-data";
    s.src = `${DATA_FILE}?v=${Date.now()}`;
    s.onload = () => { if (old) old.remove(); onData(); };
    s.onerror = () => { s.remove(); status("error", `${DATA_FILE} did not load. Run: swarm board sync`); };
    document.head.appendChild(s);
  }

  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-cap]");
    if (!btn) return;
    capacity = btn.dataset.cap;
    store.set("board.capacity", capacity);
    if (board) renderDecisions(sortedProducts());
  });
  document.addEventListener("change", (e) => {
    if (e.target.id !== "owner") return;
    owner = e.target.value;
    store.set("board.owner", owner);
    if (board) renderOwners(sortedProducts());
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) loadData(); });

  loadData();
  root.setInterval(loadData, POLL_MS);
})(typeof window !== "undefined" ? window : globalThis);
