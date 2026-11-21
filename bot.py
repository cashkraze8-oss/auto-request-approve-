"""
Telegram Auto-Approve Join Requests Bot  (Telethon + Firebase Firestore)

Private chat : /start /login /logout /accept_bulk /accept_pending /help /admin
Channel/group: /accept  (toggle auto / pending)   /accept_multi  (approve stored pending)

ENV: API_ID, API_HASH (my.telegram.org), BOT_TOKEN (@BotFather), ENCRYPTION_KEY (Fernet key),
     ADMIN_IDS (comma-separated Telegram IDs),
     FIREBASE_CREDENTIALS (service-account JSON text)  or  FIREBASE_CREDENTIALS_FILE (path to it)
"""
import asyncio
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
from telethon import Button, TelegramClient, errors, events, functions, types
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
bot = TelegramClient(StringSession(), API_ID, API_HASH)
BOT_USERNAME = ""
db: firestore.AsyncClient = None  # created in main()


# ───────────────────────── fancy font ─────────────────────────
def ft(text: str) -> str:
    """Convert to 𝗕𝗼𝗹𝗱 𝗦𝗮𝗻𝘀 unicode style."""
    out = []
    for ch in text:
        if "a" <= ch <= "z":
            out.append(chr(0x1D5EE + ord(ch) - 97))
        elif "A" <= ch <= "Z":
            out.append(chr(0x1D5D4 + ord(ch) - 65))
        elif "0" <= ch <= "9":
            out.append(chr(0x1D7EC + ord(ch) - 48))
        else:
            out.append(ch)
    return "".join(out)


def btn(label: str, data: str):
    return Button.inline(ft(label), data.encode())


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


def make_db() -> firestore.AsyncClient:
    raw = os.environ.get("FIREBASE_CREDENTIALS")
    path = os.environ.get("FIREBASE_CREDENTIALS_FILE")
    if raw:
        info = json.loads(raw)
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


async def save_session(user_id: int, session: str):
    await db.collection("sessions").document(str(user_id)).set(
        {"session": FERNET.encrypt(session.encode()).decode()}
    )


async def load_session(user_id: int):
    snap = await db.collection("sessions").document(str(user_id)).get()
    if not snap.exists:
        return None
    return FERNET.decrypt(snap.to_dict()["session"].encode()).decode()


async def delete_session(user_id: int):
    await db.collection("sessions").document(str(user_id)).delete()


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


async def pending_count(client, entity) -> int:
    full = await client(functions.channels.GetFullChannelRequest(entity))
    return getattr(full.full_chat, "requests_pending", 0) or 0


# ───────────────────────── menus / texts ─────────────────────────
def menu_buttons(logged_in: bool):
    rows = [
        [
            Button.url(ft("➕ Add to Channel"), f"https://t.me/{BOT_USERNAME}?startchannel=true&admin=invite_users"),
            Button.url(ft("➕ Add to Group"), f"https://t.me/{BOT_USERNAME}?startgroup=true&admin=invite_users"),
        ]
    ]
    if logged_in:
        rows += [
            [btn("✅ Accept Bulk", "bulk"), btn("📊 My Channels", "channels")],
            [btn("📋 Commands", "commands"), btn("🚪 Logout", "logout")],
        ]
    else:
        rows += [[btn("🔐 Login", "login"), btn("📋 Commands", "commands")]]
    return rows


START_TEXT = (
    f"<b>{ft('Auto Approve Bot')}</b>\n\n"
    "Add me as <b>admin</b> to your channel or group and I'll approve join requests for you.\n\n"
    "Use <b>Login</b> to also clear requests that were already pending."
)

COMMANDS_TEXT = (
    f"<b>{ft('Available Commands')}</b>\n\n"
    "/start - Start the bot\n"
    "/login - Authenticate your account to accept pending requests\n"
    "/logout - Remove your saved session\n"
    "/accept_bulk - Accept all pending requests in every channel you admin\n"
    "/accept_pending <code>channel_id delay limit</code> - Accept pending requests of one channel "
    "(delay = seconds between approvals, limit = max count, 0 = all). "
    "If there are 10k+ pending, run again after a 4-hour gap once approvals stop.\n\n"
    f"<b>{ft('Channel commands')}</b>\n"
    "/accept_multi - Post in channel: approve all requests the bot stored "
    "(bot must have been admin before the request arrived)\n"
    "/accept - Post in channel: toggle auto-accept ⇄ pending mode"
)


async def send_menu(chat_id, uid, edit_event=None):
    buttons = menu_buttons(await load_session(uid) is not None)
    if uid in ADMIN_IDS:
        buttons.append([btn("🛡 Admin Panel", "adm_home")])
    if edit_event:
        await edit_event.edit(START_TEXT, buttons=buttons, parse_mode="html")
    else:
        await bot.send_message(chat_id, START_TEXT, buttons=buttons, parse_mode="html")


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
            await bot.send_message(user_id, WELCOME_DM.format(chat=chat.title), parse_mode="html")
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
    msg = await event.respond(text, parse_mode="html")
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
    status = await event.respond(f"⏳ {ft('Approving')} {len(docs)} …", parse_mode="html")
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
        f"✅ <b>{ft('Done')}</b>\nApproved: <b>{ok}</b>\nFailed: <b>{fail}</b>", parse_mode="html"
    )


# ───────────────────────── private commands ─────────────────────────
@bot.on(events.NewMessage(pattern=r"^/start", func=lambda e: e.is_private))
async def cmd_start(event):
    if not await gate(event):
        return
    await send_menu(event.chat_id, event.sender_id)


@bot.on(events.NewMessage(pattern=r"^/help", func=lambda e: e.is_private))
async def cmd_help(event):
    if not await gate(event):
        return
    await event.respond(COMMANDS_TEXT, parse_mode="html", buttons=[[btn("⬅️ Back", "back")]])


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
    start_task(event.sender_id, do_bulk(event.sender_id))


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
            parse_mode="html",
        )
    start_task(
        event.sender_id,
        do_pending(event.sender_id, chan, float(delay or 1), int(limit or 0)),
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
            uid, f"✅ <b>{ft('Already logged in')}</b>", parse_mode="html", buttons=menu_buttons(True)
        )
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    try:
        async with bot.conversation(uid, timeout=300) as conv:
            await conv.send_message(
                f"🔐 <b>{ft('Login')}</b>\nSend your phone number with country code, e.g. <code>+919876543210</code>\n"
                "(/cancel to abort)",
                parse_mode="html",
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
                parse_mode="html",
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
                f"✅ <b>{ft('Login successful')}</b>", parse_mode="html", buttons=menu_buttons(True)
            )
    except asyncio.TimeoutError:
        await bot.send_message(uid, "⌛ Login timed out. Run /login again.")
    except errors.PasswordHashInvalidError:
        await bot.send_message(uid, "❌ Wrong 2FA password. Run /login again.")
    except Exception as e:
        log.exception("login failed")
        await bot.send_message(uid, f"❌ Login failed: {e}")
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
    await bot.send_message(uid, f"🚪 <b>{ft('Logged out')}</b>", parse_mode="html", buttons=menu_buttons(False))


# ───────────────────────── account-based tasks ─────────────────────────
async def need_login(uid: int):
    client = await get_user_client(uid)
    if not client:
        await bot.send_message(
            uid, f"🔐 {ft('Please login first.')}", buttons=[[btn("🔐 Login", "login")]]
        )
    return client


async def do_bulk(uid: int):
    client = await need_login(uid)
    if not client:
        return
    status = await bot.send_message(uid, f"⏳ {ft('Scanning your channels…')}", buttons=[[btn("🛑 Stop", "stop")]])
    done, total, lines = 0, 0, []
    try:
        async for d in client.iter_dialogs():
            ent = d.entity
            if not can_approve(ent):
                continue
            try:
                n = await pending_count(client, ent)
                if n <= 0:
                    continue
                await client(functions.messages.HideAllChatJoinRequestsRequest(peer=ent, approved=True))
                done += 1
                total += n
                await bump(n)
                lines.append(f"• {ent.title} — <b>{n}</b>")
            except errors.FloodWaitError as e:
                await asyncio.sleep(e.seconds)
            except Exception as e:
                lines.append(f"• {ent.title} — ❌ {type(e).__name__}")
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        await status.edit(f"🛑 {ft('Stopped')}")
        raise
    body = "\n".join(lines[:40]) or "No pending requests found."
    await status.edit(
        f"✅ <b>{ft('Bulk accept finished')}</b>\nChannels: <b>{done}</b> | Requests: <b>{total}</b>\n\n{body}\n\n"
        "<i>If requests remain, Telegram's limit may apply — retry after ~4 hours.</i>",
        parse_mode="html",
        buttons=menu_buttons(True),
    )


async def do_pending(uid: int, chan: str, delay: float, limit: int):
    client = await need_login(uid)
    if not client:
        return
    try:
        ref = int(chan) if re.fullmatch(r"-?\d+", chan) else chan
        try:
            ent = await client.get_entity(ref)
        except (ValueError, errors.RPCError):
            await client.get_dialogs()
            ent = await client.get_entity(ref)
    except Exception:
        return await bot.send_message(uid, "❌ Channel not found. Check the ID/username and that your account is in it.")
    if not can_approve(ent):
        return await bot.send_message(uid, "❌ Your account needs the <b>Invite Users</b> admin right there.", parse_mode="html")

    total = await pending_count(client, ent)
    status = await bot.send_message(
        uid,
        f"⏳ <b>{ft(ent.title)}</b>\nPending: <b>{total}</b>",
        parse_mode="html",
        buttons=[[btn("🛑 Stop", "stop")]],
    )
    approved = 0
    try:
        while not limit or approved < limit:
            batch = await client(
                functions.messages.GetChatInviteImportersRequest(
                    peer=ent, requested=True, offset_date=None, offset_user=types.InputUserEmpty(), limit=100
                )
            )
            if not batch.importers:
                break
            for imp in batch.importers:
                if limit and approved >= limit:
                    break
                try:
                    await client(
                        functions.messages.HideChatJoinRequestRequest(
                            peer=ent, user_id=await client.get_input_entity(imp.user_id), approved=True
                        )
                    )
                    approved += 1
                    await bump()
                except errors.FloodWaitError as e:
                    await status.edit(f"⏸ Flood wait {e.seconds}s … approved so far: <b>{approved}</b>", parse_mode="html")
                    await asyncio.sleep(e.seconds)
                except Exception:
                    pass
                if approved and approved % 50 == 0:
                    try:
                        await status.edit(
                            f"⏳ <b>{ft(ent.title)}</b>\nApproved: <b>{approved}</b> / {total}",
                            parse_mode="html",
                            buttons=[[btn("🛑 Stop", "stop")]],
                        )
                    except Exception:
                        pass
                await asyncio.sleep(delay)
    except asyncio.CancelledError:
        await status.edit(f"🛑 {ft('Stopped')} — approved <b>{approved}</b>", parse_mode="html")
        raise
    except errors.RPCError as e:
        await bot.send_message(uid, f"⚠️ Telegram stopped the process: {type(e).__name__}. Retry after ~4 hours.")
    await status.edit(
        f"✅ <b>{ft('Finished')}</b>\n{ent.title}\nApproved: <b>{approved}</b>",
        parse_mode="html",
        buttons=menu_buttons(True),
    )


async def show_channels(uid: int, event):
    client = await need_login(uid)
    if not client:
        return
    await event.edit(f"⏳ {ft('Loading…')}")
    lines = []
    async for d in client.iter_dialogs():
        if can_approve(d.entity):
            try:
                n = await pending_count(client, d.entity)
            except Exception:
                n = "?"
            lines.append(f"• {d.entity.title} — <code>{get_peer_id(d.entity)}</code> — <b>{n}</b>")
        if len(lines) >= 40:
            break
    await event.edit(
        f"<b>{ft('Your Channels')}</b> (id — pending)\n\n" + ("\n".join(lines) or "None found."),
        parse_mode="html",
        buttons=[[btn("✅ Accept Bulk", "bulk"), btn("⬅️ Back", "back")]],
    )


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
        missing = await missing_channels(uid)
        if missing:
            await event.answer("⚠️ You haven't joined all required channels yet!", alert=True)
            return
        await event.answer()
        if await gate(event):
            await send_menu(event.chat_id, uid, edit_event=event)
        return

    if uid not in ADMIN_IDS and not await gate(event):
        return

    if data == "login":
        asyncio.create_task(do_login(uid))
    elif data == "logout":
        await do_logout(uid)
    elif data == "bulk":
        start_task(uid, do_bulk(uid))
    elif data == "channels":
        await show_channels(uid, event)
    elif data == "commands":
        await event.edit(COMMANDS_TEXT, parse_mode="html", buttons=[[btn("⬅️ Back", "back")]])
    elif data == "back":
        await send_menu(event.chat_id, uid, edit_event=event)
    elif data == "stop":
        task = running_tasks.get(uid)
        if task and not task.done():
            task.cancel()


# ───────────────────────── force join gate ─────────────────────────
async def is_member(uid: int, ch: dict) -> bool:
    try:
        channel = types.InputChannel(int(ch["channel_id"]), int(ch["access_hash"]))
        res = await bot(
            functions.channels.GetParticipantRequest(
                channel=channel, participant=await bot.get_input_entity(uid)
            )
        )
        part = res.participant
        return not isinstance(part, (types.ChannelParticipantLeft, types.ChannelParticipantBanned))
    except errors.UserNotParticipantError:
        return False
    except Exception as e:  # bot removed / channel deleted → don't lock users out
        log.warning("membership check failed for %s in %s: %s", uid, ch.get("chat_id"), e)
        return True


async def missing_channels(uid: int):
    missing = []
    for ch in force_cache:
        if not await is_member(uid, ch):
            missing.append((ch["title"], ch["link"]))
    return missing


def join_buttons(missing):
    rows = [[Button.url(ft(f"📢 Join {title}"[:40]), link)] for title, link in missing]
    rows.append([btn("✅ I've Joined", "check_join")])
    return rows


async def gate(event) -> bool:
    """True if the user may use the bot. Otherwise sends the reason and returns False."""
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
        await bot.send_message(uid, f"🚫 <b>{ft('You are banned from using this bot.')}</b>", parse_mode="html")
        return False
    if config.get("maintenance"):
        await bot.send_message(uid, f"🛠 <b>{ft('Bot is under maintenance. Try again later.')}</b>", parse_mode="html")
        return False
    missing = await missing_channels(uid)
    if missing:
        await bot.send_message(
            uid,
            f"🔒 <b>{ft('Join required')}</b>\n\nJoin the channel(s) below, then tap <b>I've Joined</b> to continue.",
            parse_mode="html",
            buttons=join_buttons(missing),
        )
        return False
    return True


# ───────────────────────── admin panel ─────────────────────────
awaiting: dict = {}   # admin_id -> what the next message is for
bc_draft: dict = {}   # admin_id -> {"msg_id": int, "pin": bool}

ADMIN_TEXT = f"🛡 <b>{ft('Admin Panel')}</b>\n\nChoose an option:"


def admin_menu():
    return [
        [btn("📊 Stats", "adm_stats"), btn("📢 Broadcast", "adm_bc")],
        [btn("🔒 Force Join", "adm_force"), btn("🚫 Ban / Unban", "adm_ban")],
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
    await event.respond(ADMIN_TEXT, buttons=admin_menu(), parse_mode="html")


async def show_force(event):
    rows = force_cache
    text = f"🔒 <b>{ft('Force Join Channels')}</b>\n\n"
    text += "\n".join(f"{i}. {r['title']} — <code>{r['chat_id']}</code>" for i, r in enumerate(rows, 1)) or (
        "None set — users can use the bot freely."
    )
    buttons = [[btn(f"🗑 {r['title'][:22]}", f"fdel_{r['id']}")] for r in rows]
    buttons += [[btn("➕ Add Channel", "adm_fadd")], [btn("⬅️ Back", "adm_home")]]
    await safe_edit(event, text, buttons=buttons, parse_mode="html")


async def show_settings(event):
    welcome = bool(config.get("welcome_dm", True))
    maint = bool(config.get("maintenance", False))
    await safe_edit(
        event,
        f"⚙️ <b>{ft('Settings')}</b>",
        parse_mode="html",
        buttons=[
            [btn(f"💌 Welcome DM: {'ON' if welcome else 'OFF'}", "set_welcome")],
            [btn(f"🛠 Maintenance: {'ON' if maint else 'OFF'}", "set_maint")],
            [btn("⬅️ Back", "adm_home")],
        ],
    )


async def admin_callback(event, data: str, uid: int):
    if data == "adm_home":
        awaiting.pop(uid, None)
        await safe_edit(event, ADMIN_TEXT, buttons=admin_menu(), parse_mode="html")

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
            event, text, parse_mode="html",
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
            parse_mode="html",
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
            parse_mode="html",
            buttons=[[btn("⬅️ Back", "adm_home")]],
        )

    elif data == "adm_bc":
        awaiting[uid] = "bc"
        await safe_edit(
            event,
            f"📢 <b>{ft('Broadcast')}</b>\n\nSend the message you want to broadcast — text, photo, video, document, "
            "voice, sticker, poll, anything. It is delivered in its original form.\n"
            f"Recipients: <b>{await recipients_count()}</b>\n\n/cancel to abort",
            parse_mode="html",
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
        await safe_edit(event, f"🚀 <b>{ft('Broadcast started')}</b>", parse_mode="html")
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
            parse_mode="html",
            buttons=bc_buttons(False),
        )
    elif mode == "fadd":
        await add_force_channel(event)
    elif mode == "ban":
        await toggle_ban(event)


async def add_force_channel(event):
    msg = event.message
    ref = None
    if msg.fwd_from and isinstance(msg.fwd_from.from_id, types.PeerChannel):
        ref = get_peer_id(msg.fwd_from.from_id)
    else:
        t = (msg.raw_text or "").strip()
        m = re.search(r"t\.me/([A-Za-z0-9_]{4,})/?$", t)
        if m:
            ref = m.group(1)
        elif re.fullmatch(r"-?\d+", t):
            ref = int(t)
        elif t.startswith("@"):
            ref = t[1:]
    if ref is None:
        awaiting[event.sender_id] = "fadd"
        return await event.respond("❌ Couldn't read that. Forward a channel post, or send @username / -100… ID (/cancel).")

    try:
        ent = await bot.get_entity(ref)
        assert isinstance(ent, types.Channel)
    except Exception:
        awaiting[event.sender_id] = "fadd"
        return await event.respond(
            "❌ Channel not found. Add the bot there as admin first, then forward a post from it (/cancel)."
        )
    try:
        perms = await bot.get_permissions(ent)
        if not perms.is_admin:
            raise ValueError
    except Exception:
        return await event.respond("❌ The bot is not an admin in that channel. Promote it, then add again.")

    if ent.username:
        link = f"https://t.me/{ent.username}"
    else:
        try:
            inv = await bot(functions.messages.ExportChatInviteRequest(peer=ent))
            link = inv.link
        except Exception:
            return await event.respond(
                "❌ Couldn't create an invite link. Give the bot the <b>Invite Users</b> admin right and retry.",
                parse_mode="html",
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
        f"✅ <b>{ft('Added')}</b>: {ent.title}\nUsers must now join it before using the bot.",
        parse_mode="html",
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
        parse_mode="html",
        buttons=[[btn("🚫 Ban / Unban another", "adm_ban"), btn("🛡 Panel", "adm_home")]],
    )


async def do_broadcast(admin_id: int, msg_id: int, as_forward: bool, pin: bool):
    users = await recipients()
    status = await bot.send_message(
        admin_id, f"📢 <b>{ft('Broadcasting…')}</b> 0 / {len(users)}", parse_mode="html",
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
                        f"📢 <b>{ft('Broadcasting…')}</b> {i} / {len(users)}",
                        parse_mode="html", buttons=[[btn("🛑 Stop", "stop")]],
                    )
                except Exception:
                    pass
            await asyncio.sleep(0.05)
    except asyncio.CancelledError:
        await mark_blocked(blocked_ids)
        await status.edit(
            f"🛑 <b>{ft('Broadcast stopped')}</b>\nSent: {sent} | Blocked: {len(blocked_ids)} | Failed: {failed}",
            parse_mode="html",
        )
        raise
    await mark_blocked(blocked_ids)
    await status.edit(
        f"✅ <b>{ft('Broadcast finished')}</b>\n\n"
        f"📨 Sent: <b>{sent}</b>\n⛔ Blocked/deleted: <b>{len(blocked_ids)}</b>\n❌ Failed: <b>{failed}</b>",
        parse_mode="html",
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
    log.info("Bot @%s is running (Firestore connected)", BOT_USERNAME)
    try:
        await bot.run_until_disconnected()
    finally:
        await flush_approved()


if __name__ == "__main__":
    bot.loop.run_until_complete(main())
