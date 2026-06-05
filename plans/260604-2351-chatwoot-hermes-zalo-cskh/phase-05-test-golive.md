# Phase 05 — Test & go-live chạy thử

**Priority:** P1 · **Status:** ⬜ chưa · **Depends:** P4

## Mục tiêu
Verify toàn luồng, chuẩn bị go-live auto support có người trực mute. **Qua tuần.**

## Test checklist
- [ ] Khách Zalo nhắn text → AI trả lời tự động (status pending).
- [ ] Khách gửi ảnh → bridge relay vào Chatwoot, AI xử lý (hoặc báo nhận ảnh).
- [ ] Support reply trong Chatwoot → AI ngừng (status open). Khách nhận tin support.
- [ ] Gắn label `mute-ai` lúc pending → AI ngừng ngay.
- [ ] Gỡ mute / resolve→reopen về pending → AI trả lời lại (xác nhận hành vi mong muốn).
- [ ] Không loop/echo (AI không tự trả lời tin của chính nó hay tin support).
- [ ] Restart từng service (chatwoot, bridge, hermes) → tự phục hồi, không mất tin (durable queue bridge).
- [ ] QR Zalo hết hạn → cảnh báo rõ trong Chatwoot (bridge có notify).

## Vận hành
- Giám sát: `journalctl -u zca-bridge -u chatwoot-web.1 -f`, log Hermes gateway, bridge admin logs.
- Quy trình team: ban đầu **mute nhiều** (human trả), mở dần cho AI khi tốt.
- "Tự đặt hàng": chờ MCP của user → AI tự xử lý qua Hermes (ngoài scope tao).

## Go-live tuần sau
- Bật Agent Bot trên inbox thật.
- Có người trực sẵn sàng bấm mute.
- Theo dõi tỉ lệ AI trả đúng → quyết định mở rộng.

## Rollback
- Tắt Agent Bot khỏi inbox → quay về 100% human (Chatwoot + bridge vẫn chạy).
- Tắt plugin trong Hermes → AI im hoàn toàn.

## Success criteria
- Luồng end-to-end ổn định ≥ 24h.
- Mute hoạt động đúng 100% các case test.
