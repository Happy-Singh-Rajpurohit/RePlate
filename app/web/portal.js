const $ = (selector) => document.querySelector(selector);
const formatNumber = new Intl.NumberFormat(undefined, {maximumFractionDigits: 2});
const formatDate = (seconds) => new Date(seconds * 1000).toLocaleString();
const redirect = location.origin + "/";
const seen = new Set();
let alertReady = false;
let config;
let me;
let view = "available";
let refreshVersion = 0;

function status(message, error = false) {
  $("#status").textContent = message;
  $("#status").classList.toggle("error", error);
}

async function api(path, method = "GET", body) {
  const headers = {};
  if (config?.demo) headers["X-RePlate-Demo-Role"] = $("#demo-role select").value;
  const token = sessionStorage.getItem("idToken");
  if (token) headers.Authorization = "Bearer " + token;
  if (body) headers["Content-Type"] = "application/json";
  const response = await fetch("/api/" + path, {
    method, headers, body: body && JSON.stringify(body),
  });
  const data = await response.json();
  if (response.status === 401 && token) {
    sessionStorage.removeItem("idToken");
    $("#workspace").hidden = true;
    $("#signout").hidden = true;
    $("#signin").hidden = false;
    $("#role").textContent = "";
    throw new Error("Your session expired. Sign in again to continue.");
  }
  if (!response.ok) throw new Error(data.error || "Unable to complete the request.");
  return data;
}

function secret() {
  return [...crypto.getRandomValues(new Uint8Array(32))]
    .map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function signIn() {
  const verifier = secret();
  const state = secret();
  const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  const challenge = btoa(String.fromCharCode(...new Uint8Array(hash)))
    .replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
  sessionStorage.setItem("oauth", JSON.stringify({verifier, state}));
  const params = new URLSearchParams({
    response_type: "code", client_id: config.client, redirect_uri: redirect,
    scope: "openid", state, code_challenge_method: "S256", code_challenge: challenge,
  });
  location.assign(config.domain + "/oauth2/authorize?" + params);
}

async function callback() {
  const params = new URLSearchParams(location.search);
  if (!params.has("code") && !params.has("error")) return;
  history.replaceState(null, "", "/");
  const pending = JSON.parse(sessionStorage.getItem("oauth") || "null");
  sessionStorage.removeItem("oauth");
  if (!pending || pending.state !== params.get("state") || !params.has("code")) {
    throw new Error("Sign-in could not be verified. Start a new sign-in.");
  }
  const response = await fetch(config.domain + "/oauth2/token", {
    method: "POST",
    body: new URLSearchParams({grant_type: "authorization_code", client_id: config.client,
      code: params.get("code"), redirect_uri: redirect, code_verifier: pending.verifier}),
  });
  if (!response.ok) throw new Error("Sign-in expired. Start a new sign-in.");
  sessionStorage.setItem("idToken", (await response.json()).id_token);
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function listingCard(listing) {
  const card = element("article", "", "offer");
  const head = element("div", "", "section-head");
  head.append(element("h3", listing.title), element("span", listing.status.toLowerCase(), "badge"));
  card.append(head, element("p", `${formatNumber.format(listing.kg)} kg · ${listing.route === "food" ? "Ready-to-eat food" : "Organic biomass"}`, "meta"),
    element("p", `Prepared or gathered ${formatDate(listing.preparedAt)} · Pickup by ${formatDate(listing.expiresAt)}`, "meta"));
  if (listing.pickup) card.append(element("p", listing.pickup));
  const action = listing.status === "OPEN" && !listing.owner ? "claim"
    : listing.status === "CLAIMED" && listing.claimant ? "collect" : null;
  if (action) {
    const button = element("button", action === "claim" ? "Claim offer" : "Confirm collection");
    card.append(button);
    button.onclick = () => busy(button, async () => {
      await api(`listings/${listing.id}/${action}`, "POST");
      if (action === "claim") setView("activity");
      await refresh();
      status(action === "claim" ? "Offer claimed. The pickup address is in My pickups." : "Collection recorded.");
    });
  }
  return card;
}

async function refresh() {
  const version = ++refreshVersion;
  const [listings, impact, alerts] = await Promise.all([
    api("listings?view=" + view), api("impact"), api("alerts"),
  ]);
  if (version !== refreshVersion) return;
  $("#listings").replaceChildren(...(listings.length ? listings.map(listingCard)
    : [element("p", "No offers to show here.", "empty")]));
  for (const [key, value] of Object.entries(impact)) {
    $("#" + key).textContent = formatNumber.format(value);
  }
  $("#alerts").replaceChildren(...(alerts.length ? alerts.map((alert) => element("li", alert.message))
    : [element("li", "No alerts yet.")]));
  for (const alert of alerts) {
    const key = alert.listingId + alert.message;
    if (alertReady && !seen.has(key) && window.Notification?.permission === "granted") {
      try { new Notification("RePlate", {body: alert.message}); } catch { /* In-app alert is still visible. */ }
    }
    seen.add(key);
  }
  alertReady = true;
}

function setView(next) {
  view = next;
  for (const button of document.querySelectorAll("[data-view]")) {
    button.setAttribute("aria-pressed", String(button.dataset.view === view));
  }
}

async function busy(button, action) {
  button.disabled = true;
  status("");
  try { await action(); } catch (error) { status(error.message, true); }
  finally { button.disabled = false; }
}

function localDate(date) {
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

function offerDefaults() {
  const form = $("#offer");
  form.elements.preparedAt.value = localDate(new Date());
  form.elements.expiresAt.value = localDate(new Date(Date.now() + 7200000));
}

$("#signin").onclick = () => busy($("#signin"), signIn);
$("#signout").onclick = () => {
  sessionStorage.removeItem("idToken");
  location.assign(config.domain + "/logout?" + new URLSearchParams({
    client_id: config.client, logout_uri: redirect,
  }));
};
$("#refresh").onclick = () => busy($("#refresh"), refresh);
$("#desktop").onclick = () => busy($("#desktop"), async () => {
  const permission = await Notification.requestPermission();
  status(permission === "granted" ? "Desktop alerts enabled while RePlate is open." : "Desktop alerts were not enabled.");
});
for (const button of document.querySelectorAll("[data-view]")) {
  button.onclick = () => busy(button, async () => { setView(button.dataset.view); await refresh(); });
}
for (const button of document.querySelectorAll(".geo")) {
  button.onclick = () => busy(button, async () => {
    if (!navigator.geolocation) throw new Error("Location access is unavailable.");
    const point = await new Promise((resolve, reject) => navigator.geolocation.getCurrentPosition(
      resolve, (error) => reject(new Error(error.message)), {timeout: 10000},
    ));
    button.form.elements.lat.value = point.coords.latitude.toFixed(4);
    button.form.elements.lon.value = point.coords.longitude.toFixed(4);
  });
}
$("#offer").onsubmit = (event) => {
  event.preventDefault();
  busy(event.submitter, async () => {
    const data = Object.fromEntries(new FormData(event.target));
    for (const field of ["preparedAt", "expiresAt"]) data[field] = new Date(data[field]).toISOString();
    await api("listings", "POST", data);
    event.target.reset();
    offerDefaults();
    await refresh();
    status("Offer published.");
  });
};
$("#location").onsubmit = (event) => {
  event.preventDefault();
  busy(event.submitter, async () => {
    const data = Object.fromEntries(new FormData(event.target));
    data.sms = event.target.elements.sms.checked;
    await api("me/location", "PUT", data);
    status("Matching location saved.");
  });
};

async function showWorkspace() {
  setView("available");
  alertReady = false;
  seen.clear();
  me = await api("me");
  const donor = me.role === "donor";
  $("#role").textContent = config.demo ? "" :
    {donor: "Donor", recipient: "NGO / volunteer", processor: "Processor"}[me.role];
  $("#board-title").textContent = donor ? "My offers" : "Surplus offers";
  $("#donor-panel").hidden = !donor;
  $("#partner-panel").hidden = donor;
  $("#tabs").hidden = donor;
  $("#desktop").hidden = !("Notification" in window);
  $("#workspace").hidden = false;
  if (donor) offerDefaults();
  else {
    $("#sms-option").hidden = !me.smsAvailable;
    $("#location").elements.sms.checked = me.sms;
    $("#location").elements.lat.value = me.lat ?? "";
    $("#location").elements.lon.value = me.lon ?? "";
  }
  await refresh();
}

async function init() {
  config = await api("config");
  if (config.demo) {
    $("#demo-role").hidden = false;
    $("#demo-role select").onchange = async () => {
      status("");
      try { await showWorkspace(); } catch (error) { status(error.message, true); }
    };
    await showWorkspace();
    status("Local demo · Sample data resets when the server restarts.");
    return;
  }
  $("#signin").hidden = false;
  if (!config.domain || !config.client) {
    $("#signin").hidden = true;
    status("Connect the portal to an AWS deployment to sign in.");
    return;
  }
  await callback();
  if (!sessionStorage.getItem("idToken")) return;
  $("#signin").hidden = true;
  $("#signout").hidden = false;
  await showWorkspace();
  setInterval(() => {
    if (!document.hidden && sessionStorage.getItem("idToken")) refresh().catch((error) => status(error.message, true));
  }, 20000);
}

init().catch((error) => status(error.message, true));
