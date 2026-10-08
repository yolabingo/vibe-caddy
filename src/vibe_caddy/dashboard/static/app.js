"use strict";

// The server renders the first paint; after that this file keeps the DOM in step with
// GET /_api/routes by patching existing nodes, so an open input or scroll position is
// never disturbed by a refresh.

const $ = (sel, root = document) => root.querySelector(sel);
const liveEl = $("#routes");
const idleEl = $("#routes-idle");
const emptyEl = $("#empty");
const editor = $("#editor");
const form = $("#editor-form");
const drawer = $("#drawer");
const logEl = $("#log");
const toastEl = $("#toast");
const connEl = $("#conn");

let editing = null; // name being edited, or null when creating
let logName = null;
let toastTimer = 0;

// ------------------------------------------------------------------- helpers

async function api(method, path, body) {
  const res = await fetch(`/_api${path}`, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || `request failed (${res.status})`);
    err.hint = data.hint;
    throw err;
  }
  return data;
}

function toast(message) {
  toastEl.textContent = message;
  toastEl.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (toastEl.hidden = true), 5000);
}

function fail(err) {
  toast(err.hint ? `${err.message} (${err.hint})` : err.message);
}

function isImageUrl(value) {
  return /^https?:\/\/\S+$/i.test(value || "");
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// ------------------------------------------------------------------ rendering

function setIcon(li, route) {
  const box = $(".icon", li);
  li.dataset.icon = route.icon || "";
  li.dataset.framework = route.framework || "";
  box.replaceChildren();
  // Same precedence as the server-rendered macro: explicit icon, then the
  // framework logo, then the initial.
  if (isImageUrl(route.icon)) {
    const img = el("img");
    img.referrerPolicy = "no-referrer";
    img.loading = "lazy";
    img.alt = "";
    img.src = route.icon;
    box.append(img);
    return;
  }
  if (!route.icon && route.framework_icon) {
    const logo = el("span", "logo");
    logo.dataset.framework = route.framework;
    box.append(logo);
    return;
  }
  const text = route.icon || route.name.slice(0, 1).toUpperCase();
  box.textContent = text.length <= 2 ? text : text.slice(0, 1);
}

function button(action, label, danger) {
  const b = el("button", danger ? "danger" : "", label);
  b.type = "button";
  b.dataset.action = action;
  b.title = label;
  return b;
}

// Mirrors the markup of templates/_route.html.
function buildItem(route) {
  const li = el("li", "route");
  li.dataset.name = route.name;

  const main = el("div", "main");
  const title = el("div", "title");
  const host = el("a", "host");
  host.target = "_blank";
  host.rel = "noopener";
  title.append(el("span", "name", route.name), host);
  const meta = el("div", "meta");
  const badge = el("span", "badge state");
  meta.append(el("span", "badge type"), badge, el("span", "port"));
  main.append(title, meta);

  const actions = el("div", "actions");
  const lifecycle = el("span", "lifecycle");
  lifecycle.append(button("start", "Start"), button("stop", "Stop"), button("restart", "Restart"));
  actions.append(
    lifecycle,
    button("adopt", "Add command"),
    button("log", "Log"),
    button("edit", "Edit"),
    button("delete", "Delete", true),
  );

  const icon = el("div", "icon");
  icon.setAttribute("aria-hidden", "true");
  li.append(icon, main, actions);
  return li;
}

function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

function patchItem(li, route) {
  li.dataset.state = route.state;
  li.dataset.type = route.type;
  li.dataset.parent = route.parent || "";
  li.classList.toggle("child", Boolean(route.parent));

  if (
    li.dataset.icon !== (route.icon || "") ||
    li.dataset.framework !== (route.framework || "") ||
    !$(".icon", li).firstChild
  )
    setIcon(li, route);

  const host = $(".host", li);
  if (host.getAttribute("href") !== route.href) host.href = route.href;
  setText(host, route.hostname);

  setText($(".badge.type", li), route.type);
  const state = $(".badge.state", li);
  setText(state, route.state);
  if (route.pid) state.title = `pid ${route.pid}`;
  else state.removeAttribute("title");
  setText($(".port", li), route.port ? `:${route.port}` : "");
  const managed = route.type === "managed" || route.type === "worktree";
  $(".lifecycle", li).hidden = !managed;
  const adopt = $('[data-action="adopt"]', li);
  if (adopt) adopt.hidden = route.type !== "static";
  const type = $(".badge.type", li);
  if (managed) type.removeAttribute("title");
  else type.title = "vibe-caddy routes this name but does not run it: there is no command to start";
}

// Routes are split so "what is serving?" is answered by position. A route that
// changes state moves between the two lists on the next poll.
const LIVE_STATES = new Set(["ready", "up", "starting"]);
const isLive = (route) => LIVE_STATES.has(route.state);

function reconcileList(listEl, routes) {
  const existing = new Map([...listEl.children].map((li) => [li.dataset.name, li]));

  routes.forEach((route, i) => {
    // An element may be arriving from the other list, so look there too rather
    // than rebuilding it and losing its state.
    const li = existing.get(route.name) || document.querySelector(`li[data-name="${CSS.escape(route.name)}"]`) || buildItem(route);
    patchItem(li, route);
    // Only touch position when it is wrong: moving a node drops focus inside it.
    if (listEl.children[i] !== li) listEl.insertBefore(li, listEl.children[i] || null);
  });
}

function reconcile(routes) {
  const wanted = new Set(routes.map((r) => r.name));
  for (const li of document.querySelectorAll("li.route")) {
    if (!wanted.has(li.dataset.name)) li.remove();
  }

  const live = routes.filter(isLive);
  const idle = routes.filter((r) => !isLive(r));
  reconcileList(liveEl, live);
  reconcileList(idleEl, idle);

  $("#group-live").hidden = live.length === 0;
  $("#group-idle").hidden = idle.length === 0;
  setText($("#group-live .count"), String(live.length));
  setText($("#group-idle .count"), String(idle.length));
  emptyEl.hidden = routes.length > 0;
}

let routeCache = new Map();

// A single failed poll is routine -- the daemon restarting, the laptop waking --
// so the banner waits for a second one rather than flickering on every blip.
const FAILURES_BEFORE_STALE = 2;
let failures = 0;
let lastSeen = null;

function setConnected(ok) {
  if (ok) {
    delete document.body.dataset.stale;
    connEl.hidden = true;
    return;
  }
  // Everything on screen is now a snapshot of unknown age. Say so, and let the
  // CSS drain the colour out of the state dots so a stale "ready" stops
  // reading as live.
  document.body.dataset.stale = "";
  const seen = lastSeen
    ? `last seen ${lastSeen.toLocaleTimeString()}`
    : "never reached the server";
  setText(connEl, `Disconnected \u00b7 ${seen}`);
  connEl.hidden = false;
}

async function refresh() {
  try {
    const routes = await api("GET", "/routes");
    routeCache = new Map(routes.map((r) => [r.name, r]));
    reconcile(routes);
    failures = 0;
    lastSeen = new Date();
    setConnected(true);
  } catch {
    failures += 1;
    if (failures >= FAILURES_BEFORE_STALE) setConnected(false);
  }
}

// -------------------------------------------------------------------- actions

async function lifecycle(name, verb) {
  try {
    await api("POST", `/routes/${encodeURIComponent(name)}/${verb}`);
  } catch (err) {
    fail(err);
  }
  refresh();
}

async function loadLog() {
  if (!logName) return;
  try {
    const { log } = await api("GET", `/routes/${encodeURIComponent(logName)}/log?lines=300`);
    logEl.textContent = log || "(no log output yet)";
    logEl.scrollTop = logEl.scrollHeight;
  } catch (err) {
    logEl.textContent = err.message;
  }
}

function openLog(name) {
  logName = name;
  $("#drawer-title").textContent = `Log: ${name}`;
  logEl.textContent = "Loading…";
  // Re-open rather than trusting `open`: a dialog shown non-modally stays
  // non-modal, and would lay out in the document instead of over it.
  if (!drawer.matches(":modal")) {
    drawer.close();
    drawer.showModal();
  }
  loadLog();
}

function openEditor(name) {
  editing = name;
  const route = name ? routeCache.get(name) : null;
  form.reset();
  $("#editor-title").textContent = name ? `Edit ${name}` : "New route";
  $("#editor-error").hidden = true;
  const nameInput = form.elements.name;
  nameInput.readOnly = Boolean(name);
  if (route) {
    for (const key of ["name", "cmd", "dir", "port", "icon", "url"]) {
      form.elements[key].value = route[key] ?? "";
    }
    for (const key of ["proxy", "insecure_skip_verify", "autostart"]) {
      form.elements[key].checked = Boolean(route[key]);
    }
  }
  editor.showModal();
  (name ? form.elements.cmd : nameInput).focus();
}

function formBody() {
  const f = form.elements;
  const text = (key) => f[key].value.trim();
  const body = {
    proxy: f.proxy.checked,
    insecure_skip_verify: f.insecure_skip_verify.checked,
    autostart: f.autostart.checked,
  };
  // Creating omits blanks (server picks defaults); editing sends null to clear a field.
  for (const key of ["cmd", "dir", "icon", "url"]) {
    if (text(key)) body[key] = text(key);
    else if (editing) body[key] = null;
  }
  if (text("port")) body.port = Number(text("port"));
  if (!editing) body.name = text("name");
  return body;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const errorEl = $("#editor-error");
  try {
    if (editing) await api("PUT", `/routes/${encodeURIComponent(editing)}`, formBody());
    else await api("POST", "/routes", formBody());
    editor.close();
    refresh();
  } catch (err) {
    errorEl.textContent = err.hint ? `${err.message} (${err.hint})` : err.message;
    errorEl.hidden = false;
  }
});

// ------------------------------------------------------------------ wiring

// Delegated from the shared ancestor: routes live in two lists and move between
// them, so binding per list would miss whichever one a route is currently in.
$("main").addEventListener("click", async (event) => {
  const li = event.target.closest(".route");
  if (!li) return;
  const name = li.dataset.name;
  const action = event.target.closest("button")?.dataset.action;

  if (event.target.closest("a")) return; // let the link navigate
  if (!action) return openLog(name); // click anywhere else on the row

  if (action === "start" || action === "stop" || action === "restart") lifecycle(name, action);
  else if (action === "log") openLog(name);
  else if (action === "edit" || action === "adopt") openEditor(name);  // openEditor already focuses the command field for an existing route
  else if (action === "delete" && confirm(`Delete route "${name}"?`)) {
    try {
      await api("DELETE", `/routes/${encodeURIComponent(name)}`);
    } catch (err) {
      fail(err);
    }
    refresh();
  }
});

for (const btn of document.querySelectorAll(".toggle button")) {
  btn.addEventListener("click", async () => {
    const view = btn.dataset.view;
    for (const ul of document.querySelectorAll(".routes")) ul.dataset.view = view;
    for (const other of document.querySelectorAll(".toggle button")) {
      other.setAttribute("aria-pressed", String(other === btn));
    }
    try {
      await api("PUT", "/preferences", { view });
    } catch (err) {
      fail(err);
    }
  });
}

$("#new-route").addEventListener("click", () => openEditor(null));
$("#log-refresh").addEventListener("click", loadLog);
for (const dlg of [editor, drawer]) {
  $("[data-close]", dlg).addEventListener("click", () => dlg.close());
}
drawer.addEventListener("close", () => (logName = null));

// Seed the cache for the edit dialog, then keep polling. Skipped while the tab is
// hidden so a background tab does not shell out to launchctl every 3 seconds.
refresh();
setInterval(() => {
  if (!document.hidden) refresh();
}, 3000);

// Waiting out the interval after the tab comes back, or after the network
// returns, would leave a stale page on screen for no reason.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});
window.addEventListener("online", refresh);
window.addEventListener("offline", () => {
  failures = FAILURES_BEFORE_STALE;
  setConnected(false);
});
