#!/usr/bin/env node
/**
 * Tiny ADB bind API for the scrcpy-web container (internal :8001).
 * Keeps Watch's adb in sync when the gateway rebinds a phone seat.
 */
const http = require("http");
const { execFile } = require("child_process");

const PORT = Number(process.env.SCRCPY_ADB_API_PORT || 8001);

function runAdb(args, timeoutMs = 20000) {
  return new Promise((resolve, reject) => {
    execFile("adb", args, { timeout: timeoutMs, encoding: "utf8" }, (err, stdout, stderr) => {
      const out = `${stdout || ""}${stderr || ""}`.trim();
      if (err && !out.toLowerCase().includes("connected")) {
        reject(new Error(out || err.message));
        return;
      }
      resolve(out);
    });
  });
}

function parseDevices(text) {
  const lines = text.split(/\r?\n/).slice(1);
  const devices = [];
  for (const line of lines) {
    const t = line.trim();
    if (!t) continue;
    const parts = t.split(/\s+/);
    if (parts.length < 2) continue;
    devices.push({ serial: parts[0], state: parts[1] });
  }
  return devices;
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      if (!chunks.length) {
        resolve({});
        return;
      }
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString("utf8")));
      } catch (e) {
        reject(e);
      }
    });
    req.on("error", reject);
  });
}

function send(res, code, body) {
  const data = JSON.stringify(body);
  res.writeHead(code, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(data),
  });
  res.end(data);
}

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);
    if (req.method === "GET" && url.pathname === "/health") {
      send(res, 200, { ok: true, service: "scrcpy-adb-api" });
      return;
    }
    if (req.method === "GET" && (url.pathname === "/devices" || url.pathname === "/device")) {
      const raw = await runAdb(["devices", "-l"], 10000);
      send(res, 200, { ok: true, devices: parseDevices(raw) });
      return;
    }
    if (req.method === "POST" && url.pathname === "/device/bind") {
      const body = await readJson(req);
      const serial = (body.serial || "").trim() || null;
      const connect = (body.connect || "").trim() || null;
      if (!serial && !connect) {
        send(res, 400, { ok: false, error: "serial and/or connect required" });
        return;
      }
      let connectResult = null;
      if (connect) {
        connectResult = await runAdb(["connect", connect]);
      }
      const raw = await runAdb(["devices", "-l"], 10000);
      const devices = parseDevices(raw);
      const want = serial || connect;
      const selected = devices.find((d) => d.serial === want) || null;
      send(res, 200, {
        ok: !!(selected && selected.state === "device"),
        bound_serial: want,
        connect,
        connect_result: connectResult,
        selected,
        devices,
      });
      return;
    }
    send(res, 404, { ok: false, error: "not found" });
  } catch (e) {
    send(res, 503, { ok: false, error: String(e && e.message ? e.message : e) });
  }
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(`scrcpy-adb-api listening on :${PORT}`);
});
