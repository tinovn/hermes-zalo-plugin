"""Logic phễu marketing Zalo — store / quota / schedule / select / sidecar /
sheet / content-gen prompt.

QUAN TRỌNG: module này KHÔNG import gateway → test cục bộ được (pytest).
Adapter (`adapter.py`) import module này và wiring tool + vòng nền nhỏ giọt,
truyền vào callable gọi LLM của gateway và hàm load OAuth Google.

Các file dữ liệu lưu tại thư mục session Zalo (mặc định /opt/data/zalo):
  marketing_campaigns.json  — chiến dịch
  marketing_leads.json      — lead theo chiến dịch
  marketing_quota.json      — đếm thao tác/ngày + override hôm nay
  marketing_queue.json      — hàng đợi nhỏ giọt (đã duyệt, chờ tới giờ gửi)
  marketing_settings.json   — hạn mức mặc định + auto_accept + cửa sổ gửi
  marketing_batches.json    — đợt soạn sẵn chờ sếp duyệt
"""
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Khung mặc định của một lead. add_leads chỉ giữ các khoá này.
_LEAD_DEFAULTS = {
    "uid": "", "name": "", "phone": None, "avatar": "", "source": "",
    "is_friend": False, "status": "new", "labels": [], "note": "",
    "invited_at": None, "accepted_at": None, "messaged_at": None, "last_error": None,
}

_SHEET_HEADER = [
    "STT", "Tên", "uid", "SĐT", "Nguồn", "Trạng thái", "Nhãn",
    "Đã mời", "Đã đồng ý", "Đã nhắn", "Ghi chú",
]


def _atomic_write(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _read(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


# Thứ hạng trạng thái để khi gộp lead trùng uid (ở nhiều nhóm) lấy trạng
# thái "tiến xa nhất" trong phễu.
_STATUS_RANK = {"new": 0, "skipped": 0, "blocked": 1, "invited": 2,
                "accepted": 3, "messaged": 4, "replied": 5, "closed": 6}

_MASTER_HEADER = ["STT", "Tên", "uid", "SĐT", "Nhóm nguồn", "Vai trò",
                  "Trạng thái", "Đã mời", "Đã đồng ý", "Đã nhắn", "Nhãn", "Ghi chú"]

# Trạng thái → chữ tiếng Việt dễ đọc trong Sheet.
_STATUS_VI = {"new": "Mới", "invited": "Đã gửi kết bạn", "accepted": "Đã là bạn",
              "messaged": "Đã nhắn", "replied": "Đã trả lời", "closed": "Chốt",
              "blocked": "Bị chặn", "skipped": "Bỏ qua"}


def merge_leads(campaigns_leads):
    """Gộp lead từ TẤT CẢ chiến dịch thành 1 view theo uid (cho sheet chung).
    Trùng uid (người ở nhiều nhóm) → gộp nguồn, lấy trạng thái tiến xa nhất,
    gộp nhãn/vai trò. Trả dict uid -> lead đã gộp (có khoá 'sources' = chuỗi)."""
    out = {}
    for cid, leads in (campaigns_leads or {}).items():
        for l in leads:
            uid = str(l.get("uid") or "")
            if not uid:
                continue
            cur = out.get(uid)
            if cur is None:
                cur = {"uid": uid, "name": "", "phone": None, "status": "new",
                       "is_friend": False, "labels": [], "note": "",
                       "invited_at": None, "accepted_at": None, "messaged_at": None,
                       "_sources": set()}
                out[uid] = cur
            src = l.get("source") or cid
            if src:
                cur["_sources"].add(str(src))
            if _STATUS_RANK.get(l.get("status"), 0) > _STATUS_RANK.get(cur.get("status"), 0):
                cur["status"] = l.get("status")
            for k in ("phone", "name", "invited_at", "accepted_at", "messaged_at", "note"):
                if not cur.get(k) and l.get(k):
                    cur[k] = l.get(k)
            if l.get("is_friend"):
                cur["is_friend"] = True
            for lb in (l.get("labels") or []):
                if lb not in cur["labels"]:
                    cur["labels"].append(lb)
    for v in out.values():
        v["sources"] = ", ".join(sorted(s for s in v.pop("_sources", set()) if s))
    return out


def build_master_rows(merged):
    """Dựng hàng cho Sheet CHUNG từ dict uid->lead đã gộp."""
    rows = []
    items = sorted(merged.values(), key=lambda l: (-_STATUS_RANK.get(l.get("status"), 0), l.get("name", "")))
    for i, l in enumerate(items, 1):
        role = ", ".join(lb for lb in (l.get("labels") or []) if lb in ("Chủ nhóm", "Phó nhóm"))
        other_labels = ", ".join(lb for lb in (l.get("labels") or []) if lb not in ("Chủ nhóm", "Phó nhóm", "Thành viên"))
        rows.append([
            i, l.get("name", ""), str(l.get("uid", "")), l.get("phone") or "",
            l.get("sources", ""), role or "Thành viên",
            _STATUS_VI.get(l.get("status"), l.get("status", "")),
            l.get("invited_at") or "", l.get("accepted_at") or "", l.get("messaged_at") or "",
            other_labels, l.get("note", ""),
        ])
    return _MASTER_HEADER, rows


# ─── Hàm thuần: gộp trùng thành viên ─────────────────────────────────────
def _dedupe_members(members):
    seen, uniq = set(), []
    for m in members:
        u = str(m.get("uid") or "")
        if u and u not in seen:
            seen.add(u)
            uniq.append(m)
    return uniq


# ─── Hàm thuần: lập lịch & chọn lead ─────────────────────────────────────
def compute_schedule(n, window_sec, now, jitter=0.5):
    """Rải n tác vụ đều trong cửa sổ window_sec kể từ now, cộng nhiễu ngẫu
    nhiên ±jitter*step. Trả list timestamp tăng dần nghiêm ngặt."""
    if n <= 0:
        return []
    step = window_sec / float(n)
    out = []
    for i in range(n):
        base = now + step * (i + 0.5)
        if jitter:
            base += random.uniform(-step * jitter, step * jitter)
        out.append(int(max(now, base)))
    out.sort()
    for i in range(1, len(out)):
        if out[i] <= out[i - 1]:
            out[i] = out[i - 1] + 1
    return out


def select_leads(leads, target, count):
    """Chọn lead theo đối tượng:
      strangers — status=new và chưa là bạn
      friends   — đã là bạn hoặc status=accepted
      new       — status=new (bất kể bạn hay chưa)
      all       — tất cả
    Giới hạn `count` (0/None = không giới hạn)."""
    def ok(l):
        if target == "strangers":
            return l.get("status") == "new" and not l.get("is_friend")
        if target == "friends":
            return bool(l.get("is_friend")) or l.get("status") == "accepted"
        if target == "new":
            return l.get("status") == "new"
        return True
    picked = [l for l in leads if ok(l)]
    return picked[:count] if count else picked


# ─── Hàm thuần: dựng Sheet & prompt sinh nội dung ────────────────────────
def build_lead_rows(leads):
    rows = []
    for i, l in enumerate(leads, 1):
        rows.append([
            i, l.get("name", ""), str(l.get("uid", "")), l.get("phone") or "",
            l.get("source", ""), l.get("status", ""), ",".join(l.get("labels", []) or []),
            l.get("invited_at") or "", l.get("accepted_at") or "", l.get("messaged_at") or "",
            l.get("note", ""),
        ])
    return _SHEET_HEADER, rows


def build_gen_prompt(brief, lead, kind):
    name = lead.get("name") or "bạn"
    verb = "lời mời kết bạn ngắn" if kind == "friend" else "tin nhắn tiếp cận"
    return (
        f"Viết MỘT {verb} bằng tiếng Việt có dấu, thân thiện, tự nhiên như người thật "
        f"nhắn, KHÔNG sáo rỗng, KHÔNG giống quảng cáo hàng loạt. Xưng hô phù hợp, có thể "
        f"nhắc tên người nhận.\n"
        f"Tên người nhận: {name}\n"
        f"Bối cảnh chiến dịch: {brief}\n"
        f"Chỉ trả về đúng nội dung tin, tối đa 280 ký tự, không kèm giải thích."
    )


# ─── Kho dữ liệu ─────────────────────────────────────────────────────────
class MarketingStore:
    _DEFAULT_SETTINGS = {
        "daily_friend_cap": 30, "daily_msg_cap": 40, "auto_accept": True,
        "send_window_sec": 86400, "jitter": 0.5,
        "scan_window_sec": 86400,  # rải các trang quét đều trong 24h
    }

    def __init__(self, base_dir):
        b = Path(base_dir)
        self.campaigns_p = b / "marketing_campaigns.json"
        self.leads_p = b / "marketing_leads.json"
        self.quota_p = b / "marketing_quota.json"
        self.queue_p = b / "marketing_queue.json"
        self.settings_p = b / "marketing_settings.json"
        self.batches_p = b / "marketing_batches.json"

    # campaigns
    def list_campaigns(self):
        return _read(self.campaigns_p, {})

    def get_campaign(self, cid):
        return self.list_campaigns().get(cid)

    def upsert_campaign(self, cid, **kw):
        c = self.list_campaigns()
        cur = c.get(cid, {})
        cur.update({"id": cid, "status": cur.get("status", "active"), **kw})
        c[cid] = cur
        _atomic_write(self.campaigns_p, c)
        return cur

    # leads
    def _all_leads(self):
        return _read(self.leads_p, {})

    def get_leads(self, cid):
        return self._all_leads().get(cid, [])

    def add_leads(self, cid, leads):
        alld = self._all_leads()
        bucket = alld.get(cid, [])
        seen = {l["uid"] for l in bucket}
        added = 0
        for l in leads:
            uid = str(l.get("uid") or "")
            if not uid or uid in seen:
                continue
            row = dict(_LEAD_DEFAULTS)
            row.update({k: v for k, v in l.items() if k in _LEAD_DEFAULTS})
            row["uid"] = uid
            bucket.append(row)
            seen.add(uid)
            added += 1
        alld[cid] = bucket
        _atomic_write(self.leads_p, alld)
        return added

    def update_lead(self, cid, uid, **changes):
        alld = self._all_leads()
        bucket = alld.get(cid, [])
        for l in bucket:
            if l["uid"] == str(uid):
                l.update(changes)
                break
        alld[cid] = bucket
        _atomic_write(self.leads_p, alld)

    def count_by_status(self, cid):
        out = {}
        for l in self.get_leads(cid):
            out[l["status"]] = out.get(l["status"], 0) + 1
        return out

    def find_lead_campaign(self, uid):
        """Tìm chiến dịch chứa uid (dùng khi cập nhật trạng thái từ event)."""
        for cid, bucket in self._all_leads().items():
            for l in bucket:
                if l["uid"] == str(uid):
                    return cid
        return None

    # settings
    def get_settings(self):
        s = dict(MarketingStore._DEFAULT_SETTINGS)
        s.update(_read(self.settings_p, {}))
        return s

    def update_settings(self, **kw):
        s = _read(self.settings_p, {})
        s.update(kw)
        _atomic_write(self.settings_p, s)
        return self.get_settings()

    # quota
    def _quota(self):
        return _read(self.quota_p, {})

    def _cap(self, kind, date):
        q = self._quota()
        today = q.get(date, {})
        ov = today.get(kind + "_cap_override")
        if ov is not None:
            return int(ov)
        s = self.get_settings()
        return s["daily_friend_cap"] if kind == "friend" else s["daily_msg_cap"]

    def set_today_cap(self, kind, date, cap):
        q = self._quota()
        q.setdefault(date, {})[kind + "_cap_override"] = int(cap)
        _atomic_write(self.quota_p, q)

    def incr(self, kind, date):
        q = self._quota()
        q.setdefault(date, {})
        q[date][kind] = q[date].get(kind, 0) + 1
        _atomic_write(self.quota_p, q)

    def used(self, kind, date):
        return self._quota().get(date, {}).get(kind, 0)

    def remaining(self, kind, date):
        return max(0, self._cap(kind, date) - self.used(kind, date))

    # batches + queue
    def _batches(self):
        return _read(self.batches_p, {})

    def create_batch(self, cid, kind, items):
        bs = self._batches()
        bid = f"{kind}-{cid}-{int(time.time() * 1000)}-{random.randint(100, 999)}"
        bs[bid] = {"id": bid, "campaign": cid, "kind": kind, "items": items,
                   "approved": False, "created": time.time()}
        _atomic_write(self.batches_p, bs)
        return bid

    def get_batch(self, bid):
        return self._batches().get(bid)

    def attach_drafts(self, bid, drafts):
        """Gắn nội dung do agent sinh vào batch (khớp theo uid). Chỉ giữ
        item có nội dung. Trả list item cuối cùng hoặc None nếu không có
        batch."""
        bs = self._batches()
        b = bs.get(bid)
        if not b:
            return None
        by_uid = {str(d.get("uid")): d for d in (drafts or [])}
        items = []
        for it in b["items"]:
            d = by_uid.get(str(it["uid"]))
            if not d:
                continue
            content = str(d.get("content", "")).strip()
            images = [str(x) for x in (d.get("images") or []) if x]
            if content or images:
                items.append({"uid": it["uid"], "name": it.get("name", ""),
                              "content": content, "images": images})
        b["items"] = items
        _atomic_write(self.batches_p, bs)
        return items

    def approve_batch(self, bid, schedule_ts):
        bs = self._batches()
        b = bs.get(bid)
        if not b:
            return False
        b["approved"] = True
        _atomic_write(self.batches_p, bs)
        q = _read(self.queue_p, [])
        for it, ts in zip(b["items"], schedule_ts):
            q.append({"id": f"{bid}:{it['uid']}", "batch": bid, "campaign": b["campaign"],
                      "kind": b["kind"], "uid": it["uid"], "content": it.get("content", ""),
                      "images": it.get("images") or [], "run_after": int(ts)})
        _atomic_write(self.queue_p, q)
        return True

    def enqueue_tasks(self, tasks):
        """Đẩy thẳng nhiều tác vụ vào hàng đợi (dùng cho quét nhỏ giọt)."""
        q = _read(self.queue_p, [])
        q.extend(tasks)
        _atomic_write(self.queue_p, q)

    def due_tasks(self, now):
        return [t for t in _read(self.queue_p, []) if t.get("run_after", 0) <= now]

    # Sheet chung (master) + cờ "cần đồng bộ"
    def get_master_sheet_id(self):
        return self.get_settings().get("master_sheet_id")

    def set_master_sheet_id(self, sid):
        self.update_settings(master_sheet_id=sid)

    def mark_master_dirty(self):
        self.update_settings(master_dirty=True)

    def is_master_dirty(self):
        return bool(self.get_settings().get("master_dirty"))

    def clear_master_dirty(self):
        self.update_settings(master_dirty=False)

    def merged_leads(self):
        return merge_leads(self._all_leads())

    def mark_task_done(self, task_id):
        q = [t for t in _read(self.queue_p, []) if t.get("id") != task_id]
        _atomic_write(self.queue_p, q)

    def pending_count(self, kind=None):
        return len([t for t in _read(self.queue_p, []) if kind is None or t.get("kind") == kind])


# ─── Client HTTP tới sidecar Node ────────────────────────────────────────
class SidecarClient:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{int(port)}"

    def _get(self, path, timeout=60):
        try:
            with urllib.request.urlopen(self.base + path, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            # Sidecar trả body JSON {"error": ...} kèm status 4xx/5xx — đọc lại.
            try:
                return json.loads(e.read().decode("utf-8", "replace"))
            except Exception:
                return {"error": f"HTTP {e.code}"}

    def _post(self, path, body, timeout=60):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8", "replace"))
            except Exception:
                return {"error": f"HTTP {e.code}"}

    def scan_group(self, link, max_pages=60, page_delay=(2.0, 5.0), sleep_fn=time.sleep):
        """Quét thành viên nhóm theo link, phân trang 100 người/trang.

        QUAN TRỌNG: chạy TỪ TỪ — nghỉ ngẫu nhiên ``page_delay`` (giây) giữa
        các trang để Zalo không thấy một loạt request dồn dập (tránh bị
        'Retry limit'). Nếu gặp lỗi giữa chừng → DỪNG ngay, trả những gì
        đã lấy được + cờ partial (không bắn tiếp làm Zalo bóp nặng hơn)."""
        members, meta, page = [], None, 1
        while page <= max_pages:
            d = self._get("/group/link-info?link=" + urllib.parse.quote(link) + f"&page={page}")
            if not d.get("ok"):
                return {"error": d.get("error", "unknown"), "meta": meta,
                        "members": _dedupe_members(members), "partial": page > 1}
            if meta is None:
                meta = {k: d.get(k) for k in
                        ("group_id", "name", "total_member", "lock_view_member", "admin_ids", "creator_id")}
            members.extend(d.get("members") or [])
            if not d.get("has_more") or not d.get("members"):
                break
            page += 1
            # Nghỉ ngẫu nhiên trước khi sang trang kế (chạy từ từ).
            lo, hi = page_delay
            sleep_fn(random.uniform(lo, hi))
        return {"meta": meta, "members": _dedupe_members(members)}

    def scan_page(self, link, page=1):
        """Lấy MỘT trang thành viên (cho quét nhỏ giọt nền). Trả raw dict."""
        return self._get("/group/link-info?link=" + urllib.parse.quote(link) + f"&page={page}")

    def lookup_phones(self, phones):
        return self._post("/users/by-phones", {"phones": list(phones)})

    def friend_request(self, uid, msg=""):
        return self._post("/friend/request", {"uid": str(uid), "msg": msg})

    def friend_sent(self):
        return self._get("/friend/sent")

    def friend_accept(self, uid):
        return self._post("/friend/accept", {"uid": str(uid)})

    def get_all_friends(self):
        return self._get("/friends/all")

    def send_text(self, thread_id, text, thread_type="user"):
        return self._post("/send/text", {"thread_id": str(thread_id), "text": text, "thread_type": thread_type})

    def send_media(self, thread_id, caption, images, thread_type="user"):
        """Gửi 1 tin kèm nhiều ảnh (images = list URL hoặc đường dẫn file)."""
        return self._post("/send/media", {"thread_id": str(thread_id), "thread_type": thread_type,
                                          "caption": caption, "images": list(images)})


# ─── Helper tạo Google Sheet (google libs import trong hàm, creds injectable) ─
def create_lead_sheet(title, header, rows, creds_loader):
    """creds_loader() -> (creds, err). Trả (url, None) hoặc (None, err)."""
    creds, err = creds_loader()
    if err:
        return None, err
    try:
        from googleapiclient.discovery import build
    except ImportError as e:
        return None, f"googleapiclient chưa cài: {e}"
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    ss = sheets.spreadsheets().create(body={"properties": {"title": title}}).execute()
    sid = ss["spreadsheetId"]
    sheets.spreadsheets().values().update(
        spreadsheetId=sid, range="A1", valueInputOption="RAW",
        body={"values": [header] + rows}).execute()
    sheets.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": [
        {"repeatCell": {"range": {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold"}},
        {"updateSheetProperties": {"properties": {"sheetId": 0, "gridProperties": {"frozenRowCount": 1}},
                                   "fields": "gridProperties.frozenRowCount"}},
    ]}).execute()
    drive.permissions().create(fileId=sid, body={"role": "reader", "type": "anyone"}).execute()
    return f"https://docs.google.com/spreadsheets/d/{sid}/edit?usp=sharing", None


def overwrite_lead_sheet(sheet_id, header, rows, creds_loader):
    """Ghi đè vùng giá trị của một Sheet đã có (dùng cho đồng bộ)."""
    creds, err = creds_loader()
    if err:
        return False, err
    try:
        from googleapiclient.discovery import build
    except ImportError as e:
        return False, f"googleapiclient chưa cài: {e}"
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    sheets.spreadsheets().values().clear(spreadsheetId=sheet_id, range="A:Z").execute()
    sheets.spreadsheets().values().update(
        spreadsheetId=sheet_id, range="A1", valueInputOption="RAW",
        body={"values": [header] + rows}).execute()
    return True, None
