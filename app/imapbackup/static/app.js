/* IMAP Backup Console - single page front end, no build step. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  data: null,
  selected: new Set(),
  filter: "",
  signature: "",
  log: { jobId: null, offset: 0, timer: null, status: null },
  pollTimer: null,
};

/* ----------------------------------------------------------------- utils */

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      // Cross-site pages cannot set this, which is what blocks CSRF.
      "X-Requested-With": "imapbackup",
      ...(options.headers || {}),
    },
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body && body.detail) {
        detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      }
    } catch (_) { /* keep status text */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function toast(message, kind = "info", timeout = 6000) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = esc(message).replace(/\n/g, "<br>");
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), timeout);
}

function fmtBytes(n) {
  if (!n) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = n, i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
  return `${value < 10 && i > 0 ? value.toFixed(1) : Math.round(value)} ${units[i]}`;
}

function fmtInt(n) {
  return n ? new Intl.NumberFormat().format(n) : (n === 0 ? "0" : "—");
}

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fmtAgo(iso) {
  if (!iso) return "never";
  const secs = (Date.now() - new Date(iso).getTime()) / 1000;
  if (Number.isNaN(secs)) return iso;
  if (secs < 90) return "just now";
  if (secs < 5400) return `${Math.round(secs / 60)} min ago`;
  if (secs < 172800) return `${Math.round(secs / 3600)} h ago`;
  return `${Math.round(secs / 86400)} d ago`;
}

function fmtDuration(from, to) {
  if (!from) return "—";
  const start = new Date(from).getTime();
  const end = to ? new Date(to).getTime() : Date.now();
  let secs = Math.max(0, Math.round((end - start) / 1000));
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  secs %= 60;
  if (mins < 60) return `${mins}m ${secs}s`;
  return `${Math.floor(mins / 60)}h ${mins % 60}m`;
}

function readOptions(prefix) {
  const get = (name) => $(`#${prefix}-${name}`);
  return {
    dry_run: get("dry_run").checked,
    automap: get("automap").checked,
    insecure_tls: get("insecure_tls").checked,
    exclude: get("exclude") ? get("exclude").value : "",
    folders: get("folders") ? get("folders").value : "",
    extra_args: get("extra_args") ? get("extra_args").value : "",
  };
}

function serverSpec(prefix) {
  const port = $(`#${prefix}-port`).value.trim();
  return {
    host: $(`#${prefix}-host`).value.trim(),
    port: port ? Number(port) : null,
    security: $(`#${prefix}-security`).value,
  };
}

function confirmDialog({ title, message, confirmLabel = "Confirm", danger = true, checkbox = null }) {
  return new Promise((resolve) => {
    const wrap = document.createElement("div");
    wrap.className = "modal";
    wrap.innerHTML = `
      <div class="modal-card" style="width:min(460px,100%)">
        <header><h3>${esc(title)}</h3></header>
        <div class="modal-body">
          <p class="note">${esc(message)}</p>
          ${checkbox ? `<label class="check"><input type="checkbox" id="cd-check"> <span>${esc(checkbox)}</span></label>` : ""}
        </div>
        <footer>
          <button class="btn ghost" data-no>Cancel</button>
          <button class="btn ${danger ? "danger" : "primary"}" data-yes>${esc(confirmLabel)}</button>
        </footer>
      </div>`;
    document.body.appendChild(wrap);
    const finish = (ok) => {
      const checked = checkbox ? $("#cd-check", wrap).checked : false;
      wrap.remove();
      resolve({ ok, checked });
    };
    $("[data-no]", wrap).onclick = () => finish(false);
    $("[data-yes]", wrap).onclick = () => finish(true);
    wrap.onclick = (e) => { if (e.target === wrap) finish(false); };
  });
}

/* ------------------------------------------------------------------ tabs */

function showTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("is-active", t.dataset.tab === name));
  $$(".panel").forEach((p) => p.classList.toggle("is-active", p.id === `panel-${name}`));
}

$("#tabs").addEventListener("click", (e) => {
  const tab = e.target.closest(".tab");
  if (tab) showTab(tab.dataset.tab);
});

$$("select[data-port]").forEach((select) => {
  select.addEventListener("change", () => {
    $(`#${select.dataset.port}`).placeholder = select.value === "ssl" ? "993" : "143";
  });
});

/* ---------------------------------------------------------------- render */

function statValue(stats, ...needles) {
  const keys = Object.keys(stats || {});
  for (const needle of needles) {
    const key = keys.find((k) => k.toLowerCase().includes(needle));
    if (key) return String(stats[key]).trim();
  }
  return null;
}

const KIND_LABEL = { backup: "backup", restore: "restore", check: "login test" };

function statusPill(job, withKind = false) {
  if (!job) return '<span class="pill none">no run yet</span>';
  const labels = {
    done: job.dry_run ? "dry run ok" : "finished",
    failed: "failed",
    running: "running",
    queued: "queued",
    cancelled: "cancelled",
  };
  const pill = `<span class="pill ${job.status}">${esc(labels[job.status] || job.status)}</span>`;
  if (!withKind || !job.kind) return pill;
  return `${pill} <span class="kind-hint">${esc(KIND_LABEL[job.kind] || job.kind)}</span>`;
}

function progressBlock(job) {
  if (!job || job.status !== "running") return "";
  const p = job.progress || {};
  let pct = null;
  let label = p.last_line || "starting…";
  if (p.messages_total > 0) {
    pct = Math.min(100, Math.round(((p.messages_total - p.messages_left) / p.messages_total) * 100));
    label = `${p.folder || ""} — ${p.messages_total - p.messages_left}/${p.messages_total} messages`;
  } else if (p.folder_total > 0) {
    pct = Math.round((p.folder_index / p.folder_total) * 100);
    label = `folder ${p.folder_index}/${p.folder_total} — ${p.folder || ""}`;
  }
  return `<div class="progress">
      <div class="bar"><i style="width:${pct === null ? 8 : pct}%"></i></div>
      <div class="label" title="${esc(label)}">${esc(label)}</div>
    </div>`;
}

function renderChips(data) {
  const running = data.counts.running || 0;
  const queued = data.counts.queued || 0;
  const chips = [
    ["Mailboxes", fmtInt(data.totals.accounts)],
    ["Messages", fmtInt(data.totals.messages)],
    ["Stored", fmtBytes(data.totals.bytes)],
    ["Free disk", fmtBytes(data.disk.free)],
  ];
  let html = chips.map(([k, v]) => `<span class="chip">${k} <b>${v}</b></span>`).join("");
  if (running || queued) {
    html += `<span class="chip live">Active <b>${running} running · ${queued} queued</b></span>`;
  }
  $("#chips").innerHTML = html;
  $("#brand-sub").textContent =
    `imapsync · ${data.config.concurrency} parallel jobs · store ${data.config.store}`;

  if (data.config.allow_extra_args === false) {
    ["bk-extra_args", "rs-extra_args"].forEach((id) => {
      const field = $(`#${id}`);
      if (!field || field.disabled) return;
      field.disabled = true;
      field.value = "";
      field.placeholder = "disabled on this instance (ALLOW_EXTRA_ARGS=0)";
    });
  }
}

function renderMailboxes(data) {
  const body = $("#mailbox-body");
  const filter = state.filter.toLowerCase();
  const accounts = data.accounts.filter(
    (a) => !filter || a.email.toLowerCase().includes(filter) ||
      a.source.host.toLowerCase().includes(filter)
  );
  $("#badge-mailboxes").textContent = data.accounts.length;

  if (!accounts.length) {
    body.innerHTML = `<tr><td colspan="9" class="empty">${
      data.accounts.length ? "No account matches the filter." :
      "No mailboxes yet — add accounts on the “New backup” tab."
    }</td></tr>`;
    return;
  }

  body.innerHTML = accounts.map((a) => {
    const job = a.latest_job;
    const busy = job && (job.status === "running" || job.status === "queued");
    return `<tr data-id="${a.id}">
      <td class="tick"><input type="checkbox" data-select="${a.id}" ${state.selected.has(a.id) ? "checked" : ""}></td>
      <td>
        <div class="primary-cell">${esc(a.email)}</div>
        <div class="mono">${esc(a.source.user)} @ ${esc(a.source.host)}:${a.source.port}</div>
      </td>
      <td class="mono">${esc(a.local_user)}</td>
      <td class="num">${fmtInt(a.mailbox.folders)}</td>
      <td class="num">${fmtInt(a.mailbox.messages)}</td>
      <td class="num">${fmtBytes(a.mailbox.bytes)}</td>
      <td title="${esc(fmtDate(a.last_backup_at))}">${a.last_backup_at ? esc(fmtAgo(a.last_backup_at)) : "never"}</td>
      <td>${statusPill(job, true)}${progressBlock(job)}${
        job && job.status === "failed" && job.error
          ? `<div class="label" style="color:var(--err);max-width:220px" title="${esc(job.error)}">${esc(job.error.slice(0, 70))}</div>`
          : ""
      }</td>
      <td class="right"><div class="actions-cell">
        ${job && job.has_log ? `<button class="btn small" data-action="log" data-job="${job.id}">Protocol</button>` : ""}
        ${busy
          ? `<button class="btn small danger" data-action="cancel" data-job="${job.id}">Stop</button>`
          : `<button class="btn small" data-action="backup" data-id="${a.id}">Back up</button>
             <button class="btn small" data-action="restore" data-id="${a.id}">Restore</button>`}
        <button class="btn small ghost" data-action="detail" data-id="${a.id}">Details</button>
      </div></td>
    </tr>`;
  }).join("");
}

function renderJobs(data) {
  const body = $("#jobs-body");
  $("#badge-jobs").textContent = (data.counts.running || 0) + (data.counts.queued || 0);
  if (!data.jobs.length) {
    body.innerHTML = '<tr><td colspan="8" class="empty">No jobs yet.</td></tr>';
    return;
  }
  body.innerHTML = data.jobs.map((job) => {
    const msgs = statValue(job.stats, "messages transferred", "msgs transferred", "transferred");
    const bytes = statValue(job.stats, "bytes transferred");
    const errors = statValue(job.stats, "detected errors", "error");
    const kind = { backup: "Backup", restore: "Restore", check: "Login test" }[job.kind] || job.kind;
    const target = job.kind === "restore" && job.target_host
      ? `<div class="mono">→ ${esc(job.target_host)} as ${esc(job.target_user || "")}</div>` : "";
    let result = "—";
    if (job.status === "done") {
      // "Total bytes transferred : 12345 (12.1 KiB)" -> leading integer only.
      const byteCount = bytes ? parseInt(bytes, 10) : 0;
      result = job.kind === "check" ? "login ok"
        : `${msgs !== null ? esc(parseInt(msgs, 10) || 0) : "0"} msgs${byteCount ? ` · ${fmtBytes(byteCount)}` : ""}`;
      if (errors && errors !== "0") result += ` · ${esc(errors)} err`;
    } else if (job.status === "failed" && job.error) {
      result = `<span style="color:var(--err)" title="${esc(job.error)}">${esc(job.error.slice(0, 60))}</span>`;
    } else if (job.status === "running") {
      result = esc((job.progress && job.progress.folder) || "…");
    }
    return `<tr>
      <td class="mono">#${job.id}${job.dry_run ? ' <span class="pill none">dry</span>' : ""}</td>
      <td><div class="primary-cell">${esc(job.email)}</div>${target}</td>
      <td>${kind}</td>
      <td>${statusPill(job)}</td>
      <td title="${esc(fmtDate(job.started_at || job.created_at))}">${esc(fmtDate(job.started_at || job.created_at))}</td>
      <td>${job.started_at ? esc(fmtDuration(job.started_at, job.finished_at)) : "—"}</td>
      <td>${result}</td>
      <td class="right"><div class="actions-cell">
        ${job.has_log ? `<button class="btn small" data-action="log" data-job="${job.id}">View</button>
          <a class="btn small ghost" href="/api/jobs/${job.id}/log/download" download>Download</a>` : ""}
        ${job.status === "running" || job.status === "queued"
          ? `<button class="btn small danger" data-action="cancel" data-job="${job.id}">Stop</button>` : ""}
      </div></td>
    </tr>`;
  }).join("");
}

function render(force = false) {
  const data = state.data;
  if (!data) return;
  const signature = JSON.stringify([data.accounts, data.jobs, data.counts, state.filter, [...state.selected]]);
  if (!force && signature === state.signature) return;
  state.signature = signature;
  renderChips(data);
  renderMailboxes(data);
  renderJobs(data);
  updateBulkbar();
}

async function poll() {
  try {
    state.data = await api("/api/state");
    render();
  } catch (err) {
    console.error(err);
  }
  const busy = state.data && (state.data.counts.running || state.data.counts.queued);
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(poll, busy ? 1500 : 5000);
}

/* ------------------------------------------------------------- selection */

function updateBulkbar() {
  const count = state.selected.size;
  $("#bulkbar").hidden = count === 0;
  $("#sel-count").textContent = count;
  const all = state.data ? state.data.accounts.length : 0;
  $("#select-all").checked = all > 0 && count === all;
}

$("#select-all").addEventListener("change", (e) => {
  state.selected.clear();
  if (e.target.checked && state.data) {
    state.data.accounts.forEach((a) => state.selected.add(a.id));
  }
  render(true);
});

$("#mailbox-filter").addEventListener("input", (e) => {
  state.filter = e.target.value.trim();
  render(true);
});

/* --------------------------------------------------------------- actions */

async function startJobs(ids, kind, options) {
  if (!ids.length) return;
  const res = await api("/api/jobs", {
    method: "POST",
    body: JSON.stringify({ account_ids: ids, kind, options: options || readOptions("bk") }),
  });
  toast(`${res.jobs.length} ${kind === "check" ? "login test" : "backup"} job(s) queued.`, "ok");
  showTab("jobs");
  poll();
}

async function deleteAccounts(ids) {
  const { ok, checked } = await confirmDialog({
    title: `Delete ${ids.length} mailbox record${ids.length > 1 ? "s" : ""}?`,
    message: "The account and its job history will be removed from the console.",
    confirmLabel: "Delete",
    checkbox: "Also delete the downloaded mail from disk (cannot be undone)",
  });
  if (!ok) return;
  for (const id of ids) {
    await api(`/api/accounts/${id}?purge=${checked}`, { method: "DELETE" });
    state.selected.delete(id);
  }
  toast(`Deleted ${ids.length} mailbox record(s).`, "ok");
  poll();
}

document.addEventListener("click", async (e) => {
  const select = e.target.closest("[data-select]");
  if (select) {
    const id = Number(select.dataset.select);
    if (select.checked) state.selected.add(id); else state.selected.delete(id);
    updateBulkbar();
    state.signature = "";
    return;
  }

  const bulk = e.target.closest("[data-bulk]");
  if (bulk) {
    const ids = [...state.selected];
    if (!ids.length) return;
    try {
      if (bulk.dataset.bulk === "restore") openRestore(ids);
      else if (bulk.dataset.bulk === "delete") await deleteAccounts(ids);
      else await startJobs(ids, bulk.dataset.bulk);
    } catch (err) { toast(err.message, "err"); }
    return;
  }

  const btn = e.target.closest("[data-action]");
  if (!btn) return;
  const { action } = btn.dataset;
  const id = Number(btn.dataset.id);
  try {
    if (action === "backup") await startJobs([id], "backup");
    else if (action === "restore") openRestore([id]);
    else if (action === "detail") await openDetail(id);
    else if (action === "log") openLog(Number(btn.dataset.job));
    else if (action === "cancel") {
      await api(`/api/jobs/${btn.dataset.job}/cancel`, { method: "POST" });
      toast("Job cancelled.", "ok");
      poll();
    }
  } catch (err) {
    toast(err.message, "err");
  }
});

/* ----------------------------------------------------------- backup form */

async function submitBulk(action) {
  const source = serverSpec("bk");
  if (!source.host) { toast("Please enter the source host.", "err"); return; }
  const credentials = $("#bk-credentials").value;
  if (!credentials.trim()) { toast("Please enter at least one account.", "err"); return; }

  const res = await api("/api/accounts/bulk", {
    method: "POST",
    body: JSON.stringify({ source, credentials, action, options: readOptions("bk") }),
  });
  const parts = [];
  if (res.created) parts.push(`${res.created} added`);
  if (res.updated) parts.push(`${res.updated} updated`);
  if (res.jobs.length) parts.push(`${res.jobs.length} job(s) queued`);
  toast(parts.join(" · ") || "Nothing to do.", "ok");
  if (res.errors.length) toast(`Skipped:\n${res.errors.join("\n")}`, "err", 12000);
  if (res.jobs.length) showTab("jobs"); else showTab("mailboxes");
  poll();
}

$("#backup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { await submitBulk("backup"); } catch (err) { toast(err.message, "err"); }
});
$("#btn-check").addEventListener("click", async () => {
  try { await submitBulk("check"); } catch (err) { toast(err.message, "err"); }
});
$("#btn-import").addEventListener("click", async () => {
  try { await submitBulk("import"); } catch (err) { toast(err.message, "err"); }
});

/* --------------------------------------------------------------- restore */

let restoreIds = [];

function openRestore(ids) {
  restoreIds = ids;
  const accounts = state.data.accounts.filter((a) => ids.includes(a.id));
  $("#restore-summary").textContent =
    `Uploading ${accounts.length} backed up mailbox(es) to the target server: ` +
    accounts.map((a) => a.email).join(", ");
  const mapping = $("#rs-mapping");
  if (!mapping.value.trim()) {
    mapping.value = accounts.map((a) => `${a.email};`).join("\n");
  }
  $("#restore-modal").hidden = false;
  syncRestoreFields();
}

function syncRestoreFields() {
  const single = $("#rs-password_mode").value === "single";
  $("#rs-single-wrap").hidden = !single;
  $("#rs-mapping-wrap").hidden = single && $("#rs-user_mode").value !== "mapping";
}

$("#rs-password_mode").addEventListener("change", syncRestoreFields);
$("#rs-user_mode").addEventListener("change", syncRestoreFields);

$("#btn-restore").addEventListener("click", async () => {
  const target = serverSpec("rs");
  if (!target.host) { toast("Please enter the target host.", "err"); return; }
  try {
    const res = await api("/api/restore", {
      method: "POST",
      body: JSON.stringify({
        account_ids: restoreIds,
        target,
        user_mode: $("#rs-user_mode").value,
        password_mode: $("#rs-password_mode").value,
        single_password: $("#rs-single_password").value,
        mapping: $("#rs-mapping").value,
        options: readOptions("rs"),
      }),
    });
    if (res.jobs.length) toast(`${res.jobs.length} restore job(s) queued.`, "ok");
    if (res.skipped.length) toast(`Skipped:\n${res.skipped.join("\n")}`, "err", 12000);
    if (res.errors.length) toast(`Credential list:\n${res.errors.join("\n")}`, "err", 12000);
    if (res.jobs.length) {
      $("#restore-modal").hidden = true;
      showTab("jobs");
    }
    poll();
  } catch (err) {
    toast(err.message, "err");
  }
});

$$("[data-close]").forEach((btn) => {
  btn.addEventListener("click", () => btn.closest(".modal").hidden = true);
});
$$(".modal").forEach((modal) => {
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.hidden = true; });
});

/* ---------------------------------------------------------------- detail */

async function openDetail(id) {
  const modal = $("#detail-modal");
  $("#detail-title").textContent = "Loading…";
  $("#detail-body").innerHTML = '<p class="note">Scanning the mailbox on disk…</p>';
  modal.hidden = false;
  const data = await api(`/api/accounts/${id}`);
  $("#detail-title").textContent = data.email;
  const folders = data.folders.length
    ? `<div class="tablewrap"><table><thead><tr><th>Folder</th><th class="num">Messages</th><th class="num">Size</th></tr></thead>
       <tbody>${data.folders.map((f) => `<tr><td class="mono">${esc(f.name)}</td>
       <td class="num">${fmtInt(f.messages)}</td><td class="num">${fmtBytes(f.bytes)}</td></tr>`).join("")}</tbody></table></div>`
    : '<p class="note">Nothing downloaded yet.</p>';
  const jobs = data.jobs.length
    ? `<div class="tablewrap"><table><thead><tr><th>Job</th><th>Type</th><th>Status</th><th>Started</th><th class="right">Protocol</th></tr></thead>
       <tbody>${data.jobs.map((j) => `<tr><td class="mono">#${j.id}</td><td>${esc(j.kind)}</td>
       <td>${statusPill(j)}</td><td>${esc(fmtDate(j.started_at || j.created_at))}</td>
       <td class="right">${j.has_log ? `<a class="btn small ghost" href="/api/jobs/${j.id}/log/download" download>Download</a>` : "—"}</td></tr>`).join("")}</tbody></table></div>`
    : "";
  $("#detail-body").innerHTML = `
    <dl class="kv">
      <dt>Source</dt><dd class="mono">${esc(data.source.user)} @ ${esc(data.source.host)}:${data.source.port} (${esc(data.source.security)})</dd>
      <dt>Local mailbox</dt><dd class="mono">${esc(data.local_user)}</dd>
      <dt>Last backup</dt><dd>${esc(fmtDate(data.last_backup_at))}</dd>
      <dt>Stored</dt><dd>${fmtInt(data.mailbox.messages)} messages · ${fmtBytes(data.mailbox.bytes)} · ${fmtInt(data.mailbox.folders)} folders</dd>
    </dl>
    <h3>Folders</h3>${folders}
    ${jobs ? `<h3>Recent jobs</h3>${jobs}` : ""}`;
}

/* ------------------------------------------------------------------- log */

function openLog(jobId) {
  state.log.jobId = jobId;
  state.log.offset = 0;
  $("#log-body").textContent = "";
  $("#log-title").textContent = `imapsync protocol — job #${jobId}`;
  $("#log-download").href = `/api/jobs/${jobId}/log/download`;
  $("#log-drawer").hidden = false;
  clearInterval(state.log.timer);
  state.log.timer = setInterval(tailLog, 1500);
  tailLog();
}

function closeLog() {
  $("#log-drawer").hidden = true;
  clearInterval(state.log.timer);
  state.log.jobId = null;
}

async function tailLog() {
  const { jobId, offset } = state.log;
  if (!jobId) return;
  try {
    const res = await api(`/api/jobs/${jobId}/log?offset=${offset}`);
    if (res.data) {
      const pre = $("#log-body");
      pre.textContent += res.data;
      state.log.offset = res.offset;
      if ($("#log-follow").checked) pre.scrollTop = pre.scrollHeight;
    }
    state.log.status = res.status;
    $("#log-sub").innerHTML = `${statusPill({ status: res.status })} · ${fmtBytes(res.size)} of log`;
    $("#log-cancel").hidden = !(res.status === "running" || res.status === "queued");
    if (["done", "failed", "cancelled"].includes(res.status)) {
      clearInterval(state.log.timer);
      state.log.timer = setInterval(tailLog, 8000);
    }
  } catch (err) {
    console.error(err);
  }
}

$("#log-close").addEventListener("click", closeLog);
$("#log-cancel").addEventListener("click", async () => {
  try {
    await api(`/api/jobs/${state.log.jobId}/cancel`, { method: "POST" });
    toast("Job cancelled.", "ok");
    poll();
  } catch (err) { toast(err.message, "err"); }
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("#log-drawer").hidden) closeLog();
  $$(".modal").forEach((m) => { m.hidden = true; });
});

poll();
