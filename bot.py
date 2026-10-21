#!/usr/bin/env python3
"""Mahesh Avi Bot (@Mahesh_avi_bot) — Raxi Win se alag, independent bot.

Features:
- /start: intro video (start-video.mp4) + James ka diya welcome text + Join Channel button
- Proof drip: one-time items (intro text, proof videos, pitch text) -> registration
  link (Register Now button) -> proof photos; har item har user ko SIRF EK BAAR,
  items ke beech 3 ghante ka gap — kuch bhi repeat nahi hota
- Naya subscriber aane par admin ko uski details (naam, username, ID) ka alert
- /channel, /help, /stats (sirf admin — total subscribers + recent list)
Stdlib only. Short polling.
RENDER BUILD: token BOT_TOKEN env se; state (users/offset/file_ids) Upstash
Redis me (env na ho to local files); uploads pure-Python multipart (curl
nahi chahiye); Render ke liye chhota health HTTP server bhi chalta hai.
"""
import json
import os
import re
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

BASE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(BASE, ".token")
VIDEO = os.path.join(BASE, "start-video.mp4")
USERS_FILE = os.path.join(BASE, "users.json")
ADMIN_FILE = os.path.join(BASE, "admin_id.txt")
OFFSET_FILE = os.path.join(BASE, "offset.txt")
FILEID_FILE = os.path.join(BASE, "file_ids.json")
PROOFS_DIR = os.path.join(BASE, "proofs")

# ---------- Channel auto-posting (James ke bheje hue posts) ----------
# Channel: Aviasoft VIP CLUB (@aviasoft_vipclub). Bot wahan admin hai.
# Queue postqueue/queue.json se seed hoti hai, state Upstash me rehta hai.
# Admin (James) bot ko DM karke override kar sakta hai: /poston /postoff
# /postnow /poststatus. Posting AUTO hai — deploy ke ~2 min baad pehla
# post, phir har 30 min me ek. /postoff se kabhi bhi ruk jayegi.
POST_CHANNEL_ID = int(os.environ.get("POST_CHANNEL_ID", "-1004453476874"))
POST_GAP = 1800        # channel posts ke beech 30 minute ka gap
POSTQUEUE_DIR = os.path.join(BASE, "postqueue")
POSTQUEUE_FILE = os.path.join(BASE, "postqueue.json")
POSTSTATE_FILE = os.path.join(BASE, "poststate.json")
# Purani queue (#1-#6) channel par ja chuki hai — inhe kabhi dobara
# seed/sync mat karo, warna repeat posts jayenge. Naye posts hamesha
# isse bade number (n) se aayenge, aur sync sirf wahi jodta hai.
POSTQUEUE_BASELINE_N = 6

CHANNEL_URL = "https://t.me/+ZJOQDxH9C985Mzdl"

PROOF_GAP = 10800      # drip items ke beech 3 ghante — kuch bhi repeat nahi hota

WELCOME_TEXT = (
    "Make sure to subscribe to my channel\n"
    "I'm documenting my journey to 10,00,0000 rupees LIVE.\n"
    "You'll find the event announcement right after the pinned message in the channel👇🏽🚀\n\n"
    + CHANNEL_URL
)

JOIN_KB = {"inline_keyboard": [[{"text": "📢 Join Channel", "url": CHANNEL_URL}]]}

# Har proof photo ke niche ye caption (channel link ke saath) jayega
LINK_CAPTION = "Join my channel 👇🏽🚀\n" + CHANNEL_URL

# Registration link (James ne diya) — pitch ke baad har subscriber ko ek baar,
# Register Now button ke saath
REG_URL = "https://1wxnxg.life/betting?p=b0nq&sharebet=MAHESH85"
REG_TEXT = "🎰 Register Now — abhi apna account banao aur shuru karo 👇\n\n" + REG_URL
REG_KB = {"inline_keyboard": [[{"text": "🟢 Register Now", "url": REG_URL}]]}

DESCRIPTION = (
    "Welcome to Mahesh's official bot 🚀\n"
    "I'm documenting my journey to 10,00,0000 rupees LIVE.\n"
    "Subscribe to the channel for event announcements and updates — "
    "you'll find the event announcement right after the pinned message in the channel."
)
SHORT_DESCRIPTION = "Mahesh LIVE — journey to ₹1 Crore 🚀 Subscribe for event announcements & updates."


def load_token():
    t = os.environ.get("BOT_TOKEN", "").strip()
    if t:
        return t
    if not os.path.exists(TOKEN_FILE):
        print("TOKEN MISSING — BOT_TOKEN env bhi nahi aur .token file bhi nahi, bot band.", flush=True)
        raise SystemExit(1)
    return open(TOKEN_FILE).read().strip()


TOKEN = load_token()
API = "https://api.telegram.org/bot" + TOKEN


def api(method, payload=None, retries=4):
    data = json.dumps(payload or {}).encode()
    for i in range(retries):
        try:
            req = urllib.request.Request(
                API + "/" + method, data=data,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            print(f"api {method} try {i + 1}/{retries} failed: {e}", flush=True)
            time.sleep(3)
    return {"ok": False}


# ---------- State store: Upstash Redis (Render) ya local files ----------
# Render ka disk temporary hota hai — restart par saaf ho jata hai. Isliye
# subscribers ka data Upstash Redis (free) me rakha hai taaki kuch bhi
# repeat na ho aur data kabhi na khoye. Pehli baar local seed files
# (repo me rakhi users.json/offset.txt/file_ids.json) se data uthakar
# Redis me daal diya jata hai. Redis env na ho to purana local-file
# behaviour hi chalta hai.
REDIS_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
REDIS_ERR = object()


def redis_cmd(*args):
    """Upstash REST command. Result, None (khaali key), ya REDIS_ERR (fail)."""
    if not REDIS_URL or not REDIS_TOKEN:
        return REDIS_ERR
    try:
        data = json.dumps(list(args)).encode()
        req = urllib.request.Request(REDIS_URL, data=data, headers={
            "Authorization": "Bearer " + REDIS_TOKEN,
            "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode()).get("result")
    except Exception as e:
        print(f"redis error: {e}", flush=True)
        return REDIS_ERR


def store_get(key, local_path):
    """Pehle Redis, warna local seed file. Return: raw string ya None."""
    raw = redis_cmd("GET", "mahesh:" + key)
    if raw is not REDIS_ERR and raw is not None:
        return raw
    try:
        with open(local_path) as f:
            content = f.read()
    except Exception:
        return None
    if raw is None:  # Redis khaali tha — local seed se bhar do
        redis_cmd("SET", "mahesh:" + key, content)
    return content


def store_set(key, local_path, content):
    redis_cmd("SET", "mahesh:" + key, content)
    try:
        tmp = local_path + ".tmp"
        with open(tmp, "w") as f:
            f.write(content)
        os.replace(tmp, local_path)
    except Exception:
        pass  # Render par disk likhna best-effort hai, asli copy Redis me hai


def _multipart(method, fields, file_field, file_path, timeout=120):
    """Telegram file upload — pure Python multipart (curl par nirbhar nahi)."""
    boundary = "----maheshbot" + str(int(time.time() * 1000))
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f"name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    with open(file_path, "rb") as f:
        content = f.read()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; "
             f"name=\"{file_field}\"; filename=\"{os.path.basename(file_path)}\"\r\n"
             "Content-Type: application/octet-stream\r\n\r\n").encode()
    body += content + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        API + "/" + method, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def health_server():
    """Render free web service ke liye chhota HTTP server — 'OK' jawab deta
    hai; free pinger ke ping se service soti nahi rehti."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Mahesh bot is running")

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    port = int(os.environ.get("PORT", "10000"))
    HTTPServer(("0.0.0.0", port), H).serve_forever()


def send_message(chat_id, text, kb=None):
    p = {"chat_id": chat_id, "text": text}
    if kb:
        p["reply_markup"] = kb
    return api("sendMessage", p)


_fid_cache = None


def _fid_load():
    global _fid_cache
    if _fid_cache is None:
        raw = store_get("fileids", FILEID_FILE)
        try:
            _fid_cache = json.loads(raw) if raw else {}
        except Exception:
            _fid_cache = {}
    return _fid_cache


def _file_id(path):
    """Telegram par pehle se upload video ki file_id (cache) — dobara upload
    karne ke bajaye file_id se bhejna lagbhag instant hota hai."""
    cache = _fid_load()
    key = os.path.basename(path) + ":" + str(os.path.getsize(path))
    return cache.get(key)


def _cache_file_id(path, fid):
    cache = _fid_load()
    key = os.path.basename(path) + ":" + str(os.path.getsize(path))
    cache[key] = fid
    store_set("fileids", FILEID_FILE, json.dumps(cache))


def _send_video_by_id(chat_id, fid, caption):
    p = {"chat_id": chat_id, "video": fid, "supports_streaming": True,
         "reply_markup": JOIN_KB}
    if caption:
        p["caption"] = caption
    return api("sendVideo", p, retries=2)


def send_video(chat_id):
    """Intro video + welcome caption + Join Channel button.
    Pehle file_id se (instant), warna upload karke file_id cache karo."""
    fid = _file_id(VIDEO)
    if fid:
        r = _send_video_by_id(chat_id, fid, WELCOME_TEXT)
        if r.get("ok"):
            return True
    for i in range(4):
        try:
            r = _multipart("sendVideo",
                           {"chat_id": str(chat_id), "caption": WELCOME_TEXT,
                            "supports_streaming": "true",
                            "reply_markup": json.dumps(JOIN_KB)},
                           "video", VIDEO, timeout=90)
            if r.get("ok"):
                v = r["result"].get("video", {})
                if v.get("file_id"):
                    _cache_file_id(VIDEO, v["file_id"])
                return True
            print(f"sendVideo not ok: {str(r)[:200]}", flush=True)
        except Exception as e:
            print(f"sendVideo try {i + 1}/4 failed: {e}", flush=True)
        time.sleep(3)
    # Fallback: sirf text bhej do taaki user khali na jaye
    send_message(chat_id, WELCOME_TEXT, JOIN_KB)
    return False


def send_video_file(chat_id, path, caption):
    """Proof video + caption + Join Channel button. Return: True / False / 'blocked'.
    Pehle file_id se bhejo (instant) — upload sirf pehli baar hota hai."""
    fid = _file_id(path)
    if fid:
        r = _send_video_by_id(chat_id, fid, caption)
        if r.get("ok"):
            return True
        if r.get("error_code") == 403:
            return "blocked"
    for i in range(4):
        try:
            fields = {"chat_id": str(chat_id), "supports_streaming": "true",
                      "reply_markup": json.dumps(JOIN_KB)}
            if caption:
                fields["caption"] = caption
            r = _multipart("sendVideo", fields, "video", path, timeout=180)
            if r.get("ok"):
                v = r["result"].get("video", {})
                if v.get("file_id"):
                    _cache_file_id(path, v["file_id"])
                return True
            if r.get("error_code") == 403:
                return "blocked"
            print(f"sendVideoFile not ok: {str(r)[:200]}", flush=True)
        except Exception as e:
            print(f"sendVideoFile try {i + 1}/4 failed: {e}", flush=True)
        time.sleep(3)
    return False


def send_photo(chat_id, path, caption):
    """Proof image + caption. Return: True / False / 'blocked'."""
    for i in range(4):
        try:
            fields = {"chat_id": str(chat_id), "reply_markup": json.dumps(JOIN_KB)}
            if caption:
                fields["caption"] = caption
            r = _multipart("sendPhoto", fields, "photo", path, timeout=60)
            if r.get("ok"):
                return True
            if r.get("error_code") == 403:
                return "blocked"
            print(f"sendPhoto not ok: {str(r)[:200]}", flush=True)
        except Exception as e:
            print(f"sendPhoto try {i + 1}/4 failed: {e}", flush=True)
        time.sleep(3)
    return False


def load_sequence():
    """proofs/ folder se teen lists (order file naam se):
    - once_items: har user ko SIRF EK BAAR jane wale — text-only files
      ("text", content) aur videos ("video", path, caption)
    - photos: [(path, caption)] — drip me har ek sirf ek baar jata hai;
      caption same-naam .txt se, warna channel-link caption.
    - tail: 'tail-' se shuru hone wale items — drip ke AAKHIR me jaate hain
      (reg + photos ke baad). ('video'/'photo'/'text', path/content, caption)"""
    if not os.path.isdir(PROOFS_DIR):
        return [], [], []
    imgs, vids, texts = {}, {}, {}
    for f in os.listdir(PROOFS_DIR):
        stem, ext = os.path.splitext(f)
        ext = ext.lower()
        if ext in (".jpg", ".jpeg", ".png", ".webp"):
            imgs[stem] = os.path.join(PROOFS_DIR, f)
        elif ext in (".mp4", ".mov"):
            vids[stem] = os.path.join(PROOFS_DIR, f)
        elif ext == ".txt":
            texts[stem] = os.path.join(PROOFS_DIR, f)
    once, photos, tail = [], [], []
    for stem in sorted(set(imgs) | set(vids) | set(texts)):
        cap = open(texts[stem]).read().strip() if stem in texts else ""
        if stem.startswith("tail-"):
            if stem in imgs:
                tail.append(("photo", imgs[stem], cap or LINK_CAPTION))
            elif stem in vids:
                tail.append(("video", vids[stem], cap or LINK_CAPTION))
            else:
                tail.append(("text", cap, ""))
        elif stem in imgs:
            photos.append((imgs[stem], cap or LINK_CAPTION))
        elif stem in vids:
            once.append(("video", vids[stem], cap or LINK_CAPTION))
        else:
            once.append(("text", open(texts[stem]).read().strip(), ""))
    return once, photos, tail


def load_offset():
    """Saved getUpdates offset — restart ke baad bot wahin se aage padhta hai,
    poori history dobara process nahi karta. Redis ya local file se."""
    raw = store_get("offset", OFFSET_FILE)
    try:
        return int(str(raw).strip())
    except Exception:
        return None


def save_offset(v):
    store_set("offset", OFFSET_FILE, str(v))


_users_cache = None


def load_users():
    global _users_cache
    if _users_cache is not None:
        return _users_cache
    raw = store_get("users", USERS_FILE)
    try:
        _users_cache = json.loads(raw) if raw else {}
    except Exception:
        _users_cache = {}
    return _users_cache


def save_users(users):
    global _users_cache
    _users_cache = users
    store_set("users", USERS_FILE, json.dumps(users, indent=1))


def admin_id():
    v = os.environ.get("ADMIN_ID", "").strip()
    if v:
        try:
            return int(v)
        except Exception:
            pass
    try:
        return int(open(ADMIN_FILE).read().strip())
    except Exception:
        return None


def register_user(u):
    """User ko users.json me save karo. Return: (is_new, total_count)."""
    users = load_users()
    key = str(u["id"])
    is_new = key not in users
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    if is_new:
        users[key] = {
            "name": (u.get("first_name", "") + " " + u.get("last_name", "")).strip(),
            "username": u.get("username", ""),
            "language": u.get("language_code", ""),
            "first_seen": now,
            "starts": 1,
            "intro_done": False,
            "once_idx": 0,
            "reg_sent": False,
            "proof_idx": 0,
            "tail_idx": 0,
            "last_proof": 0,
            "blocked": False,
            # Onboarding (UID -> deposit -> channel), drip se alag parallel
            "uid": None,
            "uid_status": None,
            "uid_asked": False,
            "dep_status": None,
            "dep_asked": False,
            "chan_sent": False,
        }
    else:
        users[key]["starts"] = users[key].get("starts", 1) + 1
        users[key]["name"] = (u.get("first_name", "") + " " + u.get("last_name", "")).strip()
        users[key]["username"] = u.get("username", "")
    save_users(users)
    return is_new, len(users)


def md_escape(s):
    t = str(s)
    for ch in ("\\", "_", "*", "`", "[", "]"):
        t = t.replace(ch, "\\" + ch)
    return t


def notify_admin(u, total):
    aid = admin_id()
    if not aid:
        return
    name = md_escape((u.get("first_name", "") + " " + u.get("last_name", "")).strip() or "—")
    uname = md_escape("@" + u["username"]) if u.get("username") else "nahi hai"
    text = (
        "🔔 *New Subscriber!*\n\n"
        f"👤 Name: {name}\n"
        f"🆔 ID: `{u['id']}`\n"
        f"📱 Username: {uname}\n"
        f"🌐 Language: {u.get('language_code', '—')}\n"
        f"📊 Total Subscribers: {total}"
    )
    p = {"chat_id": aid, "text": text, "parse_mode": "Markdown"}
    api("sendMessage", p)


def send_text_item(chat_id, text):
    r = api("sendMessage", {"chat_id": chat_id, "text": text})
    if r.get("ok"):
        return True
    if r.get("error_code") == 403:
        return "blocked"
    return False


def send_reg(chat_id):
    """Registration link + Register Now button. Return: True / False / 'blocked'."""
    r = api("sendMessage", {"chat_id": chat_id, "text": REG_TEXT, "reply_markup": REG_KB})
    if r.get("ok"):
        return True
    if r.get("error_code") == 403:
        return "blocked"
    return False


def send_next_photo(chat_id, users, key, photos):
    """User ka agla proof photo — niche channel link caption + Join button.
    users dict in-place update. Return True agar gaya (ya block mark hua)."""
    d = users[key]
    idx = d.get("proof_idx", 0)
    path, cap = photos[idx % len(photos)]
    res = send_photo(chat_id, path, cap)
    if res == "blocked":
        d["blocked"] = True
        print(f"User {key} ne bot block kiya — drip band.", flush=True)
        return True
    if res:
        d["proof_idx"] = idx + 1
        d["last_proof"] = time.time()
        print(f"Proof photo #{idx + 1} -> user {key}", flush=True)
        return True
    return False


def send_once_item(chat_id, users, key, item):
    """One-time item (text ya video) bhejo — har user ko sirf ek baar.
    users dict in-place update. Return True agar state change hua."""
    d = users[key]
    if item[0] == "video":
        res = send_video_file(chat_id, item[1], item[2])
    else:
        res = send_text_item(chat_id, item[1])
    if res == "blocked":
        d["blocked"] = True
        d["once_idx"] = d.get("once_idx", 0) + 1
        print(f"User {key} ne bot block kiya — drip band.", flush=True)
        return True
    if res:
        d["once_idx"] = d.get("once_idx", 0) + 1
        d["intro_done"] = d["once_idx"] >= 1
        d["last_proof"] = time.time()
        print(f"Once item #{d['once_idx']} ({item[0]}) -> user {key}", flush=True)
        return True
    return False


def send_reg_item(chat_id, users, key):
    """Registration message — har user ko sirf ek baar, pitch ke baad.
    Return True agar state change hua."""
    d = users[key]
    res = send_reg(chat_id)
    if res == "blocked":
        d["blocked"] = True
        d["reg_sent"] = True
        print(f"User {key} ne bot block kiya — drip band.", flush=True)
        return True
    if res:
        d["reg_sent"] = True
        d["last_proof"] = time.time()
        print(f"Registration link -> user {key}", flush=True)
        # Reg ke saath hi UID step shuru: naya user register karke aaye to
        # UID bhej sake. (Purane users /verify se shuru kar sakte hain.)
        if send_message(chat_id, UID_REQUEST).get("ok"):
            d["uid_asked"] = True
        return True
    return False


# ---------- Automated onboarding: UID -> deposit -> channel ----------
# Flow (drip se alag, parallel chalta hai — drip kabhi nahi rukta):
# reg -> UID request -> user UID bhejta hai -> admin /approve ->
# deposit request (min Rs 1000) -> user screenshot bhejta hai ->
# admin /approve -> channel invite link. UID/deposit ka automatic
# verification possible nahi hai (1win ka public API nahi; screenshot se
# pakka proof nahi milta), isliye dono jagah admin ka 1-tap approval hai.
UID_REQUEST = ("✅ Register ho gaya? Ab apna 1win UID yahan bhej do — taaki hum "
               "verify kar sakein ki tum hamare link se register hue ho.\n\n"
               "UID tumhe 1win account me milega (profile section me).")
UID_RECEIVED = ("✅ Tumhara UID mil gaya hai — verification ke liye bhej diya gaya hai.\n\n"
                "Verify hote hi tumhe agla step bataya jayega, thoda wait karo.")
UID_REJECTED = ("❌ Tumhara UID verify nahi ho paya.\n\n"
                "Sahi UID dobara bhej do — 1win account me dikhne wala number.")
DEP_REQUEST = ("🎉 Tumhara UID verify ho gaya hai!\n\n"
               "Ab aage badhne ke liye minimum ₹1000 ka deposit karo, phir "
               "deposit ka screenshot yahan bhej do.\n\n"
               "Screenshot me amount aur date saaf dikhni chahiye.")
DEP_RECEIVED = ("✅ Deposit screenshot mil gaya hai — verification ke liye bhej diya gaya hai.\n\n"
                "Verify hote hi tumhe VIP channel ka link milega.")
DEP_REJECTED = ("❌ Deposit screenshot verify nahi ho paya.\n\n"
                "Sahi screenshot dobara bhej do — jisme amount aur date saaf dikhe.")
CHAN_INVITE = ("🎉 Deposit verify ho gaya hai! Ab tum VIP CLUB join kar sakte ho 👇\n\n"
               "Join karne ke baad /joined dabana, taaki hum confirm kar sakein.")
JOINED_OK = "🎉 Welcome to Aviasoft VIP CLUB! Tum channel ke member ban gaye ho. 🔥"
JOINED_NOT = "Tum abhi channel join nahi hue ho. Neeche diye link se join karo, phir /joined dabao 👇"
VERIFY_PENDING_UID = "⏳ Tumhara UID verification me hai — thoda wait karo, verify hote hi bataya jayega."
VERIFY_PENDING_DEP = "⏳ Tumhara deposit screenshot verification me hai — thoda wait karo."
VERIFY_DEP_ASKED = "💰 Tumhara UID approved hai. Ab minimum ₹1000 ka deposit karke uska screenshot yahan bhej do."
VERIFY_DONE = "✅ Tumhara onboarding poora ho chuka hai. Channel link ke liye /channel dabao."
UID_RE = re.compile(r"^\d{6,14}$")


def handle_verify(chat_id, users, key):
    """User ka /verify — onboarding me wo kahan hai, wahi batayo ya shuru karo."""
    d = users[key]
    if d.get("uid_status") == "approved" and d.get("dep_status") == "approved":
        send_message(chat_id, VERIFY_DONE)
    elif d.get("dep_status") == "pending":
        send_message(chat_id, VERIFY_PENDING_DEP)
    elif d.get("uid_status") == "pending":
        send_message(chat_id, VERIFY_PENDING_UID)
    elif d.get("dep_asked"):
        send_message(chat_id, VERIFY_DEP_ASKED)
    else:
        if send_message(chat_id, UID_REQUEST).get("ok"):
            d["uid_asked"] = True
            save_users(users)


def handle_joined(chat_id, users, key):
    """User ka /joined — channel membership check (bot channel ka admin hai)."""
    r = api("getChatMember", {"chat_id": POST_CHANNEL_ID, "user_id": int(key)})
    if not r.get("ok"):
        send_message(chat_id, "Check nahi ho paya — thodi der baad /joined dobara dabao.")
        return
    if r["result"].get("status") in ("creator", "administrator", "member"):
        send_message(chat_id, JOINED_OK)
    else:
        send_message(chat_id, JOINED_NOT, JOIN_KB)


def handle_uid_text(chat_id, users, key, uid_text):
    """User ne UID bheja — save karo, admin ko verification ke liye bhejo."""
    d = users[key]
    d["uid"] = uid_text
    d["uid_status"] = "pending"
    save_users(users)
    send_message(chat_id, UID_RECEIVED)
    aid = admin_id()
    if aid:
        name = d.get("name") or "—"
        uname = "@" + d["username"] if d.get("username") else "nahi hai"
        send_message(aid, f"🆔 UID Verification Pending\n\nName: {name}\nUser ID: {key}\nUsername: {uname}\nUID: {uid_text}\n\nApprove: /approve {key}\nReject: /reject {key}")


def handle_deposit_photo(chat_id, users, key, msg):
    """User ne deposit screenshot bheja — admin ko forward karo."""
    d = users[key]
    d["dep_status"] = "pending"
    save_users(users)
    send_message(chat_id, DEP_RECEIVED)
    aid = admin_id()
    if aid:
        name = d.get("name") or "—"
        uname = "@" + d["username"] if d.get("username") else "nahi hai"
        cap = (msg.get("caption") or "").strip()
        alert = (f"💰 Deposit Proof Pending\n\nName: {name}\nUser ID: {key}\nUsername: {uname}\nUID: {d.get('uid') or '—'}\n"
                 + (f"User ka caption: {cap}\n" if cap else "")
                 + f"\nApprove: /approve {key}\nReject: /reject {key}")
        send_message(aid, alert)
        api("forwardMessage", {"chat_id": aid, "from_chat_id": chat_id,
                               "message_id": msg["message_id"]})


def handle_admin_onboard(admin_chat, cmd, text):
    """Admin ke onboarding commands: /approve, /reject, /pending, /status.
    /approve stage-aware hai: pehle pending UID, phir pending deposit."""
    users = load_users()
    if cmd == "/pending":
        lines = []
        for uid, d in users.items():
            if d.get("uid_status") == "pending":
                lines.append(f"• {d.get('name') or '—'} — ID: {uid} — UID: {d.get('uid')} — Stage: UID")
            elif d.get("dep_status") == "pending":
                lines.append(f"• {d.get('name') or '—'} — ID: {uid} — UID: {d.get('uid')} — Stage: Deposit")
        send_message(admin_chat, "⏳ Pending Approvals:\n\n" + "\n".join(lines) if lines else "Koi pending approval nahi hai.")
        return
    parts = text.split()
    if len(parts) < 2:
        send_message(admin_chat, f"Usage: {cmd} <user_id>  (pending dekhne ke liye /pending)")
        return
    target = parts[1]
    if target not in users:
        send_message(admin_chat, "User nahi mila — sahi ID bhejo (dekhne ke liye /pending).")
        return
    d = users[target]
    if cmd == "/approve":
        if d.get("uid_status") == "pending":
            d["uid_status"] = "approved"
            d["dep_asked"] = True
            save_users(users)
            send_message(int(target), DEP_REQUEST)
            send_message(admin_chat, f"✅ UID approved ({target}) — user ko deposit request bhej diya.")
        elif d.get("dep_status") == "pending":
            d["dep_status"] = "approved"
            d["chan_sent"] = True
            save_users(users)
            send_message(int(target), CHAN_INVITE, JOIN_KB)
            send_message(admin_chat, f"✅ Deposit approved ({target}) — user ko channel invite link bhej diya.")
        else:
            send_message(admin_chat, f"Is user ka kuch pending nahi hai. (UID: {d.get('uid_status')}, Deposit: {d.get('dep_status')})")
    elif cmd == "/reject":
        if d.get("uid_status") == "pending":
            d["uid_status"] = "rejected"
            save_users(users)
            send_message(int(target), UID_REJECTED)
            send_message(admin_chat, f"❌ UID rejected ({target}) — user ko dobara bhejne ko bola gaya.")
        elif d.get("dep_status") == "pending":
            d["dep_status"] = "rejected"
            save_users(users)
            send_message(int(target), DEP_REJECTED)
            send_message(admin_chat, f"❌ Deposit rejected ({target}) — user ko dobara bhejne ko bola gaya.")
        else:
            send_message(admin_chat, "Is user ka kuch pending nahi hai.")
    elif cmd == "/status":
        send_message(admin_chat, f"👤 {d.get('name') or '—'} (@{d.get('username') or '—'})\nID: {target}\nUID: {d.get('uid') or '—'} ({d.get('uid_status') or 'nahi diya'})\nDeposit: {d.get('dep_status') or 'nahi diya'}\nChannel link: {'bheja gaya' if d.get('chan_sent') else 'nahi bheja'}\nDrip: once {d.get('once_idx', 0)}, reg {'ho gaya' if d.get('reg_sent') else 'nahi'}, photos {d.get('proof_idx', 0)}, tail {d.get('tail_idx', 0)}")


def send_tail_item(chat_id, users, key, item):
    """Tail item (drip ke aakhir me) bhejo — har user ko sirf ek baar.
    users dict in-place update. Return True agar state change hua."""
    d = users[key]
    kind = item[0]
    if kind == "video":
        res = send_video_file(chat_id, item[1], item[2])
    elif kind == "photo":
        res = send_photo(chat_id, item[1], item[2])
    else:
        res = send_text_item(chat_id, item[1])
    if res == "blocked":
        d["blocked"] = True
        d["tail_idx"] = d.get("tail_idx", 0) + 1
        print(f"User {key} ne bot block kiya — drip band.", flush=True)
        return True
    if res:
        d["tail_idx"] = d.get("tail_idx", 0) + 1
        d["last_proof"] = time.time()
        print(f"Tail item #{d['tail_idx']} ({kind}) -> user {key}", flush=True)
        return True
    return False


def proof_loop():
    """Background drip — kuch bhi repeat nahi hota. Order per user:
    one-time items (intro, proof videos, pitch) -> registration link ->
    proof photos (har ek sirf ek baar) -> tail items (drip ke aakhir me).
    Items ke beech 3 ghante ka gap.
    State users.json me — restart ke baad bhi wahin se aage badhta hai."""
    while True:
        try:
            once, photos, tail = load_sequence()
            if once or photos or tail:
                users = load_users()
                changed = False
                now = time.time()
                for key, d in list(users.items()):
                    if d.get("blocked"):
                        continue
                    due = now - d.get("last_proof", 0) >= PROOF_GAP
                    oi = d.get("once_idx", 0)
                    if oi < len(once):
                        # Pehla once item turant, baaki 3-ghante gap par
                        if oi == 0 or due:
                            if send_once_item(int(key), users, key, once[oi]):
                                changed = True
                        continue
                    if not d.get("reg_sent"):
                        if due and send_reg_item(int(key), users, key):
                            changed = True
                        continue
                    if photos and d.get("proof_idx", 0) < len(photos) and due:
                        if send_next_photo(int(key), users, key, photos):
                            changed = True
                    elif tail and d.get("tail_idx", 0) < len(tail) and due:
                        if send_tail_item(int(key), users, key, tail[d.get("tail_idx", 0)]):
                            changed = True
                if changed:
                    save_users(users)
        except Exception as e:
            print(f"proof loop error: {e}", flush=True)
        time.sleep(30)


_postqueue_cache = None
_poststate_cache = None


def load_postqueue():
    global _postqueue_cache
    if _postqueue_cache is not None:
        return _postqueue_cache
    raw = store_get("postqueue", POSTQUEUE_FILE)
    q = None
    try:
        q = json.loads(raw) if raw else None
    except Exception:
        q = None
    if q is None:
        # Pehli baar: repo se seed bharo — queue.json pehle repo ke root me
        # dekho, phir postqueue/ folder me (jahan bhi James ne dala ho).
        # Baseline se purane items (#1-#6) kabhi dobara seed mat karo —
        # wo channel par ja chuke hain, seed sirf naye posts ke liye hai.
        q = []
        for cand in (os.path.join(BASE, "queue.json"),
                     os.path.join(POSTQUEUE_DIR, "queue.json")):
            try:
                with open(cand) as f:
                    for item in json.load(f):
                        if item.get("n", 0) > POSTQUEUE_BASELINE_N:
                            q.append({"n": item["n"], "file": item["file"],
                                      "caption": item["caption"]})
                break
            except Exception:
                q = []
        if q:
            save_postqueue(q)
    _postqueue_cache = q
    return _postqueue_cache


def _queue_path(name):
    """Post ki media file ka asli path — postqueue/ me ya repo root me."""
    p = os.path.join(POSTQUEUE_DIR, name)
    if os.path.exists(p):
        return p
    return os.path.join(BASE, name)


def save_postqueue(q):
    global _postqueue_cache
    _postqueue_cache = q
    store_set("postqueue", POSTQUEUE_FILE, json.dumps(q))


def load_poststate():
    global _poststate_cache
    if _poststate_cache is not None:
        return _poststate_cache
    raw = store_get("poststate", POSTSTATE_FILE)
    try:
        _poststate_cache = json.loads(raw) if raw else {}
    except Exception:
        _poststate_cache = {}
    fresh = not _poststate_cache
    _poststate_cache.setdefault("on", True)
    _poststate_cache.setdefault("last_post", 0)
    _poststate_cache.setdefault("posted_total", 0)
    _poststate_cache.setdefault("seen_max", 0)
    if fresh:
        # AUTO posting: pehla post deploy ke ~2 minute baad jaye,
        # phir har 30 min me ek (last_post peeche rakhke gap ghata diya)
        _poststate_cache["last_post"] = time.time() - POST_GAP + 120
        save_poststate(_poststate_cache)
    return _poststate_cache


def save_poststate(s):
    global _poststate_cache
    _poststate_cache = s
    store_set("poststate", POSTSTATE_FILE, json.dumps(s))


def sync_postqueue_from_seed():
    """Repo ki queue.json me jo NAYE items aaye (n > baseline aur n >
    seen_max), unhe live queue ke aakhir me jodo. Isse James jab bhi
    naya post queue.json me daalega, wo apne aap live queue me jud
    jayega — aur purane posts (#1-#6) kabhi dobara nahi judenge."""
    try:
        seed = None
        for cand in (os.path.join(BASE, "queue.json"),
                     os.path.join(POSTQUEUE_DIR, "queue.json")):
            try:
                with open(cand) as f:
                    seed = json.load(f)
                break
            except Exception:
                seed = None
        if not seed:
            return
        q = load_postqueue()
        st = load_poststate()
        seen = max(st.get("seen_max", 0), POSTQUEUE_BASELINE_N,
                   max((x["n"] for x in q), default=0))
        have = {x["n"] for x in q}
        added = False
        for item in seed:
            n = item.get("n")
            if n is None or n <= seen or n in have:
                continue
            q.append({"n": n, "file": item["file"],
                      "caption": item["caption"]})
            have.add(n)
            added = True
        if added:
            save_postqueue(q)
            print(f"Queue sync: naye posts jud gaye, queue ab {len(q)}", flush=True)
        st["seen_max"] = max(seen, max((x["n"] for x in q), default=seen))
        save_poststate(st)
    except Exception as e:
        print(f"queue sync error: {e}", flush=True)


def send_channel_photo(chat_id, path, caption):
    """Channel par photo + caption. Caption me shabd hi link hote hain
    (HTML parse_mode) — koi lambi link text me nahi dikhti."""
    for i in range(4):
        try:
            fields = {"chat_id": str(chat_id), "parse_mode": "HTML"}
            if caption:
                fields["caption"] = caption
            r = _multipart("sendPhoto", fields, "photo", path, timeout=60)
            if r.get("ok"):
                return True
            print(f"channel sendPhoto not ok: {str(r)[:200]}", flush=True)
        except Exception as e:
            print(f"channel sendPhoto try {i + 1}/4 failed: {e}", flush=True)
        time.sleep(3)
    return False


def send_channel_video(chat_id, path, caption):
    """Channel par video + caption. Pehle file_id se (instant), warna upload."""
    fid = _file_id(path)
    if fid:
        p = {"chat_id": chat_id, "video": fid, "supports_streaming": True,
             "parse_mode": "HTML"}
        if caption:
            p["caption"] = caption
        r = api("sendVideo", p, retries=2)
        if r.get("ok"):
            return True
    for i in range(4):
        try:
            fields = {"chat_id": str(chat_id), "supports_streaming": "true",
                      "parse_mode": "HTML"}
            if caption:
                fields["caption"] = caption
            r = _multipart("sendVideo", fields, "video", path, timeout=180)
            if r.get("ok"):
                v = r["result"].get("video", {})
                if v.get("file_id"):
                    _cache_file_id(path, v["file_id"])
                return True
            print(f"channel sendVideo not ok: {str(r)[:200]}", flush=True)
        except Exception as e:
            print(f"channel sendVideo try {i + 1}/4 failed: {e}", flush=True)
        time.sleep(3)
    return False


def poster_tick(force=False):
    """Ek posting pass — queue ka pehla item channel par bhejo.
    Gap (30 min) aur ON/OFF ka dhyaan; force=True par dono bypass.
    Return: True agar post lag gaya."""
    st = load_poststate()
    if not st.get("on") and not force:
        return False
    q = load_postqueue()
    if not q:
        return False
    now = time.time()
    if not force and now - st.get("last_post", 0) < POST_GAP:
        return False
    item = q[0]
    path = _queue_path(item["file"])
    cap = item.get("caption", "")
    if item["file"].lower().endswith((".mp4", ".mov")):
        ok = send_channel_video(POST_CHANNEL_ID, path, cap)
    else:
        ok = send_channel_photo(POST_CHANNEL_ID, path, cap)
    if ok:
        q.pop(0)
        save_postqueue(q)
        st = load_poststate()
        st["last_post"] = now
        st["posted_total"] = st.get("posted_total", 0) + 1
        st["seen_max"] = max(st.get("seen_max", 0), item["n"])
        save_poststate(st)
        print(f"Channel post #{item['n']} lag gaya. Queue me bache: {len(q)}", flush=True)
        aid = admin_id()
        if aid:
            send_message(aid, f"✅ Post #{item['n']} channel par lag gaya!\nQueue me bache posts: {len(q)}")
        return True
    print(f"Channel post #{item['n']} FAIL — agli pass me phir try hoga.", flush=True)
    return False


def poster_loop():
    """Background channel poster — har 30 min me ek post (queue se).
    Har pass se pehle queue.json se naye posts sync karo."""
    while True:
        try:
            sync_postqueue_from_seed()
            poster_tick()
        except Exception as e:
            print(f"poster loop error: {e}", flush=True)
        time.sleep(30)


def handle_start(chat_id, u):
    is_new, total = register_user(u)
    send_video(chat_id)
    if is_new:
        notify_admin(u, total)
        print(f"NEW subscriber: {u.get('id')} @{u.get('username', '')} (total {total})", flush=True)
        # Welcome ke turant baad: intro text + pehla proof video (one-time items)
        once, photos, tail = load_sequence()
        users = load_users()
        key = str(u["id"])
        if key in users and not users[key].get("blocked"):
            changed = False
            for _ in range(min(2, len(once))):
                oi = users[key].get("once_idx", 0)
                if oi >= len(once):
                    break
                if not send_once_item(chat_id, users, key, once[oi]):
                    break
                changed = True
            if changed:
                save_users(users)


def handle_stats(chat_id):
    if chat_id != admin_id():
        send_message(chat_id, "Ye command sirf admin ke liye hai.")
        return
    users = load_users()
    if not users:
        send_message(chat_id, "📊 Abhi koi subscriber nahi hai.")
        return
    lines = [f"📊 *Total Subscribers: {len(users)}*\n"]
    recent = list(users.items())[-5:]
    for uid, d in reversed(recent):
        uname = md_escape("@" + d["username"]) if d.get("username") else "—"
        lines.append(f"• {md_escape(d.get('name', '—'))} ({uname}) — `{uid}`\n  {d.get('first_seen', '')}")
    p = {"chat_id": chat_id, "text": "\n".join(lines), "parse_mode": "Markdown"}
    api("sendMessage", p)


def handle_message(msg):
    chat_id = msg["chat"]["id"]
    u = msg.get("from", {})
    text = (msg.get("text") or "").strip()
    cmd = text.split()[0].split("@")[0].lower() if text else ""
    # Channel posting control — sirf admin, sirf uske DM se
    if chat_id == admin_id() and cmd in ("/poston", "/postoff", "/postnow", "/poststatus"):
        st = load_poststate()
        q = load_postqueue()
        if cmd == "/poston":
            st["on"] = True
            st["last_post"] = 0
            save_poststate(st)
            send_message(chat_id, f"▶️ Channel posting ON ho gayi.\nQueue me posts: {len(q)}\nPehla post abhi jayega, phir har 30 minute me ek.")
            poster_tick()
        elif cmd == "/postoff":
            st["on"] = False
            save_poststate(st)
            send_message(chat_id, "⏸️ Channel posting OFF ho gayi. Queue safe hai, koi post nahi jayega.")
        elif cmd == "/postnow":
            if not q:
                send_message(chat_id, "Queue khaali hai — koi post nahi bacha.")
            elif not poster_tick(force=True):
                send_message(chat_id, "⚠️ Post nahi ho paya — thodi der baad phir try karo.")
        elif cmd == "/poststatus":
            last = st.get("last_post", 0)
            last_txt = datetime.fromtimestamp(last, IST).strftime("%d %b, %I:%M %p IST") if last else "abhi tak koi nahi"
            send_message(chat_id, f"📊 Channel Posting Status\n\nPosting: {'ON ▶️' if st.get('on') else 'OFF ⏸️'}\nQueue me bache: {len(q)}\nAb tak lage: {st.get('posted_total', 0)}\nAakhri post: {last_txt}")
        return
    # Onboarding approvals — sirf admin, sirf uske DM se
    if chat_id == admin_id() and cmd in ("/approve", "/reject", "/pending", "/status"):
        handle_admin_onboard(chat_id, cmd, text)
        return
    if cmd == "/start":
        handle_start(chat_id, u)
    elif cmd == "/channel":
        send_message(chat_id, "📢 Hamara channel join karo — event announcement pinned message ke right baad milega 👇", JOIN_KB)
    elif cmd == "/stats":
        handle_stats(chat_id)
    elif cmd == "/verify":
        users = load_users()
        key = str(chat_id)
        if key in users and not users[key].get("blocked"):
            handle_verify(chat_id, users, key)
        else:
            send_message(chat_id, "Namaste 🙏 Channel join karne ke liye /start dabao — intro video aur saari details mil jayengi.", JOIN_KB)
    elif cmd == "/joined":
        users = load_users()
        key = str(chat_id)
        if key in users and not users[key].get("blocked"):
            handle_joined(chat_id, users, key)
        else:
            send_message(chat_id, "Namaste 🙏 Channel join karne ke liye /start dabao — intro video aur saari details mil jayengi.", JOIN_KB)
    elif cmd == "/help":
        send_message(chat_id,
                     "Commands:\n/start — Intro video + channel link\n/channel — Channel join link\n/verify — UID verification shuru karo\n/joined — Channel join check karo\n/help — Ye help",
                     JOIN_KB)
    elif msg.get("photo"):
        # Deposit screenshot — sirf tab jab UID approved aur deposit manga gaya ho
        users = load_users()
        key = str(chat_id)
        d = users.get(key)
        if (d and not d.get("blocked") and chat_id != admin_id()
                and d.get("uid_status") == "approved"
                and d.get("dep_asked") and d.get("dep_status") != "approved"):
            handle_deposit_photo(chat_id, users, key, msg)
        # warna: photo ignore (pehle jaisa behaviour)
    elif text:
        users = load_users()
        key = str(chat_id)
        d = users.get(key)
        if (d and not d.get("blocked") and chat_id != admin_id()
                and d.get("uid_asked") and d.get("uid_status") != "approved"
                and UID_RE.match(text)):
            handle_uid_text(chat_id, users, key, text)
        else:
            send_message(chat_id, "Namaste 🙏 Channel join karne ke liye /start dabao — intro video aur saari details mil jayengi.", JOIN_KB)


def setup():
    api("setMyCommands", {"commands": [
        {"command": "start", "description": "Start — intro video & channel"},
        {"command": "channel", "description": "Join the channel"},
        {"command": "verify", "description": "UID verification shuru karo"},
        {"command": "joined", "description": "Channel join check karo"},
        {"command": "help", "description": "Help"},
    ]})
    api("setMyDescription", {"description": DESCRIPTION})
    api("setMyShortDescription", {"short_description": SHORT_DESCRIPTION})


def migrate_users():
    """Purane records ko naye format me lao:
    - v1 (intro-split se pehle): purana proof_idx intro ko item 0 ginta tha —
      photo index = idx-1, idx>=1 walon ka intro ho chuka.
    - v2 (once-items): once_idx = intro ho chuka hai to 1, warna 0
      (proof videos/text abhi kisi ko nahi gaye)."""
    users = load_users()
    changed = False
    for key, d in users.items():
        if "intro_done" not in d:
            old_idx = d.get("proof_idx", 0)
            d["intro_done"] = old_idx >= 1
            d["proof_idx"] = max(0, old_idx - 1)
            changed = True
        if "once_idx" not in d:
            d["once_idx"] = 1 if d.get("intro_done") else 0
            changed = True
    if changed:
        save_users(users)
        print("users.json migrate ho gaya.", flush=True)


def main():
    me = None
    for attempt in range(60):
        me = api("getMe")
        if me.get("ok"):
            break
        print(f"getMe try {attempt + 1}/60 failed — proxy wave me exit nahi, 60s me retry.", flush=True)
        time.sleep(60)
    if not me or not me.get("ok"):
        print("getMe failed — token galat hai ya network down.", flush=True)
        raise SystemExit(1)
    print(f"Bot polling shuru... @{me['result'].get('username')}", flush=True)
    setup()
    migrate_users()
    threading.Thread(target=health_server, daemon=True).start()
    threading.Thread(target=proof_loop, daemon=True).start()
    threading.Thread(target=poster_loop, daemon=True).start()
    offset = load_offset()
    if offset is None:
        # Pehli baar: purana backlog skip karo — wo updates pehle ke runs me
        # process ho chuki hain; unhe dobara process karne se bot hamesha
        # hazaaron messages peeche rehta hai aur naye commands kabhi nahi aate.
        r = api("getUpdates", {"offset": -1, "timeout": 0}, retries=2)
        if r.get("ok") and r["result"]:
            offset = r["result"][-1]["update_id"] + 1
        else:
            offset = 0
        save_offset(offset)
        print(f"Offset initialised at {offset} (purana backlog skipped).", flush=True)
    while True:
        try:
            r = api("getUpdates", {"offset": offset, "timeout": 0,
                                   "allowed_updates": ["message"]}, retries=1)
            if r.get("ok"):
                for upd in r["result"]:
                    offset = upd["update_id"] + 1
                    msg = upd.get("message")
                    if msg:
                        try:
                            handle_message(msg)
                        except Exception as e:
                            print(f"handle error: {e}", flush=True)
                if r["result"]:
                    save_offset(offset)
        except Exception as e:
            print(f"poll error: {e}", flush=True)
        time.sleep(2)


if __name__ == "__main__":
    main()
