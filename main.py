import json
import logging
import os
import re
import time

from dotenv import load_dotenv
from telebot import TeleBot
from telebot.types import *

load_dotenv()

TOKEN = os.getenv("TOKEN")
# OWNER = the main control person (your current ADMIN id becomes the owner).
# The old ADMIN variable still works, so your current .env does not have to change.
OWNERS = {a.strip() for a in (os.getenv("OWNER") or os.getenv("ADMIN", "")).split(",") if a.strip()}
DB_PATH = "requests.json"
TZ_OFFSET = float(os.getenv("TZ_OFFSET", "5"))   # UTC+5, used to show end times

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("surveybot")
if not OWNERS:
    log.warning("No OWNER (or ADMIN) set in .env: nobody will be able to manage the bot")

# threaded=False: updates are handled one at a time, so two votes can never
# be written at the same moment (no threading/lock needed)
bot = TeleBot(TOKEN, threaded=False)
BOT_ID = None   # filled in main()

# Admin panel = keyboard buttons under the message input
BTN_NEW = "➕ New Survey"
BTN_ALL = "📋 All Surveys"
BTN_CHANNELS = "📢 Channels"
BTN_ADMINS = "👑 Admins"          # owner only
PANEL_BUTTONS = {BTN_NEW, BTN_ALL, BTN_CHANNELS, BTN_ADMINS}

TIME_HELP = ("Send the duration like: 30m, 2h, 1d, 1d 12h.\n"
             "A plain number means minutes. Minimum 1 minute, maximum 365 days.")

CHANNEL_ASK = ("Send the channel @username, a t.me link, the numeric id (-100...), "
               "or forward any post from the channel here.\n"
               "The bot must already be an administrator of the channel.")

ADMIN_ASK = ("Send the new admin in one of these ways:\n"
             "• forward any message from them\n"
             "• share their contact\n"
             "• send their numeric Telegram id\n"
             "• send their @username (works if they already pressed /start in this bot)")

DIRTY = set()   # survey ids whose channel posts must be re-drawn


# =====================================================================
# Database (one JSON file, safe writes, automatic migration)
# =====================================================================

def empty_db():
    return {"surveys": [], "users": [], "user_info": {}, "drafts": {}, "channels": [], "admins": []}


def migrate(data):
    """Accept the old format (a plain list) and old keys ("voters")."""
    db = empty_db()
    if isinstance(data, list):
        db["surveys"] = data
    elif isinstance(data, dict):
        db["surveys"] = data.get("surveys", [])
        db["users"] = data.get("users", [])
        db["user_info"] = data.get("user_info", {})
        db["drafts"] = data.get("drafts", {})
        db["channels"] = data.get("channels", [])
        db["admins"] = data.get("admins", [])

    for s in db["surveys"]:
        old = s.pop("voters", None)           # old key -> new key
        s.setdefault("voted_users", {})
        if old:
            s["voted_users"].update(old)
        s.setdefault("closed", False)
        s.setdefault("closed_by", None)       # None / "admin" / "time"
        s.setdefault("duration", 0)           # seconds, 0 = no limit
        s.setdefault("ends_at", None)         # unix time or None
        s.setdefault("posts", [])             # channel posts: {"chat_id", "message_id"}
        s.setdefault("created_by", None)      # {"id", "name"}
    for d in db["drafts"].values():
        d.setdefault("duration", 0)
        d.setdefault("targets", ["users"])    # where to send: "users" and/or channel ids
    return db


def load_db():
    if not os.path.exists(DB_PATH):
        return empty_db()
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            return migrate(json.load(f))
    except (json.JSONDecodeError, OSError) as e:
        # Never silently overwrite a broken file: keep it as a backup
        backup = f"{DB_PATH}.broken-{int(time.time())}"
        try:
            os.replace(DB_PATH, backup)
        except OSError:
            pass
        log.error("Database was broken (%s). Saved a copy as %s", e, backup)
        return empty_db()


def save_db():
    """Write to a temp file first, then swap it in. A crash in the middle of
    saving can no longer destroy requests.json."""
    tmp = DB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False, indent=4)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, DB_PATH)


DB = load_db()   # loaded once and kept in memory: no file read on every vote
save_db()        # writes the migrated format


# =====================================================================
# Roles: owner > admin > user
# =====================================================================

def is_owner(user_id) -> bool:
    return str(user_id) in OWNERS


def is_admin(user_id) -> bool:
    """Owners and admins added by the owner. Owners can do everything admins can."""
    uid = str(user_id)
    return uid in OWNERS or any(a["id"] == uid for a in DB["admins"])


def all_admin_ids():
    return set(OWNERS) | {a["id"] for a in DB["admins"]}


def full_name(user):
    return " ".join(p for p in (user.first_name, user.last_name) if p) or str(user.id)


def person_label(uid):
    """Readable name for a Telegram id (name + @username if the bot knows them)."""
    uid = str(uid)
    info = DB["user_info"].get(uid) or next((a for a in DB["admins"] if a["id"] == uid), None)
    if not info:
        return uid
    label = info.get("name") or uid
    if info.get("username"):
        label += f" (@{info['username']})"
    return label


# =====================================================================
# Helpers
# =====================================================================

def find_survey(survey_id):
    return next((s for s in DB["surveys"] if s["id"] == survey_id), None)


def find_channel(channel_id):
    return next((c for c in DB["channels"] if c["id"] == channel_id), None)


def channel_label(c):
    return c["title"] + (f" (@{c['username']})" if c.get("username") else "")


def short(text, n=22):
    return text if len(text) <= n else text[:n - 1] + "…"


def popup(call: CallbackQuery, text: str):
    """Pop-up window in the middle of the screen (Telegram limit: 200 chars)."""
    bot.answer_callback_query(call.id, text[:200], show_alert=True)


def safe_edit(text, chat_id, message_id, markup=None):
    try:
        bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
    except Exception as e:
        if "message is not modified" not in str(e):
            log.warning("Edit error: %s", e)


def register_user(user):
    """Remember everyone who started the bot (needed for notifications and for
    finding people by @username when the owner adds an admin)."""
    uid = str(user.id)
    info = {"name": full_name(user), "username": user.username}
    changed = False
    if uid not in DB["users"]:
        DB["users"].append(uid)
        changed = True
    if DB["user_info"].get(uid) != info:
        DB["user_info"][uid] = info
        changed = True
    if changed:
        save_db()


def delete_msgs(chat_id, *message_ids):
    """Delete messages, ignore errors (already deleted, too old, etc.)."""
    for mid in message_ids:
        if mid is None:
            continue
        try:
            bot.delete_message(chat_id, mid)
        except Exception as e:
            log.debug("Could not delete message %s: %s", mid, e)


def cancelled(message: Message) -> bool:
    """Called by every 'waiting for text' step.
    - A command (/start ...) cancels the step.
    - An admin panel button cancels the step AND runs what the button asks for."""
    text = message.text or ""
    if text in PANEL_BUTTONS and is_admin(message.from_user.id):
        panel_action(message)
        return True
    return text.startswith("/")


def ask(chat_id, text, handler, *args):
    """Send a question and wait for the answer.
    The handler is called as handler(message, prompt_id, *args)."""
    prompt = bot.send_message(chat_id, text)
    bot.register_next_step_handler(prompt, handler, prompt.message_id, *args)
    return prompt


def retry(message, prompt_id, text, handler, *args):
    """Wrong answer: delete it, show the error inside the SAME question message,
    and wait for a new answer. No new messages appear in the chat."""
    delete_msgs(message.chat.id, message.message_id)
    try:
        bot.edit_message_text(text, message.chat.id, prompt_id)
    except Exception:
        pass  # same text as before ("message is not modified")
    bot.register_next_step_handler(message, handler, prompt_id, *args)


# =====================================================================
# Time control
# =====================================================================

def parse_duration(text):
    """'30m', '2h', '1d', '1d 12h', '90' (minutes) -> seconds, or None if invalid."""
    text = text.strip().lower().replace(",", " ")
    if text.isdigit():
        secs = int(text) * 60
    else:
        parts = re.findall(r"(\d+)\s*([dhm])", text)
        leftover = re.sub(r"\d+\s*[dhm]|\s", "", text)
        if not parts or leftover:
            return None
        units = {"d": 86400, "h": 3600, "m": 60}
        secs = sum(int(n) * units[u] for n, u in parts)
    if secs < 60 or secs > 365 * 86400:
        return None
    return secs


def fmt_duration(secs):
    secs = int(secs)
    if secs <= 0:
        return "no limit"
    d, h, m = secs // 86400, (secs % 86400) // 3600, (secs % 3600) // 60
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    return " ".join(parts) if parts else "<1m"


def fmt_time(ts):
    """Absolute time in the timezone from TZ_OFFSET (default UTC+5)."""
    shifted = ts + TZ_OFFSET * 3600
    return time.strftime("%d %b %Y, %H:%M", time.gmtime(shifted)) + f" (UTC{TZ_OFFSET:+g})"


def time_left(survey):
    """Seconds left, or None if the survey has no time limit."""
    if not survey.get("ends_at"):
        return None
    return survey["ends_at"] - time.time()


def is_expired(survey):
    left = time_left(survey)
    return left is not None and left <= 0


def is_open(survey):
    """The ONE place that decides if voting is allowed (manual close + timer)."""
    return not survey.get("closed") and not is_expired(survey)


def status_text(survey):
    if survey.get("closed") or is_expired(survey):
        if survey.get("closed_by") == "time" or (not survey.get("closed") and is_expired(survey)):
            return "🔒 Closed (time is over)"
        if survey.get("closed_by") == "admin":
            return "🔒 Closed by admin"
        return "🔒 Closed"
    left = time_left(survey)
    if left is None:
        return "🟢 Open — no time limit"
    return f"🟢 Open — ⏳ {fmt_duration(left)} left"


def mark_dirty(survey_id):
    """Ask the main loop to re-draw this survey's channel posts."""
    DIRTY.add(survey_id)


def sweep_expired():
    """Close surveys whose time is over and tell the admins. Runs every few seconds."""
    now = time.time()
    expired = [s for s in DB["surveys"]
               if not s.get("closed") and s.get("ends_at") and now >= s["ends_at"]]
    if not expired:
        return

    for s in expired:
        s["closed"] = True
        s["closed_by"] = "time"
        mark_dirty(s["id"])
    save_db()

    for s in expired:
        text = (f"⏰ Time is over for \"{s['title']}\". Voting is closed.\n\n"
                f"{results_text(s)}\n\nTotal voters: {len(s['voted_users'])}")
        for admin_id in all_admin_ids():
            try:
                bot.send_message(int(admin_id), text)
            except Exception as e:
                log.warning("Could not notify admin %s: %s", admin_id, e)
        log.info("Survey %s closed by timer", s["id"])


def time_picker_markup(choose_prefix, custom_cb, back_cb):
    """Preset durations. Buttons send f'{choose_prefix}{seconds}' (0 = no limit)."""
    presets = [("30 min", 1800), ("1 hour", 3600), ("6 hours", 21600),
               ("24 hours", 86400), ("3 days", 259200), ("7 days", 604800)]
    markup = InlineKeyboardMarkup()
    for i in range(0, len(presets), 2):
        markup.row(*[InlineKeyboardButton(label, callback_data=f"{choose_prefix}{secs}")
                     for label, secs in presets[i:i + 2]])
    markup.row(
        InlineKeyboardButton("♾ No limit", callback_data=f"{choose_prefix}0"),
        InlineKeyboardButton("✏️ Custom", callback_data=custom_cb),
    )
    markup.add(InlineKeyboardButton("⬅️ Back", callback_data=back_cb))
    return markup


# =====================================================================
# Text builders (votes are ALWAYS visible)
# =====================================================================

def results_text(survey):
    total = sum(survey["users"].values())
    ranked = sorted(survey["users"].items(), key=lambda x: x[1], reverse=True)
    lines = []
    for n, (name, votes) in enumerate(ranked, start=1):
        pct = f" ({votes * 100 / total:.0f}%)" if total else ""
        lines.append(f"{n}. {name} — {votes}{pct}")
    return "\n".join(lines) if lines else "No options."


def results_view(survey, user_id):
    """Live results message for one user, with a Refresh button."""
    text = (f"📊 {survey['title']}\n{status_text(survey)}\n\n{results_text(survey)}\n\n"
            f"Total voters: {len(survey['voted_users'])}")
    mine = survey["voted_users"].get(str(user_id))
    if mine:
        text += f"\nYour vote: {mine}"
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🔄 Refresh", callback_data=f"res_{survey['id']}"))
    return text, markup


# =====================================================================
# Owner: managing admins
# =====================================================================

def extract_user_ref(message: Message):
    """-> (user_id, name, username) or an error text (str) or None."""
    # forwarded message
    user = getattr(message, "forward_from", None)
    origin = getattr(message, "forward_origin", None)
    if user is None and origin is not None:
        user = getattr(origin, "sender_user", None)
    if user is not None:
        return user.id, full_name(user), user.username
    if getattr(message, "forward_sender_name", None) or getattr(origin, "type", None) == "hidden_user":
        return ("⚠️ That person hides their account in forwarded messages. "
                "Send their numeric id or @username instead.")

    # shared contact
    contact = getattr(message, "contact", None)
    if contact is not None:
        if contact.user_id:
            name = " ".join(p for p in (contact.first_name, contact.last_name) if p) or str(contact.user_id)
            info = DB["user_info"].get(str(contact.user_id), {})
            return contact.user_id, name, info.get("username")
        return "⚠️ That contact is not a Telegram user."

    text = (message.text or "").strip()
    if not text:
        return None

    # numeric id
    if re.fullmatch(r"\d{5,15}", text):
        info = DB["user_info"].get(text, {})
        return int(text), info.get("name") or text, info.get("username")

    # @username (only people who pressed /start in this bot are known)
    match = re.fullmatch(r"(?:https?://t\.me/|@)?([A-Za-z][A-Za-z0-9_]{3,})", text)
    if match:
        wanted = match.group(1).lower()
        for uid, info in DB["user_info"].items():
            if (info.get("username") or "").lower() == wanted:
                return int(uid), info.get("name") or uid, info.get("username")
        return ("⚠️ I don't know that username. They must press /start in this bot first, "
                "or send their numeric id.")
    return None


def admins_menu(note=""):
    lines = ["👑 Admins", "", "Owner:"]
    lines += [f"• {person_label(o)}" for o in sorted(OWNERS)] or ["• (not set)"]
    lines += ["", "Admins:"]
    if DB["admins"]:
        lines += [f"{n}. {person_label(a['id'])}" for n, a in enumerate(DB["admins"], start=1)]
    else:
        lines.append("(none yet)")
    if note:
        lines += ["", note]

    markup = InlineKeyboardMarkup()
    for a in DB["admins"]:
        markup.add(InlineKeyboardButton(f"👤 {short(person_label(a['id']), 32)}",
                                        callback_data=f"admv_{a['id']}"))
    markup.add(InlineKeyboardButton("➕ Add admin", callback_data="admadd"))
    markup.add(InlineKeyboardButton("✖️ Close", callback_data="closemenu"))
    return "\n".join(lines), markup


def show_admins(chat_id):
    text, markup = admins_menu()
    bot.send_message(chat_id, text, reply_markup=markup)


def edit_admins(call: CallbackQuery):
    text, markup = admins_menu()
    safe_edit(text, call.message.chat.id, call.message.message_id, markup)


def add_admin_step(message: Message, prompt_id, menu_id):
    chat_id = message.chat.id
    if cancelled(message) or not is_owner(message.from_user.id):
        return delete_msgs(chat_id, prompt_id)

    def again(text):
        retry(message, prompt_id, text, add_admin_step, menu_id)

    found = extract_user_ref(message)
    if found is None:
        return again("⚠️ I could not find that user.\n" + ADMIN_ASK)
    if isinstance(found, str):
        return again(found + "\nTry again:")

    user_id, name, username = found
    if is_admin(user_id):
        return again("⚠️ This person is already the owner or an admin. Send someone else:")

    DB["admins"].append({"id": str(user_id), "name": name, "username": username,
                         "added_by": str(message.from_user.id), "added_at": time.time()})
    save_db()

    note = ""
    try:
        bot.send_message(user_id, "👑 The owner made you an admin. Use the buttons below to manage surveys.",
                         reply_markup=admin_keyboard(user_id))
    except Exception:
        note = ("ℹ️ I could not message them, because they have not started the bot yet. "
                "Ask them to press /start, and the admin buttons will appear.")

    text, markup = admins_menu(note)
    safe_edit(text, chat_id, menu_id, markup)
    delete_msgs(chat_id, prompt_id, message.message_id)


def view_admin(call: CallbackQuery, uid):
    admin = next((a for a in DB["admins"] if a["id"] == str(uid)), None)
    if admin is None:
        return popup(call, "Admin not found.")
    text = (f"👤 {person_label(uid)}\n"
            f"ID: {uid}\n"
            f"Added: {fmt_time(admin['added_at'])}\n"
            f"Surveys created: {sum(1 for s in DB['surveys'] if (s.get('created_by') or {}).get('id') == str(uid))}")
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🗑 Remove admin", callback_data=f"admd_{uid}"))
    markup.add(InlineKeyboardButton("⬅️ Admins", callback_data="admins"))
    bot.answer_callback_query(call.id)
    safe_edit(text, call.message.chat.id, call.message.message_id, markup)


def ask_remove_admin(call: CallbackQuery, uid):
    if not any(a["id"] == str(uid) for a in DB["admins"]):
        return popup(call, "Admin not found.")
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("Yes, remove", callback_data=f"admdy_{uid}"),
        InlineKeyboardButton("No", callback_data=f"admv_{uid}"),
    )
    bot.answer_callback_query(call.id)
    safe_edit(f"Remove {person_label(uid)} from the admins?\n"
              f"Their surveys stay, but they lose access to the admin panel immediately.",
              call.message.chat.id, call.message.message_id, markup)


def remove_admin(call: CallbackQuery, uid):
    uid = str(uid)
    DB["admins"] = [a for a in DB["admins"] if a["id"] != uid]
    draft = DB["drafts"].pop(uid, None)            # their unfinished draft is dropped
    save_db()
    if draft:
        delete_msgs(draft["chat_id"], draft["message_id"])

    try:
        bot.send_message(int(uid), "Your admin access was removed by the owner.",
                         reply_markup=ReplyKeyboardRemove())
    except Exception:
        pass
    bot.answer_callback_query(call.id, "Admin removed.")
    edit_admins(call)


# =====================================================================
# Channels: checking, adding, removing, posting
# =====================================================================

def bot_can_post(chat_id):
    """Is the bot an administrator that can post in this channel? -> (ok, reason)"""
    try:
        member = bot.get_chat_member(chat_id, BOT_ID)
    except Exception:
        return False, "The bot is not in that channel. Add it as an administrator."
    if member.status == "creator":
        return True, ""
    if member.status != "administrator":
        return False, "The bot is not an administrator of the channel. Make it an admin."
    if not getattr(member, "can_post_messages", False):
        return False, "The bot is an admin but has no \"Post messages\" permission."
    return True, ""


def extract_channel_ref(message: Message):
    """Channel id or @username from a forwarded post, an id, a t.me link or @name."""
    fc = getattr(message, "forward_from_chat", None)
    if fc is not None:
        return fc.id
    origin = getattr(message, "forward_origin", None)
    if origin is not None and getattr(origin, "chat", None) is not None:
        return origin.chat.id

    text = (message.text or "").strip()
    if not text:
        return None
    if re.fullmatch(r"-?\d+", text):
        value = int(text)
        # ids copied from some apps come without the -100 prefix
        return value if text.startswith("-") else int("-100" + text)
    match = re.search(r"(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{3,})", text)
    if match:
        return "@" + match.group(1)
    if re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,}", text):
        return "@" + text.lstrip("@")
    return None


def channels_menu():
    chans = DB["channels"]
    if chans:
        text = "📢 Channels\n\n" + "\n".join(f"{n}. {channel_label(c)}" for n, c in enumerate(chans, start=1))
    else:
        text = "📢 Channels\n\nNo channels yet. Add the bot to a channel as an administrator, then press ➕ Add channel."
    markup = InlineKeyboardMarkup()
    for c in chans:
        markup.add(InlineKeyboardButton(f"📢 {short(c['title'], 30)}", callback_data=f"chv_{c['id']}"))
    markup.add(InlineKeyboardButton("➕ Add channel", callback_data="chadd"))
    markup.add(InlineKeyboardButton("✖️ Close", callback_data="closemenu"))
    return text, markup


def show_channels(chat_id):
    text, markup = channels_menu()
    bot.send_message(chat_id, text, reply_markup=markup)


def edit_channels(call: CallbackQuery):
    text, markup = channels_menu()
    safe_edit(text, call.message.chat.id, call.message.message_id, markup)


def view_channel(call: CallbackQuery, channel_id):
    channel = find_channel(channel_id)
    if channel is None:
        return popup(call, "Channel not found.")
    ok, reason = bot_can_post(channel_id)
    status = "✅ The bot can post here." if ok else f"⚠️ {reason}"
    surveys = [s for s in DB["surveys"] if any(p["chat_id"] == channel_id for p in s["posts"])]
    text = (f"📢 {channel_label(channel)}\n"
            f"ID: {channel_id}\n"
            f"Surveys posted here: {len(surveys)}\n\n{status}")
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🗑 Remove channel", callback_data=f"chd_{channel_id}"))
    markup.add(InlineKeyboardButton("⬅️ Channels", callback_data="channels"))
    bot.answer_callback_query(call.id)
    safe_edit(text, call.message.chat.id, call.message.message_id, markup)


def ask_remove_channel(call: CallbackQuery, channel_id):
    channel = find_channel(channel_id)
    if channel is None:
        return popup(call, "Channel not found.")
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("Yes, remove", callback_data=f"chdy_{channel_id}"),
        InlineKeyboardButton("No", callback_data=f"chv_{channel_id}"),
    )
    bot.answer_callback_query(call.id)
    safe_edit(f"Remove \"{channel['title']}\" from the list?\n"
              f"Posts that are already in the channel stay there, and voting on them "
              f"will no longer require following this channel.",
              call.message.chat.id, call.message.message_id, markup)


def remove_channel(call: CallbackQuery, channel_id):
    DB["channels"] = [c for c in DB["channels"] if c["id"] != channel_id]
    for d in DB["drafts"].values():          # unfinished drafts forget this channel
        d["targets"] = [t for t in d.get("targets", []) if t != str(channel_id)]
    save_db()
    bot.answer_callback_query(call.id, "Channel removed.")
    edit_channels(call)


def add_channel_step(message: Message, prompt_id, menu_id):
    chat_id = message.chat.id
    if cancelled(message) or not is_admin(message.from_user.id):
        return delete_msgs(chat_id, prompt_id)

    def again(text):
        retry(message, prompt_id, text, add_channel_step, menu_id)

    ref = extract_channel_ref(message)
    if ref is None:
        return again("⚠️ I could not understand that.\n" + CHANNEL_ASK)
    try:
        chat = bot.get_chat(ref)
    except Exception:
        return again("⚠️ Channel not found. For a private channel send its numeric id "
                     "or forward a post from it.\nTry again:")
    if chat.type != "channel":
        return again("⚠️ That is not a channel. Send a channel and try again:")
    if find_channel(chat.id):
        return again("⚠️ This channel is already added. Send another one:")
    ok, reason = bot_can_post(chat.id)
    if not ok:
        return again(f"⚠️ {reason}\nFix it, then send the channel again:")

    DB["channels"].append({"id": chat.id, "title": chat.title or str(chat.id),
                           "username": chat.username})
    save_db()

    text, markup = channels_menu()
    safe_edit(text, chat_id, menu_id, markup)
    delete_msgs(chat_id, prompt_id, message.message_id)


def channel_post_text(survey):
    lines = [f"📊 {survey['title']}", ""]
    if is_open(survey):
        lines.append("Vote with the buttons below. One vote per person.")
        lines.append("📢 You must follow this channel to vote.")
        if survey.get("ends_at"):
            lines.append(f"⏳ Voting ends: {fmt_time(survey['ends_at'])}")
        else:
            lines.append("No time limit.")
        lines += ["", results_text(survey), "", f"🗳 Voters: {len(survey['voted_users'])}"]
    else:
        lines += [status_text(survey), "", results_text(survey),
                  "", f"Total voters: {len(survey['voted_users'])}"]
    return "\n".join(lines)


def channel_markup(survey):
    markup = InlineKeyboardMarkup()
    if not is_open(survey):
        return markup                       # empty keyboard removes the buttons
    for i, (name, votes) in enumerate(survey["users"].items()):
        markup.add(InlineKeyboardButton(f"{name} — {votes}", callback_data=f"vote_{survey['id']}_{i}"))
    return markup


def post_to_channel(survey, channel):
    """Send the survey to a channel and remember the post. -> (ok, reason)"""
    ok, reason = bot_can_post(channel["id"])
    if not ok:
        return False, reason
    try:
        sent = bot.send_message(channel["id"], channel_post_text(survey),
                                reply_markup=channel_markup(survey))
    except Exception as e:
        log.warning("Could not post to %s: %s", channel["id"], e)
        return False, "Telegram refused to send the post."
    survey["posts"].append({"chat_id": channel["id"], "message_id": sent.message_id})
    save_db()
    return True, ""


def refresh_channel_posts():
    """Re-draw the channel posts of changed surveys. Called from the main loop right
    after the votes are processed, so counts update within a second or two and many
    votes in a row cost only one edit per post."""
    if not DIRTY:
        return
    for survey_id in list(DIRTY):
        DIRTY.discard(survey_id)
        survey = find_survey(survey_id)
        if survey is None:
            continue
        text, markup = channel_post_text(survey), channel_markup(survey)
        for post in survey.get("posts", []):
            try:
                bot.edit_message_text(text, post["chat_id"], post["message_id"], reply_markup=markup)
            except Exception as e:
                if "message is not modified" in str(e):
                    continue
                if getattr(e, "error_code", None) == 429:   # Telegram says: slow down
                    DIRTY.add(survey_id)
                    return
                log.warning("Could not update post %s/%s: %s", post["chat_id"], post["message_id"], e)
            time.sleep(0.1)


# =====================================================================
# Follow check: users must follow the channel(s) the survey was sent to
# =====================================================================

def missing_channels(survey, user_id):
    """Channels that carry this survey and that the user has NOT joined."""
    missing = []
    for post in survey.get("posts", []):
        channel = find_channel(post["chat_id"])
        if channel is None or channel in missing:
            continue                        # removed from the list, or already checked
        try:
            member = bot.get_chat_member(channel["id"], user_id)
        except Exception as e:
            # The check itself failed (for example the bot lost its admin rights):
            # do not block voters because of a bot problem
            log.warning("Could not check membership in %s: %s", channel["id"], e)
            continue
        joined = member.status in ("creator", "administrator", "member") or (
            member.status == "restricted" and getattr(member, "is_member", False))
        if not joined:
            missing.append(channel)
    return missing


def follow_popup(call: CallbackQuery, missing):
    names = ", ".join(channel_label(c) for c in missing)
    popup(call, f"📢 Please follow the channel first: {names}\n"
                f"Then come back and press the vote button again.")


# =====================================================================
# Admin panel (keyboard buttons)
# =====================================================================

def admin_keyboard(user_id):
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row(KeyboardButton(BTN_NEW), KeyboardButton(BTN_ALL))
    if is_owner(user_id):
        keyboard.row(KeyboardButton(BTN_CHANNELS), KeyboardButton(BTN_ADMINS))
    else:
        keyboard.row(KeyboardButton(BTN_CHANNELS))
    return keyboard


def panel_action(message: Message):
    """Runs what a panel button asks for, then removes the button's text
    from the chat so the chat stays clean."""
    chat_id = message.chat.id
    try:
        if message.text == BTN_NEW:
            new_survey_step_1(message)
        elif message.text == BTN_ALL:
            show_all_surveys(chat_id)
        elif message.text == BTN_CHANNELS:
            show_channels(chat_id)
        elif message.text == BTN_ADMINS and is_owner(message.from_user.id):
            show_admins(chat_id)
    finally:
        delete_msgs(chat_id, message.message_id)


@bot.message_handler(func=lambda m: m.text in PANEL_BUTTONS and is_admin(m.from_user.id),
                     content_types=["text"])
def panel_button_pressed(message: Message):
    panel_action(message)


# =====================================================================
# Commands
# =====================================================================

@bot.message_handler(commands=["start"])
def start(message: Message):
    register_user(message.from_user)
    chat_id, uid = message.chat.id, message.from_user.id

    if is_admin(uid):
        role = "owner" if is_owner(uid) else "admin"
        bot.send_message(chat_id, f"Hello, {role}! Use the buttons below to manage surveys.",
                         reply_markup=admin_keyboard(uid))
    else:
        bot.send_message(chat_id, "Hello! Choose a survey and vote. Each survey allows only one vote.",
                         reply_markup=ReplyKeyboardRemove())
    show_vote_list(chat_id, uid)


@bot.message_handler(commands=["admin"])
def admin_command(message: Message):
    """Brings the admin keyboard back if it was hidden."""
    if is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "Admin panel is on the keyboard below.",
                         reply_markup=admin_keyboard(message.from_user.id))


# =====================================================================
# Callback router (errors are caught, nothing crashes the bot)
# =====================================================================

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call: CallbackQuery):
    try:
        route_callback(call)
    except Exception:
        log.exception("Callback error for data=%r", call.data)
        try:
            bot.answer_callback_query(call.id, "Something went wrong. Please try again.", show_alert=True)
        except Exception:
            pass


def route_callback(call: CallbackQuery):
    data = call.data
    chat_id = call.message.chat.id

    # ---------- public: voting (bot chat AND channel posts) ----------
    if data.startswith("vsurvey_"):
        return open_vote_survey(call, int(data[8:]))
    if data.startswith("vote_"):
        _, survey_id, index = data.split("_")
        return cast_vote(call, int(survey_id), int(index))
    if data.startswith("res_"):
        return show_results(call, int(data[4:]))

    # ---------- everything below is for admins (owner included) ----------
    if not is_admin(call.from_user.id):
        return popup(call, "Not allowed.")

    # ---------- owner only: managing admins ----------
    if data in ("admins", "admadd") or data.startswith(("admv_", "admd_", "admdy_")):
        if not is_owner(call.from_user.id):
            return popup(call, "Only the owner can manage admins.")
        if data == "admins":
            bot.answer_callback_query(call.id)
            return edit_admins(call)
        if data == "admadd":
            bot.answer_callback_query(call.id)
            return ask(chat_id, ADMIN_ASK, add_admin_step, call.message.message_id)
        if data.startswith("admv_"):
            return view_admin(call, int(data[5:]))
        if data.startswith("admd_"):
            return ask_remove_admin(call, int(data[5:]))
        return remove_admin(call, int(data[6:]))

    # --- menus ---
    if data == "closemenu":
        bot.answer_callback_query(call.id)
        return delete_msgs(chat_id, call.message.message_id)
    if data == "all_surveys":
        bot.answer_callback_query(call.id)
        text, markup = surveys_list()
        return safe_edit(text, chat_id, call.message.message_id, markup)

    # --- channels ---
    if data == "channels":
        bot.answer_callback_query(call.id)
        return edit_channels(call)
    if data == "chadd":
        bot.answer_callback_query(call.id)
        return ask(chat_id, CHANNEL_ASK, add_channel_step, call.message.message_id)
    if data.startswith("chv_"):
        return view_channel(call, int(data[4:]))
    if data.startswith("chd_"):
        return ask_remove_channel(call, int(data[4:]))
    if data.startswith("chdy_"):
        return remove_channel(call, int(data[5:]))

    # --- draft buttons (+ / - / ✅ / time / where to send / publish / cancel) ---
    if (data in ("add_user", "remove_user", "back_survey", "finish_survey",
                 "cancel_survey", "d_time", "fpub")
            or data.startswith("del_") or data.startswith("dd_") or data.startswith("fc_")):
        draft = DB["drafts"].get(str(call.from_user.id))
        if not draft or draft["message_id"] != call.message.message_id:
            return popup(call, "This draft is outdated. Create a new survey.")
        return handle_draft_button(call, draft, data)

    # --- saved surveys ---
    if data.startswith("view_"):
        bot.answer_callback_query(call.id)
        return view_survey(chat_id, int(data[5:]))
    if data.startswith("sv_"):                      # same view, edited in place
        bot.answer_callback_query(call.id)
        return edit_view(call, int(data[3:]))
    if data.startswith("close_"):
        return close_survey(call, int(data[6:]))
    if data.startswith("time_"):
        return show_time_picker(call, int(data[5:]))
    if data.startswith("stc_"):
        return ask_custom_time_saved(call, int(data[4:]))
    if data.startswith("st_"):
        _, survey_id, secs = data.split("_")
        return set_survey_time(call, int(survey_id), int(secs))
    if data.startswith("pc_"):
        return show_post_picker(call, int(data[3:]))
    if data.startswith("pcs_"):
        _, survey_id, channel_id = data.split("_")
        return post_saved_survey(call, int(survey_id), int(channel_id))
    if data.startswith("delete_"):
        return ask_delete(call, int(data[7:]))
    if data.startswith("delyes_"):
        return delete_survey(call, int(data[7:]))

    bot.answer_callback_query(call.id)


# =====================================================================
# Creating a survey (the draft is stored in the JSON file, so it
# survives a restart; every admin has their own draft)
# =====================================================================

def draft_text(draft):
    text = (f"Survey: {draft['title']}\n"
            f"⏱ Time limit: {fmt_duration(draft['duration'])}\n\nOptions:\n")
    if not draft["users"]:
        return text + "(none yet — press +)"
    return text + "\n".join(f"{n}. {name}" for n, name in enumerate(draft["users"], start=1))


def draft_markup(draft):
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton(" + ", callback_data="add_user"),
        InlineKeyboardButton(" - ", callback_data="remove_user"),
        InlineKeyboardButton(" ✅ ", callback_data="finish_survey"),
    )
    markup.add(InlineKeyboardButton(f"⏱ Time: {fmt_duration(draft['duration'])}", callback_data="d_time"))
    markup.add(InlineKeyboardButton("🚫 Cancel", callback_data="cancel_survey"))
    return markup


def refresh_draft(draft):
    safe_edit(draft_text(draft), draft["chat_id"], draft["message_id"], draft_markup(draft))


def new_survey_step_1(message: Message):
    ask(message.chat.id, "Please enter the title of the new survey:", new_survey_step_2)


def new_survey_step_2(message: Message, prompt_id):
    chat_id = message.chat.id
    if cancelled(message):
        return delete_msgs(chat_id, prompt_id)
    if not message.text:
        return retry(message, prompt_id, "⚠️ The title must be text. Enter the title again:",
                     new_survey_step_2)

    uid = str(message.from_user.id)
    old = DB["drafts"].get(uid)               # an unfinished older draft is replaced
    if old:
        delete_msgs(old["chat_id"], old["message_id"])

    draft = {"chat_id": chat_id, "message_id": None, "title": message.text,
             "users": [], "duration": 0, "targets": ["users"]}
    sent = bot.send_message(chat_id, draft_text(draft), reply_markup=draft_markup(draft))
    draft["message_id"] = sent.message_id           # id of the bot's own message
    DB["drafts"][uid] = draft
    save_db()

    delete_msgs(chat_id, prompt_id, message.message_id)


def handle_draft_button(call: CallbackQuery, draft, data):
    chat_id = call.message.chat.id

    if data == "add_user":
        if len(draft["users"]) >= 20:
            return popup(call, "Maximum 20 options per survey.")
        bot.answer_callback_query(call.id)
        ask(chat_id, "Please enter the name of the new option:", add_user_step_2)

    elif data == "remove_user":
        if not draft["users"]:
            return popup(call, "There is nothing to remove.")
        bot.answer_callback_query(call.id)
        markup = InlineKeyboardMarkup()
        for i, name in enumerate(draft["users"]):
            markup.add(InlineKeyboardButton(f"❌ {name}", callback_data=f"del_{i}"))
        markup.add(InlineKeyboardButton("⬅️ Back", callback_data="back_survey"))
        safe_edit("Choose an option to remove:", chat_id, draft["message_id"], markup)

    elif data.startswith("del_"):
        index = int(data[4:])
        if index < len(draft["users"]):
            del draft["users"][index]
            save_db()
        bot.answer_callback_query(call.id, "Removed.")
        refresh_draft(draft)

    elif data == "back_survey":
        bot.answer_callback_query(call.id)
        refresh_draft(draft)

    # ----- time limit of the new survey -----
    elif data == "d_time":
        bot.answer_callback_query(call.id)
        safe_edit("How long should the survey run?\n"
                  "The timer starts when you publish the survey.",
                  chat_id, draft["message_id"],
                  time_picker_markup("dd_", "dd_custom", "back_survey"))

    elif data == "dd_custom":
        bot.answer_callback_query(call.id)
        ask(chat_id, TIME_HELP, custom_time_draft)

    elif data.startswith("dd_"):
        draft["duration"] = int(data[3:])
        save_db()
        bot.answer_callback_query(call.id, f"Time limit: {fmt_duration(draft['duration'])}")
        refresh_draft(draft)

    # ----- last step: choose where to send (✅ opens this screen) -----
    elif data == "finish_survey":
        finish_survey(call, draft)

    elif data.startswith("fc_"):
        value = data[3:]                       # "users" or a channel id
        if value != "users" and not find_channel(int(value)):
            return popup(call, "Channel not found.")
        targets = draft.setdefault("targets", [])
        if value in targets:
            targets.remove(value)
        else:
            targets.append(value)
        save_db()
        bot.answer_callback_query(call.id)
        show_target_picker(draft)

    elif data == "fpub":
        valid = [t for t in draft.get("targets", []) if t == "users" or find_channel(int(t))]
        if not valid:
            return popup(call, "Select at least one place to send the survey.")
        draft["targets"] = valid
        publish_survey(call, draft)

    elif data == "cancel_survey":
        del DB["drafts"][str(call.from_user.id)]
        save_db()
        bot.answer_callback_query(call.id, "Cancelled.")
        safe_edit("🚫 Survey creation cancelled.", chat_id, draft["message_id"])


def custom_time_draft(message: Message, prompt_id):
    chat_id = message.chat.id
    if cancelled(message):
        return delete_msgs(chat_id, prompt_id)

    draft = DB["drafts"].get(str(message.from_user.id))
    if not draft:
        delete_msgs(chat_id, prompt_id, message.message_id)
        return bot.send_message(chat_id, "No active draft. Create a new survey first.")

    secs = parse_duration(message.text or "")
    if secs is None:
        return retry(message, prompt_id, "⚠️ I could not read that.\n" + TIME_HELP + "\nTry again:",
                     custom_time_draft)

    draft["duration"] = secs
    save_db()
    refresh_draft(draft)
    delete_msgs(chat_id, prompt_id, message.message_id)


def add_user_step_2(message: Message, prompt_id):
    chat_id = message.chat.id
    if cancelled(message):
        return delete_msgs(chat_id, prompt_id)

    draft = DB["drafts"].get(str(message.from_user.id))
    if not draft:
        delete_msgs(chat_id, prompt_id, message.message_id)
        return bot.send_message(chat_id, "No active draft. Create a new survey first.")

    name = (message.text or "").strip()
    if not name or len(name) > 40:
        return retry(message, prompt_id,
                     "⚠️ The name must be text (up to 40 characters). Enter it again:",
                     add_user_step_2)
    if name in draft["users"]:
        return retry(message, prompt_id,
                     f"⚠️ \"{name}\" is already in the list. Enter a different name:",
                     add_user_step_2)

    draft["users"].append(name)
    save_db()
    refresh_draft(draft)
    delete_msgs(chat_id, prompt_id, message.message_id)   # clean the chat


def finish_survey(call: CallbackQuery, draft):
    """✅ pressed: last step is choosing where the survey is sent."""
    if not draft["users"]:
        return popup(call, "Add at least one option first.")

    if not DB["channels"]:                  # nothing to choose: send to the bot users
        draft["targets"] = ["users"]
        return publish_survey(call, draft)

    bot.answer_callback_query(call.id)
    show_target_picker(draft)


def show_target_picker(draft):
    targets = draft.get("targets", [])
    markup = InlineKeyboardMarkup()
    for c in DB["channels"]:
        mark = "✅" if str(c["id"]) in targets else "⬜"
        markup.add(InlineKeyboardButton(f"{mark} 📢 {short(c['title'], 30)}", callback_data=f"fc_{c['id']}"))
    mark = "✅" if "users" in targets else "⬜"
    markup.add(InlineKeyboardButton(f"{mark} 👥 Bot users (everyone who started the bot)",
                                    callback_data="fc_users"))
    markup.row(
        InlineKeyboardButton("⬅️ Back", callback_data="back_survey"),
        InlineKeyboardButton("🚀 Publish", callback_data="fpub"),
    )
    safe_edit(f"{draft_text(draft)}\n\n"
              f"Where should the survey be sent?\n"
              f"Tap to select one or more places, then press 🚀 Publish.\n"
              f"ℹ️ People must follow the selected channels to vote.",
              draft["chat_id"], draft["message_id"], markup)


def publish_survey(call: CallbackQuery, draft):
    """Save the survey and send it to the selected places."""
    now = time.time()
    duration = draft.get("duration", 0)
    new_id = max((s["id"] for s in DB["surveys"]), default=0) + 1
    survey = {
        "id": new_id,
        "title": draft["title"],
        "users": {name: 0 for name in draft["users"]},   # option -> number of votes
        "voted_users": {},                               # telegram id -> option they chose
        "closed": False,
        "closed_by": None,
        "duration": duration,
        "ends_at": now + duration if duration else None,  # timer starts now
        "posts": [],                                     # channel posts of this survey
        "created_by": {"id": str(call.from_user.id), "name": full_name(call.from_user)},
    }
    DB["surveys"].append(survey)
    targets = draft.get("targets") or ["users"]
    del DB["drafts"][str(call.from_user.id)]
    save_db()

    sent_lines, ok_names, failed_names = [], [], []
    for t in targets:
        if t == "users":
            continue
        channel = find_channel(int(t))
        if channel is None:
            continue
        ok, reason = post_to_channel(survey, channel)
        if ok:
            ok_names.append(channel["title"])
            sent_lines.append(f"📢 {channel['title']}")
        else:
            failed_names.append(channel["title"])
            sent_lines.append(f"⚠️ {channel['title']} — failed: {reason}")
    to_users = "users" in targets
    if to_users:
        sent_lines.append("👥 Bot users")

    safe_edit(f"✅ Published\n\n{draft_text(draft)}\n\nSent to:\n" + "\n".join(sent_lines),
              draft["chat_id"], draft["message_id"])

    summary = "Survey saved."
    if ok_names:
        summary += " Posted to: " + ", ".join(ok_names) + "."
    if failed_names:
        summary += " ⚠️ Failed: " + ", ".join(failed_names) + " (use \"Post to channel\" later)."
    bot.answer_callback_query(call.id, summary[:200], show_alert=bool(failed_names))

    if to_users:
        broadcast_new_survey(survey, skip_id=str(call.from_user.id))


def broadcast_new_survey(survey, skip_id):
    """Tell everyone who ever pressed /start about the new survey."""
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🗳 Vote", callback_data=f"vsurvey_{survey['id']}"))
    when = f"⏳ Open for {fmt_duration(survey['duration'])}" if survey["duration"] else "No time limit"

    sent = failed = 0
    for uid in list(DB["users"]):
        if uid == skip_id:
            continue
        try:
            bot.send_message(int(uid), f"🆕 New survey: {survey['title']}\n{when}", reply_markup=markup)
            sent += 1
        except Exception:
            failed += 1                      # user blocked the bot, etc.
        time.sleep(0.05)                     # stay under Telegram's rate limit
    log.info("Broadcast for survey %s: sent=%s failed=%s", survey["id"], sent, failed)


# =====================================================================
# Saved surveys (admins)
# =====================================================================

def surveys_list():
    markup = InlineKeyboardMarkup()
    if not DB["surveys"]:
        return "There are no surveys yet.", markup
    for s in DB["surveys"]:
        icon = "🟢" if is_open(s) else "🔒"
        markup.add(InlineKeyboardButton(
            f"{icon} {s['id']}. {short(s['title'], 28)} · 🗳 {len(s['voted_users'])}",
            callback_data=f"view_{s['id']}"))
    markup.add(InlineKeyboardButton("✖️ Close", callback_data="closemenu"))
    return "All surveys:", markup


def show_all_surveys(chat_id):
    text, markup = surveys_list()
    bot.send_message(chat_id, text, reply_markup=markup)


def admin_view(survey):
    text = (f"{survey['title']}\n{status_text(survey)}\n\n"
            f"{results_text(survey)}\n\nTotal voters: {len(survey['voted_users'])}")
    posted = [find_channel(p["chat_id"]) for p in survey["posts"]]
    names = ", ".join(c["title"] for c in posted if c)
    if names:
        text += f"\n📢 Posted in (voters must follow): {names}"
    if survey.get("created_by"):
        text += f"\n👤 Created by: {survey['created_by']['name']}"

    markup = InlineKeyboardMarkup()
    if is_open(survey):
        markup.row(
            InlineKeyboardButton("🔒 Close now", callback_data=f"close_{survey['id']}"),
            InlineKeyboardButton("⏱ Change time", callback_data=f"time_{survey['id']}"),
        )
        markup.add(InlineKeyboardButton("📢 Post to channel", callback_data=f"pc_{survey['id']}"))
    else:
        markup.add(InlineKeyboardButton("🔓 Reopen…", callback_data=f"time_{survey['id']}"))
    markup.row(
        InlineKeyboardButton("🔄 Refresh", callback_data=f"sv_{survey['id']}"),
        InlineKeyboardButton("🗑 Delete", callback_data=f"delete_{survey['id']}"),
    )
    markup.add(InlineKeyboardButton("⬅️ All surveys", callback_data="all_surveys"))
    return text, markup


def view_survey(chat_id, survey_id):
    survey = find_survey(survey_id)
    if survey is None:
        return bot.send_message(chat_id, "Survey not found.")
    text, markup = admin_view(survey)
    bot.send_message(chat_id, text, reply_markup=markup)


def edit_view(call: CallbackQuery, survey_id):
    survey = find_survey(survey_id)
    if survey is None:
        return safe_edit("Survey not found.", call.message.chat.id, call.message.message_id)
    text, markup = admin_view(survey)
    safe_edit(text, call.message.chat.id, call.message.message_id, markup)


def close_survey(call: CallbackQuery, survey_id):
    """MANUAL turn-off: works at any moment, even when a timer is running."""
    survey = find_survey(survey_id)
    if survey is None:
        return popup(call, "Survey not found.")
    survey["closed"] = True
    survey["closed_by"] = "admin"
    save_db()
    mark_dirty(survey_id)
    bot.answer_callback_query(call.id, "Voting closed.")
    edit_view(call, survey_id)


def show_time_picker(call: CallbackQuery, survey_id):
    survey = find_survey(survey_id)
    if survey is None:
        return popup(call, "Survey not found.")
    verb = "Change the time of" if is_open(survey) else "Reopen"
    bot.answer_callback_query(call.id)
    safe_edit(f"{verb} \"{survey['title']}\".\n"
              f"Choose how long voting stays open, counted from now:",
              call.message.chat.id, call.message.message_id,
              time_picker_markup(f"st_{survey_id}_", f"stc_{survey_id}", f"sv_{survey_id}"))


def set_survey_time(call: CallbackQuery, survey_id, secs):
    survey = find_survey(survey_id)
    if survey is None:
        return popup(call, "Survey not found.")

    survey["closed"] = False          # picking a time also reopens a closed survey
    survey["closed_by"] = None
    survey["duration"] = secs
    survey["ends_at"] = time.time() + secs if secs else None
    save_db()
    mark_dirty(survey_id)

    bot.answer_callback_query(
        call.id, f"Voting is open: {fmt_duration(secs)}" if secs else "Voting is open with no time limit.")
    edit_view(call, survey_id)


def ask_custom_time_saved(call: CallbackQuery, survey_id):
    if find_survey(survey_id) is None:
        return popup(call, "Survey not found.")
    bot.answer_callback_query(call.id)
    ask(call.message.chat.id, TIME_HELP, custom_time_saved, survey_id)


def custom_time_saved(message: Message, prompt_id, survey_id):
    chat_id = message.chat.id
    if cancelled(message) or not is_admin(message.from_user.id):
        return delete_msgs(chat_id, prompt_id)

    survey = find_survey(survey_id)
    if survey is None:
        delete_msgs(chat_id, prompt_id, message.message_id)
        return bot.send_message(chat_id, "Survey not found.")

    secs = parse_duration(message.text or "")
    if secs is None:
        return retry(message, prompt_id, "⚠️ I could not read that.\n" + TIME_HELP + "\nTry again:",
                     custom_time_saved, survey_id)

    survey["closed"] = False
    survey["closed_by"] = None
    survey["duration"] = secs
    survey["ends_at"] = time.time() + secs
    save_db()
    mark_dirty(survey_id)

    text, markup = admin_view(survey)
    bot.send_message(chat_id, f"✅ Voting is open for {fmt_duration(secs)}.\n\n{text}", reply_markup=markup)
    delete_msgs(chat_id, prompt_id, message.message_id)


def show_post_picker(call: CallbackQuery, survey_id):
    survey = find_survey(survey_id)
    if survey is None:
        return popup(call, "Survey not found.")
    if not is_open(survey):
        return popup(call, "Reopen the survey first, then post it.")

    markup = InlineKeyboardMarkup()
    for c in DB["channels"]:
        done = any(p["chat_id"] == c["id"] for p in survey["posts"])
        mark = "✅ " if done else ""
        markup.add(InlineKeyboardButton(f"{mark}📢 {short(c['title'], 30)}",
                                        callback_data=f"pcs_{survey_id}_{c['id']}"))
    markup.add(InlineKeyboardButton("⬅️ Back", callback_data=f"sv_{survey_id}"))
    text = (f"Post \"{survey['title']}\" to which channel?\n(✅ = already posted there)"
            if DB["channels"] else
            "No channels yet. Add one with the 📢 Channels button.")
    bot.answer_callback_query(call.id)
    safe_edit(text, call.message.chat.id, call.message.message_id, markup)


def post_saved_survey(call: CallbackQuery, survey_id, channel_id):
    survey, channel = find_survey(survey_id), find_channel(channel_id)
    if survey is None or channel is None:
        return popup(call, "Survey or channel not found.")
    if not is_open(survey):
        return popup(call, "Reopen the survey first, then post it.")
    if any(p["chat_id"] == channel_id for p in survey["posts"]):
        return popup(call, "This survey is already posted in that channel.")

    ok, reason = post_to_channel(survey, channel)
    if not ok:
        return popup(call, f"⚠️ {reason}")
    bot.answer_callback_query(call.id, f"Posted to {channel['title']}.")
    edit_view(call, survey_id)


def ask_delete(call: CallbackQuery, survey_id):
    survey = find_survey(survey_id)
    if survey is None:
        return popup(call, "Survey not found.")
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("Yes, delete", callback_data=f"delyes_{survey_id}"),
        InlineKeyboardButton("No", callback_data=f"sv_{survey_id}"),
    )
    bot.answer_callback_query(call.id)
    safe_edit(f"Delete \"{survey['title']}\", all its votes and its channel posts?", call.message.chat.id,
              call.message.message_id, markup)


def delete_survey(call: CallbackQuery, survey_id):
    survey = find_survey(survey_id)
    if survey:
        for post in survey.get("posts", []):
            delete_msgs(post["chat_id"], post["message_id"])
    DIRTY.discard(survey_id)
    DB["surveys"] = [s for s in DB["surveys"] if s["id"] != survey_id]
    save_db()
    bot.answer_callback_query(call.id, "Deleted.")
    text, markup = surveys_list()                     # the message turns back into the list
    safe_edit(text, call.message.chat.id, call.message.message_id, markup)


# =====================================================================
# Voting (everyone, including admins and the owner)
# =====================================================================

def show_vote_list(chat_id, user_id):
    surveys = [s for s in DB["surveys"] if s["users"] and is_open(s)]
    if not surveys:
        return bot.send_message(chat_id, "There are no open surveys right now.")

    markup = InlineKeyboardMarkup()
    for s in surveys:
        mark = " ✅" if str(user_id) in s["voted_users"] else ""
        left = time_left(s)
        timer = f" ⏳{fmt_duration(left)}" if left is not None else ""
        markup.add(InlineKeyboardButton(
            f"{short(s['title'], 24)}{mark} · 🗳 {len(s['voted_users'])}{timer}",
            callback_data=f"vsurvey_{s['id']}"))
    bot.send_message(chat_id,
                     "Open surveys (✅ = you voted, tap to see live results, 🗳 = voters):",
                     reply_markup=markup)


def closed_popup(call: CallbackQuery, survey):
    if survey.get("closed_by") == "time" or is_expired(survey):
        popup(call, f"⏰ Time is over. Voting in \"{survey['title']}\" is closed.")
    else:
        popup(call, f"🔒 Voting in \"{survey['title']}\" is closed.")


def open_vote_survey(call: CallbackQuery, survey_id):
    survey = find_survey(survey_id)
    if survey is None:
        return popup(call, "This survey no longer exists.")

    # Already voted (maybe in a channel) or voting is closed:
    # show the live results instead of an error
    voted = str(call.from_user.id) in survey["voted_users"]
    if voted or not is_open(survey):
        if voted:
            toast = "You already voted. Here are the live results."
        elif survey.get("closed_by") == "time" or is_expired(survey):
            toast = "Time is over. Final results:"
        else:
            toast = "Voting is closed. Final results:"
        bot.answer_callback_query(call.id, toast)
        text, markup = results_view(survey, call.from_user.id)
        return bot.send_message(call.message.chat.id, text, reply_markup=markup)

    # Must follow the channel(s) this survey was sent to
    missing = missing_channels(survey, call.from_user.id)
    if missing:
        return follow_popup(call, missing)

    markup = InlineKeyboardMarkup()
    for i, (name, votes) in enumerate(survey["users"].items()):
        markup.add(InlineKeyboardButton(f"{name} — {votes}", callback_data=f"vote_{survey_id}_{i}"))

    left = time_left(survey)
    timer = f"\n⏳ Time left: {fmt_duration(left)}" if left is not None else ""

    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id,
                     f"{survey['title']}{timer}\n🗳 Voters so far: {len(survey['voted_users'])}\n\n"
                     f"Choose one option. You can vote only once:",
                     reply_markup=markup)


def cast_vote(call: CallbackQuery, survey_id, index):
    voter = str(call.from_user.id)

    survey = find_survey(survey_id)
    if survey is None:
        return popup(call, "This survey no longer exists.")
    if not is_open(survey):          # checks manual close AND the timer
        return closed_popup(call, survey)

    names = list(survey["users"])
    if index >= len(names):
        return popup(call, "This option no longer exists.")

    # One vote per user: is the id already saved in this survey?
    if voter in survey["voted_users"]:
        return popup(call, f"❌ You have already voted in \"{survey['title']}\".\n"
                           f"Your vote: {survey['voted_users'][voter]}")

    # Must follow the channel(s) this survey was sent to
    missing = missing_channels(survey, call.from_user.id)
    if missing:
        return follow_popup(call, missing)

    name = names[index]
    survey["voted_users"][voter] = name
    survey["users"][name] += 1
    save_db()
    mark_dirty(survey_id)            # channel posts will be refreshed by the main loop

    popup(call, f"✅ Your vote has been counted!\nSurvey: {survey['title']}\n"
                f"You voted for: {name}\n{name} now has {survey['users'][name]} vote(s). "
                f"Total voters: {len(survey['voted_users'])}.")

    # In the bot chat the voting message turns into live results; a channel post stays as it is
    if call.message.chat.type == "private":
        text, markup = results_view(survey, call.from_user.id)
        safe_edit(text, call.message.chat.id, call.message.message_id, markup)


def show_results(call: CallbackQuery, survey_id):
    survey = find_survey(survey_id)
    if survey is None:
        return popup(call, "This survey no longer exists.")

    allowed = (str(call.from_user.id) in survey["voted_users"]
               or not is_open(survey) or is_admin(call.from_user.id))
    if not allowed:
        return popup(call, "Vote first to see the results.")

    bot.answer_callback_query(call.id, "Updated.")
    text, markup = results_view(survey, call.from_user.id)
    safe_edit(text, call.message.chat.id, call.message.message_id, markup)


# =====================================================================
# Main loop: single thread, checks the timers every few seconds
# =====================================================================

def main():
    global BOT_ID
    BOT_ID = bot.get_me().id
    log.info("Bot started")
    offset = None

    # skip updates that piled up while the bot was off
    try:
        pending = bot.get_updates(offset=-1, timeout=5)
        if pending:
            offset = pending[-1].update_id + 1
    except Exception:
        log.exception("Could not skip pending updates")

    try:
        while True:
            try:
                updates = bot.get_updates(
                    offset=offset,
                    timeout=15,                 # network timeout
                    long_polling_timeout=10,    # so the timers are checked every ~10 s
                    allowed_updates=["message", "callback_query"],
                )
                if updates:
                    offset = updates[-1].update_id + 1
                    bot.process_new_updates(updates)
                sweep_expired()
                refresh_channel_posts()
            except Exception:
                log.exception("Polling error")
                time.sleep(3)
    except KeyboardInterrupt:
        log.info("Bot stopped")


if __name__ == "__main__":
    main()