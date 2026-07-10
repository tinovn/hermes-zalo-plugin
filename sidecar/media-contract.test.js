/**
 * Tests for media-contract.js — magic sniffing, MIME/ext mapping, cache-root
 * resolution, and bounded streaming download. Runs with `node --test`, no Zalo.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { promises as fs } from "node:fs";
import path from "node:path";
import os from "node:os";
import {
  sniffImage,
  looksLikeImage,
  imageMimeForExt,
  extForImageMime,
  safeExtFromFilename,
  resolveSessionDir,
  resolveMediaCacheDir,
  CANONICAL_SESSION_DIR,
  createSemaphore,
  downloadMediaStreamed,
  DEFAULT_MAX_DOWNLOAD_BYTES,
} from "./media-contract.js";

// ─── Fixtures: minimal valid magic-byte headers ─────────────────────────────

const JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0x4a, 0x46, 0x49, 0x46, 0x00, 0x01]);
const PNG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d]);
const GIF89 = Buffer.from([0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 0x01, 0x00, 0x01, 0x00, 0x80, 0x00]);
const GIF87 = Buffer.from([0x47, 0x49, 0x46, 0x38, 0x37, 0x61, 0x01, 0x00, 0x01, 0x00, 0x80, 0x00]);
function webp() {
  const b = Buffer.alloc(16);
  b.write("RIFF", 0, "ascii");
  b.writeUInt32LE(8, 4);
  b.write("WEBP", 8, "ascii");
  return b;
}
// A PDF header — a non-image file frequently sent as a Zalo File with a .jpg name.
const PDF = Buffer.from("%PDF-1.7\n%\xe2\xe3\xcf\xd3", "latin1");

// ─── sniffImage ─────────────────────────────────────────────────────────────

test("sniffImage detects JPEG/PNG/GIF/WebP", () => {
  assert.deepEqual(sniffImage(JPEG), { mime: "image/jpeg", ext: "jpg" });
  assert.deepEqual(sniffImage(PNG), { mime: "image/png", ext: "png" });
  assert.deepEqual(sniffImage(GIF89), { mime: "image/gif", ext: "gif" });
  assert.deepEqual(sniffImage(GIF87), { mime: "image/gif", ext: "gif" });
  assert.deepEqual(sniffImage(webp()), { mime: "image/webp", ext: "webp" });
});

test("sniffImage rejects non-image and short buffers", () => {
  assert.equal(sniffImage(PDF), null);
  assert.equal(sniffImage(Buffer.from([0x00, 0x01])), null);
  assert.equal(sniffImage(null), null);
  assert.equal(looksLikeImage(PDF), false);
  assert.equal(looksLikeImage(PNG), true);
});

test("mime <-> ext mapping is canonical", () => {
  assert.equal(imageMimeForExt("jpg"), "image/jpeg");
  assert.equal(imageMimeForExt(".JPEG"), "image/jpeg");
  assert.equal(imageMimeForExt("webp"), "image/webp");
  assert.equal(imageMimeForExt("txt"), null);
  assert.equal(extForImageMime("image/png"), "png");
  assert.equal(extForImageMime("image/jpeg; charset=binary"), "jpg");
  assert.equal(extForImageMime("application/pdf"), null);
});

test("safeExtFromFilename lowercases and falls back", () => {
  assert.equal(safeExtFromFilename("VOICE.M4A", "m4a"), "m4a");
  assert.equal(safeExtFromFilename("report.PDF"), "pdf");
  assert.equal(safeExtFromFilename("noext", "bin"), "bin");
  assert.equal(safeExtFromFilename("", "m4a"), "m4a");
});

// ─── cache-root resolver (fixes Node vs Python mismatch) ─────────────────────

test("resolveSessionDir defaults to canonical /opt/data/zalo", () => {
  assert.equal(resolveSessionDir({}), CANONICAL_SESSION_DIR);
  assert.equal(resolveSessionDir({ ZALO_PERSONAL_SESSION_DIR: "  " }), CANONICAL_SESSION_DIR);
  assert.equal(resolveSessionDir({ ZALO_PERSONAL_SESSION_DIR: "/custom/root" }), "/custom/root");
  assert.equal(resolveMediaCacheDir({ ZALO_PERSONAL_SESSION_DIR: "/custom/root" }), path.join("/custom/root", "media-cache"));
});

// ─── semaphore ───────────────────────────────────────────────────────────────

test("createSemaphore caps concurrency", async () => {
  const sem = createSemaphore(2);
  let peak = 0, cur = 0;
  const task = () => sem.run(async () => {
    cur += 1; peak = Math.max(peak, cur);
    await new Promise((r) => setTimeout(r, 10));
    cur -= 1;
  });
  await Promise.all([task(), task(), task(), task(), task()]);
  assert.ok(peak <= 2, `peak concurrency ${peak} should be <= 2`);
});

// ─── streaming download ──────────────────────────────────────────────────────

let TMP;
test("setup tmp cache dir", async () => {
  TMP = await fs.mkdtemp(path.join(os.tmpdir(), "media-contract-"));
  await fs.chmod(TMP, 0o700);
});

function respFrom(bytes, { contentType, contentLength, status = 200, delayMs = 0, chunkSize } = {}) {
  const headers = new Map();
  if (contentType) headers.set("content-type", contentType);
  if (contentLength != null) headers.set("content-length", String(contentLength));
  const src = Buffer.isBuffer(bytes) ? bytes : Buffer.from(bytes);
  const cs = chunkSize || src.length || 1;
  const stream = new ReadableStream({
    async start(controller) {
      for (let i = 0; i < src.length; i += cs) {
        if (delayMs) await new Promise((r) => setTimeout(r, delayMs));
        controller.enqueue(new Uint8Array(src.subarray(i, i + cs)));
      }
      controller.close();
    },
  });
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (k) => headers.get(String(k).toLowerCase()) ?? null },
    body: stream,
  };
}

test("download writes a JPEG with detected .jpg extension", async () => {
  const fetchFn = async () => respFrom(JPEG, { contentType: "image/jpeg" });
  const r = await downloadMediaStreamed(fetchFn, "https://x/y", { cacheDir: TMP, kind: "image" });
  assert.equal(r.is_image, true);
  assert.equal(r.mime, "image/jpeg");
  assert.equal(r.ext, "jpg");
  assert.ok(r.path.endsWith(".jpg"));
  const st = await fs.stat(r.path);
  assert.equal(st.size, JPEG.length);
  // mode 0600 on the final file
  assert.equal(st.mode & 0o777, 0o600);
});

test("PNG sent with a .jpg hint is saved as .png (magic wins)", async () => {
  const fetchFn = async () => respFrom(PNG, { contentType: "image/jpeg" });
  const r = await downloadMediaStreamed(fetchFn, "https://x/y.jpg", { cacheDir: TMP, kind: "image", hintExt: "jpg" });
  assert.equal(r.ext, "png");
  assert.equal(r.mime, "image/png");
});

test("image-as-file (PDF bytes, kind=image) is rejected as invalid_image, no file kept", async () => {
  const before = (await fs.readdir(TMP)).length;
  const fetchFn = async () => respFrom(PDF, { contentType: "image/jpeg" });
  const r = await downloadMediaStreamed(fetchFn, "https://x/fake.jpg", { cacheDir: TMP, kind: "image" });
  assert.equal(r.invalid_image, true);
  assert.equal(r.is_image, false);
  const after = (await fs.readdir(TMP)).length;
  assert.equal(after, before, "no new file should remain for an invalid image");
});

test("non-image file keeps hint extension", async () => {
  const fetchFn = async () => respFrom(PDF, { contentType: "application/pdf" });
  const r = await downloadMediaStreamed(fetchFn, "https://x/doc", { cacheDir: TMP, kind: "file", hintExt: "pdf" });
  assert.equal(r.is_image, false);
  assert.equal(r.ext, "pdf");
  assert.ok(r.path.endsWith(".pdf"));
});

test("early Content-Length over cap short-circuits", async () => {
  const fetchFn = async () => respFrom(JPEG, { contentType: "image/jpeg", contentLength: DEFAULT_MAX_DOWNLOAD_BYTES + 1 });
  const r = await downloadMediaStreamed(fetchFn, "https://x/big", { cacheDir: TMP, kind: "image" });
  assert.equal(r.too_large, true);
});

test("streamed body exceeding cap aborts and leaves no partial file", async () => {
  const before = (await fs.readdir(TMP)).length;
  const big = Buffer.concat([JPEG, Buffer.alloc(2048, 0x41)]);
  // no content-length header → only the streaming backstop can catch it
  const fetchFn = async () => respFrom(big, { chunkSize: 256 });
  const r = await downloadMediaStreamed(fetchFn, "https://x/big", { cacheDir: TMP, kind: "image", maxBytes: 1024 });
  assert.equal(r.too_large, true);
  const after = (await fs.readdir(TMP)).filter((f) => !f.startsWith(".tmp-"));
  assert.equal(after.length, before, "no partial temp file should survive");
});

test("deadline abort returns timed_out and cleans up", async () => {
  const before = (await fs.readdir(TMP)).filter((f) => !f.startsWith(".tmp-")).length;
  const fetchFn = async () => respFrom(JPEG, { delayMs: 50, chunkSize: 1 });
  const r = await downloadMediaStreamed(fetchFn, "https://x/slow", { cacheDir: TMP, kind: "image", timeoutMs: 20 });
  assert.ok(r === null || r.too_large || r.timed_out, "slow download should not succeed");
  await new Promise((res) => setTimeout(res, 60));
  const after = (await fs.readdir(TMP)).filter((f) => !f.startsWith(".tmp-")).length;
  assert.equal(after, before, "no completed file after deadline abort");
});

test("HTTP error returns null", async () => {
  const fetchFn = async () => respFrom(JPEG, { status: 404 });
  const r = await downloadMediaStreamed(fetchFn, "https://x/404", { cacheDir: TMP, kind: "image" });
  assert.equal(r, null);
});

test("cleanup tmp cache dir", async () => {
  if (TMP) await fs.rm(TMP, { recursive: true, force: true });
});
