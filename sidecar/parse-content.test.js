/**
 * Tests for parseContent() — nhận diện log cuộc gọi (chat.recommended +
 * content.action chứa "call") và không làm hỏng các nhánh cũ (ảnh, file,
 * text, link preview).
 *
 * server.js khởi động HTTP/WS ngay khi import nên không import trực tiếp
 * được; hàm được cắt ra khỏi source bằng đếm ngoặc rồi eval — cùng tinh thần
 * với cách các test Python lift hàm khỏi adapter.py.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));

async function loadParseContent() {
  const src = await fs.readFile(path.join(HERE, "server.js"), "utf8");
  const start = src.indexOf("function parseContent(");
  assert.notEqual(start, -1, "parseContent not found in server.js");
  let depth = 0;
  let i = src.indexOf("{", start);
  const open = i;
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") {
      depth--;
      if (depth === 0) break;
    }
  }
  const body = src.slice(start, i + 1);
  assert.ok(open > 0 && body.endsWith("}"));
  return new Function(`${body}; return parseContent;`)();
}

const parseContent = await loadParseContent();

test("missed call: action chứa call + miss", () => {
  const c = parseContent({
    action: "recommened.rmsg.call.miss",
    title: "Cuộc gọi thoại",
    href: "https://zalo.me/call",
    thumb: "https://zalo.me/t.png",
  });
  assert.equal(c.kind, "call");
  assert.equal(c.missed, true);
  assert.equal(c.video, false);
  assert.equal(c.action, "recommened.rmsg.call.miss");
});

test("video call nhỡ", () => {
  const c = parseContent({ action: "recommened.rmsg.video.call.miss" });
  assert.equal(c.kind, "call");
  assert.equal(c.missed, true);
  assert.equal(c.video, true);
});

test("cuộc gọi đã kết nối: là call nhưng missed=false", () => {
  const c = parseContent({ action: "recommened.rmsg.call.connected" });
  assert.equal(c.kind, "call");
  assert.equal(c.missed, false);
});

test("call thắng nhánh link-preview (title+href không nuốt mất action)", () => {
  const c = parseContent({ action: "call.miss", title: "x", href: "http://a.b" });
  assert.equal(c.kind, "call");
});

test("link preview thường vẫn là text", () => {
  const c = parseContent({ title: "xem cái này", href: "http://a.b", description: "d" });
  assert.equal(c.kind, "text");
  assert.equal(c.link_href, "http://a.b");
});

test("text thường không đổi", () => {
  assert.deepEqual(parseContent("chào em"), { kind: "text", text: "chào em" });
  assert.equal(parseContent({ text: "hi" }).kind, "text");
});

test("ảnh / file không bị nhận nhầm", () => {
  assert.equal(parseContent({ thumbUrl: "http://a/t.jpg" }).kind, "image");
  assert.equal(parseContent({ fileUrl: "http://a/f.pdf", fileName: "f.pdf" }).kind, "file");
});

test("action không liên quan tới call thì bỏ qua nhánh mới", () => {
  const c = parseContent({ action: "recommened.rmsg.sticker", title: "t" });
  assert.equal(c.kind, "text");
});
