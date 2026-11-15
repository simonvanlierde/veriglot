"use strict";
// Review page: loads findings, collects decisions in memory, saves them through the API.
// All catalog text goes into the page with textContent, never innerHTML.

const token = new URLSearchParams(location.hash.slice(1)).get("token") || "";
const $ = (id) => document.getElementById(id);
// Live regions are written only when their text changes, so screen readers announce changes, not keystrokes.
const say = (id, text) => { if ($(id).textContent !== text) $(id).textContent = text; };
const note = (text, error = false) => { $("message").textContent = text; $("message").classList.toggle("error", error); };
const state = { findings: [], decisions: new Map(), outcomes: new Map(), selected: null, dirty: false };

async function api(path, body) {
  const options = { headers: { "X-Veriglot-Token": token } };
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  if (!response.ok) {
    let reason = response.statusText;
    try { reason = (await response.json()).error || reason; } catch { /* not JSON */ }
    throw new Error(`${response.status} ${reason}`);
  }
  return response;
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  Object.assign(node, props);
  node.append(...children);
  return node;
}

// Text with edge spaces, line breaks, tabs and invisible format characters (bidi controls,
// zero-width) made visible.
function visible(text) {
  if (text === "" || text == null) return el("span", { className: "empty" }, "(empty)");
  const out = document.createDocumentFragment();
  const chars = [...text];
  let lead = 0;
  while (lead < chars.length && /\s/u.test(chars[lead])) lead += 1;
  let trail = 0;
  while (trail < chars.length - lead && /\s/u.test(chars[chars.length - 1 - trail])) trail += 1;
  let plain = "";
  const flush = () => { if (plain) out.append(plain); plain = ""; };
  chars.forEach((ch, i) => {
    const edge = i < lead || i >= chars.length - trail;
    let mark = { " ": edge ? "\u00b7" : "", "\u00a0": "\u2423", "\t": "\u2192", "\n": "\u21b5\n" }[ch];
    if (/\p{Cf}/u.test(ch)) mark = `\u2039U+${ch.codePointAt(0).toString(16).toUpperCase().padStart(4, "0")}\u203a`;
    if (mark) { flush(); out.append(el("span", { className: "ws" }, mark)); } else plain += ch;
  });
  flush();
  return out;
}

const decisionOf = (id) => state.decisions.get(id);
const filtered = () => {
  const [loc, kind, sev] = ["f-locale", "f-kind", "f-severity"].map((id) => $(id).value);
  const q = $("f-text").value.toLowerCase();
  return state.findings.filter((f) =>
    (!loc || f.locale === loc) && (!kind || f.kind === kind) && (!sev || f.severity === sev) &&
    (!q || [f.key, f.source, f.target, f.message].some((t) => t.toLowerCase().includes(q))));
};

function fillSelect(select, values) {
  const current = select.value;
  select.replaceChildren(el("option", { value: "" }, "All"), ...values.map((v) => el("option", { value: v }, v)));
  select.value = values.includes(current) ? current : "";
}

function render() {
  const rows = filtered();
  if (!rows.some((f) => f.id === state.selected)) state.selected = rows[0]?.id ?? null;
  const list = $("list");
  list.replaceChildren(...rows.map((f) => {
    const d = decisionOf(f.id);
    const o = state.outcomes.get(f.id);
    const badge = d ? d.decision : o ? o.status : "";
    const button = el("button", { type: "button", tabIndex: f.id === state.selected ? 0 : -1 },
      el("span", { className: `sev-${f.severity}` }, f.severity),
      el("span", { className: "key" }, `${f.locale} ${f.key} [${f.kind}]`),
      el("span", { className: `badge ${badge}` }, badge));
    if (f.id === state.selected) button.setAttribute("aria-current", "true");
    button.dataset.id = f.id;
    return el("li", {}, button);
  }));
  say("shown", `${rows.length} of ${state.findings.length} findings shown`);
  const decided = [...state.decisions.values()].filter((d) => d.decision).length;
  say("counter", `${decided} decided${state.dirty ? ", unsaved changes" : ""}`);
  renderDetail();
}

function renderDetail() {
  const body = $("detail-body");
  const f = state.findings.find((x) => x.id === state.selected);
  if (!f) { body.replaceChildren(el("p", {}, "No finding selected.")); return; }
  const d = decisionOf(f.id);
  const o = state.outcomes.get(f.id);
  const text = (title, value) => el("div", { className: "text" }, el("h3", {}, title), el("pre", {}, visible(value)));
  const meta = el("dl", {});
  for (const [k, v] of [["Key", f.key], ["Locale", f.locale], ["Kind", f.kind], ["Severity", f.severity],
    ["Problem", f.message], ["Context", f.context || "(none)"]]) meta.append(el("dt", {}, k), el("dd", {}, v));
  const choice = (name, label, key, disabled = false) => {
    const b = el("button", { type: "button", disabled }, `${label} (${key})`);
    b.setAttribute("aria-pressed", String(d?.decision === name));
    b.setAttribute("aria-keyshortcuts", key);
    b.addEventListener("click", () => decide(name));
    return b;
  };
  const clear = el("button", { type: "button", disabled: !d }, "Clear (c)");
  clear.setAttribute("aria-keyshortcuts", "c");
  clear.addEventListener("click", () => decide(""));
  const parts = [meta, text("Source", f.source), text("Translation", f.target), text("Proposed", f.proposed),
    el("div", { className: "buttons", role: "group" }, choice("accept", "Accept", "a", f.proposed == null || f.proposed === ""),
      choice("reject", "Reject", "r"), choice("edit", "Edit", "e"), clear)];
  parts[4].setAttribute("aria-label", "Decision");
  if (d?.decision === "edit") {
    const area = el("textarea", { id: "edited", value: d.edited, spellcheck: false });
    // A new object each time, so a save in flight can tell the row changed after it was sent.
    area.addEventListener("input", () => { state.decisions.set(f.id, { ...state.decisions.get(f.id), edited: area.value }); markDirty(); });
    area.addEventListener("keydown", (e) => { if (e.key === "Escape") list_focus(); });
    parts.push(el("label", { htmlFor: "edited" }, "Edited translation (Escape returns to the list)"), area);
  }
  if (o) parts.push(el("p", { className: `badge ${o.status}` }, `Last result: ${outcomeText(o)}`));
  body.replaceChildren(...parts);
}

const outcomeText = (o) => `${o.status}${o.reason ? `: ${o.reason}` : ""}${o.detail ? ` (${o.detail})` : ""}`;

function markDirty() {
  state.dirty = true;
  say("counter", `${[...state.decisions.values()].filter((d) => d.decision).length} decided, unsaved changes`);
}

function decide(name) {
  const f = state.findings.find((x) => x.id === state.selected);
  if (!f) return;
  if (name === "accept" && !f.proposed) return;
  if (name === "") state.decisions.delete(f.id);
  else {
    const old = state.decisions.get(f.id);
    // The reviewer sees the current text when deciding, so the fingerprint is the current one.
    state.decisions.set(f.id, {
      decision: name, fingerprint: f.fingerprint, note: old?.note ?? "",
      edited: name === "edit" ? old?.edited || f.proposed || f.target : old?.edited ?? "",
    });
  }
  state.outcomes.delete(f.id);
  state.dirty = true;
  render();
  if (name === "edit") $("edited")?.focus();
  else focusButton(f.id);
}

function focusButton(id) {
  [...$("list").querySelectorAll("button")].find((b) => b.dataset.id === id)?.focus();
}
function list_focus() { focusButton(state.selected); }

function move(step) {
  const rows = filtered();
  const i = rows.findIndex((f) => f.id === state.selected);
  const next = rows[Math.min(rows.length - 1, Math.max(0, i + step))];
  if (!next) return;
  state.selected = next.id;
  render();
  focusButton(next.id);
}

function rowsToSend() {
  return [...state.decisions].filter(([, d]) => d.decision).map(([id, d]) => {
    const f = state.findings.find((x) => x.id === id);
    return { id, fingerprint: d.fingerprint, decision: d.decision, proposed: f?.proposed ?? null,
      edited: d.decision === "edit" ? d.edited : null, note: d.note };
  });
}

function showOutcomes(outcomes, prefix) {
  state.outcomes = new Map(outcomes.map((o) => [o.id, o]));
  const counts = { applied: 0, rejected: 0, skipped: 0 };
  outcomes.forEach((o) => { counts[o.status] += 1; });
  note(`${prefix}: ${counts.applied} applied, ${counts.rejected} rejected, ${counts.skipped} skipped.`);
  $("results").replaceChildren(...outcomes.map((o) => el("li", {}, `${o.id}: ${outcomeText(o)}`)));
}

async function save(dryRun) {
  const rows = rowsToSend();
  if (!rows.length) { note("Nothing decided yet."); return; }
  const sent = new Map(state.decisions);
  try {
    const report = await (await api("/api/decisions", { rows, dry_run: dryRun })).json();
    showOutcomes(report.outcomes, dryRun ? "Preview, nothing written" : report.written ? "Saved" : "Nothing written");
    if (dryRun) { render(); return; }
    // Applied and no-longer-needed decisions are done; rejected ones stay for another look.
    // Only if the row was not changed while the save ran.
    for (const o of report.outcomes) {
      if (o.status !== "rejected" && state.decisions.get(o.id) === sent.get(o.id)) state.decisions.delete(o.id);
    }
    state.dirty = state.decisions.size > 0;
    await load();
  } catch (error) { note(`Save failed: ${error.message}`, true); }
}

async function load() {
  const data = await (await api("/api/findings")).json();
  state.findings = data.findings;
  const ids = new Set(data.findings.map((f) => f.id));
  const gone = [...state.decisions.keys()].filter((id) => !ids.has(id));
  gone.forEach((id) => state.decisions.delete(id));
  if (gone.length) state.dirty = state.decisions.size > 0;
  const notes = [];
  if (gone.length) notes.push(`${gone.length} decision${gone.length === 1 ? "" : "s"} dropped: the finding is gone.`);
  data.errors.forEach((e) => notes.push(`${e.locale}: ${e.message}.`));
  if (notes.length) note(`${$("message").textContent} ${notes.join(" ")}`.trim(), $("message").classList.contains("error"));
  fillSelect($("f-locale"), data.locales);
  fillSelect($("f-kind"), [...new Set(data.findings.map((f) => f.kind))].sort());
  fillSelect($("f-severity"), [...new Set(data.findings.map((f) => f.severity))]);
  render();
}

async function exportCsv() {
  const decisions = rowsToSend().map(({ id, fingerprint, decision, edited, note }) => ({ id, fingerprint, decision, edited, note }));
  const csv = await (await api("/api/export", { decisions })).blob();
  const link = el("a", { href: URL.createObjectURL(csv), download: "veriglot-review.csv" });
  link.click();
  URL.revokeObjectURL(link.href);
}

async function importCsv(file) {
  try {
    const { rows } = await (await api("/api/import", { csv: await file.text() })).json();
    let used = 0;
    for (const r of rows.filter((x) => x.decision)) {
      if (!state.findings.some((f) => f.id === r.id)) continue;
      state.decisions.set(r.id, { decision: r.decision.trim().toLowerCase(), fingerprint: r.fingerprint,
        edited: r.edited ?? "", note: r.note });
      used += 1;
    }
    state.dirty = used > 0;
    note(`Imported ${used} decisions from ${rows.length} rows.`);
    render();
  } catch (error) { note(`Import failed: ${error.message}`, true); }
}

document.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
  const action = { j: () => move(1), ArrowDown: () => move(1), k: () => move(-1), ArrowUp: () => move(-1),
    a: () => decide("accept"), r: () => decide("reject"), e: () => decide("edit"), c: () => decide("") }[e.key];
  if (action) { e.preventDefault(); action(); }
});
$("list").addEventListener("click", (e) => {
  const id = e.target.closest("button")?.dataset.id;
  if (id) { state.selected = id; render(); focusButton(id); }
});
$("filters").addEventListener("input", render);
$("filters").addEventListener("submit", (e) => e.preventDefault());
$("save").addEventListener("click", () => save(false));
$("preview").addEventListener("click", () => save(true));
$("export").addEventListener("click", exportCsv);
$("import").addEventListener("change", (e) => { if (e.target.files[0]) importCsv(e.target.files[0]); e.target.value = ""; });
addEventListener("beforeunload", (e) => { if (state.dirty) e.preventDefault(); });
load().catch((error) => { note(`Could not load findings: ${error.message}`, true); });
