"""
Telegram Auto-Approve Join Requests Bot  (Telethon + Firebase Firestore)

Private chat : /start /login /logout /accept_bulk /accept_pending /help /admin
Channel/group: /accept  (toggle auto / pending)   /accept_multi  (approve stored pending)

ENV: API_ID, API_HASH (my.telegram.org), BOT_TOKEN (@BotFather), ENCRYPTION_KEY (Fernet key),
     ADMIN_IDS (comma-separated Telegram IDs),
     FIREBASE_CREDENTIALS (service-account JSON text)  or  FIREBASE_CREDENTIALS_FILE (path to it)
"""
import asyncio
import base64
import html
import json
import logging
import os
import re
import tempfile
import time

import aiohttp
from aiohttp import web
from cryptography.fernet import Fernet
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from telethon import Button, TelegramClient, errors, events, functions, types, utils
from telethon.extensions import html as tl_html
from telethon.sessions import StringSession
from telethon.utils import get_peer_id

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("approve-bot")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
FERNET = Fernet(os.environ["ENCRYPTION_KEY"].encode())
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}

WELCOME_DM = "✅ Your request to join <b>{chat}</b> was approved. Welcome!"  # "" to disable

# In-memory session: Render's free plan has no persistent disk, so nothing is written to local files.
bot = None  # created below, after StyledClient is defined
BOT_USERNAME = ""
db: firestore.AsyncClient = None  # created in main()


# ───────────────────────── stylish fonts ─────────────────────────
# Headings use 𝗕𝗼𝗹𝗱 𝗦𝗮𝗻𝘀 (ft). Everything else (body text + button labels) uses the font chosen by the
# admin in Settings: "caps" = ꜱᴍᴀʟʟ ᴄᴀᴘꜱ (default), "bold" = 𝗕𝗼𝗹𝗱 𝗦𝗮𝗻𝘀, "plain" = normal.
_SMALL_CAPS = "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"
MAP_BOLD: dict = {}
for _i in range(26):
    MAP_BOLD[65 + _i] = chr(0x1D5D4 + _i)
    MAP_BOLD[97 + _i] = chr(0x1D5EE + _i)
for _i in range(10):
    MAP_BOLD[48 + _i] = chr(0x1D7EC + _i)
MAP_CAPS = {}
for _i, _ch in enumerate(_SMALL_CAPS):
    MAP_CAPS[97 + _i] = _ch
    MAP_CAPS[65 + _i] = _ch
FONT_MAPS = {"caps": MAP_CAPS, "bold": MAP_BOLD, "plain": {}}
FONT_LABELS = {"caps": "Small Caps", "bold": "Bold Sans", "plain": "Plain"}


def esc(x) -> str:
    """Escape user-controlled text (titles, names, errors) for HTML messages."""
    return html.escape(str(x), quote=False)


def ft(text: str) -> str:
    """Convert to 𝗕𝗼𝗹𝗱 𝗦𝗮𝗻𝘀 (used for headings)."""
    return text.translate(MAP_BOLD)


def sf(text: str) -> str:
    """Convert to the currently selected body font (used for button labels)."""
    return text.translate(FONT_MAPS.get(config.get("font", "caps"), MAP_CAPS))


_TAG_RE = re.compile(r"(<[^>]*>)")
_SKIP_OPEN = re.compile(r"<(code|pre|a)\b", re.I)
_SKIP_CLOSE = re.compile(r"</(code|pre|a)\s*>", re.I)
# never restyle: HTML entities, URLs, /commands, @mentions
_PROTECT_RE = re.compile(r"(&#?\w+;|https?://\S+|t\.me/\S+|(?<![\w/])/[A-Za-z_]\w*|@\w+)")


def stylize(text: str) -> str:
    """Apply the body font to the plain-text parts of an HTML message (tags, code, links, commands untouched)."""
    table = FONT_MAPS.get(config.get("font", "caps"), MAP_CAPS)
    if not table:
        return text
    out, skip = [], 0
    for part in _TAG_RE.split(text):
        if not part:
            continue
        if _TAG_RE.fullmatch(part):
            if _SKIP_OPEN.match(part):
                skip += 1
            elif _SKIP_CLOSE.match(part):
                skip = max(0, skip - 1)
            out.append(part)
        elif skip:
            out.append(part)
        else:
            out.append("".join(seg if i % 2 else seg.translate(table) for i, seg in enumerate(_PROTECT_RE.split(part))))
    return "".join(out)


# ───────────────────────── premium (custom) emoji ─────────────────────────
# The admin sends a message containing premium emoji (/admin → Premium Emoji). The bot learns
# "normal emoji → premium emoji id" and then uses the premium version automatically everywhere the
# normal one appears: in message text (entities) and as the icon of coloured buttons.
# Telegram only lets some bots do this (owner has Premium, or the bot owns a Fragment username),
# so the bot tests it and falls back to normal emoji if it isn't allowed.
emoji_map: dict = {}      # "😍" -> custom emoji document id
_emoji_re = None
ICONS_OK = True           # try premium emoji as button icons; switched off automatically if Telegram refuses


def rebuild_emoji():
    global _emoji_re
    emoji_map.clear()
    try:
        pairs = json.loads(config.get("emoji_json") or "{}")
    except Exception:
        pairs = {}
    for key, doc_id in pairs.items():
        if not key:
            continue
        doc_id = int(doc_id)
        emoji_map[key] = doc_id
        base = key.replace("\ufe0f", "")
        if base:  # match the emoji with or without the variation selector
            emoji_map.setdefault(base, doc_id)
            emoji_map.setdefault(base + "\ufe0f", doc_id)
    keys = sorted(emoji_map, key=len, reverse=True)
    _emoji_re = re.compile("|".join(re.escape(k) for k in keys)) if keys else None


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def add_custom_emoji(text, entities):
    """Wrap every mapped emoji in `text` with a custom-emoji entity (UTF-16 offsets, as Telegram needs)."""
    if not _emoji_re or not text:
        return text, entities
    extra, u16, pos = [], 0, 0
    for m in _emoji_re.finditer(text):
        u16 += utf16_len(text[pos : m.start()])
        length = utf16_len(m.group())
        extra.append(types.MessageEntityCustomEmoji(offset=u16, length=length, document_id=emoji_map[m.group()]))
        u16 += length
        pos = m.end()
    if extra:
        entities = sorted(list(entities) + extra, key=lambda e: e.offset)
    return text, entities


class StyledHTML:
    """Default parse mode for the whole bot: HTML + stylish font + premium emoji on every message/caption."""

    @staticmethod
    def parse(message):
        text, entities = tl_html.parse(stylize(message))
        return add_custom_emoji(text, entities)

    @staticmethod
    def unparse(text, entities):
        return tl_html.unparse(text, entities)




# ───────────────────────── colourful buttons ─────────────────────────
# Button colours (blue / green / red) and premium-emoji icons are set through Telegram's Bot API
# (InlineKeyboardButton.style / .icon_custom_emoji_id), the same way other bots do it. Every message the bot
# sends or edits with buttons is first delivered by Telethon with plain buttons, then immediately restyled via
# the Bot API (editMessageReplyMarkup). Progress screens are edited via the Bot API directly (no flicker).
# If Telegram refuses the icons the bot keeps the normal emoji; if the Bot API is unreachable it keeps plain
# buttons - nothing ever breaks because of styling.
STYLE_API = os.environ.get("BUTTON_STYLES", "1") != "0"      # BUTTON_STYLES=0 switches styling off
_STYLE_NAME = {"blue": "primary", "green": "success", "red": "danger"}
_GREEN = {"login", "bulk", "check_join", "adm_fadd", "bc_copy", "bc_fwd", "adm_wvid", "adm_wprev", "adm_eadd"}
_RED = {"logout", "stop", "adm_close", "bc_cancel", "adm_ban", "adm_wrmv", "adm_eclr"}
# what we learned from Telegram: colours/icons accepted? last error text, consecutive failures
style_state: dict = {"colors": None, "icons": None, "error": None, "fails": 0, "skip": None}
MAX_STYLE_FAILS = 5       # after this many failures in a row, stop trying until the admin presses 🧪 Test


def _auto_color(label: str, data: str):
    if "⬅️" in label or data == "noop":
        return None  # back buttons stay neutral
    if data in _GREEN or data.startswith("pa_"):
        return "green"
    if data in _RED or data.startswith("fdel_"):
        return "red"
    return "blue"


def _split_icon(label: str):
    """'😍 Hello' -> ('Hello', <premium id>) if the label starts with an emoji that has a premium version."""
    if _emoji_re:
        m = _emoji_re.match(label)
        if m:
            rest = label[m.end():].lstrip()
            if rest:
                return rest, emoji_map[m.group()]
    return label, None


def _decorate(b, label: str, color):
    rest, icon = _split_icon(label)
    if color or icon:
        b._sty = (color, sf(rest), icon)   # remembered so the Bot API restyle step knows what to apply
    return b


def btn(label: str, data: str, color="auto"):
    if color == "auto":
        color = _auto_color(label, data)
    return _decorate(Button.inline(sf(label), data.encode()), label, color)


def ubtn(label: str, url: str, color="blue"):
    return _decorate(Button.url(sf(label), url), label, color)


def _rows(buttons):
    if not buttons:
        return None
    if isinstance(buttons, (list, tuple)):
        if all(isinstance(r, (list, tuple)) for r in buttons):
            return [list(r) for r in buttons]
        return [list(buttons)]
    return [[buttons]]


def api_markup(buttons, use_icons: bool):
    """Telethon buttons -> Bot API inline_keyboard. Returns (rows, has_style, has_icon); rows is None if the
    keyboard contains something we can't express (then we simply leave the message alone)."""
    rows = _rows(buttons)
    if not rows:
        return None, False, False
    out, styled, iconed = [], False, False
    for row in rows:
        orow = []
        for b in row:
            inner = getattr(b, "button", b)
            text = getattr(inner, "text", None)
            data = getattr(inner, "data", None)
            url = getattr(inner, "url", None)
            if text is None:
                return None, False, False
            d = {"text": text}
            if data is not None:
                d["callback_data"] = bytes(data).decode("utf-8", "ignore")
            elif url:
                d["url"] = url
            else:
                return None, False, False
            sty = getattr(b, "_sty", None)
            if sty:
                color, rest, icon = sty
                if color:
                    d["style"] = _STYLE_NAME[color]
                    styled = True
                if icon and use_icons:
                    d["icon_custom_emoji_id"] = str(icon)
                    d["text"] = rest
                    iconed = True
            orow.append(d)
        out.append(orow)
    return out, styled or iconed, iconed


_http = None


async def tg_api(method: str, payload: dict) -> dict:
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
    async with _http.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload) as r:
        return await r.json(content_type=None)


async def _call(method: str, payload: dict):
    """Bot API call with one retry on a short 429. Returns (ok, description). 'message is not modified' = ok."""
    desc = ""
    for attempt in range(2):
        try:
            res = await tg_api(method, payload)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
        if res.get("ok"):
            return True, ""
        desc = str(res.get("description", ""))
        if res.get("error_code") == 429 and attempt == 0:
            wait = int((res.get("parameters") or {}).get("retry_after", 1))
            if wait <= 10:
                await asyncio.sleep(wait + 0.5)
                continue
        break
    return ("not modified" in desc.lower()), desc


def _note_failure(err: str):
    style_state["error"] = err
    style_state["fails"] += 1
    if style_state["fails"] >= MAX_STYLE_FAILS:
        style_state["colors"] = False
        log.warning("Button styling disabled after %s failures in a row (last: %s). Press Test in the admin panel to retry.",
                    style_state["fails"], err)


async def push_styled(method: str, payload: dict, buttons) -> bool:
    """Send `payload` + the styled keyboard to the Bot API. True if Telegram accepted it."""
    global ICONS_OK
    use_icons = ICONS_OK and bool(emoji_map)
    markup, styled, iconed = api_markup(buttons, use_icons)
    if markup is None:
        _skip("keyboard contains a button type the Bot API step can't read")
        return False
    if not styled:
        return False
    ok, err = await _call(method, {**payload, "reply_markup": {"inline_keyboard": markup}})
    if ok:
        style_state.update(colors=True, fails=0, error=None)
        if iconed:
            style_state["icons"] = True
        return True
    if iconed:  # maybe only the premium icons are the problem → retry with colours only
        markup2, _, _ = api_markup(buttons, False)
        ok2, err2 = await _call(method, {**payload, "reply_markup": {"inline_keyboard": markup2}})
        if ok2:
            ICONS_OK = False
            style_state.update(colors=True, icons=False, fails=0, error=err)
            log.warning("Telegram refused premium emoji on buttons (%s) - using normal emoji on buttons.", err)
            return True
        err = err2 or err
    _note_failure(err)
    log.info("button styling failed: %s", err)
    return False


_skip_seen: set = set()


def _skip(reason: str):
    """Remember (and log once) why a keyboard was left unstyled, so problems are visible instead of silent."""
    style_state["skip"] = reason
    if reason not in _skip_seen:
        _skip_seen.add(reason)
        log.info("button styling skipped: %s", reason)


def msg_target(msg, entity=None):
    """(private chat id, message id) of a message we just sent/edited. Looks at the message first and falls back
    to the entity the call was made with, so it keeps working whatever shape Telethon returns."""
    mid = getattr(msg, "id", None)
    cid = None
    probes = (
        lambda: msg.chat_id,
        lambda: msg.peer_id.user_id,
        lambda: entity if isinstance(entity, int) else None,
        lambda: entity.user_id,
        lambda: entity.id if type(entity).__name__ == "User" else None,
    )
    for probe in probes:
        try:
            v = probe()
        except Exception:
            continue
        if isinstance(v, int):
            cid = v
            break
    return cid, mid


async def style_message(msg, buttons, entity=None):
    """After Telethon sent/edited `msg`, recolour its buttons / add premium icons. Never raises."""
    try:
        if not STYLE_API or style_state["colors"] is False or not buttons:
            return
        if isinstance(msg, (list, tuple)):
            msg = msg[-1] if msg else None
        if msg is None:
            return _skip("Telethon returned no message object")
        chat_id, mid = msg_target(msg, entity)
        if chat_id is None or not mid:
            return _skip(f"could not work out chat/message id (chat={chat_id!r}, id={mid!r})")
        if chat_id <= 0:
            return  # groups/channels never carry our inline keyboards
        await push_styled("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": mid}, buttons)
    except Exception as e:
        _skip(f"{type(e).__name__}: {e}")


def to_api_html(text: str) -> str:
    """Bot API HTML for a message: stylish font + premium emoji as <tg-emoji>."""
    out = stylize(text)
    if not _emoji_re:
        return out
    res, skip = [], 0
    for part in _TAG_RE.split(out):
        if not part:
            continue
        if _TAG_RE.fullmatch(part):
            if _SKIP_OPEN.match(part):
                skip += 1
            elif _SKIP_CLOSE.match(part):
                skip = max(0, skip - 1)
            res.append(part)
        elif skip:
            res.append(part)
        else:
            res.append(_emoji_re.sub(lambda m: f'<tg-emoji emoji-id="{emoji_map[m.group()]}">{m.group()}</tg-emoji>', part))
    return "".join(res)


async def api_edit(msg, text: str, buttons) -> bool:
    """Edit text + styled keyboard of one of our private-chat messages in a single Bot API call (no flicker)."""
    try:
        if not STYLE_API or style_state["colors"] is False or not buttons:
            return False
        chat_id, mid = msg_target(msg)
        if not isinstance(chat_id, int) or chat_id <= 0 or not mid:
            return False
        media = getattr(msg, "media", None)
        has_media = media is not None and not isinstance(media, types.MessageMediaWebPage)
        payload = {"chat_id": chat_id, "message_id": mid, "parse_mode": "HTML"}
        if has_media:
            payload["caption"] = to_api_html(text)
            return await push_styled("editMessageCaption", payload, buttons)
        payload["text"] = to_api_html(text)
        payload["link_preview_options"] = {"is_disabled": True}
        return await push_styled("editMessageText", payload, buttons)
    except Exception as e:
        log.info("api_edit skipped: %s", e)
        return False


class StyledClient(TelegramClient):
    """Telethon client that restyles inline buttons right after every send / edit."""

    @staticmethod
    def _entity(args, kwargs):
        return args[0] if args else kwargs.get("entity")

    async def send_message(self, *args, **kwargs):
        m = await super().send_message(*args, **kwargs)
        await style_message(m, kwargs.get("buttons"), self._entity(args, kwargs))
        return m

    async def send_file(self, *args, **kwargs):
        m = await super().send_file(*args, **kwargs)
        await style_message(m, kwargs.get("buttons"), self._entity(args, kwargs))
        return m

    async def edit_message(self, *args, **kwargs):
        m = await super().edit_message(*args, **kwargs)
        await style_message(m, kwargs.get("buttons"), self._entity(args, kwargs))
        return m


bot = StyledClient(StringSession(), API_ID, API_HASH)
bot.parse_mode = StyledHTML()


# ───────────────────────── Firestore layer ─────────────────────────
# Collections
#   users/{user_id}            first_name, username, username_lower, joined, last_active, banned, blocked, access_hash
#   sessions/{user_id}         session (Fernet-encrypted Telethon StringSession)
#   chats/{chat_id}            auto (bool)
#   pending/{chat_id}_{user}   chat_id, user_id, access_hash
#   force_channels/{chat_id}   chat_id, channel_id, access_hash, title, link, created
#   meta/config                welcome_dm, maintenance, approved
#
# The free Spark plan allows 50K reads / 20K writes per day, so the hot paths are cached in memory.

config = {"welcome_dm": True, "maintenance": False, "approved": 0}
banned_ids: set = set()
force_cache: list = []
auto_cache: dict = {}
seen_users: dict = {}          # uid -> time of last Firestore write for that user
USER_WRITE_INTERVAL = 300      # seconds between "last_active" writes per user
approved_buffer = 0
approved_flushed_at = time.time()


def parse_credentials(raw: str) -> dict:
    """Accepts the service-account JSON as plain text (optionally wrapped in quotes) or base64-encoded."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        raw = raw[1:-1].strip()
    if not raw.startswith("{"):
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8").strip()
        except Exception:
            pass
    try:
        return json.loads(raw, strict=False)  # strict=False tolerates real newlines inside private_key
    except json.JSONDecodeError:
        raise SystemExit(
            "FIREBASE_CREDENTIALS is not valid JSON (value length %d, starts with %r). "
            "Paste the ENTIRE service-account file contents, from the first { to the last }, "
            "with no extra text." % (len(raw), raw[:1])
        )


def make_db() -> firestore.AsyncClient:
    raw = os.environ.get("FIREBASE_CREDENTIALS")
    path = os.environ.get("FIREBASE_CREDENTIALS_FILE")
    if raw:
        info = parse_credentials(raw)
    elif path:
        with open(path, encoding="utf-8") as f:
            info = json.load(f)
    else:
        raise SystemExit("Set FIREBASE_CREDENTIALS (JSON text) or FIREBASE_CREDENTIALS_FILE (path).")
    return firestore.AsyncClient.from_service_account_info(info)


async def count(coll: str, *filters) -> int:
    q = db.collection(coll)
    for f in filters:
        q = q.where(filter=f)
    res = await q.count().get()
    return int(res[0][0].value)


async def load_caches():
    snap = await db.collection("meta").document("config").get()
    if snap.exists:
        config.update(snap.to_dict() or {})
    config["approved"] = int(config.get("approved", 0) or 0)
    rebuild_emoji()
    banned_ids.clear()
    q = db.collection("users").where(filter=FieldFilter("banned", "==", True))
    async for d in q.select([]).stream():
        banned_ids.add(int(d.id))
    await refresh_force()


async def refresh_force():
    rows = [{"id": d.id, **(d.to_dict() or {})} async for d in db.collection("force_channels").stream()]
    rows.sort(key=lambda r: r.get("created", 0))
    force_cache[:] = rows


async def set_config(key: str, value):
    config[key] = value
    await db.collection("meta").document("config").set({key: value}, merge=True)


async def flush_approved():
    global approved_buffer, approved_flushed_at
    if approved_buffer:
        n, approved_buffer = approved_buffer, 0
        approved_flushed_at = time.time()
        try:
            await db.collection("meta").document("config").set({"approved": firestore.Increment(n)}, merge=True)
        except Exception as e:
            approved_buffer += n
            log.warning("could not flush approved counter: %s", e)


async def bump(n: int = 1):
    """Count approvals in memory; write to Firestore in batches to save quota."""
    global approved_buffer
    config["approved"] += n
    approved_buffer += n
    if approved_buffer >= 50 or time.time() - approved_flushed_at > 120:
        await flush_approved()


logged_cache: dict = {}   # uid -> bool, so building a menu doesn't hit Firestore every time


async def is_logged_in(uid: int) -> bool:
    if uid not in logged_cache:
        snap = await db.collection("sessions").document(str(uid)).get()
        logged_cache[uid] = bool(snap.exists)
    return logged_cache[uid]


async def save_session(user_id: int, session: str):
    await db.collection("sessions").document(str(user_id)).set(
        {"session": FERNET.encrypt(session.encode()).decode()}
    )
    logged_cache[user_id] = True


async def load_session(user_id: int):
    snap = await db.collection("sessions").document(str(user_id)).get()
    if not snap.exists:
        return None
    return FERNET.decrypt(snap.to_dict()["session"].encode()).decode()


async def delete_session(user_id: int):
    await db.collection("sessions").document(str(user_id)).delete()
    logged_cache[user_id] = False
    chan_cache.pop(user_id, None)


async def is_auto(chat_id: int) -> bool:
    if chat_id not in auto_cache:
        snap = await db.collection("chats").document(str(chat_id)).get()
        data = snap.to_dict() or {} if snap.exists else {}
        auto_cache[chat_id] = bool(data.get("auto", True))  # default: auto-accept
    return auto_cache[chat_id]


async def set_auto(chat_id: int, value: bool):
    auto_cache[chat_id] = value
    await db.collection("chats").document(str(chat_id)).set({"auto": value})


async def track_user(uid: int, first_name: str, username: str, access_hash):
    now = time.time()
    last = seen_users.get(uid)
    if last and now - last < USER_WRITE_INTERVAL:
        return
    try:
        ref = db.collection("users").document(str(uid))
        fields = {
            "first_name": first_name,
            "username": username,
            "username_lower": username.lower(),
            "last_active": int(now),
            "blocked": False,
            "access_hash": access_hash,
        }
        if last is None:  # first time this process sees the user → is it a brand-new user?
            snap = await ref.get()
            if not snap.exists:
                fields.update(joined=int(now), banned=False)
        await ref.set(fields, merge=True)
        seen_users[uid] = now
    except Exception as e:  # never lock users out because of a database hiccup / quota
        log.warning("track_user failed: %s", e)


def is_banned(uid: int) -> bool:
    return uid in banned_ids


async def recipients_count() -> int:
    return await count(
        "users", FieldFilter("banned", "==", False), FieldFilter("blocked", "==", False)
    )


async def recipients():
    q = (
        db.collection("users")
        .where(filter=FieldFilter("banned", "==", False))
        .where(filter=FieldFilter("blocked", "==", False))
    )
    return [(int(d.id), (d.to_dict() or {}).get("access_hash")) async for d in q.select(["access_hash"]).stream()]


async def mark_blocked(ids):
    ids = list(ids)
    for i in range(0, len(ids), 400):
        batch = db.batch()
        for uid in ids[i : i + 400]:
            batch.set(db.collection("users").document(str(uid)), {"blocked": True}, merge=True)
        await batch.commit()


async def delete_docs(refs):
    refs = list(refs)
    for i in range(0, len(refs), 400):
        batch = db.batch()
        for r in refs[i : i + 400]:
            batch.delete(r)
        await batch.commit()


def user_peer(uid: int, access_hash):
    """Works after a restart too (Telethon's entity cache may be empty on ephemeral hosts)."""
    return types.InputPeerUser(uid, access_hash) if access_hash else uid


# ───────────────────────── user clients ─────────────────────────
user_clients: dict = {}
running_tasks: dict = {}


async def get_user_client(user_id: int):
    client = user_clients.get(user_id)
    if client and client.is_connected():
        return client
    session = await load_session(user_id)
    if not session:
        return None
    client = TelegramClient(StringSession(session), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        await delete_session(user_id)
        return None
    user_clients[user_id] = client
    return client


def can_approve(entity) -> bool:
    if not isinstance(entity, types.Channel):
        return False
    if entity.creator:
        return True
    rights = entity.admin_rights
    return bool(rights and rights.invite_users)


# ───────────────────────── menus / texts ─────────────────────────
def menu_buttons(logged_in: bool, uid=None):
    rows = [
        [
            ubtn("➕ Add to Channel", f"https://t.me/{BOT_USERNAME}?startchannel=true&admin=invite_users"),
            ubtn("➕ Add to Group", f"https://t.me/{BOT_USERNAME}?startgroup=true&admin=invite_users"),
        ]
    ]
    if logged_in:
        rows += [
            [btn("✅ Approve Pending", "bulk")],
            [btn("📋 Commands", "commands"), btn("🚪 Logout", "logout")],
        ]
    else:
        rows += [[btn("🔐 Login", "login"), btn("📋 Commands", "commands")]]
    links = []
    if config.get("support_link"):
        links.append(ubtn("🆘 Support", config["support_link"], "green"))
    if config.get("updates_link"):
        links.append(ubtn("📣 Updates", config["updates_link"], "blue"))
    if links:
        rows.append(links)
    if uid in ADMIN_IDS:
        rows.append([btn("🛡 Admin Panel", "adm_home")])
    return rows


async def build_menu(uid: int):
    return menu_buttons(await is_logged_in(uid), uid)


# Shown to every user on /start (admins can replace it from  /admin → Welcome Setup).  {name} = first name.
DEFAULT_WELCOME = (
    f"<b>{ft('Auto Approve Bot')}</b>\n\n"
    "Hey <b>{name}</b> 👋\n\n"
    "⚡ I approve join requests for your channels &amp; groups instantly.\n"
    "➕ Add me as <b>admin</b> with the <i>Invite Users</i> right and you're done.\n\n"
    "🔐 Tap <b>Login</b> to also clear requests that were already pending."
)


def welcome_text(name: str) -> str:
    base = config.get("welcome_text") or DEFAULT_WELCOME
    return base.replace("{name}", esc(name or "there"))

COMMANDS_TEXT = (
    f"<b>{ft('Available Commands')}</b>\n\n"
    "/start - Start the bot\n"
    "/login - Log in so I can approve requests that were already pending\n"
    "/logout - Remove your saved session\n"
    "/accept_bulk - Pick one of your channels and approve its pending requests "
    "(100 / 500 / 1000 / all)\n"
    "/accept_pending <code>channel_id delay limit</code> - Same, by command: delay = seconds between approvals "
    "(default 0.3), limit = how many (0 = all)\n"
    "/id - Show your Telegram ID (send it inside a channel/group to get that chat's ID)\n\n"
    f"<b>{ft('Channel commands')}</b>\n"
    "/accept_multi - Post in channel: approve all requests the bot stored "
    "(bot must have been admin before the request arrived)\n"
    "/accept - Post in channel: toggle auto-accept ⇄ pending mode"
)


async def send_with_media(chat_id, text: str, buttons):
    """Send `text` under the configured welcome video (caption); falls back to plain text if it can't be sent."""
    media = config.get("welcome_media")
    if media and media.get("file_id"):
        try:
            return await bot.send_file(chat_id, media["file_id"], caption=text, buttons=buttons)
        except Exception as e:
            log.warning("welcome media could not be sent (%s) - falling back to text", e)
    return await bot.send_message(chat_id, text, buttons=buttons)


async def send_welcome(chat_id, uid: int, name: str):
    await send_with_media(chat_id, welcome_text(name), await build_menu(uid))


async def show(event, text: str, buttons=None):
    """Edit the message a button belongs to. If it carries the welcome video (a caption can't hold long text),
    replace it with a fresh text message instead."""
    try:
        msg = await event.get_message()
    except Exception:
        msg = None
    media = getattr(msg, "media", None)
    if media is not None and not isinstance(media, types.MessageMediaWebPage):
        try:
            await msg.delete()
        except Exception:
            pass
        return await bot.send_message(event.chat_id, text, buttons=buttons)
    try:
        return await event.edit(text, buttons=buttons)
    except errors.MessageNotModifiedError:
        return msg


async def send_menu(chat_id, uid, edit_event=None, name=""):
    if edit_event is None:
        return await send_welcome(chat_id, uid, name)
    await show(edit_event, welcome_text(name), await build_menu(uid))


# ───────────────────────── join-request handling (bot) ─────────────────────────
async def bot_approve(peer, user_id: int, access_hash=None):
    user = types.InputUser(user_id, access_hash) if access_hash else await bot.get_input_entity(user_id)
    await bot(
        functions.messages.HideChatJoinRequestRequest(
            peer=await bot.get_input_entity(peer), user_id=user, approved=True
        )
    )


@bot.on(events.Raw(types.UpdateBotChatInviteRequester))
async def on_join_request(update):
    chat_id = get_peer_id(update.peer)
    user_id = update.user_id
    if not await is_auto(chat_id):
        try:
            access_hash = (await bot.get_input_entity(user_id)).access_hash
        except Exception:
            access_hash = None
        await db.collection("pending").document(f"{chat_id}_{user_id}").set(
            {"chat_id": chat_id, "user_id": user_id, "access_hash": access_hash}
        )
        return
    try:
        await bot_approve(update.peer, user_id)
        log.info("approved %s in %s", user_id, chat_id)
        await bump()
    except errors.FloodWaitError as e:
        await asyncio.sleep(e.seconds)
        await bot_approve(update.peer, user_id)
    except Exception as e:
        log.error("approve failed %s/%s: %s", chat_id, user_id, e)
        return
    if WELCOME_DM and config.get("welcome_dm", True):
        try:
            chat = await bot.get_entity(update.peer)
            await bot.send_message(user_id, WELCOME_DM.format(chat=chat.title))
        except Exception:
            pass


# ───────────────────────── channel commands ─────────────────────────
async def authorized(event) -> bool:
    if event.is_private:
        return False
    if event.is_channel and not event.is_group:  # broadcast channel: only admins can post
        return True
    try:
        perms = await bot.get_permissions(event.chat_id, event.sender_id)
        return bool(perms.is_admin or perms.is_creator)
    except Exception:
        return False


async def reply_temp(event, text, seconds=15):
    msg = await event.respond(text)
    await asyncio.sleep(seconds)
    for m in (msg, event.message):
        try:
            await m.delete()
        except Exception:
            pass


@bot.on(events.NewMessage(pattern=r"^/accept(@\w+)?\s*$", func=lambda e: not e.is_private))
async def toggle_auto(event):
    if not await authorized(event):
        return
    new = not await is_auto(event.chat_id)
    await set_auto(event.chat_id, new)
    if new:
        text = f"🟢 <b>{ft('Auto-accept ON')}</b>\nNew join requests are approved instantly."
    else:
        text = (
            f"🟡 <b>{ft('Pending mode ON')}</b>\nNew requests are stored. "
            "Send /accept_multi to approve them in bulk."
        )
    await reply_temp(event, text)


@bot.on(events.NewMessage(pattern=r"^/accept_multi(@\w+)?\s*$", func=lambda e: not e.is_private))
async def accept_multi(event):
    if not await authorized(event):
        return
    chat_id = event.chat_id
    q = db.collection("pending").where(filter=FieldFilter("chat_id", "==", chat_id))
    docs = [d async for d in q.stream()]
    if not docs:
        return await reply_temp(event, f"ℹ️ <b>{ft('No stored pending requests')}</b>")
    status = await event.respond(f"⏳ {ft('Approving')} {len(docs)} …")
    ok = fail = 0
    for d in docs:
        data = d.to_dict() or {}
        uid, ah = int(data["user_id"]), data.get("access_hash")
        try:
            await bot_approve(chat_id, uid, ah)
            ok += 1
        except errors.FloodWaitError as e:
            await asyncio.sleep(e.seconds)
            try:
                await bot_approve(chat_id, uid, ah)
                ok += 1
            except Exception:
                fail += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.3)
    await delete_docs(d.reference for d in docs)
    await bump(ok)
    await status.edit(
        f"✅ <b>{ft('Done')}</b>\nApproved: <b>{ok}</b>\nFailed: <b>{fail}</b>"
    )


# ───────────────────────── private commands ─────────────────────────
@bot.on(events.NewMessage(pattern=r"^/start", func=lambda e: e.is_private))
async def cmd_start(event):
    if not await gate(event, with_video=True):
        return
    sender = await event.get_sender()
    await send_welcome(event.chat_id, event.sender_id, getattr(sender, "first_name", "") or "")


@bot.on(events.NewMessage(pattern=r"^/id(@\w+)?\s*$"))
async def cmd_id(event):
    if event.is_private:
        if not await gate(event):
            return
        await event.respond(f"🆔 <b>{ft('Your ID')}</b>: <code>{event.sender_id}</code>")
    elif await authorized(event):
        await reply_temp(event, f"🆔 <b>{ft('Chat ID')}</b>: <code>{event.chat_id}</code>", 30)


@bot.on(events.NewMessage(pattern=r"^/help", func=lambda e: e.is_private))
async def cmd_help(event):
    if not await gate(event):
        return
    await event.respond(COMMANDS_TEXT, buttons=[[btn("⬅️ Back", "back")]])


@bot.on(events.NewMessage(pattern=r"^/login", func=lambda e: e.is_private))
async def cmd_login(event):
    if not await gate(event):
        return
    await do_login(event.sender_id)


@bot.on(events.NewMessage(pattern=r"^/logout", func=lambda e: e.is_private))
async def cmd_logout(event):
    if not await gate(event):
        return
    await do_logout(event.sender_id)


@bot.on(events.NewMessage(pattern=r"^/accept_bulk", func=lambda e: e.is_private))
async def cmd_bulk(event):
    if not await gate(event):
        return
    await open_picker(event.sender_id)


@bot.on(
    events.NewMessage(
        pattern=r"^/accept_pending(?:\s+(\S+))?(?:\s+(\d+(?:\.\d+)?))?(?:\s+(\d+))?\s*$",
        func=lambda e: e.is_private,
    )
)
async def cmd_pending(event):
    if not await gate(event):
        return
    chan, delay, limit = event.pattern_match.groups()
    if not chan:
        return await event.respond(
            f"<b>{ft('Usage')}</b>\n<code>/accept_pending channel_id delay limit</code>\n"
            "Example: <code>/accept_pending -1001234567890 1 500</code>",
        )
    start_task(
        event.sender_id,
        command_approval(event.sender_id, chan, float(delay or BASE_DELAY), int(limit or 0)),
    )


def start_task(uid: int, coro):
    old = running_tasks.get(uid)
    if old and not old.done():
        coro.close()
        return asyncio.create_task(
            bot.send_message(uid, f"⚠️ {ft('A task is already running.')}", buttons=[[btn("🛑 Stop", "stop")]])
        )
    running_tasks[uid] = asyncio.create_task(coro)


# ───────────────────────── login / logout ─────────────────────────
async def do_login(uid: int):
    if await get_user_client(uid):
        return await bot.send_message(
            uid, f"✅ <b>{ft('Already logged in')}</b>", buttons=menu_buttons(True, uid)
        )
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    try:
        async with bot.conversation(uid, timeout=300) as conv:
            await conv.send_message(
                f"🔐 <b>{ft('Login')}</b>\nSend your phone number with country code, e.g. <code>+919876543210</code>\n"
                "(/cancel to abort)",
            )
            resp = await conv.get_response()
            if resp.text.strip().startswith("/cancel"):
                return await conv.send_message("❌ Cancelled.")
            phone = resp.text.strip()
            try:
                sent = await client.send_code_request(phone)
            except errors.PhoneNumberInvalidError:
                return await conv.send_message("❌ Invalid phone number.")

            await conv.send_message(
                "📩 Code sent to your Telegram app.\n"
                "Send it <b>with spaces</b> between digits (e.g. <code>1 2 3 4 5</code>) — "
                "Telegram blocks codes sent as plain digits.",
            )
            resp = await conv.get_response()
            code = re.sub(r"\D", "", resp.text)
            try:
                await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
            except errors.SessionPasswordNeededError:
                await conv.send_message("🔑 Two-step verification is on. Send your password:")
                pw = await conv.get_response()
                try:
                    await pw.delete()
                except Exception:
                    pass
                await client.sign_in(password=pw.text)
            except (errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError):
                return await conv.send_message("❌ Invalid or expired code. Run /login again.")

            await save_session(uid, client.session.save())
            await conv.send_message(
                f"✅ <b>{ft('Login successful')}</b>", buttons=menu_buttons(True, uid)
            )
    except asyncio.TimeoutError:
        await bot.send_message(uid, "⌛ Login timed out. Run /login again.")
    except errors.PasswordHashInvalidError:
        await bot.send_message(uid, "❌ Wrong 2FA password. Run /login again.")
    except Exception as e:
        log.exception("login failed")
        await bot.send_message(uid, f"❌ Login failed: {esc(e)}")
    finally:
        await client.disconnect()


async def do_logout(uid: int):
    client = user_clients.pop(uid, None)
    if client:
        try:
            await client.log_out()
        except Exception:
            pass
    await delete_session(uid)
    await bot.send_message(uid, f"🚪 <b>{ft('Logged out')}</b>", buttons=menu_buttons(False, uid))


# ───────────────────────── account-based tasks ─────────────────────────
async def need_login(uid: int):
    client = await get_user_client(uid)
    if not client:
        await bot.send_message(
            uid, f"🔐 {ft('Please login first.')}", buttons=[[btn("🔐 Login", "login")]]
        )
    return client


# ── approval engine ──────────────────────────────────────────────────────────
BASE_DELAY = 0.3          # seconds between approvals (adapts upward automatically if Telegram slows us down)
MAX_DELAY = 3.0
MAX_FLOOD_WAIT = 900      # if Telegram asks us to wait longer than this (seconds), stop and report instead
PAGE = 100                # importers fetched per request (Telegram's maximum)
PICK_PAGE = 8             # channels per page in the picker
chan_cache: dict = {}     # uid -> {"ts": float, "items": [{"id","title","peer","inp","pending"}]}


def fmt_dur(sec: float) -> str:
    sec = int(max(0, sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec // 3600}h {(sec % 3600) // 60:02d}m"


def bar(frac: float, width: int = 12) -> str:
    frac = min(1.0, max(0.0, frac))
    n = int(round(frac * width))
    return "▰" * n + "▱" * (width - n)


def _norm_err(e) -> str:
    return re.sub(r"[^A-Z0-9]", "", f"{type(e).__name__} {e} {getattr(e, 'message', '')}".upper())


async def pending_count(client, channel) -> int:
    full = await client(functions.channels.GetFullChannelRequest(channel))
    return getattr(full.full_chat, "requests_pending", 0) or 0


async def load_channels(uid: int, client, force: bool = False):
    """Channels where the user's account may approve requests, with live pending counts (cached 3 min)."""
    c = chan_cache.get(uid)
    if c and not force and time.time() - c["ts"] < 180:
        return c["items"]
    items = []
    async for d in client.iter_dialogs():
        ent = d.entity
        if can_approve(ent):
            items.append(
                {"id": ent.id, "title": ent.title or "?", "peer": utils.get_input_peer(ent),
                 "inp": utils.get_input_channel(ent), "pending": None}
            )
    sem = asyncio.Semaphore(6)

    async def fill(it):
        async with sem:
            try:
                it["pending"] = await pending_count(client, it["inp"])
            except Exception as e:  # leave unknown rather than failing the whole list
                log.info("pending count failed for %s: %s", it["title"], e)

    await asyncio.gather(*(fill(i) for i in items))
    chan_cache[uid] = {"ts": time.time(), "items": items}
    return items


async def find_channel(uid: int, client, cid: int):
    for it in (chan_cache.get(uid) or {}).get("items", []):
        if it["id"] == cid:
            return it
    for it in await load_channels(uid, client, force=True):
        if it["id"] == cid:
            return it
    return None


async def picker_screen(uid: int, client, page: int = 0, force: bool = False):
    items = [i for i in await load_channels(uid, client, force) if (i["pending"] or 0) > 0]
    items.sort(key=lambda i: i["pending"], reverse=True)
    pages = max(1, (len(items) + PICK_PAGE - 1) // PICK_PAGE)
    page = min(max(0, page), pages - 1)
    chunk = items[page * PICK_PAGE : (page + 1) * PICK_PAGE]
    total = sum(i["pending"] for i in items)
    if not items:
        text = (
            f"✅ <b>{ft('Approve Pending')}</b>\n\n"
            "No channel of yours has pending join requests right now. 🎉\n"
            "<i>Make sure your logged-in account is admin with the Invite Users right.</i>"
        )
    else:
        text = (
            f"✅ <b>{ft('Approve Pending')}</b>\n\n"
            f"📊 <b>{len(items)}</b> channel(s) · <b>{total:,}</b> pending request(s)\n\n"
            "Pick a channel:"
        )
    rows = [[btn(f"📢 {i['title'][:26]}  ·  {i['pending']:,}", f"pc_{i['id']}")] for i in chunk]
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(btn("◀️ Prev", f"pl_{page - 1}"))
        nav.append(btn(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(btn("Next ▶️", f"pl_{page + 1}"))
        rows.append(nav)
    rows.append([btn("🔄 Refresh", "pl_r"), btn("⬅️ Back", "back")])
    return text, rows


async def amount_screen(uid: int, client, cid: int):
    it = await find_channel(uid, client, cid)
    if not it:
        return "❌ Channel not found (is your account still admin there?).", [[btn("⬅️ Back", "pl_0")]]
    try:
        n = await pending_count(client, it["inp"])
        it["pending"] = n
    except Exception:
        n = it["pending"] or 0
    if n <= 0:
        return (
            f"📢 <b>{esc(ft(it['title']))}</b>\n\nNo pending requests here. 🎉",
            [[btn("🔄 Check Again", f"pc_{cid}"), btn("⬅️ Back", "pl_0")]],
        )
    text = (
        f"📢 <b>{esc(ft(it['title']))}</b>\n\n"
        f"⏳ Pending requests: <b>{n:,}</b>\n\n"
        "How many should I approve now?\n"
        "<i>I pace myself automatically so Telegram doesn't throttle you.</i>"
    )
    amounts = [a for a in (100, 500, 1000) if a < n]
    rows = []
    if amounts:
        rows.append([btn(f"✅ {a:,}", f"pa_{cid}_{a}") for a in amounts])
    rows.append([btn(f"⚡ All ({n:,})", f"pa_{cid}_0")])
    rows.append([btn("⬅️ Back", "pl_0")])
    return text, rows


async def edit_quiet(msg, text, buttons=None):
    if buttons and await api_edit(msg, text, buttons):
        return
    try:
        await msg.edit(text, buttons=buttons)
    except errors.MessageNotModifiedError:
        pass
    except Exception as e:
        log.info("status edit skipped: %s", e)


async def run_approval(uid, client, it, limit, base_delay, status):
    """Approve up to `limit` (0 = all) pending requests of channel `it`, with live progress.
    Pages through the request list explicitly, never depends on one big call, and slows itself down
    if Telegram starts throttling."""
    peer, title, cid = it["peer"], it["title"], it["id"]
    stop_btn = [[btn("🛑 Stop", "stop")]]
    try:
        total = await pending_count(client, it["inp"])
    except Exception:
        total = it.get("pending") or 0
    target = min(limit, total) if limit else total
    if target <= 0:
        return await edit_quiet(status, f"📢 <b>{esc(ft(title))}</b>\n\nNo pending requests. 🎉", [[btn("⬅️ Back", "pl_0")]])

    done = skipped = failed = 0
    last_err = ""
    delay = max(0.05, base_delay)
    started = time.monotonic()
    last_edit = 0.0
    stop_reason = None
    offset_date, offset_user = None, types.InputUserEmpty()

    def progress(note=""):
        el = max(1e-6, time.monotonic() - started)
        rate = done / el
        eta = (target - done) / rate if rate > 0 else 0
        text = (
            f"⏳ <b>{esc(ft(title))}</b>\n\n"
            f"{bar(done / target)}  <b>{int(done * 100 / target)}%</b>\n"
            f"✅ Approved: <b>{done:,}</b> / {target:,}\n"
            f"⏭ Skipped: {skipped:,}   ❌ Failed: {failed:,}\n"
            f"⚡ {rate:.1f}/s   ⏱ ~{fmt_dur(eta)} left"
        )
        return text + (f"\n\n{note}" if note else "")

    await edit_quiet(status, progress(), stop_btn)
    try:
        while not stop_reason and (not limit or done < limit):
            res = await client(
                functions.messages.GetChatInviteImportersRequest(
                    peer=peer, requested=True, offset_date=offset_date, offset_user=offset_user, limit=PAGE
                )
            )
            importers = res.importers
            if not importers:
                break
            users = {u.id: u for u in res.users}
            for imp in importers:
                if limit and done >= limit:
                    break
                user = users.get(imp.user_id)
                if user is None:
                    skipped += 1
                    continue
                t0 = time.monotonic()
                for attempt in range(2):
                    try:
                        await client(
                            functions.messages.HideChatJoinRequestRequest(
                                peer=peer, user_id=utils.get_input_user(user), approved=True
                            )
                        )
                        done += 1
                        await bump()
                        break
                    except errors.FloodWaitError as e:
                        if e.seconds > MAX_FLOOD_WAIT or attempt == 1:
                            stop_reason = f"⏸ Telegram rate limit — wait ~{fmt_dur(e.seconds)} and run again."
                            break
                        wait = e.seconds + 1
                        delay = min(MAX_DELAY, delay * 1.5)
                        while wait > 0 and not stop_reason:
                            await edit_quiet(status, progress(f"⏸ Telegram asked to slow down — resuming in {fmt_dur(wait)}"), stop_btn)
                            step = min(wait, 20)
                            await asyncio.sleep(step)
                            wait -= step
                    except errors.RPCError as e:
                        norm = _norm_err(e)
                        if any(k in norm for k in ("HIDEREQUESTERMISSING", "USERALREADYPARTICIPANT",
                                                   "USERCHANNELSTOOMUCH", "INPUTUSERDEACTIVATED", "USERIDINVALID")):
                            skipped += 1  # already handled / user can't join — nothing to do
                        elif "CHATADMINREQUIRED" in norm or "CHANNELPRIVATE" in norm:
                            stop_reason = "❌ Your account no longer has the Invite Users admin right in this channel."
                        else:
                            failed += 1
                            last_err = type(e).__name__
                        break
                if stop_reason:
                    break
                took = time.monotonic() - t0
                if took > 2.0:  # Telethon silently waited out a flood → ease off
                    delay = min(MAX_DELAY, delay * 1.5)
                elif delay > base_delay:
                    delay = max(base_delay, delay * 0.97)
                await asyncio.sleep(delay)
                if time.monotonic() - last_edit > 3.0:
                    last_edit = time.monotonic()
                    await edit_quiet(status, progress(), stop_btn)
            last = importers[-1]
            lu = users.get(last.user_id)
            if lu is None:
                break
            offset_date, offset_user = last.date, utils.get_input_user(lu)
    except asyncio.CancelledError:
        it["pending"] = max(0, (it.get("pending") or 0) - done)
        await edit_quiet(
            status,
            f"🛑 <b>{ft('Stopped')}</b>\n\n✅ Approved: <b>{done:,}</b> of {target:,}",
            [[btn("🔁 Approve More", f"pc_{cid}"), btn("⬅️ Back", "pl_0")]],
        )
        raise
    except errors.RPCError as e:
        stop_reason = f"⚠️ Telegram stopped the process ({type(e).__name__}). Try again in a while."

    try:
        it["pending"] = await pending_count(client, it["inp"])
    except Exception:
        it["pending"] = max(0, total - done)
    head = "✅ <b>%s</b>" % ft("Done") if not stop_reason else "⚠️ <b>%s</b>" % ft("Finished early")
    text = (
        f"{head}\n\n📢 {esc(title)}\n"
        f"✅ Approved: <b>{done:,}</b>\n"
        f"⏭ Skipped: {skipped:,}   ❌ Failed: {failed:,}\n"
        f"⏳ Still pending: <b>{it['pending']:,}</b>\n"
        f"⏱ Took {fmt_dur(time.monotonic() - started)}"
    )
    if last_err:
        text += f"\n<i>Last error: {esc(last_err)}</i>"
    if stop_reason:
        text += f"\n\n{stop_reason}"
    elif it["pending"] > 0 and limit:
        text += "\n\nTap <b>Approve More</b> to continue."
    await edit_quiet(
        status, text,
        [[btn("🔁 Approve More", f"pc_{cid}"), btn("📊 Channels", "pl_0")], [btn("⬅️ Back", "back")]],
    )


async def approval_task(uid: int, cid: int, limit: int, status):
    client = await get_user_client(uid)
    if not client:
        return await edit_quiet(status, f"🔐 {ft('Please login first.')}", [[btn("🔐 Login", "login")]])
    it = await find_channel(uid, client, cid)
    if not it:
        return await edit_quiet(status, "❌ Channel not found (is your account still admin there?).", [[btn("⬅️ Back", "pl_0")]])
    await run_approval(uid, client, it, limit, BASE_DELAY, status)


async def command_approval(uid: int, chan: str, delay: float, limit: int):
    """/accept_pending channel_id delay limit"""
    client = await need_login(uid)
    if not client:
        return
    status = await bot.send_message(uid, f"⏳ {ft('Looking up the channel…')}")
    ref = int(chan) if re.fullmatch(r"-?\d+", chan) else chan
    it = None
    try:
        ent = await client.get_entity(ref)
    except Exception:
        try:
            await client.get_dialogs()
            ent = await client.get_entity(ref)
        except Exception:
            return await edit_quiet(status, "❌ Channel not found. Check the ID/username and that your account is in it.")
    if not can_approve(ent):
        return await edit_quiet(status, "❌ Your account needs the <b>Invite Users</b> admin right there.")
    it = {"id": ent.id, "title": ent.title or "?", "peer": utils.get_input_peer(ent),
          "inp": utils.get_input_channel(ent), "pending": None}
    await run_approval(uid, client, it, limit, max(0.05, delay), status)


async def open_picker(uid: int, event=None, page: int = 0, force: bool = False):
    """Show the channel picker, either by editing the button's message (event) or as a new message."""
    client = await need_login(uid)
    if not client:
        return
    if event is not None:
        msg = await show(event, f"⏳ {ft('Scanning your channels…')}")
    else:
        msg = await bot.send_message(uid, f"⏳ {ft('Scanning your channels…')}")
    try:
        text, rows = await picker_screen(uid, client, page, force)
    except errors.FloodWaitError as e:
        text, rows = f"⏸ Telegram asked to wait {fmt_dur(e.seconds)}. Try again then.", [[btn("⬅️ Back", "back")]]
    except Exception as e:
        log.exception("picker failed")
        text, rows = f"❌ Couldn't load your channels: {esc(type(e).__name__)}", [[btn("⬅️ Back", "back")]]
    await edit_quiet(msg, text, rows)


# ───────────────────────── inline buttons ─────────────────────────
@bot.on(events.CallbackQuery)
async def on_button(event):
    data = event.data.decode()
    uid = event.sender_id

    if data != "check_join":
        try:
            await event.answer()
        except Exception:
            pass

    # admin-only callbacks
    if data.startswith(("adm_", "bc_", "fdel_", "set_")):
        if uid in ADMIN_IDS:
            await admin_callback(event, data, uid)
        return

    # force-join re-check
    if data == "check_join":
        missing = await missing_channels(uid, await get_peer(event))
        if missing:
            await event.answer("⚠️ You haven't joined all required channels yet!", alert=True)
            return
        await event.answer()
        if await gate(event):
            try:  # the join prompt may carry the video → remove it and send the full welcome (video + menu)
                old = await event.get_message()
                await old.delete()
            except Exception:
                pass
            sender = await event.get_sender()
            await send_welcome(event.chat_id, uid, getattr(sender, "first_name", "") or "")
        return

    if uid not in ADMIN_IDS and not await gate(event):
        return

    if data == "login":
        asyncio.create_task(do_login(uid))
    elif data == "logout":
        await do_logout(uid)
    elif data == "bulk":
        await open_picker(uid, event)
    elif data.startswith("pl_"):
        arg = data[3:]
        await open_picker(uid, event, page=0 if arg == "r" else int(arg), force=(arg == "r"))
    elif data.startswith("pc_"):
        client = await need_login(uid)
        if client:
            msg = await show(event, f"⏳ {ft('Loading…')}")
            try:
                text, rows = await amount_screen(uid, client, int(data[3:]))
            except Exception as e:
                text, rows = f"❌ {esc(type(e).__name__)}", [[btn("⬅️ Back", "pl_0")]]
            await edit_quiet(msg, text, rows)
    elif data.startswith("pa_"):
        _, cid, amount = data.split("_")
        status = await show(event, f"⏳ {ft('Starting…')}", [[btn("🛑 Stop", "stop")]])
        start_task(uid, approval_task(uid, int(cid), int(amount), status))
    elif data == "commands":
        await show(event, COMMANDS_TEXT, [[btn("⬅️ Back", "back")]])
    elif data == "back":
        sender = await event.get_sender()
        await send_menu(event.chat_id, uid, edit_event=event, name=getattr(sender, "first_name", "") or "")
    elif data == "stop":
        task = running_tasks.get(uid)
        if task and not task.done():
            task.cancel()


# ───────────────────────── force join gate ─────────────────────────
async def get_peer(event):
    """Input peer (with access hash) of whoever triggered the event; works even after a restart."""
    try:
        return await event.get_input_sender()
    except Exception:
        return None


async def is_member(uid: int, ch: dict, peer=None) -> bool:
    try:
        channel = types.InputChannel(int(ch["channel_id"]), int(ch["access_hash"]))
        res = await bot(
            functions.channels.GetParticipantRequest(
                channel=channel, participant=peer or await bot.get_input_entity(uid)
            )
        )
        part = res.participant
        return not isinstance(part, (types.ChannelParticipantLeft, types.ChannelParticipantBanned))
    except errors.UserNotParticipantError:
        return False
    except Exception as e:  # bot removed / channel deleted → don't lock users out
        log.warning("membership check failed for %s in %s: %s", uid, ch.get("chat_id"), e)
        return True


async def missing_channels(uid: int, peer=None):
    missing = []
    for ch in force_cache:
        if not await is_member(uid, ch, peer):
            missing.append((ch["title"], ch["link"]))
    return missing


def join_buttons(missing):
    """Blue "Join" buttons, two per row (an odd last one is full width), then a green confirm button."""
    joins = [ubtn("Join", link, "blue") for _title, link in missing]
    rows = [joins[i : i + 2] for i in range(0, len(joins), 2)]
    rows.append([btn("✅ I've Joined", "check_join", "green")])
    return rows


async def gate(event, with_video: bool = False) -> bool:
    """True if the user may use the bot. Otherwise sends the reason and returns False.
    with_video: show the welcome video together with the join prompt (used for /start)."""
    uid = event.sender_id
    sender = await event.get_sender()
    await track_user(
        uid,
        getattr(sender, "first_name", "") or "",
        getattr(sender, "username", "") or "",
        getattr(sender, "access_hash", None),
    )
    if uid in ADMIN_IDS:
        return True
    if is_banned(uid):
        await bot.send_message(uid, f"🚫 <b>{ft('You are banned from using this bot.')}</b>")
        return False
    if config.get("maintenance"):
        await bot.send_message(uid, f"🛠 <b>{ft('Bot is under maintenance. Try again later.')}</b>")
        return False
    missing = await missing_channels(uid, await get_peer(event))
    if missing:
        text = (
            f"😍 <b>Hey {esc(getattr(sender, 'first_name', '') or 'there')}! Welcome to the bot!</b>\n\n"
            "🔴 <b>Must join all channels below to continue</b>\n\n"
            "✅ <b>After joining, tap I've Joined</b>"
        )
        if with_video:
            await send_with_media(uid, text, join_buttons(missing))
        else:
            await bot.send_message(uid, text, buttons=join_buttons(missing))
        return False
    return True


# ───────────────────────── admin panel ─────────────────────────
awaiting: dict = {}   # admin_id -> what the next message is for
bc_draft: dict = {}   # admin_id -> {"msg_id": int, "pin": bool}

ADMIN_TEXT = f"🛡 <b>{ft('Admin Panel')}</b>\n\nChoose an option:"


def admin_menu():
    return [
        [btn("📊 Stats", "adm_stats"), btn("📢 Broadcast", "adm_bc")],
        [btn("🔒 Force Join", "adm_force"), btn("🎬 Welcome Setup", "adm_welcome")],
        [btn("💎 Premium Emoji", "adm_emoji"), btn("🚫 Ban / Unban", "adm_ban")],
        [btn("📤 Export Users", "adm_export"), btn("⚙️ Settings", "adm_settings")],
        [btn("❌ Close", "adm_close")],
    ]


def bc_buttons(pin: bool):
    return [
        [btn("📨 Send as Copy", "bc_copy"), btn("↪️ Send as Forward", "bc_fwd")],
        [btn(f"📌 Pin: {'ON' if pin else 'OFF'}", "bc_pin")],
        [btn("❌ Cancel", "bc_cancel")],
    ]


async def safe_edit(event, *args, **kwargs):
    try:
        await event.edit(*args, **kwargs)
    except errors.MessageNotModifiedError:
        pass


@bot.on(events.NewMessage(pattern=r"^/admin", func=lambda e: e.is_private and e.sender_id in ADMIN_IDS))
async def cmd_admin(event):
    awaiting.pop(event.sender_id, None)
    await event.respond(ADMIN_TEXT, buttons=admin_menu())


async def show_force(event):
    rows = force_cache
    text = f"🔒 <b>{ft('Force Join Channels')}</b>\n\n"
    text += "\n".join(f"{i}. {esc(r['title'])} — <code>{r['chat_id']}</code>" for i, r in enumerate(rows, 1)) or (
        "None set — users can use the bot freely."
    )
    buttons = [[btn(f"🗑 {r['title'][:22]}", f"fdel_{r['id']}")] for r in rows]
    buttons += [[btn("➕ Add Channel", "adm_fadd")], [btn("⬅️ Back", "adm_home")]]
    await safe_edit(event, text, buttons=buttons)


async def show_settings(event):
    welcome = bool(config.get("welcome_dm", True))
    maint = bool(config.get("maintenance", False))
    font = FONT_LABELS.get(config.get("font", "caps"), "Small Caps")
    await safe_edit(
        event,
        f"⚙️ <b>{ft('Settings')}</b>",
        buttons=[
            [btn(f"💌 Approval DM: {'ON' if welcome else 'OFF'}", "set_welcome")],
            [btn(f"🛠 Maintenance: {'ON' if maint else 'OFF'}", "set_maint")],
            [btn(f"🔤 Font: {font}", "set_font")],
            [btn("⬅️ Back", "adm_home")],
        ],
    )


async def show_welcome_setup(event):
    media = config.get("welcome_media")
    text = (
        f"🎬 <b>{ft('Welcome Setup')}</b>\n\n"
        f"🎞 Video: <b>{esc(media.get('kind', 'set')) if media else 'not set'}</b>\n"
        f"📝 Text: <b>{'custom' if config.get('welcome_text') else 'default'}</b>\n"
        f"🆘 Support link: <b>{esc(config.get('support_link') or 'not set')}</b>\n"
        f"📣 Updates link: <b>{esc(config.get('updates_link') or 'not set')}</b>\n\n"
        "The video + text appear on /start, and again right after a user joins the required channel(s)."
    )
    await safe_edit(
        event,
        text,
        buttons=[
            [btn("🎬 Set Video", "adm_wvid"), btn("📝 Set Text", "adm_wtxt")],
            [btn("👁 Preview", "adm_wprev"), btn("🗑 Remove Video", "adm_wrmv")],
            [btn("🆘 Support Link", "adm_wsup"), btn("📣 Updates Link", "adm_wupd")],
            [btn("♻️ Reset Text", "adm_wrst")],
            [btn("⬅️ Back", "adm_home")],
        ],
    )


premium_status: dict = {"text": None, "icons": None}


async def probe_styles(chat_id) -> bool:
    """Send a throw-away message with one green button to `chat_id` and see whether Telegram accepts the styling.
    Returns False if the message itself could not be sent (e.g. that user never started the bot)."""
    style_state.update(colors=None, error=None, fails=0, skip=None)
    try:
        test_btn = btn("Test", "noop", "green")
        log.info("style probe button: %s text=%r data=%r has_style=%s", type(test_btn).__name__,
                 getattr(test_btn, "text", None), getattr(test_btn, "data", None), hasattr(test_btn, "_sty"))
        m = await bot.send_message(chat_id, ".", buttons=[[test_btn]], silent=True)
    except Exception as e:
        log.info("style probe could not message %s: %s", chat_id, e)
        return False
    try:
        await m.delete()
    except Exception:
        pass
    log.info("Button colours: %s (error: %s, skipped: %s)", style_state["colors"], style_state["error"], style_state["skip"])
    return True


async def probe_premium(chat_id):
    """Learn whether Telegram shows this bot's premium emoji (in text and as button icons)."""
    global ICONS_OK
    premium_status.update(text=None, icons=None)
    style_state["icons"] = None
    ICONS_OK = True
    if not emoji_map:
        return premium_status
    sample = next(iter(emoji_map))
    try:
        m = await bot.send_message(chat_id, sample, silent=True)
        premium_status["text"] = any(isinstance(e, types.MessageEntityCustomEmoji) for e in (m.entities or []))
        await m.delete()
    except Exception as e:
        log.info("premium text probe failed: %s", e)
    if STYLE_API and style_state["colors"] is not False:
        try:
            m = await bot.send_message(chat_id, ".", buttons=[[btn(f"{sample} Test", "noop", "green")]], silent=True)
            await m.delete()
        except Exception as e:
            log.info("premium icon probe failed: %s", e)
    premium_status["icons"] = style_state["icons"]
    log.info("Premium emoji status: %s | styles: %s", premium_status, style_state)
    return premium_status


def _tick(v, ok="✅ working"):
    return ok if v else ("⚠️ not accepted by Telegram" if v is False else "❔ not tested")


def emoji_status_text() -> str:
    text = (
        f"Premium emoji in text: <b>{_tick(premium_status['text'])}</b>\n"
        f"Button colours: <b>{_tick(style_state['colors'], '✅ accepted by Telegram')}</b>\n"
        f"Premium icons on buttons: <b>{_tick(style_state['icons'])}</b>"
    )
    if not emoji_map:
        text += "\n<i>No premium emoji saved yet - tap Add Emoji.</i>"
    if style_state.get("error"):
        text += f"\n<i>Telegram said: {esc(style_state['error'])}</i>"
    if style_state.get("skip") and style_state["colors"] is None:
        text += f"\n<i>Styling was skipped: {esc(style_state['skip'])}</i>"
    return text


EMOJI_NEEDS = (
    "<i>Telegram lets a bot show premium emoji only if the bot's owner account has Telegram Premium "
    "(or the bot owns a Fragment username). If it isn't allowed, the bot keeps using normal emoji.</i>"
)


async def show_emoji_panel(event):
    try:
        n = len(json.loads(config.get("emoji_json") or "{}"))
    except Exception:
        n = 0
    text = (
        f"💎 <b>{ft('Premium Emoji')}</b>\n\n"
        f"Saved: <b>{n}</b>\n{emoji_status_text()}\n\n"
        "Send me a message containing premium emoji (several at once is fine). I learn each one and use it "
        "automatically wherever its normal version appears — in texts and as button icons.\n\n"
        "Used in the bot: ✅ 🔴 😍 ⚡ ➕ 🔐 📋 🚪 🛡 📊 📢 🔒 🎬 ⚙️ 🆘 📣 🛑 🔄 🔁 ⏳ ❌ 🎉\n\n" + EMOJI_NEEDS
    )
    await safe_edit(
        event,
        text,
        buttons=[
            [btn("➕ Add Emoji", "adm_eadd"), btn("📋 Saved", "adm_elist")],
            [btn("🧪 Test", "adm_etest"), btn("🧹 Clear All", "adm_eclr")],
            [btn("⬅️ Back", "adm_home")],
        ],
    )


async def save_premium_emoji(event):
    uid = event.sender_id
    msg = event.message
    ents = [e for e in (msg.entities or []) if isinstance(e, types.MessageEntityCustomEmoji)]
    if not ents:
        awaiting[uid] = "emoji"
        return await event.respond(
            "❌ I didn't see any premium emoji in that message. Send one that contains premium emoji "
            "(they animate in the emoji keyboard) (/cancel)."
        )
    inner = utils.get_inner_text(msg.raw_text or "", ents)
    try:
        pairs = json.loads(config.get("emoji_json") or "{}")
    except Exception:
        pairs = {}
    added = []
    for ent, ch in zip(ents, inner):
        ch = ch.strip()
        if ch:
            pairs[ch] = int(ent.document_id)
            added.append(ch)
    await set_config("emoji_json", json.dumps(pairs, ensure_ascii=False))
    rebuild_emoji()
    await event.respond(f"✅ <b>{ft('Saved')}</b> {len(added)}: {' '.join(added)}\n\n⏳ Testing with Telegram…")
    await probe_styles(uid)
    await probe_premium(uid)
    await event.respond(
        f"💎 <b>{ft('Premium Emoji')}</b>\n\n{emoji_status_text()}\n\n"
        "Send more premium emoji any time, or tap below.",
        buttons=[[btn("➕ Add More", "adm_eadd"), btn("💎 Premium Emoji", "adm_emoji")]],
    )


async def admin_callback(event, data: str, uid: int):
    if data == "adm_home":
        awaiting.pop(uid, None)
        await safe_edit(event, ADMIN_TEXT, buttons=admin_menu())

    elif data == "adm_close":
        awaiting.pop(uid, None)
        await event.delete()

    elif data == "adm_stats":
        now = int(time.time())
        await flush_approved()
        total = await count("users")
        new24 = await count("users", FieldFilter("joined", ">", now - 86400))
        active24 = await count("users", FieldFilter("last_active", ">", now - 86400))
        blocked = await count("users", FieldFilter("blocked", "==", True))
        logins = await count("sessions")
        stored = await count("pending")
        text = (
            f"📊 <b>{ft('Bot Statistics')}</b>\n\n"
            f"👥 Total users: <b>{total}</b>\n"
            f"🆕 New (24h): <b>{new24}</b>\n"
            f"🟢 Active (24h): <b>{active24}</b>\n"
            f"🚫 Banned: <b>{len(banned_ids)}</b>\n"
            f"⛔ Blocked the bot: <b>{blocked}</b>\n"
            f"🔐 Logged-in accounts: <b>{logins}</b>\n"
            f"🔒 Force-join channels: <b>{len(force_cache)}</b>\n"
            f"⏳ Stored pending requests: <b>{stored}</b>\n"
            f"✅ Total approved: <b>{config['approved']}</b>"
        )
        await safe_edit(
            event, text,
            buttons=[[btn("🔄 Refresh", "adm_stats"), btn("⬅️ Back", "adm_home")]],
        )

    elif data == "adm_force":
        await show_force(event)

    elif data == "adm_fadd":
        awaiting[uid] = "fadd"
        await safe_edit(
            event,
            f"➕ <b>{ft('Add force-join channel')}</b>\n\n"
            "Make the bot <b>admin</b> in that channel first (give it <i>Invite Users</i> for private channels).\n\n"
            "Then either:\n• <b>forward any post</b> from the channel (works for private ones), or\n"
            "• send its <code>@username</code> / numeric ID <code>-100…</code>\n\n/cancel to abort",
            buttons=[[btn("⬅️ Back", "adm_force")]],
        )

    elif data.startswith("fdel_"):
        await db.collection("force_channels").document(data[5:]).delete()
        await refresh_force()
        await show_force(event)

    elif data == "adm_ban":
        awaiting[uid] = "ban"
        await safe_edit(
            event,
            f"🚫 <b>{ft('Ban / Unban')}</b>\n\nSend the user's <b>ID</b> or <b>@username</b>, or forward one of their messages. "
            "Sending again toggles the ban.\n\n/cancel to abort",
            buttons=[[btn("⬅️ Back", "adm_home")]],
        )

    elif data == "adm_bc":
        awaiting[uid] = "bc"
        await safe_edit(
            event,
            f"📢 <b>{ft('Broadcast')}</b>\n\nSend the message you want to broadcast — text, photo, video, document, "
            "voice, sticker, poll, anything. It is delivered in its original form.\n"
            f"Recipients: <b>{await recipients_count()}</b>\n\n/cancel to abort",
            buttons=[[btn("⬅️ Back", "adm_home")]],
        )

    elif data == "bc_pin":
        draft = bc_draft.get(uid)
        if draft:
            draft["pin"] = not draft["pin"]
            await safe_edit(event, buttons=bc_buttons(draft["pin"]))

    elif data in ("bc_copy", "bc_fwd"):
        draft = bc_draft.pop(uid, None)
        if not draft:
            return await safe_edit(event, "⌛ Draft expired. Open /admin → Broadcast again.")
        await safe_edit(event, f"🚀 <b>{ft('Broadcast started')}</b>")
        start_task(uid, do_broadcast(uid, draft["msg_id"], data == "bc_fwd", draft["pin"]))

    elif data == "bc_cancel":
        bc_draft.pop(uid, None)
        await safe_edit(event, "❌ Broadcast cancelled.", buttons=[[btn("⬅️ Back", "adm_home")]])

    elif data == "adm_export":
        path = None
        n = 0
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as f:
            path = f.name
            f.write("user_id,username,first_name,joined_unix,banned,blocked\n")
            async for d in db.collection("users").stream():
                r = d.to_dict() or {}
                name = (r.get("first_name") or "").replace(",", " ").replace("\n", " ")
                f.write(
                    f"{d.id},{r.get('username') or ''},{name},{r.get('joined', '')},"
                    f"{int(bool(r.get('banned')))},{int(bool(r.get('blocked')))}\n"
                )
                n += 1
        try:
            await bot.send_file(uid, path, caption=f"📤 {n} users", file_name="users_export.csv")
        finally:
            os.remove(path)

    elif data == "adm_settings":
        await show_settings(event)

    elif data == "set_welcome":
        await set_config("welcome_dm", not config.get("welcome_dm", True))
        await show_settings(event)

    elif data == "set_maint":
        await set_config("maintenance", not config.get("maintenance", False))
        await show_settings(event)

    elif data == "adm_emoji":
        awaiting.pop(uid, None)
        await show_emoji_panel(event)

    elif data == "adm_eadd":
        awaiting[uid] = "emoji"
        await safe_edit(
            event,
            f"💎 <b>{ft('Add premium emoji')}</b>\n\nSend a message containing the premium emoji you want "
            "(e.g. ✅ 🔴 😍 ⚡ — use the premium version of each).\n\n/cancel to abort",
            buttons=[[btn("⬅️ Back", "adm_emoji")]],
        )

    elif data == "adm_elist":
        try:
            pairs = json.loads(config.get("emoji_json") or "{}")
        except Exception:
            pairs = {}
        body = "\n".join(f"{k} → <code>{v}</code>" for k, v in list(pairs.items())[:40]) or "None saved yet."
        await safe_edit(event, f"📋 <b>{ft('Saved premium emoji')}</b>\n\n{body}", buttons=[[btn("⬅️ Back", "adm_emoji")]])

    elif data == "adm_etest":
        await safe_edit(event, f"🧪 {ft('Testing…')}")
        await probe_styles(uid)
        await probe_premium(uid)
        await show_emoji_panel(event)

    elif data == "adm_eclr":
        await set_config("emoji_json", None)
        rebuild_emoji()
        premium_status.update(text=None, icons=None)
        style_state["icons"] = None
        await show_emoji_panel(event)

    elif data == "set_font":
        order = ["caps", "bold", "plain"]
        cur = config.get("font", "caps")
        await set_config("font", order[(order.index(cur) + 1) % 3 if cur in order else 0])
        await show_settings(event)

    elif data == "adm_welcome":
        awaiting.pop(uid, None)
        await show_welcome_setup(event)

    elif data == "adm_wvid":
        awaiting[uid] = "wvid"
        await safe_edit(
            event,
            f"🎬 <b>{ft('Set welcome video')}</b>\n\nSend the short video now (a GIF or photo also works). "
            "Keep it small so it loads fast.\n\n/cancel to abort",
            buttons=[[btn("⬅️ Back", "adm_welcome")]],
        )

    elif data == "adm_wtxt":
        awaiting[uid] = "wtxt"
        await safe_edit(
            event,
            f"📝 <b>{ft('Set welcome text')}</b>\n\nSend the message shown under the video. Bold / italic formatting is kept. "
            "Write <code>{name}</code> where the user's first name should appear. Max 700 characters.\n\n/cancel to abort",
            buttons=[[btn("⬅️ Back", "adm_welcome")]],
        )

    elif data in ("adm_wsup", "adm_wupd"):
        awaiting[uid] = "wsup" if data == "adm_wsup" else "wupd"
        label = "Support" if data == "adm_wsup" else "Updates"
        await safe_edit(
            event,
            f"🔗 <b>{ft(label + ' link')}</b>\n\nSend a link (<code>https://…</code> or <code>t.me/…</code>). "
            "Send <code>off</code> to remove it.\n\n/cancel to abort",
            buttons=[[btn("⬅️ Back", "adm_welcome")]],
        )

    elif data == "adm_wprev":
        await send_welcome(uid, uid, "Admin")

    elif data == "adm_wrmv":
        await set_config("welcome_media", None)
        await show_welcome_setup(event)

    elif data == "adm_wrst":
        await set_config("welcome_text", None)
        await show_welcome_setup(event)


@bot.on(
    events.NewMessage(
        func=lambda e: e.is_private and e.sender_id in ADMIN_IDS and e.sender_id in awaiting
    )
)
async def admin_input(event):
    uid = event.sender_id
    mode = awaiting.pop(uid)
    text = (event.raw_text or "").strip()
    if text.startswith("/"):
        if text.startswith("/cancel"):
            await event.respond("❌ Cancelled.", buttons=admin_menu())
        return  # other commands are handled by their own handlers

    if mode == "bc":
        bc_draft[uid] = {"msg_id": event.id, "pin": False}
        await event.reply(
            f"📢 <b>{ft('Ready to broadcast')}</b> to <b>{await recipients_count()}</b> users.\n"
            "Choose how to deliver the message above:",
            buttons=bc_buttons(False),
        )
    elif mode == "fadd":
        await add_force_channel(event)
    elif mode == "ban":
        await toggle_ban(event)
    elif mode == "emoji":
        await save_premium_emoji(event)
    elif mode == "wvid":
        await save_welcome_media(event)
    elif mode == "wtxt":
        body = (event.message.text or "").strip()  # keeps the admin's bold/italic as HTML
        if not body or len(text) > 700:
            awaiting[uid] = "wtxt"
            return await event.respond("❌ Send text of 1–700 characters (/cancel).")
        await set_config("welcome_text", body)
        await event.respond(
            f"✅ <b>{ft('Welcome text saved')}</b>",
            buttons=[[btn("👁 Preview", "adm_wprev"), btn("🎬 Welcome Setup", "adm_welcome")]],
        )
    elif mode in ("wsup", "wupd"):
        key = "support_link" if mode == "wsup" else "updates_link"
        link = text.strip()
        if link.lower() in ("off", "none", "remove"):
            link = None
        else:
            if re.match(r"^(t\.me|telegram\.me)/", link, re.I):
                link = "https://" + link
            if not re.match(r"^https?://\S+\.\S+$", link):
                awaiting[uid] = mode
                return await event.respond("❌ That doesn't look like a link. Try again, or send <code>off</code> (/cancel).")
        await set_config(key, link)
        await event.respond(
            f"✅ <b>{ft('Saved')}</b>",
            buttons=[[btn("👁 Preview", "adm_wprev"), btn("🎬 Welcome Setup", "adm_welcome")]],
        )


async def save_welcome_media(event):
    msg = event.message
    kind = "video" if msg.video else "gif" if msg.gif else "photo" if msg.photo else None
    if not kind and msg.document and (getattr(msg.file, "mime_type", "") or "").startswith("video/"):
        kind = "video"
    file_id = utils.pack_bot_file_id(msg.media) if kind else None
    if not file_id:
        awaiting[event.sender_id] = "wvid"
        return await event.respond("❌ Please send a video, GIF or photo as a normal message (/cancel).")
    await set_config("welcome_media", {"file_id": file_id, "kind": kind})
    await event.respond(
        f"✅ <b>{ft('Welcome media saved')}</b> ({kind})",
        buttons=[[btn("👁 Preview", "adm_wprev"), btn("🎬 Welcome Setup", "adm_welcome")]],
    )


async def bot_admin_state(ent):
    """(is_admin, can_invite) for the BOT ITSELF in `ent`.
    (The old check used get_permissions() without a user, which only returns the chat's *default* permissions
    and therefore always said "not an admin".)"""
    try:
        res = await bot(
            functions.channels.GetParticipantRequest(channel=ent, participant=types.InputPeerSelf())
        )
        part = res.participant
        if isinstance(part, types.ChannelParticipantCreator):
            return True, True
        if isinstance(part, types.ChannelParticipantAdmin):
            rights = part.admin_rights
            return True, bool(rights and rights.invite_users)
        return False, False
    except errors.UserNotParticipantError:
        return False, False
    except Exception as e:
        log.warning("GetParticipant(self) failed (%s) - trying get_permissions", e)
        perms = await bot.get_permissions(ent, "me")
        return bool(perms.is_admin), bool(getattr(perms, "invite_users", False) or perms.is_creator)


async def add_force_channel(event):
    uid = event.sender_id
    msg = event.message
    ent, ref = None, None
    fwd = msg.forward
    if fwd is not None:
        if isinstance(getattr(fwd, "chat", None), types.Channel):
            ent = fwd.chat
        elif msg.fwd_from and isinstance(msg.fwd_from.from_id, types.PeerChannel):
            ref = get_peer_id(msg.fwd_from.from_id)
        else:
            awaiting[uid] = "fadd"
            return await event.respond(
                "❌ That post doesn't reveal its channel. Send the channel's <code>@username</code> or <code>-100…</code> ID instead (/cancel)."
            )
    else:
        t = (msg.raw_text or "").strip()
        m = re.search(r"(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,})/?$", t)
        if m:
            ref = m.group(1)
        elif re.fullmatch(r"-?\d+", t):
            ref = int(t)
        elif t.startswith("@"):
            ref = t[1:]
    if ent is None:
        if ref is None:
            awaiting[uid] = "fadd"
            return await event.respond("❌ Couldn't read that. Forward a channel post, or send @username / -100… ID (/cancel).")
        try:
            ent = await bot.get_entity(ref)
        except Exception:
            awaiting[uid] = "fadd"
            return await event.respond(
                "❌ Channel not found. Add the bot there as admin first, then forward a post from it (/cancel)."
            )
    if not isinstance(ent, types.Channel):
        awaiting[uid] = "fadd"
        return await event.respond("❌ Only channels and supergroups can be used (not small legacy groups) (/cancel).")

    is_admin, can_invite = await bot_admin_state(ent)
    if not is_admin:
        awaiting[uid] = "fadd"
        return await event.respond(
            f"❌ The bot is not an admin in <b>{esc(ent.title)}</b> yet.\n"
            "Open the channel → <b>Administrators</b> → <b>Add Admin</b> → pick this bot, then send the post again (/cancel)."
        )

    if ent.username:
        link = f"https://t.me/{ent.username}"
    else:
        try:
            if not can_invite:
                raise ValueError("no invite right")
            inv = await bot(functions.messages.ExportChatInviteRequest(peer=ent))
            link = inv.link
        except Exception:
            awaiting[uid] = "fadd"
            return await event.respond(
                f"❌ <b>{esc(ent.title)}</b> is private, so the bot needs the <b>Invite Users</b> admin right to create a join link. "
                "Enable it for the bot, then send the post again (/cancel)."
            )
    chat_id = get_peer_id(ent)
    await db.collection("force_channels").document(str(chat_id)).set(
        {
            "chat_id": chat_id,
            "channel_id": ent.id,
            "access_hash": ent.access_hash,
            "title": ent.title,
            "link": link,
            "created": int(time.time()),
        }
    )
    await refresh_force()
    await event.respond(
        f"✅ <b>{ft('Added')}</b>: {esc(ent.title)}\nUsers must now join it before using the bot.",
        buttons=[[btn("🔒 Force Join List", "adm_force"), btn("🛡 Panel", "adm_home")]],
    )


async def toggle_ban(event):
    msg = event.message
    target = None
    if msg.fwd_from and isinstance(msg.fwd_from.from_id, types.PeerUser):
        target = msg.fwd_from.from_id.user_id
    else:
        t = (msg.raw_text or "").strip()
        if re.fullmatch(r"\d+", t):
            target = int(t)
        elif t.startswith("@"):
            q = db.collection("users").where(filter=FieldFilter("username_lower", "==", t[1:].lower())).limit(1)
            found = [d async for d in q.stream()]
            target = int(found[0].id) if found else None
    if target is None:
        awaiting[event.sender_id] = "ban"
        return await event.respond("❌ User not found. Send a numeric ID, a known @username, or forward their message (/cancel).")
    if target in ADMIN_IDS:
        return await event.respond("❌ You can't ban an admin.")

    ref = db.collection("users").document(str(target))
    snap = await ref.get()
    if not snap.exists:
        now = int(time.time())
        await ref.set(
            {"first_name": "", "username": "", "username_lower": "", "joined": now, "last_active": now,
             "banned": True, "blocked": False, "access_hash": None}
        )
        new = True
    else:
        new = not bool((snap.to_dict() or {}).get("banned"))
        await ref.set({"banned": new}, merge=True)
    (banned_ids.add if new else banned_ids.discard)(target)
    await event.respond(
        f"{'🚫 Banned' if new else '✅ Unbanned'} <code>{target}</code>",
        buttons=[[btn("🚫 Ban / Unban another", "adm_ban"), btn("🛡 Panel", "adm_home")]],
    )


async def do_broadcast(admin_id: int, msg_id: int, as_forward: bool, pin: bool):
    users = await recipients()
    status = await bot.send_message(
        admin_id, f"📢 <b>{ft('Broadcasting…')}</b> 0 / {len(users)}",
        buttons=[[btn("🛑 Stop", "stop")]],
    )
    sent = failed = 0
    blocked_ids = []
    try:
        for i, (u, ah) in enumerate(users, 1):
            for _ in range(2):  # one retry after a flood wait
                try:
                    m = await bot.forward_messages(
                        user_peer(u, ah), msg_id, from_peer=admin_id, drop_author=not as_forward
                    )
                    sent += 1
                    if pin:
                        try:
                            await bot.pin_message(user_peer(u, ah), m, notify=False)
                        except Exception:
                            pass
                    break
                except errors.FloodWaitError as e:
                    await asyncio.sleep(e.seconds + 1)
                except (errors.UserIsBlockedError, errors.InputUserDeactivatedError):
                    blocked_ids.append(u)
                    break
                except Exception:
                    failed += 1
                    break
            else:
                failed += 1
            if i % 50 == 0:
                try:
                    await status.edit(
                        f"📢 <b>{ft('Broadcasting…')}</b> {i} / {len(users)}", buttons=[[btn("🛑 Stop", "stop")]],
                    )
                except Exception:
                    pass
            await asyncio.sleep(0.05)
    except asyncio.CancelledError:
        await mark_blocked(blocked_ids)
        await status.edit(
            f"🛑 <b>{ft('Broadcast stopped')}</b>\nSent: {sent} | Blocked: {len(blocked_ids)} | Failed: {failed}",
        )
        raise
    await mark_blocked(blocked_ids)
    await status.edit(
        f"✅ <b>{ft('Broadcast finished')}</b>\n\n"
        f"📨 Sent: <b>{sent}</b>\n⛔ Blocked/deleted: <b>{len(blocked_ids)}</b>\n❌ Failed: <b>{failed}</b>",
        buttons=[[btn("🛡 Panel", "adm_home")]],
    )


# ───────────────────────── main ─────────────────────────
async def start_web():
    """Tiny HTTP server: Render web services must listen on $PORT, and it gives pingers something to hit."""
    async def health(_request):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "10000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Health server listening on :%s", port)


async def keep_alive():
    """Ping our own public URL every 9 minutes so Render's free plan doesn't spin the service down (15 min idle)."""
    base = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("KEEPALIVE_URL")
    if not base:
        log.info("No RENDER_EXTERNAL_URL / KEEPALIVE_URL set — self-ping disabled.")
        return
    url = base.rstrip("/") + "/health"
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            await asyncio.sleep(540)
            try:
                async with session.get(url) as resp:
                    log.info("keep-alive ping → %s", resp.status)
            except Exception as e:
                log.warning("keep-alive ping failed: %s", e)


async def probe_at_startup():
    """Check at boot whether Telegram accepts coloured buttons / premium icons, and log the result."""
    if not STYLE_API:
        log.info("Button styling disabled by BUTTON_STYLES=0")
        return
    for admin in ADMIN_IDS:
        if await probe_styles(admin):
            if emoji_map:
                await probe_premium(admin)
            log.info("Button styling: colours=%s icons=%s error=%s", style_state["colors"], style_state["icons"], style_state["error"])
            return
    log.info("Button styling: not tested yet (no admin has started the bot) - it is applied anyway.")


async def main():
    global db, BOT_USERNAME
    await start_web()  # open the port first so Render sees the service as live
    asyncio.create_task(keep_alive())
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS is empty — admin panel is disabled until you set it.")
    db = make_db()
    await load_caches()
    await bot.start(bot_token=BOT_TOKEN)
    BOT_USERNAME = (await bot.get_me()).username
    await probe_at_startup()
    log.info("Bot @%s is running (Firestore connected)", BOT_USERNAME)
    try:
        await bot.run_until_disconnected()
    finally:
        await flush_approved()
        if _http is not None and not _http.closed:
            await _http.close()


if __name__ == "__main__":
    bot.loop.run_until_complete(main())
