"use strict";

const $ = id => document.getElementById(id);
const fields = new Map();
const resets = new Set();
const loaded = new Set();
let snapshot;
let preview = false;
let busy = false;
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const ONNX_SSD = "/app/data/ssd_mobilenet_v1_10.onnx";

function notice(message, kind = "info") {
  $("notice").textContent = message;
  $("notice").dataset.kind = kind;
  $("notice").hidden = false;
}

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

// GET returns the data; a change returns 202 with a job, which is polled until it ends.
async function api(path, { json, ...init } = {}) {
  if (json !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(json);
  }
  let response;
  try {
    response = await fetch(path, { cache: "no-store", ...init });
  } catch (error) {
    throw new Error("Lost the connection to the device (" + error.message + "). If you changed its network, open this page at its new address.");
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Request failed: " + response.status);
  if (response.status !== 202) return data;
  for (;;) {
    await pause(1000);
    const job = await api("/api/jobs/" + data.job);
    if (job.state === "failed") throw new Error(job.error);
    if (job.state === "done") return job.result;
  }
}

function changes() {
  const result = {};
  for (const [key, input] of fields) {
    if (resets.has(key)) result[key] = null;
    else if (input.value !== snapshot.values[key]) result[key] = input.value;
  }
  return result;
}

function controls() {
  const count = snapshot ? Object.keys(changes()).length : 0;
  document.querySelectorAll("[data-write]").forEach(element => { element.disabled = preview || busy; });
  $("apply").disabled = preview || busy || count === 0;
  $("reload").disabled = busy || !snapshot;
  $("pending-count").textContent = count ? count + (count === 1 ? " change" : " changes") + " not applied" : "";
}

async function action(message, work) {
  if (busy) return;
  busy = true;
  controls();
  notice(message);
  try {
    const result = await work();
    notice(result?.message || "Done.");
  } catch (error) {
    notice(error.message, "error");
  } finally {
    busy = false;
    controls();
  }
}

function button(text, handler, write = true) {
  const element = node("button", text);
  element.type = "button";
  if (write) element.dataset.write = "";
  element.addEventListener("click", handler);
  return element;
}

function labelFor(key) {
  return key.replace(/^(EYE_|MOUTH_|EAR_|VOICE_|MQTT_|PROMETHEUS_|HDMI_)/, "")
    .toLowerCase().replaceAll("_", " ").replace(/^\w/, letter => letter.toUpperCase());
}

function modelOptions(list) {
  list.replaceChildren();
  for (const model of snapshot.models) {
    if (!model.value) continue;
    const option = node("option");
    option.value = model.value;
    option.label = model.label;
    list.append(option);
  }
}

// One card per section of config.example/percept.env, so every setting the kit has shows up here.
function renderFields() {
  fields.clear();
  resets.clear();
  document.querySelectorAll("[data-settings-section]").forEach(container => {
    container.replaceChildren();
    const specs = snapshot.schema.filter(field => field.section === container.dataset.settingsSection);
    if (!specs.length) return;
    const card = node("div", undefined, "card");
    const title = container.dataset.settingsSection.replace(/^./, c => c.toUpperCase());
    if (title !== container.closest(".tab").querySelector("h2").textContent) card.append(node("h3", title));
    if (container.dataset.settingsSection === "building the images") {
      card.append(node("p", "Used the next time an image is built, not by the running containers.", "muted"));
    }
    for (const spec of specs) {
      const wrapper = node("div", undefined, "field");
      const heading = node("div", undefined, "field-heading");
      const label = node("label", labelFor(spec.key));
      label.htmlFor = spec.key;
      label.append(node("span", spec.key, "field-key"));
      let input;
      if (spec.choices) {
        input = node("select");
        for (const value of spec.choices) {
          const option = node("option", value === "" ? "Automatic" : value);
          option.value = value;
          input.append(option);
        }
      } else {
        input = node("input");
        input.type = spec.kind ? "number" : "text";
        if (spec.kind) {
          input.min = spec.minimum;
          input.max = spec.maximum;
          input.step = spec.kind === "number" ? "any" : "1";
        }
      }
      input.id = spec.key;
      input.value = snapshot.values[spec.key];
      input.autocomplete = "off";
      input.addEventListener("input", () => { resets.delete(spec.key); controls(); });
      if (spec.key === "EYE_MODEL") {
        const list = node("datalist");
        list.id = "model-options";
        modelOptions(list);
        input.setAttribute("list", list.id);
        input.placeholder = "Default SSD MobileNet v2 (COCO)";
        input.addEventListener("change", () => {
          if (input.value === ONNX_SSD) { fields.get("EYE_PARSER").value = "onnxssd"; controls(); }
        });
        wrapper.append(list);
      }
      heading.append(label, button("Default", () => {
        input.value = spec.default;
        resets.add(spec.key);
        controls();
      }, false));
      wrapper.prepend(heading);
      wrapper.append(input, node("small", spec.description), node("small", "Default: " + (spec.default || "empty")));
      card.append(wrapper);
      fields.set(spec.key, input);
    }
    container.append(card);
  });
}

function table(container, headers, rows) {
  if (!rows.length) {
    container.replaceChildren(node("p", "None.", "muted"));
    return;
  }
  const element = node("table");
  const head = node("tr");
  headers.forEach(text => head.append(node("th", text)));
  const thead = node("thead");
  thead.append(head);
  const body = node("tbody");
  for (const cells of rows) {
    const row = node("tr");
    for (const value of cells) {
      const cell = node("td");
      cell.append(value instanceof Node ? value : node("span", String(value)));
      row.append(cell);
    }
    body.append(row);
  }
  element.append(thead, body);
  container.replaceChildren(element);
  controls();
}

function networkInputs() {
  $("net-static").hidden = $("net-mode").value !== "static";
  $("net-wifi").hidden = $("net-interface").value !== "wlan0";
}

async function overview() {
  const data = await api("/api/overview");
  preview = data.preview;
  $("device-summary").textContent = data.hostname + " · " + (data.disk_free / 1024 ** 3).toFixed(1) + " GB free";
  $("hostname").placeholder = data.hostname;
  $("sync-state").textContent = data.web_changes
    ? "Changed here since the last deploy. Before you deploy from your PC, run './percept-kit.sh settings pull' there."
    : "";
  const display = data.display;
  let state = display.error;
  if (!state) {
    state = "Monitor " + (display.connected ? "connected" : "not connected") + " · showing the " + display.mode;
    if (display.mode === "camera") {
      const camera = display.camera;
      const running = display.player === "active" || display.player === "activating";
      state += camera && (running || camera.state === "failed")
        ? " · " + camera.state + (camera.detail ? ": " + camera.detail : "")
        : " · player " + display.player;
    }
  }
  $("display-state").textContent = state;
  $("display-state").dataset.kind = display.camera?.state === "failed" ? "error" : "";
  const list = $("interface-list");
  list.replaceChildren();
  if (data.interfaces.error) {
    list.append(node("p", data.interfaces.error, "muted"));
  } else {
    const selected = $("net-interface").value;
    $("net-interface").replaceChildren();
    for (const item of data.interfaces) {
      list.append(node("p", item.name + " (" + item.state.toLowerCase() + "): " + (item.addresses.join(", ") || "no address"), "muted"));
      const option = node("option", item.name);
      option.value = item.name;
      $("net-interface").append(option);
    }
    if ([...$("net-interface").options].some(option => option.value === selected)) $("net-interface").value = selected;
  }
  networkInputs();
  controls();
}

async function loadConfiguration() {
  snapshot = await api("/api/config");
  renderFields();
  await Promise.all([overview(), loadFiles()]);
}

async function loadFiles() {
  const files = await api("/api/files");
  table($("file-list"), ["File", "Size", ""], files.map(file => [
    file.name, (file.bytes / 1048576).toFixed(1) + " MB",
    button("Remove", () => {
      if (!confirm("Remove " + file.name + " from the device?")) return;
      action("Removing " + file.name, async () => {
        const result = await api("/api/files/delete", { method: "POST", json: { name: file.name } });
        await loadFiles();
        return result;
      });
    }),
  ]));
}

async function bluetooth() {
  const data = await api("/api/bluetooth");
  $("audio-status").textContent = "Sound plays on: " + (data.audio.name || data.audio.sink) +
    (data.audio.bt_admin ? "" : " · pairing is turned off (Bt admin = 0 below)");
  const yes = value => value === true || value === "yes";
  table($("bt-devices"), ["Device", "State", ""], data.devices.filter(d => yes(d.audio) || yes(d.paired)).map(device => {
    const actions = node("div");
    const operations = yes(device.connected) ? ["disconnect"] : ["connect"];
    if (yes(device.paired)) operations.push("forget");
    for (const operation of operations) {
      actions.append(button(operation[0].toUpperCase() + operation.slice(1), () =>
        action(operation === "connect" ? "Connecting to " + device.name + " (up to a minute)" : operation + ": " + device.name, async () => {
          await api("/api/bluetooth", { method: "POST", json: { action: operation, mac: device.mac } });
          await bluetooth();
          return { message: device.name + ": " + operation + " done." };
        })));
    }
    return [device.name + " · " + device.mac,
      yes(device.connected) ? "Connected" : yes(device.paired) ? "Paired" : "Found", actions];
  }));
}

async function services() {
  const data = await api("/api/services");
  table($("service-list"), ["Container", "State", ""], data.map(service => {
    const actions = node("div");
    for (const operation of ["start", "stop", "restart"]) {
      actions.append(button(operation[0].toUpperCase() + operation.slice(1), () =>
        action(operation + ": " + service.service, async () => {
          const result = await api("/api/services", { method: "POST", json: { action: operation, service: service.service } });
          await services();
          return result;
        })));
    }
    actions.append(button("Logs", () => action("Reading the log of " + service.service, async () => {
      $("service-logs").textContent = (await api("/api/logs/" + encodeURIComponent(service.service))).text;
      $("service-logs").hidden = false;
      return { message: "The last 80 lines of " + service.service + "'s log are below the table." };
    }), false));
    return [service.service, service.status + (service.health ? " · " + service.health : ""), actions];
  }));
}

// Read-only lists load when their tab opens; failures show in place.
function loadList(name, work, container) {
  if (loaded.has(name)) return;
  loaded.add(name);
  work().catch(error => {
    loaded.delete(name);
    container.replaceChildren(node("p", error.message, "muted"));
  });
}

function selectTab(name) {
  const tab = [...document.querySelectorAll("[data-tab]")].find(item => item.dataset.tab === name) ||
    document.querySelector("[data-tab]");
  document.querySelectorAll(".tab").forEach(section => { section.hidden = section.id !== tab.dataset.tab; });
  document.querySelectorAll("[data-tab]").forEach(item => item.classList.toggle("selected", item === tab));
  if (tab.dataset.tab === "audio") loadList("audio", bluetooth, $("bt-devices"));
  if (tab.dataset.tab === "system") loadList("system", services, $("service-list"));
}

document.querySelectorAll("[data-tab]").forEach(tab => tab.addEventListener("click", () => { location.hash = tab.dataset.tab; }));
window.addEventListener("hashchange", () => selectTab(location.hash.slice(1)));

$("home-link").href = location.protocol + "//" + location.hostname + "/";
$("reload").addEventListener("click", () => {
  if (Object.keys(changes()).length && !confirm("Discard the changes you haven't applied?")) return;
  action("Reloading", async () => { await loadConfiguration(); return { message: "Reloaded." }; });
});
$("apply").addEventListener("click", () => {
  const edited = changes();
  action("Applying " + Object.keys(edited).length + " change(s): the containers they belong to restart", async () => {
    const result = await api("/api/config", { method: "POST", json: { revision: snapshot.revision, changes: edited } });
    await loadConfiguration();
    const eye = Object.keys(edited).some(key => key.startsWith("EYE_"));
    return { message: result.message + (eye ? " The Eye takes up to a minute to start with them." : "") };
  });
});
$("bt-refresh").addEventListener("click", () => action("Reading the Bluetooth devices", async () => {
  await bluetooth();
  return { message: "Bluetooth devices refreshed." };
}));
$("bt-scan").addEventListener("click", () => action("Scanning for 12 seconds", async () => {
  await api("/api/bluetooth", { method: "POST", json: { action: "scan" } });
  await bluetooth();
  return { message: "Scan finished." };
}));
$("services-refresh").addEventListener("click", () => action("Reading the containers", async () => {
  await services();
  return { message: "Containers refreshed." };
}));
$("upload-form").addEventListener("submit", event => {
  event.preventDefault();
  action("Uploading", async () => {
    for (const file of $("uploads").files) {
      await api("/api/files/" + encodeURIComponent(file.name), { method: "PUT", body: file });
    }
    $("uploads").value = "";
    snapshot.models = (await api("/api/config")).models;
    const list = $("model-options");
    if (list) modelOptions(list);
    await Promise.all([loadFiles(), overview()]);
    return { message: "Uploaded. Pick the file in the setting that uses it." };
  });
});
$("hostname-form").addEventListener("submit", event => {
  event.preventDefault();
  action("Renaming the device", async () => {
    const result = await api("/api/hostname", { method: "POST", json: { hostname: $("hostname").value } });
    $("hostname").value = "";
    await overview();
    return result;
  });
});
$("net-interface").addEventListener("change", networkInputs);
$("net-mode").addEventListener("change", networkInputs);
$("network-form").addEventListener("submit", event => {
  event.preventDefault();
  if (!confirm("Change the network settings of " + $("net-interface").value + "? This page and SSH may lose the device for a minute.")) return;
  action("Changing the network settings: up to a minute", async () => {
    const result = await api("/api/network", { method: "POST", json: {
      interface: $("net-interface").value, mode: $("net-mode").value, address: $("net-address").value,
      gateway: $("net-gateway").value, dns: $("net-dns").value, ssid: $("net-ssid").value,
      password: $("net-password").value,
    } });
    $("net-password").value = "";
    await overview();
    return result;
  });
});
for (const [id, power, question] of [
  ["reboot", "reboot", "Reboot the device? The demo is back in about two minutes."],
  ["shutdown", "poweroff", "Shut the device down? It stays off until you unplug and replug its power."],
]) {
  $(id).addEventListener("click", () => {
    if (confirm(question)) action("Sending " + power, () => api("/api/power", { method: "POST", json: { action: power } }));
  });
}
window.addEventListener("beforeunload", event => {
  if (snapshot && Object.keys(changes()).length) event.preventDefault();
});
setInterval(() => {
  if (snapshot && !busy) overview().catch(error => notice(error.message, "error"));
}, 15000);

selectTab(location.hash.slice(1));
action("Loading", async () => {
  await loadConfiguration();
  return { message: preview ? "Preview with the example settings: changes are turned off." : "Ready." };
});
