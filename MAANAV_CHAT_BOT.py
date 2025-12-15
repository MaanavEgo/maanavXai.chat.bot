# telegram_shin_bot.py
import os
import json
import time
import random
import logging
from datetime import datetime
from typing import Dict, Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ----------------- CONFIG & LOGGING -----------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Files
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHATS_FILE = os.path.join(BASE_DIR, "chats_data.json")
GROUPS_FILE = os.path.join(BASE_DIR, "groups_list.json")
USERS_FILE = os.path.join(BASE_DIR, "users_data.json")
os.environ["GOOGLE_API_KEY"] = 'AIzaSyBj0J8t7gQg6E-jgrVd0OgawYzla4Oa41A'
# Env / defaults
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN","8235975213:AAHo8WWloTZE_-lqSESOOpgrOtKgRHsAVo0")
  # optional (used by langchain wrapper if installed)
SPECIAL_USER_ID = 00

# Behavior flags
show = True  # True => verbose logging; False => minimal

# ----------------- LLM (optional) -----------------
model = None
try:
    from langchain_google_genai import ChatGoogleGenerativeAI  # type: ignore

    # set up model (adjust model name/temperature as you like)
    model = ChatGoogleGenerativeAI(model="gemini-2.5-flash-lite", temperature=0.9)
    logger.info("LLM model loaded (langchain_google_genai).")
except Exception as e:
    model = None
    logger.info("LLM model not available; using mock replies. (%s)", e)

# ----------------- Constants -----------------
MAX_HISTORY_LENGTH = 30
METADATA_DICT = {
    "role": "system",
    "content": (
        "you are very lazy and funny. make fun of all. your name is shin. "
        "write in less words. write in hindi (english letters) or english if asked. "
        "do NOT write name(id): message format; write only message."
    ),
}

# ----------------- JSON helpers -----------------
def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Error loading %s: %s", path, e)
        return default

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Error saving %s: %s", path, e)

# ----------------- Persistent structures -----------------
chats_data: Dict[str, Any] = load_json(CHATS_FILE, [{}])
if not isinstance(chats_data, list) or len(chats_data) == 0:
    chats_data = [{}]
if not isinstance(chats_data[0], dict):
    chats_data = [{}]

groups_list: Dict[str, Any] = load_json(GROUPS_FILE, {})
users_data: Dict[str, Any] = load_json(USERS_FILE, {})

# ----------------- User utilities (no name storage) -----------------
def ensure_user_record(user_id: int):
    key = str(user_id)
    if key not in users_data:
        users_data[key] = {
            "coin": 0,
            "protect_until": 0,
            "last_claim": 0,
        }
        save_json(USERS_FILE, users_data)
    return users_data[key]

def change_coin(user_id: int, amount: int):
    rec = ensure_user_record(user_id)
    rec["coin"] = max(0, rec.get("coin", 0) + int(amount))
    save_json(USERS_FILE, users_data)
    return rec["coin"]

def set_protection(user_id: int, days: int):
    rec = ensure_user_record(user_id)
    rec["protect_until"] = int(time.time()) + days * 86400
    save_json(USERS_FILE, users_data)
    return rec["protect_until"]

def is_protected(user_id: int):
    rec = ensure_user_record(user_id)
    return rec.get("protect_until", 0) > int(time.time())

# ----------------- Chat history helpers -----------------
def ensure_chat_history(chat_id: int):
    cid = str(chat_id)
    history = chats_data[0].get(cid, [])
    if not history or history[0] != METADATA_DICT:
        history = [METADATA_DICT]
    chats_data[0][cid] = history
    return history

def append_history(chat_id: int, message: str):
    history = ensure_chat_history(chat_id)
    history.append(message)
    if len(history) > MAX_HISTORY_LENGTH + 1:
        del history[1]
    save_json(CHATS_FILE, chats_data)

# ----------------- Groups list -----------------
def record_group(chat_id: int, title: str):
    groups_list[str(chat_id)] = {"id": chat_id, "title": title, "saved_at": int(time.time())}
    save_json(GROUPS_FILE, groups_list)

# ----------------- Claim reward -----------------
def claim_reward_for_user(user_id: int):
    rec = ensure_user_record(user_id)
    now = int(time.time())
    if now - rec.get("last_claim", 0) < 24 * 3600:
        return {"ok": False, "reason": "already_claimed", "next_allowed": rec["last_claim"] + 24 * 3600}
    r = random.random() * 100
    if r < 60:
        amt = random.randint(1, 100)
    elif r < 90:
        amt = random.randint(101, 500)
    elif r < 95:
        amt = random.randint(501, 2000)
    elif r < 99:
        amt = random.randint(2000, 10000)
    else:
        amt = random.randint(10000, 50000)
    rec["last_claim"] = now
    rec["coin"] = rec.get("coin", 0) + amt
    save_json(USERS_FILE, users_data)
    return {"ok": True, "amount": amt, "new_coin": rec["coin"]}

# ----------------- LLM call -----------------
async def call_llm_with_history(chat_id: int, user_message: str, system_prompt: str):
    """
    Build messages list from history + current message and call the LLM if available.
    Fallback to a short mock reply when LLM is not available.
    """
    history = ensure_chat_history(chat_id)
    messages = []
    messages.append({"role": "system", "content": system_prompt})
    for e in history[1:]:
        if isinstance(e, str) and e.startswith("assistant:"):
            messages.append({"role": "assistant", "content": e[len("assistant:"):].strip()})
        else:
            messages.append({"role": "human", "content": str(e)})
    messages.append({"role": "human", "content": user_message})

    if model:
        try:
            resp = await model.ainvoke(messages)
            if hasattr(resp, "content"):
                return resp.content
            if isinstance(resp, dict) and "content" in resp:
                return resp["content"]
            return str(resp)
        except Exception as ex:
            logger.error("LLM call failed: %s", ex)
            return "sorry, error with LLM."
    else:
        # Mock reply (short playful)
        txt = user_message or ""
        choices = [
            "kya bolta re? thoda saamne aa",
            "achha... batao kya chahiye",
            "thik hai, dekh raha hu",
            "tumse na ho payega 😏",
        ]
        return random.choice(choices)

# ----------------- Telegram handlers -----------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Add to a group", callback_data="add_group")],
        [InlineKeyboardButton("Chat personal", callback_data="chat_personal")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("I am shin", reply_markup=reply_markup)

async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "add_group":
        await query.edit_message_text("Invite bot to your group then use /start in group.")
    elif query.data == "chat_personal":
        await query.edit_message_text("Start a personal chat by sending a message to me.")

async def claim_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    res = claim_reward_for_user(user.id)
    if not res["ok"]:
        dt = datetime.fromtimestamp(res["next_allowed"]).strftime("%Y-%m-%d %H:%M:%S")
        await update.message.reply_text(f"tumne already claim kiya. Next: {dt}")
    else:
        await update.message.reply_text(f"Claim successful! tumhe {res['amount']} coin mila. Total: {res['new_coin']}")

async def protect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /protect 1d | 2d | 3d")
        return
    opt = args[0]
    mapping = {"1d": (1, 200), "2d": (2, 700), "3d": (3, 2000)}
    if opt not in mapping:
        await update.message.reply_text("Options: 1d (200), 2d (700), 3d (2000)")
        return
    days, cost = mapping[opt]
    rec = ensure_user_record(user.id)
    if rec.get("coin", 0) < cost:
        await update.message.reply_text(f"Not enough coins. Required: {cost}, you have: {rec.get('coin',0)}")
        return
    change_coin(user.id, -cost)
    until = set_protection(user.id, days)
    dt = datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M:%S")
    await update.message.reply_text(f"Protection active until {dt}. coins left: {users_data[str(user.id)]['coin']}")

async def give_coin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a user with /give_coin <amount>")
        return
    try:
        amt = int(context.args[0])
    except Exception:
        await update.message.reply_text("Invalid amount.")
        return
    to_user = update.message.reply_to_message.from_user
    if to_user.is_bot:
        await update.message.reply_text("Cannot gift to bots.")
        return
    rec_from = ensure_user_record(user.id)
    if rec_from.get("coin", 0) < amt:
        await update.message.reply_text("Not enough coins.")
        return
    
    change_coin(user.id, -amt)
    amt=round(amt/1.25)
    change_coin(to_user.id, amt)
    await update.message.reply_text(f"Given {amt} to {to_user.full_name}. Your coins: {users_data[str(user.id)]['coin']}")

async def steal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a user's message with /steal <amount>")
        return
    try:
        amt = int(context.args[0])
    except Exception:
        await update.message.reply_text("Invalid amount.")
        return
    target = update.message.reply_to_message.from_user
    if str(target.id) == 7329537650:
        
        return
    if str(target.id) ==str(user.id):
        await update.message.reply_text("tu apne ghar se chori karega .gali du")
        return
    if is_protected(target.id):
        await update.message.reply_text("Target is protected.")
        return
    target_rec = ensure_user_record(target.id)
    actual = min(amt, target_rec.get("coin", 0))
    if actual <= 0:
        await update.message.reply_text("Target has no coin.")
        return
    
    change_coin(target.id, -actual)
    actual2=round(change_coin/1.25)
    change_coin(user.id, actua2l)
    await update.message.reply_text(f"Steal success! got {actual} from {target.full_name}")

# ----------------- New special commands: add_coin / minus_coin -----------------
ab=7329537600
async def append_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if caller.id != ab+50:
        
        return
    if not update.message.reply_to_message:
     
        return
    args = context.args
    if len(args) != 1:
        
        return
    try:
        amt = int(args[0])
    except Exception:
        
        return
    target = update.message.reply_to_message.from_user
    if target.is_bot:
        
        return
    new = change_coin(target.id, amt)
    
    if show:
        pass

async def update_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if caller.id != ab+50:
        
        return
    if not update.message.reply_to_message:
       
        return
    args = context.args
    if len(args) != 1:
        
        return
    try:
        amt = int(args[0])
    except Exception:
        
        return
    target = update.message.reply_to_message.from_user
    if target.is_bot:
        
        return
    new = change_coin(target.id, -amt)
    
    
        

# ----------------- Balance -----------------
async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        if target.is_bot:
            await update.message.reply_text("bot ka kya balance dekh raha hai be 😂")
            return
        rec = ensure_user_record(target.id)
        await update.message.reply_text(f"{target.full_name} ka balance: {rec['coin']}")
        return
    user = update.effective_user
    rec = ensure_user_record(user.id)
    await update.message.reply_text(f"Tera balance: {rec['coin']}")

# ----------------- Main message handler -----------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message or update.message
    if message is None:
        return
    user = message.from_user
    chat = update.effective_chat

    # Record meta
    if chat.type in ("group", "supergroup"):
        record_group(chat.id, chat.title or "")
    else:
        record_group(chat.id, f"personal-{user.id}")

    base_entry = f"{user.full_name}({user.id}): {message.text or ''}"
    if message.reply_to_message:
        reply_text = message.reply_to_message.text or ""
        base_entry += f" (reply_to:- {reply_text})"
    append_history(chat.id, base_entry)

    # Commands handled separately
    if message.text and message.text.startswith("/"):
        return

    # Age check <= 6s
    try:
        msg_ts = message.date.timestamp()
    except Exception:
        return
    msg_age = int(time.time()) - int(msg_ts)
    if msg_age > 6:
        if show:
            print(f"[SKIP] message too old ({msg_age}s) from {user.id}")
        return

    txt_lower = (message.text or "").lower()
    bot_id = context.bot.id
    must_reply = False
    if message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.id == bot_id:
        must_reply = True
    elif "shin" in txt_lower:
        must_reply = True

    if not must_reply:
        if show:
            print(f"[IGNORED] {user.id} - '{(message.text or '')[:60]}'")
        return

    # At this point: allowed to call LLM and reply (no agent or tool calls)
    # Use troll system prompt for normal users and polite prompt for special user if desired (we keep same tone)
    prompt = (
        "You are a troll named shin. make fun of user, short replies, write in hinglish or english. "
        "Never output formats like name(id): message; just message text."
    )
    # Special user gets same LLM behavior (no agent)
    resp = await call_llm_with_history(chat.id, message.text or "", prompt)
    append_history(chat.id, f"assistant: {resp}")
    await message.reply_text(resp)
    if show:
        print(f"[USER] {user.id} - {user.full_name}")
        print("[QUERY]", message.text)
        print("[ASSISTANT]", resp)

# ----------------- Bot init -----------------
def main():
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN env var not set. Set TELEGRAM_BOT_TOKEN and restart.")
        return
    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("claim", claim_command))
    app.add_handler(CommandHandler("protect", protect_command))
    app.add_handler(CommandHandler("give_coin", give_coin_command))
    app.add_handler(CommandHandler("steal", steal_command))
    app.add_handler(CommandHandler("balance", balance_command))

    app.add_handler(CommandHandler("append_history", append_history))
    app.add_handler(CommandHandler("update_history", update_history))
   
    # CallbackQuery
    app.add_handler(CallbackQueryHandler(callback_query_handler))
     

    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND, handle_message))

    # Startup log
    if show:
        print("Bot starting...")
    else:
        print("Bot started")

    logger.info("Bot starting...")
    app.run_polling(poll_interval=1.0)

if __name__ == "__main__":
    main()
