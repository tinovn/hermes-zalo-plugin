/**
 * media-contract.js — pure, testable helpers for the Zalo inbound media contract.
 *
 * Extracted from server.js so the media boundary can be unit-tested without a
 * live Zalo login. No zca-js / express imports here on purpose.
 *
 * Responsibilities:
 *   - Authoritative image detection by magic bytes (header/extension are hints).
 *   - Canonical MIME <-> extension mapping.
 *   - The SINGLE session/media-cache root resolver shared by startup, /health
 *     and tests (fixes the historical Node `$HOME/zalo` vs Python `/opt/data/zalo`
 *     mismatch — canonical default is `/opt/data/zalo`).
 *   - Bounded streaming download: hard byte cap enforced DURING streaming, a
 *     wall-clock deadline, a concurrency semaphore, atomic rename only after the
 *     bytes are on disk, and guaranteed cleanup of partial temp files.
 */

import { promises as fs } from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";

// ─── Cache root resolver (single source of truth) ──────────────────────────

// Canonical default agreed with the Python adapter and plugin.yaml.
export const CANONICAL_SESSION_DIR = "/opt/data/zalo";

/**
 * Resolve the Zalo session directory from the environment.
 * @param {Record<string,string|undefined>} [env=process.env]
 */
export function resolveSessionDir(env = process.env) {
  const v = env.ZALO_PERSONAL_SESSION_DIR;
  if (v && String(v).trim()) return String(v).trim();
  return CANONICAL_SESSION_DIR;
}

/** Resolve the media-cache directory (always `<session-dir>/media-cache`). */
export function resolveMediaCacheDir(env = process.env) {
  return path.join(resolveSessionDir(env), "media-cache");
}

// ─── Magic-byte image sniffing (authoritative) ─────────────────────────────

const IMAGE_MIME_BY_EXT = {
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  png: "image/png",
  gif: "image/gif",
  webp: "image/webp",
};

const EXT_BY_IMAGE_MIME = {
  "image/jpeg": "jpg",
  "image/png": "png",
  "image/gif": "gif",
  "image/webp": "webp",
};

export function imageMimeForExt(ext) {
  return IMAGE_MIME_BY_EXT[String(ext || "").toLowerCase().replace(/^\./, "")] || null;
}

export function extForImageMime(mime) {
  return EXT_BY_IMAGE_MIME[String(mime || "").toLowerCase().split(";")[0].trim()] || null;
}

/**
 * Detect an image type from leading bytes. Returns `{ mime, ext }` or null.
 * Recognizes JPEG, PNG, GIF (87a/89a) and WebP. This is authoritative; the
 * Content-Type header and the sender-declared extension are only hints.
 * @param {Uint8Array|Buffer} bytes
 */
export function sniffImage(bytes) {
  if (!bytes || bytes.length < 12) return null;
  const b = bytes;
  // JPEG: FF D8 FF
  if (b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) {
    return { mime: "image/jpeg", ext: "jpg" };
  }
  // PNG: 89 50 4E 47 0D 0A 1A 0A
  if (
    b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47 &&
    b[4] === 0x0d && b[5] === 0x0a && b[6] === 0x1a && b[7] === 0x0a
  ) {
    return { mime: "image/png", ext: "png" };
  }
  // GIF: "GIF87a" / "GIF89a"
  if (b[0] === 0x47 && b[1] === 0x49 && b[2] === 0x46 && b[3] === 0x38 &&
      (b[4] === 0x37 || b[4] === 0x39) && b[5] === 0x61) {
    return { mime: "image/gif", ext: "gif" };
  }
  // WEBP: "RIFF"...."WEBP"
  if (
    b[0] === 0x52 && b[1] === 0x49 && b[2] === 0x46 && b[3] === 0x46 &&
    b[8] === 0x57 && b[9] === 0x45 && b[10] === 0x42 && b[11] === 0x50
  ) {
    return { mime: "image/webp", ext: "webp" };
  }
  return null;
}

/** True when leading bytes are a recognized image type. */
export function looksLikeImage(bytes) {
  return sniffImage(bytes) !== null;
}

// ─── Safe extension for non-image files (voice/file) ────────────────────────

export function safeExtFromFilename(filename, fallback = "bin") {
  const m = String(filename || "").match(/\.([a-zA-Z0-9]{1,8})$/);
  return m ? m[1].toLowerCase() : fallback;
}

/** Sanitize a bare extension hint (already stripped of any filename). */
export function sanitizeExt(ext, fallback = "bin") {
  const m = String(ext || "").replace(/^\./, "").match(/^([a-zA-Z0-9]{1,8})$/);
  return m ? m[1].toLowerCase() : fallback;
}

// ─── Concurrency semaphore ──────────────────────────────────────────────────

export function createSemaphore(max) {
  let active = 0;
  const queue = [];
  const acquire = () =>
    new Promise((resolve) => {
      if (active < max) { active += 1; resolve(); }
      else queue.push(resolve);
    });
  const release = () => {
    active = Math.max(0, active - 1);
    const next = queue.shift();
    if (next) { active += 1; next(); }
  };
  return {
    run: async (fn) => {
      await acquire();
      try { return await fn(); }
      finally { release(); }
    },
    get active() { return active; },
    get waiting() { return queue.length; },
  };
}

// ─── Bounded streaming download ─────────────────────────────────────────────

export const DEFAULT_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024; // 50MB
export const DEFAULT_DOWNLOAD_TIMEOUT_MS = 30_000;

/**
 * Stream a URL to a temp file under `cacheDir` with a hard byte cap enforced
 * WHILE streaming, a wall-clock deadline, atomic rename only after success, and
 * guaranteed partial-file cleanup.
 *
 * @param {Function} fetchFn                fetch-compatible (url, {signal}) => Response
 * @param {string}   url
 * @param {object}   opts
 * @param {string}   opts.cacheDir          destination dir (mode-0700 expected)
 * @param {"image"|"voice"|"file"} [opts.kind]
 * @param {string}   [opts.hintExt]         sender extension hint (non-image kinds)
 * @param {number}   [opts.maxBytes]
 * @param {number}   [opts.timeoutMs]
 * @returns {Promise<null | {too_large:true,bytes:number} | {cache_key,path,bytes,mime,ext,content_type,is_image}>}
 */
export async function downloadMediaStreamed(fetchFn, url, opts = {}) {
  if (!url) return null;
  const {
    cacheDir,
    kind = "file",
    hintExt = "",
    maxBytes = DEFAULT_MAX_DOWNLOAD_BYTES,
    timeoutMs = DEFAULT_DOWNLOAD_TIMEOUT_MS,
  } = opts;
  if (!cacheDir) throw new Error("downloadMediaStreamed: cacheDir required");

  const controller = new AbortController();
  const deadline = setTimeout(() => controller.abort(new Error("download deadline exceeded")), timeoutMs);
  const tmpPath = path.join(cacheDir, `.tmp-${randomUUID()}`);
  let fh = null;
  let wroteTemp = false;

  try {
    const resp = await fetchFn(url, { signal: controller.signal });
    if (!resp || !resp.ok) return null;

    // Early hint: reject before streaming when the server declares an oversize body.
    const clen = Number(resp.headers?.get?.("content-length") || 0);
    if (clen && clen > maxBytes) return { too_large: true, bytes: clen };
    const contentType = String(resp.headers?.get?.("content-type") || "").split(";")[0].trim();

    fh = await fs.open(tmpPath, "wx", 0o600);
    wroteTemp = true;

    const header = Buffer.alloc(0);
    let head = header;
    let total = 0;

    // resp.body is a web ReadableStream (undici/node fetch) — async-iterable.
    const body = resp.body;
    if (!body) return null;
    const deadlineAt = Date.now() + timeoutMs;
    for await (const chunk of body) {
      // Defense in depth: honor the deadline even if the fetch impl does not
      // propagate the abort signal into the body stream.
      if (controller.signal.aborted || Date.now() > deadlineAt) {
        return { too_large: true, bytes: total, timed_out: true };
      }
      const buf = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      total += buf.length;
      if (total > maxBytes) {
        controller.abort(new Error("byte cap exceeded"));
        return { too_large: true, bytes: total };
      }
      if (head.length < 32) {
        head = Buffer.concat([head, buf.subarray(0, 32 - head.length)]);
      }
      await fh.write(buf);
    }
    await fh.close();
    fh = null;

    const sniff = sniffImage(head);
    let ext, mime, isImage;
    if (sniff) {
      ext = sniff.ext; mime = sniff.mime; isImage = true;
    } else if (kind === "image") {
      // Declared an image but bytes don't validate → not a usable image.
      // Do NOT keep the file; the adapter must not route it to vision.
      return { invalid_image: true, bytes: total, is_image: false };
    } else {
      ext = sanitizeExt(hintExt || "", kind === "voice" ? "m4a" : "bin");
      mime = contentType || null;
      isImage = false;
    }

    const cacheKey = `${randomUUID()}.${ext}`;
    const finalPath = path.join(cacheDir, cacheKey);
    await fs.rename(tmpPath, finalPath);
    wroteTemp = false;
    return { cache_key: cacheKey, path: finalPath, bytes: total, mime, ext, content_type: contentType || null, is_image: isImage };
  } catch (e) {
    if (e?.name === "AbortError" || /deadline|cap/i.test(e?.message || "")) {
      return { too_large: true, bytes: 0, timed_out: true };
    }
    return null;
  } finally {
    clearTimeout(deadline);
    if (fh) { try { await fh.close(); } catch { /* noop */ } }
    if (wroteTemp) { try { await fs.unlink(tmpPath); } catch { /* noop */ } }
  }
}
