/**
 * Hermes Zalo Personal — Node.js Sidecar
 *
 * Wrap zca-js để Python adapter (Hermes gateway) gọi qua HTTP + WebSocket.
 *
 * Endpoints:
 *   GET  /health                — { status, uid, name }
 *   POST /login/qr              — start QR login flow
 *   GET  /qr.png                — QR PNG image (chỉ khi pending)
 *   POST /send/text             — { thread_id, thread_type, text, quote_id? }
 *   POST /send/image            — { thread_id, thread_type, file_path, caption? }
 *   POST /send/file             — { thread_id, thread_type, file_path }
 *   GET  /media/:cache_key      — download media đã cache từ tin nhận
 *   POST /logout                — logout, xoá session
 *   WS   /events                — push incoming messages + login state
 *
 * Env vars:
 *   ZALO_PERSONAL_PROXY         — proxy URL (http://user:pass@host:port hoặc socks5://...)
 *   ZALO_PERSONAL_SIDECAR_PORT  — port HTTP/WS (default 3838)
 *   ZALO_PERSONAL_SESSION_DIR   — folder lưu session.json (default ~/.hermes/zalo)
 */

import express from "express";
import { WebSocketServer } from "ws";
import { createServer } from "node:http";
import { promises as fs } from "node:fs";
import path from "node:path";
import { Buffer } from "node:buffer";
import { randomUUID } from "node:crypto";
import { Zalo, LoginQRCallbackEventType, ThreadType, Reactions } from "zca-js";
import { HttpsProxyAgent } from "https-proxy-agent";
import { ProxyAgent, fetch as undiciFetch } from "undici";

// ─── Config ──────────────────────────────────────────────────────────────

const PORT = parseInt(process.env.ZALO_PERSONAL_SIDECAR_PORT || "3838", 10);
const SESSION_DIR = process.env.ZALO_PERSONAL_SESSION_DIR || path.join(process.env.HOME || "/opt/data", "zalo");
const SESSION_FILE = path.join(SESSION_DIR, "session.json");
const QR_FILE = path.join(SESSION_DIR, "qr.png");
const MEDIA_CACHE_DIR = path.join(SESSION_DIR, "media-cache");
const PROXY = process.env.ZALO_PERSONAL_PROXY || "";

// UA pool — VN focused (Win Chrome + Win Edge + Mac Chrome), KHÔNG có Linux/Firefox
const UA_POOL = [
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
];

// ─── State ───────────────────────────────────────────────────────────────

let state = {
  status: "disconnected", // pending | scanned | connected | disconnected | error
  uid: null,
  name: null,
  phone: null,
  api: null,
  error: null,
  qrBase64: null,
  ua: null,
  imei: null,
};

const wsClients = new Set();

// ─── Helpers ─────────────────────────────────────────────────────────────

function broadcast(event) {
  const msg = JSON.stringify(event);
  for (const ws of wsClients) {
    if (ws.readyState === 1) {
      try { ws.send(msg); } catch (e) { console.warn("[ws] send fail:", e.message); }
    }
  }
}

function pickUa() {
  // Deterministic per session — chọn UA đầu tiên nếu chưa có
  if (state.ua) return state.ua;
  // Lần đầu: lấy random rồi save
  state.ua = UA_POOL[Math.floor(Math.random() * UA_POOL.length)];
  return state.ua;
}

async function ensureDirs() {
  await fs.mkdir(SESSION_DIR, { recursive: true });
  await fs.mkdir(MEDIA_CACHE_DIR, { recursive: true });
}

async function saveSession(creds) {
  const data = {
    cookie: creds.cookie,
    imei: creds.imei,
    user_agent: creds.userAgent || state.ua,
    uid: state.uid,
    saved_at: Date.now(),
  };
  await fs.writeFile(SESSION_FILE, JSON.stringify(data, null, 2));
  console.log(`[session] saved to ${SESSION_FILE}`);
}

async function loadSession() {
  try {
    const text = await fs.readFile(SESSION_FILE, "utf-8");
    return JSON.parse(text);
  } catch {
    return null;
  }
}

/**
 * Build Zalo options with proxy.
 *
 * Cần CẢ HAI:
 *   - agent: HttpsProxyAgent cho WebSocket (zca-js dùng ws library)
 *   - polyfill: custom fetch dùng undici ProxyAgent dispatcher
 *
 * Lý do: Node native fetch (undici) KHÔNG respect `agent` option,
 * nên chỉ pass agent không đủ — HTTP requests vẫn đi trực tiếp.
 */
// Đọc kích thước ảnh (width/height/size) không cần thư viện ngoài — zca-js
// bắt buộc có imageMetadataGetter mới gửi được ảnh. Hỗ trợ PNG/JPEG/GIF.
async function imageMeta(filePath) {
  try {
    const st = await fs.stat(filePath);
    const buf = await fs.readFile(filePath);
    let width = 0, height = 0;
    if (buf.length > 24 && buf[0] === 0x89 && buf[1] === 0x50) {            // PNG
      width = buf.readUInt32BE(16); height = buf.readUInt32BE(20);
    } else if (buf.length > 4 && buf[0] === 0xFF && buf[1] === 0xD8) {       // JPEG
      let off = 2;
      while (off + 9 < buf.length) {
        if (buf[off] !== 0xFF) { off++; continue; }
        const marker = buf[off + 1];
        if (marker >= 0xC0 && marker <= 0xCF && marker !== 0xC4 && marker !== 0xC8 && marker !== 0xCC) {
          height = buf.readUInt16BE(off + 5); width = buf.readUInt16BE(off + 7); break;
        }
        off += 2 + buf.readUInt16BE(off + 2);
      }
    } else if (buf.length > 10 && buf[0] === 0x47 && buf[1] === 0x49) {      // GIF
      width = buf.readUInt16LE(6); height = buf.readUInt16LE(8);
    }
    return { width: width || 1024, height: height || 1024, size: st.size };
  } catch (e) {
    console.warn("[imageMeta]", e?.message || e);
    return null;
  }
}

function makeZaloOptions() {
  const opts = { selfListen: false, checkUpdate: false, logging: true, imageMetadataGetter: imageMeta };
  if (!PROXY) return opts;
  try {
    opts.agent = new HttpsProxyAgent(PROXY);
    const dispatcher = new ProxyAgent(PROXY);
    opts.polyfill = (url, options) => undiciFetch(url, { ...options, dispatcher });
    console.log(`[zalo] using proxy: ${PROXY.replace(/:[^:@]+@/, ":***@")}`);
  } catch (e) {
    console.error("[zalo] proxy setup failed:", e.message);
  }
  return opts;
}

// ─── Login flows ─────────────────────────────────────────────────────────

async function tryRestore() {
  const saved = await loadSession();
  if (!saved?.cookie || !saved?.imei) {
    console.log("[restore] no saved session");
    return false;
  }
  state.ua = saved.user_agent || pickUa();
  state.imei = saved.imei;

  try {
    const zalo = new Zalo(makeZaloOptions());
    const api = await zalo.login({
      cookie: saved.cookie,
      imei: saved.imei,
      userAgent: state.ua,
      language: "vi",
    });
    attachApi(api);
    state.status = "connected";
    state.uid = api.getOwnId?.() || saved.uid;
    console.log(`[restore] OK, uid=${state.uid}`);
    broadcast({ type: "login_state", state: "connected", uid: state.uid });
    return true;
  } catch (e) {
    console.warn("[restore] failed:", e.message);
    state.error = e.message;
    return false;
  }
}

async function startQrLogin() {
  state.status = "pending";
  state.qrBase64 = null;
  state.ua = pickUa();
  broadcast({ type: "login_state", state: "pending" });

  const zalo = new Zalo(makeZaloOptions());
  try {
    const api = await zalo.loginQR({ userAgent: state.ua, language: "vi" }, async (event) => {
      try {
        switch (event.type) {
          case LoginQRCallbackEventType.QRCodeGenerated:
            // event.data.image: base64 PNG (string)
            state.qrBase64 = event.data.image;
            const png = Buffer.from(event.data.image, "base64");
            await fs.writeFile(QR_FILE, png);
            console.log(`[qr] generated, saved to ${QR_FILE}`);
            broadcast({ type: "qr_ready", path: QR_FILE });
            break;
          case LoginQRCallbackEventType.QRCodeScanned:
            state.status = "scanned";
            console.log("[qr] scanned, verifying...");
            broadcast({ type: "login_state", state: "scanned" });
            break;
          case LoginQRCallbackEventType.GotLoginInfo:
            console.log("[qr] got login info");
            // event.data: { cookie, imei, userAgent }
            await saveSession(event.data);
            state.imei = event.data.imei;
            break;
          case LoginQRCallbackEventType.QRCodeExpired:
            state.status = "error";
            state.error = "QR expired";
            broadcast({ type: "login_state", state: "expired" });
            break;
        }
      } catch (e) { console.error("[qr cb] error:", e); }
    });

    attachApi(api);
    state.status = "connected";
    state.uid = api.getOwnId?.();
    console.log(`[login] connected, uid=${state.uid}`);
    broadcast({ type: "login_state", state: "connected", uid: state.uid });
  } catch (e) {
    state.status = "error";
    state.error = e.message;
    console.error("[login] failed:", e);
    broadcast({ type: "login_state", state: "error", error: e.message });
  }
}

// ─── Listener ────────────────────────────────────────────────────────────

function attachApi(api) {
  state.api = api;

  api.listener.on("message", async (msg) => {
    try {
      console.log("[msg]", JSON.stringify({ type: msg.type, data: msg.data }).slice(0, 500));
      const d = msg.data || {};
      // zca-js: msg.type numeric — 0=DirectMessage, 1=GroupMessage.
      // Fallback: idTo !== self uid means it's a group thread (group id vs bot uid).
      const isGroup =
        msg.type === 1 ||
        msg.type === "GroupMessage" ||
        (state.uid && d.idTo && String(d.idTo) !== String(state.uid));
      let content = await hydrateMedia(parseContent(d.content));
      // chat.photo (ảnh KÈM caption) bị Zalo gửi dạng {title,href,thumb} →
      // parseContent nhầm thành text. Ép lại thành image để tải file về
      // (cho tính năng bot dùng ảnh sếp gửi). Giữ caption làm text.
      if (String(d.msgType || "").toLowerCase().includes("photo") && content.kind !== "image") {
        const cc = d.content || {};
        const purl = cc.hdUrl || cc.normalUrl || cc.thumbUrl || cc.href || cc.thumb;
        if (purl) {
          const caption = cc.title || content.text || "";
          content = await hydrateMedia({ kind: "image", url: purl, title: caption, caption });
        }
      }
      // Quote info (reply-to). zca-js exposes TQuote on d.quote when set.
      let quote = null;
      if (d.quote && typeof d.quote === "object") {
        quote = {
          owner_id: String(d.quote.ownerId || ""),
          global_msg_id: String(d.quote.globalMsgId || ""),
          cli_msg_id: String(d.quote.cliMsgId || ""),
          text: String(d.quote.msg || ""),
        };
      }
      const event = {
        type: "message",
        msg_id: d.msgId,
        thread_id: isGroup ? d.idTo : d.uidFrom,
        thread_type: isGroup ? "group" : "user",
        from_uid: d.uidFrom,
        from_name: d.dName || "",
        ts: d.ts || Date.now(),
        content,
        mentions: Array.isArray(d.mentions) ? d.mentions : [],
        quote,
        self_uid: state.uid || null,
        self_name: state.name || null,
        raw_type: msg.type,
        msg_subtype: (d.msgType !== undefined ? d.msgType : ((d.content && d.content.msgType) || null)),
      };
      broadcast(event);
    } catch (e) { console.error("[listener msg]", e); }
  });

  // DEBUG: log mọi event để biết zca-js có nhận tin không
  for (const evName of ["typing", "old_messages", "seen_messages", "delivered_messages", "reaction", "old_reactions", "undo", "friend_event", "group_event", "cipher_key", "upload_attachment"]) {
    api.listener.on(evName, (...args) => {
      console.log(`[${evName}]`, JSON.stringify(args).slice(0, 300));
      // Forward friend_event tới adapter để tự-động-chấp-nhận lời mời kết bạn.
      if (evName === "friend_event") {
        try { broadcast({ type: "friend_event", data: args.length === 1 ? args[0] : args }); }
        catch (e) { /* noop */ }
      }
    });
  }

  api.listener.on("error", (err) => {
    console.error("[listener err]", err);
    broadcast({ type: "error", error: String(err) });
  });

  api.listener.on("connected", () => {
    console.log("[listener] WS connected");
    state.status = "connected";
    wsReconnectAttempts = 0;
    if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
    broadcast({ type: "listener_state", state: "connected" });
  });

  api.listener.on("closed", (code, reason) => {
    console.log(`[listener] WS closed: code=${code} reason=${reason}`);
    // CRITICAL: mark the sidecar as no-longer-connected so /health reports
    // the truth. Otherwise the gateway watchdog keeps seeing "connected"
    // while Zalo silently stopped delivering messages — the bot looks alive
    // but never responds. Code 1006 = abnormal close (proxy/network drop).
    state.status = "disconnected";
    broadcast({ type: "listener_state", state: "closed", code, reason });
    scheduleWsReconnect();
  });

  api.listener.on("disconnected", (code, reason) => {
    console.log(`[listener] WS disconnected: code=${code} reason=${reason}`);
    state.status = "disconnected";
    broadcast({ type: "listener_state", state: "disconnected", code, reason });
    scheduleWsReconnect();
  });

  api.listener.start();
  console.log("[listener] starting...");
}

// ---------------------------------------------------------------------------
// WebSocket auto-reconnect with exponential backoff.
//
// zca-js's listener drops the WS on network/proxy hiccups (commonly code
// 1006). Left alone the bot goes mute. We re-`start()` the existing listener
// with backoff; if zca-js refuses to restart a closed listener, the gateway
// watchdog still rescues us because state.status is now "disconnected" and
// /health reports it (triggering a full sidecar respawn).
// ---------------------------------------------------------------------------
let wsReconnectAttempts = 0;
let wsReconnectTimer = null;

function scheduleWsReconnect() {
  if (wsReconnectTimer) return; // a reconnect is already pending
  const delay = Math.min(5000 * Math.pow(2, wsReconnectAttempts), 60000);
  wsReconnectAttempts += 1;
  console.log(`[listener] scheduling WS reconnect #${wsReconnectAttempts} in ${delay}ms`);
  wsReconnectTimer = setTimeout(() => {
    wsReconnectTimer = null;
    try {
      if (state.api && state.api.listener) {
        console.log("[listener] attempting WS reconnect via listener.start()");
        state.api.listener.start();
      } else {
        console.warn("[listener] reconnect: no api/listener — relying on watchdog respawn");
      }
    } catch (e) {
      console.error("[listener] reconnect attempt failed:", e?.message || e);
      // Leave state.status="disconnected" so the watchdog respawns us.
      scheduleWsReconnect();
    }
  }, delay);
}

function parseContent(c) {
  if (typeof c === "string") return { kind: "text", text: c };
  if (!c || typeof c !== "object") return { kind: "unknown" };
  if (c.thumbUrl || c.normalUrl || c.hdUrl) {
    return {
      kind: "image",
      url: c.hdUrl || c.normalUrl || c.thumbUrl,
      title: c.title || "",
      width: c.width || null,
      height: c.height || null,
    };
  }
  if (c.fileUrl) {
    return {
      kind: "file",
      url: c.fileUrl,
      filename: c.fileName,
      size: c.totalSize,
    };
  }
  // Voice: heuristic on URL/filename + fallback to audio properties
  if (c.href && /\.(m4a|aac|mp3|opus|ogg)(\?|$)/i.test(c.fileName || c.href)) {
    return {
      kind: "voice",
      url: c.href,
      filename: c.fileName || "voice.m4a",
      duration_ms: c.properties?.duration || c.duration || null,
    };
  }
  if (c.text) return { kind: "text", text: c.text };
  // Link preview / chat.recommended: when user paste/recommend a link, Zalo
  // emits content as { title: "<user text>", description, href, thumb }.
  // Treat the title as the actual text and surface the link metadata so the
  // bot can browse it.
  if (c.title && (c.href || c.description || c.thumb)) {
    return {
      kind: "text",
      text: String(c.title),
      link_href: c.href || null,
      link_description: c.description || null,
      link_thumb: c.thumb || null,
    };
  }
  // Title-only fallback (no link metadata) — still surface as text so the
  // adapter doesn't drop the message.
  if (c.title) return { kind: "text", text: String(c.title) };
  return { kind: "unknown", raw: c };
}

const MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024; // 50MB

async function downloadMedia(url, ext = "") {
  if (!url) return null;
  try {
    const fetchFn = makeZaloOptions().polyfill || fetch;
    const resp = await fetchFn(url);
    if (!resp.ok) {
      console.warn(`[media] download ${url} → HTTP ${resp.status}`);
      return null;
    }
    // Chặn sớm theo Content-Length nếu có.
    const clen = Number(resp.headers.get("content-length") || 0);
    if (clen && clen > MAX_DOWNLOAD_BYTES) {
      console.warn(`[media] file qua lon (${clen} bytes > 50MB), bo qua: ${url}`);
      return { too_large: true, bytes: clen };
    }
    const buf = Buffer.from(await resp.arrayBuffer());
    // Backstop: thieu/khai man Content-Length.
    if (buf.length > MAX_DOWNLOAD_BYTES) {
      console.warn(`[media] file qua lon (${buf.length} bytes > 50MB), bo ghi: ${url}`);
      return { too_large: true, bytes: buf.length };
    }
    const cacheKey = randomUUID() + (ext ? `.${ext.replace(/^\./, "")}` : "");
    const filePath = path.join(MEDIA_CACHE_DIR, cacheKey);
    await fs.writeFile(filePath, buf);
    return { cache_key: cacheKey, path: filePath, bytes: buf.length };
  } catch (e) {
    console.warn("[media] download failed:", e.message);
    return null;
  }
}

function guessExt(content) {
  if (content.kind === "image") return "jpg";
  if (content.kind === "voice") {
    const fname = content.filename || "";
    const m = fname.match(/\.([a-zA-Z0-9]+)$/);
    return m ? m[1] : "m4a";
  }
  if (content.kind === "file") {
    const fname = content.filename || "";
    const m = fname.match(/\.([a-zA-Z0-9]+)$/);
    return m ? m[1] : "bin";
  }
  return "";
}

// Đuôi file nguy hiểm — KHÔNG tải về (file thực thi, script, macro office...).
const DANGEROUS_EXTS = new Set([
  "exe","scr","bat","cmd","com","pif","msi","msp","cpl","jar","apk","app","deb","rpm","dmg","pkg",
  "js","jse","mjs","vbs","vbe","ps1","psm1","wsf","wsh","hta","reg","lnk","sh","bash","zsh","bin",
  "run","gadget","dll","sys","drv","ocx","scf","inf","ace","iso","img","vbox",
  "docm","xlsm","pptm","dotm","xltm","potm",
]);
function isDangerousFile(filename) {
  const m = String(filename || "").toLowerCase().match(/\.([a-z0-9]+)$/);
  return m ? DANGEROUS_EXTS.has(m[1]) : false;
}

async function hydrateMedia(content) {
  if (!content || !content.url) return content;
  if (!["image", "voice", "file"].includes(content.kind)) return content;
  // Chặn đuôi nguy hiểm: KHÔNG tải về đĩa, đánh dấu để adapter báo người dùng.
  if (content.kind === "file" && isDangerousFile(content.filename)) {
    console.warn(`[media] BLOCKED dangerous file: ${content.filename}`);
    content.blocked = true;
    content.block_reason = "dangerous_extension";
    return content;
  }
  const ext = guessExt(content);
  const cached = await downloadMedia(content.url, ext);
  if (cached && cached.too_large) {
    content.too_large = true;
    content.bytes = cached.bytes;
  } else if (cached) {
    content.cache_key = cached.cache_key;
    content.local_path = cached.path;
    content.bytes = cached.bytes;
    content.media_url = `/media/${cached.cache_key}`;
  }
  return content;
}

// ─── HTTP API ────────────────────────────────────────────────────────────

const app = express();
app.use(express.json({ limit: "5mb" }));

app.get("/health", (req, res) => {
  res.json({
    status: state.status,
    uid: state.uid,
    name: state.name,
    error: state.error,
    has_session_file: !!(state.imei),
  });
});

app.post("/login/qr", async (req, res) => {
  if (state.status === "connected") return res.json({ status: "already_connected", uid: state.uid });
  startQrLogin().catch(e => console.error("[login flow]", e));
  res.json({ status: "pending", qr_url: "/qr.png" });
});

app.get("/qr.png", async (req, res) => {
  try {
    const png = await fs.readFile(QR_FILE);
    res.setHeader("Content-Type", "image/png");
    res.send(png);
  } catch (e) {
    res.status(404).json({ error: "QR not ready yet" });
  }
});

function threadTypeFromString(s) {
  return s === "group" ? ThreadType.Group : ThreadType.User;
}

app.post("/send/text", async (req, res) => {
  if (state.status !== "connected") return res.status(503).json({ error: "not_connected" });
  const { thread_id, thread_type = "user", text, quote, mentions } = req.body || {};
  if (!thread_id || !text) return res.status(400).json({ error: "thread_id and text required" });

  const content = { msg: String(text) };
  // Quote (reply-to) support — adapter passes the original message payload
  // captured from a previous listener event. zca-js requires the full
  // SendMessageQuote shape, so we accept it as-is.
  if (quote && typeof quote === "object" && quote.msgId) {
    content.quote = quote;
  }
  // @mention support — adapter resolves "@TênHiểnThị" → {pos, uid, len}
  // from its group-member directory. zca-js expects this exact shape.
  if (Array.isArray(mentions) && mentions.length > 0) {
    content.mentions = mentions
      .filter((m) => m && Number.isFinite(m.pos) && m.uid && Number.isFinite(m.len))
      .map((m) => ({
        pos: Math.max(0, Math.floor(Number(m.pos))),
        uid: String(m.uid),
        len: Math.max(0, Math.floor(Number(m.len))),
      }));
  }

  try {
    const result = await state.api.sendMessage(
      content,
      thread_id,
      threadTypeFromString(thread_type),
    );
    res.json({ ok: true, msg_id: result?.msgId, raw: result });
  } catch (e) {
    console.error("[send text]", e);
    res.status(500).json({ error: e.message });
  }
});

// React with emoji on a previously seen message.
app.post("/react", async (req, res) => {
  if (state.status !== "connected") return res.status(503).json({ error: "not_connected" });
  const { thread_id, thread_type = "user", msg_id, cli_msg_id, icon = "like" } = req.body || {};
  if (!thread_id || !msg_id) {
    return res.status(400).json({ error: "thread_id and msg_id required" });
  }
  // Resolve common emoji aliases to zca-js Reactions enum or fall back to
  // custom emoji.
  const aliasMap = {
    like: Reactions.LIKE,
    love: Reactions.HEART,
    heart: Reactions.HEART,
    haha: Reactions.HAHA,
    wow: Reactions.WOW,
    sad: Reactions.SAD,
    angry: Reactions.ANGRY,
    "❤️": Reactions.HEART,
    "👍": Reactions.LIKE,
    "😂": Reactions.HAHA,
    "😮": Reactions.WOW,
    "😢": Reactions.SAD,
    "😡": Reactions.ANGRY,
  };
  const resolvedIcon = aliasMap[String(icon).toLowerCase()] ?? aliasMap[icon] ?? {
    rType: 1,
    source: 1,
    icon: String(icon),
  };
  try {
    const result = await state.api.addReaction(resolvedIcon, {
      data: { msgId: String(msg_id), cliMsgId: String(cli_msg_id || msg_id) },
      threadId: String(thread_id),
      type: threadTypeFromString(thread_type),
    });
    res.json({ ok: true, raw: result });
  } catch (e) {
    console.error("[react]", e?.message || e);
    res.status(500).json({ error: e?.message || String(e) });
  }
});

// Send a sticker by keyword: search then send the best match.
app.post("/sticker/send", async (req, res) => {
  if (state.status !== "connected") return res.status(503).json({ error: "not_connected" });
  const { thread_id, thread_type = "user", keyword } = req.body || {};
  if (!thread_id || !keyword) return res.status(400).json({ error: "thread_id and keyword required" });
  try {
    const found = await state.api.searchSticker(String(keyword), 10);
    if (!Array.isArray(found) || found.length === 0) {
      return res.json({ ok: false, error: "no_sticker_found" });
    }
    const sk = found[0];
    const result = await state.api.sendSticker(
      { id: sk.sticker_id, cateId: sk.cate_id, type: sk.type },
      String(thread_id),
      threadTypeFromString(thread_type),
    );
    res.json({ ok: true, sticker: { id: sk.sticker_id, cateId: sk.cate_id, type: sk.type }, raw: result });
  } catch (e) {
    console.error("[sticker]", e?.message || e);
    res.status(500).json({ error: e?.message || String(e) });
  }
});

// Fetch group meta (name, member count, avatar) — used by adapter to
// resolve human-readable group names for alerts and digests.
app.get("/group/:groupId", async (req, res) => {
  if (state.status !== "connected") return res.status(503).json({ error: "not_connected" });
  const groupId = String(req.params.groupId || "");
  try {
    const fetcher = state.api?.getGroupInfo;
    if (typeof fetcher !== "function") {
      return res.status(501).json({ error: "getGroupInfo not available" });
    }
    const result = await fetcher.call(state.api, groupId);
    const info = (result?.gridInfoMap || {})[groupId] || null;
    if (!info) {
      return res.status(404).json({ error: "group not found", raw: result });
    }
    res.json({
      ok: true,
      group_id: groupId,
      name: info.name || info.groupName || null,
      desc: info.desc || null,
      member_count: info.totalMember || info.memVerList?.length || null,
      type: info.type || null,
      raw: info,
    });
  } catch (e) {
    console.error("[group info]", e?.message || e);
    res.status(500).json({ error: e?.message || String(e) });
  }
});

// Fetch full member roster (uid + display name) of a group. Used by the
// adapter to resolve outbound "@DisplayName" mentions for members who
// haven't sent a message yet (so the bot can tag anyone in the group, not
// just people it has already observed). Two-step: getGroupInfo → memberIds,
// then getGroupMembersInfo → profile names.
app.get("/group/:groupId/members", async (req, res) => {
  if (state.status !== "connected") return res.status(503).json({ error: "not_connected" });
  const groupId = String(req.params.groupId || "");
  try {
    const giFn = state.api?.getGroupInfo;
    if (typeof giFn !== "function") {
      return res.status(501).json({ error: "getGroupInfo not available" });
    }
    const gi = await giFn.call(state.api, groupId);
    const info = (gi?.gridInfoMap || {})[groupId] || null;
    if (!info) return res.status(404).json({ error: "group not found", _gikeys: Object.keys(gi || {}).join(",") });
    // Member uid source — Zalo populates these inconsistently. Try, in order:
    // memberIds (array) → memVerList (array of "<uid>_<ver>") → memberIds
    // values when it's an object. All entries look like "<uid>_0".
    let rawIds = [];
    if (Array.isArray(info.memberIds) && info.memberIds.length) {
      rawIds = info.memberIds;
    } else if (Array.isArray(info.memVerList) && info.memVerList.length) {
      rawIds = info.memVerList;
    } else if (info.memberIds && typeof info.memberIds === "object") {
      rawIds = Object.values(info.memberIds);
    }
    const cleanIds = [...new Set(rawIds.map((x) => String(x).split("_")[0]).filter(Boolean))];
    if (!cleanIds.length) {
      return res.json({ ok: true, group_id: groupId, count: 0, members: [],
        _debug: "no member uids in memberIds/memVerList" });
    }
    const members = [];
    let debugSample = null;
    const apiMethods = Object.keys(state.api || {}).filter((k) => /member|getGroup/i.test(k)).join(",");
    const gmFn = state.api?.getGroupMembersInfo;
    const withTimeout = (p, ms) => Promise.race([
      p,
      new Promise((_, rej) => setTimeout(() => rej(new Error("timeout " + ms + "ms")), ms)),
    ]);
    if (typeof gmFn === "function") {
      for (let i = 0; i < cleanIds.length; i += 50) {
        const chunk = cleanIds.slice(i, i + 50);
        try {
          const prof = await withTimeout(gmFn.call(state.api, chunk), 15000);
          if (debugSample === null) debugSample = JSON.stringify(prof).slice(0, 500);
          let entries = [];
          if (Array.isArray(prof)) entries = prof;
          else if (prof && typeof prof === "object") {
            const cont = prof.profiles || prof.unchangedProfiles || prof.gridInfoMap || prof;
            entries = Array.isArray(cont) ? cont : Object.values(cont);
          }
          for (const p of entries) {
            if (!p || typeof p !== "object") continue;
            const uid = String(p.userId || p.uid || p.id || p.globalId || "");
            const name = p.zaloName || p.displayName || p.dName || p.name || p.username || "";
            if (uid && name) members.push({ uid, name: String(name) });
          }
        } catch (e) {
          if (debugSample === null) debugSample = "ERR:" + (e?.message || String(e)).slice(0, 200);
          console.warn("[group members] chunk fail:", e?.message || e);
        }
      }
    } else {
      debugSample = "getGroupMembersInfo NOT a function";
    }
    res.json({ ok: true, group_id: groupId, total_ids: cleanIds.length, count: members.length, members, _methods: apiMethods, _debug: debugSample });
  } catch (e) {
    console.error("[group members]", e?.message || e);
    res.status(500).json({ error: e?.message || String(e) });
  }
});

// Fetch recent group chat history (used by adapter for backfill on restart).
app.get("/history/group/:groupId", async (req, res) => {
  if (state.status !== "connected") return res.status(503).json({ error: "not_connected" });
  const groupId = req.params.groupId;
  const count = Math.min(parseInt(req.query.count || "50", 10) || 50, 200);
  try {
    const fetcher = state.api?.getGroupChatHistory;
    if (typeof fetcher !== "function") {
      return res.status(501).json({ error: "getGroupChatHistory not available" });
    }
    const result = await fetcher.call(state.api, groupId, count);
    res.json({ ok: true, group_id: groupId, count, data: result });
  } catch (e) {
    console.error("[history group]", e?.message || e);
    res.status(500).json({ error: e?.message || String(e) });
  }
});

app.post("/send/image", async (req, res) => {
  if (state.status !== "connected") return res.status(503).json({ error: "not_connected" });
  const { thread_id, thread_type = "user", file_path, caption } = req.body || {};
  if (!thread_id || !file_path) {
    return res.status(400).json({ error: "thread_id and file_path required" });
  }
  try {
    await fs.access(file_path);
  } catch {
    return res.status(404).json({ error: `file not found: ${file_path}` });
  }
  try {
    const result = await state.api.sendMessage(
      { msg: caption ? String(caption) : "", attachments: [file_path] },
      thread_id,
      threadTypeFromString(thread_type),
    );
    res.json({ ok: true, msg_id: result?.msgId, raw: result });
  } catch (e) {
    console.error("[send image]", e);
    res.status(500).json({ error: e.message });
  }
});

app.post("/send/file", async (req, res) => {
  if (state.status !== "connected") return res.status(503).json({ error: "not_connected" });
  const { thread_id, thread_type = "user", file_path, caption } = req.body || {};
  if (!thread_id || !file_path) {
    return res.status(400).json({ error: "thread_id and file_path required" });
  }
  try {
    await fs.access(file_path);
  } catch {
    return res.status(404).json({ error: `file not found: ${file_path}` });
  }
  try {
    const result = await state.api.sendMessage(
      { msg: caption ? String(caption) : "", attachments: [file_path] },
      thread_id,
      threadTypeFromString(thread_type),
    );
    res.json({ ok: true, msg_id: result?.msgId, raw: result });
  } catch (e) {
    console.error("[send file]", e);
    res.status(500).json({ error: e.message });
  }
});

// Gửi 1 tin kèm NHIỀU ảnh. images[] = link URL (tự tải về) hoặc đường dẫn
// file local (vd ảnh sếp gửi vào đã cache). Tối đa 10 ảnh/tin.
app.post("/send/media", async (req, res) => {
  if (state.status !== "connected") return res.status(503).json({ error: "not_connected" });
  const { thread_id, thread_type = "user", caption = "", images } = req.body || {};
  if (!thread_id || !Array.isArray(images) || !images.length) {
    return res.status(400).json({ error: "thread_id and images[] required" });
  }
  const paths = [];
  for (const img of images.slice(0, 10)) {
    const s = String(img || "");
    if (/^https?:\/\//i.test(s)) {
      const c = await downloadMedia(s, "jpg");
      if (c?.path) paths.push(c.path);
    } else {
      try { await fs.access(s); paths.push(s); } catch { /* bỏ qua file không tồn tại */ }
    }
  }
  if (!paths.length) return res.status(400).json({ error: "no valid images (URL tải lỗi / file không tồn tại)" });
  try {
    const result = await state.api.sendMessage(
      { msg: caption ? String(caption) : "", attachments: paths },
      thread_id,
      threadTypeFromString(thread_type),
    );
    res.json({ ok: true, sent: paths.length, msg_id: result?.msgId, raw: result });
  } catch (e) {
    console.error("[send media]", e);
    res.status(500).json({ error: e.message });
  }
});

app.post("/typing", async (req, res) => {
  if (state.status !== "connected") return res.status(503).json({ error: "not_connected" });
  const { thread_id, thread_type = "user" } = req.body || {};
  if (!thread_id) return res.status(400).json({ error: "thread_id required" });
  try {
    const sendTyping = state.api?.sendTypingEvent;
    if (typeof sendTyping !== "function") {
      return res.status(501).json({ error: "sendTypingEvent not available in this zca-js version" });
    }
    await sendTyping.call(
      state.api,
      thread_id,
      threadTypeFromString(thread_type),
    );
    res.json({ ok: true });
  } catch (e) {
    console.error("[typing]", e?.message || e);
    res.status(500).json({ error: e?.message || String(e) });
  }
});

app.get("/media/:cacheKey", async (req, res) => {
  const cacheKey = req.params.cacheKey;
  if (!/^[a-zA-Z0-9._-]+$/.test(cacheKey)) {
    return res.status(400).json({ error: "invalid cache key" });
  }
  const filePath = path.join(MEDIA_CACHE_DIR, cacheKey);
  try {
    await fs.access(filePath);
    res.sendFile(filePath);
  } catch {
    res.status(404).json({ error: "not found" });
  }
});

app.post("/logout", async (req, res) => {
  try {
    if (state.api?.listener) state.api.listener.stop();
    await fs.unlink(SESSION_FILE).catch(() => {});
    state = { ...state, status: "disconnected", uid: null, api: null };
    broadcast({ type: "login_state", state: "disconnected" });
    res.json({ ok: true });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

// ─── Marketing: quét link, tra SĐT, kết bạn, danh bạ ─────────────────────
// Chuẩn hoá uid/tên/avatar từ nhiều dạng field zca-js trả (camelCase &
// snake_case lẫn lộn tuỳ method).
function _normUser(u) {
  if (!u || typeof u !== "object") return null;
  const uid = String(u.userId || u.uid || u.globalId || u.id || "");
  const name = u.zaloName || u.zalo_name || u.displayName || u.display_name || u.dName || u.name || "";
  return uid ? { uid, name: String(name), avatar: u.avatar || "" } : null;
}

// Quét thành viên nhóm theo LINK (không cần tham gia). Phân trang.
app.get("/group/link-info", async (req, res) => {
  if (state.status !== "connected") return res.status(503).json({ error: "not_connected" });
  const link = String(req.query.link || "");
  const page = parseInt(req.query.page || "1", 10) || 1;
  if (!link) return res.status(400).json({ error: "link required" });
  try {
    const fn = state.api?.getGroupLinkInfo;
    if (typeof fn !== "function") return res.status(501).json({ error: "getGroupLinkInfo unavailable" });
    const info = await fn.call(state.api, { link, memberPage: page });
    res.json({
      ok: true, group_id: info.groupId, name: info.name, total_member: info.totalMember,
      lock_view_member: info.setting?.lockViewMember ?? null, admin_ids: info.adminIds || [],
      creator_id: info.creatorId || "", has_more: info.hasMoreMember || 0,
      members: (info.currentMems || []).map((m) => ({
        uid: String(m.id || ""), name: m.zaloName || m.dName || "", avatar: m.avatar || "",
      })),
    });
  } catch (e) { res.status(500).json({ error: e?.message || String(e) }); }
});

// Tra Zalo theo danh sách SĐT
app.post("/users/by-phones", async (req, res) => {
  if (state.status !== "connected") return res.status(503).json({ error: "not_connected" });
  const phones = Array.isArray(req.body?.phones) ? req.body.phones.map(String) : [];
  if (!phones.length) return res.status(400).json({ error: "phones[] required" });
  try {
    const fn = state.api?.getMultiUsersByPhones;
    if (typeof fn !== "function") return res.status(501).json({ error: "getMultiUsersByPhones unavailable" });
    const r = await fn.call(state.api, phones);
    const out = [];
    for (const [phone, u] of Object.entries(r || {})) {
      const n = _normUser(u);
      if (n) out.push({ phone, ...n });
    }
    res.json({ ok: true, count: out.length, users: out });
  } catch (e) { res.status(500).json({ error: e?.message || String(e) }); }
});

// Gửi lời mời kết bạn
app.post("/friend/request", async (req, res) => {
  if (state.status !== "connected") return res.status(503).json({ error: "not_connected" });
  const { uid, msg } = req.body || {};
  if (!uid) return res.status(400).json({ error: "uid required" });
  try {
    const r = await state.api.sendFriendRequest(String(msg || ""), String(uid));
    res.json({ ok: true, raw: r });
  } catch (e) { res.status(500).json({ error: e?.message || String(e) }); }
});

// Danh sách lời mời đã gửi
app.get("/friend/sent", async (req, res) => {
  if (state.status !== "connected") return res.status(503).json({ error: "not_connected" });
  try { res.json({ ok: true, raw: await state.api.getSentFriendRequest() }); }
  catch (e) { res.status(500).json({ error: e?.message || String(e) }); }
});

// Chấp nhận lời mời kết bạn
app.post("/friend/accept", async (req, res) => {
  if (state.status !== "connected") return res.status(503).json({ error: "not_connected" });
  const { uid } = req.body || {};
  if (!uid) return res.status(400).json({ error: "uid required" });
  try { res.json({ ok: true, raw: await state.api.acceptFriendRequest(String(uid)) }); }
  catch (e) { res.status(500).json({ error: e?.message || String(e) }); }
});

// Toàn bộ danh bạ bạn bè
app.get("/friends/all", async (req, res) => {
  if (state.status !== "connected") return res.status(503).json({ error: "not_connected" });
  try {
    const r = await state.api.getAllFriends(1000, 1);
    const arr = Array.isArray(r) ? r : Object.values(r || {});
    const friends = arr.map(_normUser).filter(Boolean);
    res.json({ ok: true, count: friends.length, friends });
  } catch (e) { res.status(500).json({ error: e?.message || String(e) }); }
});

// ─── Server bootstrap ────────────────────────────────────────────────────

const server = createServer(app);
const wss = new WebSocketServer({ server, path: "/events" });

wss.on("connection", (ws) => {
  wsClients.add(ws);
  ws.on("close", () => wsClients.delete(ws));
  // Send initial state
  ws.send(JSON.stringify({
    type: "login_state",
    state: state.status,
    uid: state.uid,
  }));
});

(async () => {
  await ensureDirs();
  server.listen(PORT, "127.0.0.1", () => {
    console.log(`[sidecar] listening on http://127.0.0.1:${PORT}`);
  });

  // Try restore session on startup
  const restored = await tryRestore();
  if (!restored) {
    console.log("[startup] no valid session — call POST /login/qr to start QR flow");
  }
})();
