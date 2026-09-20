"""
GOLD INSIGHT - PROP FIRM - Verification Bot (Google Sheet dropdown se approval)
-------------------------------------------------------------------------
User ka flow:
1. /start -> Welcome message + 3 buttons:
      FUNDED BUY / ALREADY BUY UNDER YOU / CONNECT SUPPORT TEAM
   - FUNDED BUY            -> funded challenge ka form link dikhata hai
   - CONNECT SUPPORT TEAM  -> support ki ID dikhata hai
   - ALREADY BUY UNDER YOU -> verification flow shuru (neeche step 2 se)
2. Name
3. Email (propfirm account wali)
4. Prop Firm
5. Account Size
6. Bot saari details dikhata hai -> user "Confirm" ya "Edit" dabata hai
7. Confirm par data Google Sheet mein "Pending" status ke sath save hota hai

Admin ka flow (Sheet ke Status dropdown se):
- Approved -> user ko private group ka invite link
- Rejected -> user ko message: "details mein masla hai, support team se contact karein"
              (channel se nahi nikalta)
- Removed  -> invite link cancel + user ko channel se nikalta hai + message
- Removed/Rejected ke baad dobara Approved -> naya invite link
Bot har 30 second mein sheet check karta hai.

Duplicate se bachao:
- Ek Telegram account ki sheet mein sirf ek row hoti hai
- Pending/Approved user dobara submit nahi kar sakta
- Rejected user dobara submit kare to usi row ko update kiya jata hai (nayi row nahi banti)
- Removed user dobara submit nahi kar sakta
"""

import os
import re
import json
import asyncio
import logging
import threading
from datetime import datetime
from html import escape

import gspread
from gspread.utils import rowcol_to_a1
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# ======== YAHAN APNI DETAILS DAALO ========
# Token ko environment variable (Railway Variables) se lo.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
# --- @LegitFunded_Bot ki settings (seedha code mein) ---
# Jin accounts ko notification jaye (ek ya zyada Telegram ID, comma se alag).
# Har admin ko bot ko ek dafa Start karna zaroori hai.
ADMIN_CHAT_IDS = [7331621975]  # @LegitFundedTeam aur owner
# In accounts par duplicate wale rules lagu nahi hote (taake aap baar baar test kar sakein).
# Sab par rules lagane ke liye ise khali kar dein:  SKIP_DUPLICATE_CHECK_FOR = []
SKIP_DUPLICATE_CHECK_FOR = ADMIN_CHAT_IDS
PRIVATE_GROUP_CHAT_ID = -1004372780406        # GOLD INSIGHT - PROP FIRM (channel)
CLUB_NAME = "GOLD INSIGHT - PROP FIRM"
SHEET_NAME = "GOLD INSIGHT DETAILS"           # Google Sheet ka naam (bilkul yehi)
FUNDED_BUY_URL = "https://forms.gle/ujbT4v5mXy4eqGHeA"    # "FUNDED BUY" par ye form dikhta hai
SUPPORT_USERNAME = "LegitFundedTeam"                      # support ki ID (bina @ ke)
GOOGLE_CREDENTIALS_FILE = "credentials.json"  # sirf apne computer par test ke liye
POLL_SECONDS = 30  # bot kitni dair baad sheet check kare
# ===========================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # logs mein bot token na aaye
logger = logging.getLogger(__name__)

# Conversation states
NAME, EMAIL, PROPFIRM, SIZE, CONFIRM, WELCOME = range(6)
TEXT_ONLY = filters.TEXT & ~filters.COMMAND
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------- User ko jane wale messages ----------------
def support_markup():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"💬 Message @{SUPPORT_USERNAME}", url=f"https://t.me/{SUPPORT_USERNAME}")
    ]])


REJECTED_TEXT = (
    "⚠️ <b>We couldn't verify your details</b>\n\n"
    "It looks like something in the information you submitted isn't correct "
    "(for example, your email or prop firm details), so we weren't able to verify it.\n\n"
    "Please contact our support team — they'll help you sort it out quickly 🤝\n"
    f"👉 @{SUPPORT_USERNAME}\n\n"
    "You can also send /start to submit your details again."
)

REMOVED_TEXT = (
    "❌ <b>Access removed</b>\n\n"
    f"Your access to <b>{escape(CLUB_NAME)}</b> has been removed by our team.\n\n"
    "If you think this is a mistake, please contact our support team:\n"
    f"👉 @{SUPPORT_USERNAME}"
)

PENDING_TEXT = (
    "⏳ <b>You've already submitted your details.</b>\n\n"
    "Our team is verifying them. Once approved, your private group access link "
    "will be sent to you right here — no need to submit again.\n\n"
    "Thank you for your patience! 🙏"
)

APPROVED_TEXT = (
    "✅ <b>You're already approved!</b>\n\n"
    "Your access link has already been sent to you. If you can't find it or need help, "
    "please contact our support team:\n"
    f"👉 @{SUPPORT_USERNAME}"
)


def duplicate_reply(kind):
    """(text, keyboard) - duplicate/purani request wale users ke liye."""
    return {
        "pending": (PENDING_TEXT, None),
        "approved": (APPROVED_TEXT, support_markup()),
        "removed": (REMOVED_TEXT, support_markup()),
    }[kind]


# ---------------- Google Sheet helpers ----------------
HEADERS = [
    "Date", "Telegram ID", "Username", "Name", "Email", "Prop Firm",
    "Account Size", "Status", "Bot Action", "Invite Link",
]
COL = {h: i for i, h in enumerate(HEADERS)}  # 0-based column index
STATUS_OPTIONS = ["Pending", "Approved", "Rejected", "Removed"]
# Status cell ka background colour (red, green, blue: 0 se 1)
STATUS_COLORS = {
    "Pending": (1.00, 0.95, 0.70),   # halka peela
    "Approved": (0.72, 0.88, 0.72),  # halka hara
    "Rejected": (0.99, 0.80, 0.55),  # orange
    "Removed": (0.96, 0.68, 0.68),   # halka laal
}
_sheet = None
_lock = threading.RLock()  # sheet ke kaam ek waqt mein ek hi chalein


def get_sheet():
    """Sheet ek dafa connect karo, header, Status dropdown aur rang set karo."""
    global _sheet
    with _lock:
        if _sheet is None:
            # Railway/server par: poori JSON key environment variable mein hoti hai.
            # Apne computer par: credentials.json file se parh leta hai.
            creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
            if creds_json:
                info = json.loads(creds_json)
                gc = gspread.service_account_from_dict(info)
                sa_email = info.get("client_email", "?")
            else:
                gc = gspread.service_account(filename=GOOGLE_CREDENTIALS_FILE)
                sa_email = "(credentials.json wala client_email)"
            try:
                ws = gc.open(SHEET_NAME).sheet1
            except gspread.SpreadsheetNotFound:
                raise RuntimeError(
                    f"Sheet '{SHEET_NAME}' nahi mili. Naam bilkul sahi likha ho aur sheet ko "
                    f"{sa_email} ke sath Editor access par share karein."
                )
            logger.info(f"Google Sheet '{SHEET_NAME}' connected (service account: {sa_email})")

            # Purani sheet mein "Investor Password" column ho to hata do
            # (baaki data khud left shift ho jata hai, kuch mitta nahi)
            old_header = ws.row_values(1)
            if "Investor Password" in old_header:
                ws.delete_columns(old_header.index("Investor Password") + 1)
                logger.info("Sheet se purana 'Investor Password' column hata diya gaya")

            # Header row
            ws.update(values=[HEADERS], range_name=f"A1:{rowcol_to_a1(1, len(HEADERS))}")

            status_col = COL["Status"]
            status_range = {
                "sheetId": ws.id,
                "startRowIndex": 1,  # row 2 se shuru (header chhor kar)
                "startColumnIndex": status_col,
                "endColumnIndex": status_col + 1,
            }

            # Status column ke purane rang wale rules dhoondo (taake dobara dobara jama na hon)
            meta = ws.spreadsheet.fetch_sheet_metadata(
                {"fields": "sheets(properties(sheetId),conditionalFormats)"}
            )
            existing_rules = []
            for sh in meta.get("sheets", []):
                if sh.get("properties", {}).get("sheetId") == ws.id:
                    existing_rules = sh.get("conditionalFormats", [])

            requests = [
                {
                    "setDataValidation": {  # rule ke baghair = purani dropdowns hata do
                        "range": {
                            "sheetId": ws.id,
                            "startRowIndex": 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": len(HEADERS),
                        }
                    }
                },
                {
                    "setDataValidation": {  # Status column mein dropdown
                        "range": status_range,
                        "rule": {
                            "condition": {
                                "type": "ONE_OF_LIST",
                                "values": [{"userEnteredValue": v} for v in STATUS_OPTIONS],
                            },
                            "showCustomUi": True,
                            "strict": True,
                        },
                    }
                },
            ]
            # Status column wale purane rang rules hatao (peeche se aage, taake index na bigde)
            for idx in range(len(existing_rules) - 1, -1, -1):
                for rng in existing_rules[idx].get("ranges", []):
                    if (rng.get("startColumnIndex", 0) == status_col
                            and rng.get("endColumnIndex") == status_col + 1):
                        requests.append({"deleteConditionalFormatRule": {"sheetId": ws.id, "index": idx}})
                        break
            # Har status ka apna background colour
            for i, (status, (red, green, blue)) in enumerate(STATUS_COLORS.items()):
                requests.append({
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [status_range],
                            "booleanRule": {
                                "condition": {
                                    "type": "TEXT_EQ",
                                    "values": [{"userEnteredValue": status}],
                                },
                                "format": {"backgroundColor": {"red": red, "green": green, "blue": blue}},
                            },
                        },
                        "index": i,
                    }
                })
            ws.spreadsheet.batch_update({"requests": requests})
            _sheet = ws
        return _sheet


class DuplicateError(Exception):
    """kind: pending / approved / removed"""

    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


def find_conflict(rows, user_id):
    """
    Sheet ki rows dekh kar batao (kind, row_number):
      pending / approved / removed -> is Telegram account ki pehle se row hai
      resubmit                     -> pehle Rejected hua tha, usi row ko update karna hai
      None                         -> bilkul nayi request
    """
    own = {}  # status -> aakhri row number
    for row_num, r in enumerate(rows[1:], start=2):  # header chhor kar
        r = r + [""] * (len(HEADERS) - len(r))
        status = r[COL["Status"]].strip() or "Pending"
        if r[COL["Telegram ID"]].strip() == str(user_id):
            own[status] = row_num

    for kind, status in (("approved", "Approved"), ("pending", "Pending"), ("removed", "Removed")):
        if status in own:
            return kind, own[status]
    if "Rejected" in own:
        return "resubmit", own["Rejected"]
    return None, None


def check_conflict(user_id):
    with _lock:
        rows = get_sheet().get_all_values()
    return find_conflict(rows, user_id)


def save_request(user_id, username, name, email, propfirm, size):
    """
    Request save karo. Duplicate ho to DuplicateError uthata hai.
    Return: "new" (nayi row) ya "resubmitted" (Rejected wali row update hui).
    """
    with _lock:
        ws = get_sheet()
        kind, row_num = None, None
        if user_id not in SKIP_DUPLICATE_CHECK_FOR:
            kind, row_num = find_conflict(ws.get_all_values(), user_id)
        if kind in ("pending", "approved", "removed"):
            raise DuplicateError(kind)

        values = {
            "Date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "Telegram ID": user_id,
            "Username": username,
            "Name": name,
            "Email": email,
            "Prop Firm": propfirm,
            "Account Size": size,
            "Status": "Pending",
        }
        row = [values.get(h, "") for h in HEADERS[: COL["Status"] + 1]]

        if kind == "resubmit":
            last = rowcol_to_a1(row_num, COL["Status"] + 1)
            ws.batch_update([
                {"range": f"A{row_num}:{last}", "values": [row]},
                # purana "Bot Action" saaf karo, taake naye status par bot dobara kaam kare
                {"range": rowcol_to_a1(row_num, COL["Bot Action"] + 1), "values": [[""]]},
            ])
            return "resubmitted"

        ws.append_row(row, value_input_option="RAW")  # RAW: user ka text formula na ban jaye
        return "new"


def read_rows():
    with _lock:
        return get_sheet().get_all_values()


def write_action(row, text, link=None):
    """'Bot Action' mein likho ke bot ne kya kiya; link ho to 'Invite Link' mein bhi."""
    with _lock:
        data = [{"range": rowcol_to_a1(row, COL["Bot Action"] + 1), "values": [[text]]}]
        if link:
            data.append({"range": rowcol_to_a1(row, COL["Invite Link"] + 1), "values": [[link]]})
        get_sheet().batch_update(data)


# ---------------- Sheet watcher ----------------
def handled_status(action):
    """'Bot Action' ka text dekh kar pata lagao ke bot ne is row par kaunsa status handle kiya."""
    for st in ("Approved", "Rejected", "Removed"):
        if action.startswith(st + ":"):
            return st
    return ""


async def notify_admin(application, text, parse_mode=None):
    """Saare admins ko message bhejo (ek fail ho to baaki ko phir bhi jaye)."""
    for admin_id in ADMIN_CHAT_IDS:
        try:
            await application.bot.send_message(chat_id=admin_id, text=text, parse_mode=parse_mode)
        except Exception as e:
            logger.error(f"Admin notify error (ID {admin_id}): {e}")


async def check_sheet(application):
    rows = await asyncio.to_thread(read_rows)

    for row_num, r in enumerate(rows[1:], start=2):  # header chhor kar
        r = r + [""] * (len(HEADERS) - len(r))
        name = r[COL["Name"]]
        status = r[COL["Status"]].strip()
        action = r[COL["Bot Action"]].strip()
        link = r[COL["Invite Link"]].strip()

        # Sirf Approved/Rejected/Removed, aur sirf tab jab bot is status par pehle kaam na kar chuka ho
        if status not in ("Approved", "Rejected", "Removed"):
            continue
        if handled_status(action) == status:
            continue

        try:
            user_id = int(r[COL["Telegram ID"]])
        except ValueError:
            await asyncio.to_thread(write_action, row_num, f"{status}: FAILED - Telegram ID galat hai")
            continue

        new_link = None
        try:
            if status == "Approved":
                invite = await application.bot.create_chat_invite_link(
                    chat_id=PRIVATE_GROUP_CHAT_ID,
                    member_limit=1,  # sirf ek dafa use ho sake
                )
                new_link = invite.invite_link
                await application.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"🎉 You're in! Your details have been verified.\n\n"
                        f"Here's your access link to <b>{escape(CLUB_NAME)}</b>:\n{new_link}"
                    ),
                    parse_mode="HTML",
                )
                result = "Link sent"

            elif status == "Rejected":
                # Sirf message: details mein masla hai, support se contact karein
                await application.bot.send_message(
                    chat_id=user_id,
                    text=REJECTED_TEXT,
                    parse_mode="HTML",
                    reply_markup=support_markup(),
                )
                result = "Message sent (contact support)"

            else:  # Removed
                was_approved = bool(link) or handled_status(action) == "Approved"
                if was_approved:
                    # Pehle wala invite link cancel karo (agar user ne abhi join nahi kiya)
                    if link:
                        try:
                            await application.bot.revoke_chat_invite_link(PRIVATE_GROUP_CHAT_ID, link)
                        except Exception as e:
                            logger.info(f"Row {row_num}: link revoke nahi hua ({e})")
                    # Group se nikalo: ban + turant unban (taake baad mein naye link se aa sake)
                    await application.bot.ban_chat_member(PRIVATE_GROUP_CHAT_ID, user_id)
                    await application.bot.unban_chat_member(
                        PRIVATE_GROUP_CHAT_ID, user_id, only_if_banned=True
                    )
                await application.bot.send_message(
                    chat_id=user_id,
                    text=REMOVED_TEXT,
                    parse_mode="HTML",
                    reply_markup=support_markup(),
                )
                result = (
                    "User removed from channel, message sent"
                    if was_approved
                    else "Message sent (user was not in channel)"
                )
            result = f"{status}: {result} {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        except Exception as e:
            logger.error(f"Row {row_num} action error: {e}")
            result = f"{status}: FAILED - {e}"
            await notify_admin(
                application, f"⚠️ Sheet row {row_num} ({name}): {status} action failed.\n{e}"
            )

        await asyncio.to_thread(write_action, row_num, result, new_link)


async def sheet_watcher(application):
    await asyncio.sleep(5)
    while True:
        try:
            await check_sheet(application)
        except Exception as e:
            logger.error(f"Sheet check error: {type(e).__name__}: {e}")
        await asyncio.sleep(POLL_SECONDS)


async def post_init(application):
    me = await application.bot.get_me()
    logger.info(f"Running as @{me.username} | club: {CLUB_NAME} | sheet: {SHEET_NAME}")
    # Background task shuru karo (reference bot_data mein rakhte hain)
    application.bot_data["watcher"] = asyncio.create_task(sheet_watcher(application))


# ---------------- Keyboards & text helpers ----------------
def clean(text):
    return text.strip()[:200]


def welcome_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("FUNDED BUY", callback_data="funded_buy")],
        [InlineKeyboardButton("ALREADY BUY UNDER YOU", callback_data="already_buy")],
        [InlineKeyboardButton("CONNECT SUPPORT TEAM", callback_data="support")],
    ])


def confirm_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm & Submit", callback_data="confirm"),
        InlineKeyboardButton("✏️ Edit", callback_data="edit"),
    ]])


def edit_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👤 Name", callback_data="edit_name"),
            InlineKeyboardButton("📧 Email", callback_data="edit_email"),
        ],
        [
            InlineKeyboardButton("🏢 Prop Firm", callback_data="edit_firm"),
            InlineKeyboardButton("💵 Account Size", callback_data="edit_size"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="back")],
    ])


def details_block(d):
    return (
        f"👤 Name: {escape(d['name'])}\n"
        f"📧 Email: {escape(d['email'])}\n"
        f"🏢 Prop Firm: {escape(d['propfirm'])}\n"
        f"💵 Account Size: {escape(d['size'])}"
    )


def summary_text(d):
    return (
        "📋 <b>Please review your details</b>\n\n"
        f"{details_block(d)}\n\n"
        "Is everything correct? Tap <b>Confirm &amp; Submit</b>, or <b>Edit</b> to fix anything."
    )


async def show_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=summary_text(context.user_data),
        parse_mode="HTML",
        reply_markup=confirm_keyboard(),
    )
    return CONFIRM


# ---------------- Conversation handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    user = update.effective_user

    # Pehle check karo: is user ki pehle se request to nahi?
    kind = None
    if user.id not in SKIP_DUPLICATE_CHECK_FOR:
        try:
            kind, _ = await asyncio.to_thread(check_conflict, user.id)
        except Exception as e:
            logger.error(f"Start check error: {type(e).__name__}: {e}")

    if kind in ("pending", "approved", "removed"):
        text, markup = duplicate_reply(kind)
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
        return ConversationHandler.END

    if kind == "resubmit":
        await update.message.reply_text(
            "🔁 Your previous submission couldn't be verified. Let's fix it — "
            "please enter your details again below."
        )

    await update.message.reply_text(
        "⚡️ Welcome to Prop Firm Community ♟\n\n"
        "🔓 Gold Insight Prop Firm Community — Buy The Funded Challenge Given Below First",
        reply_markup=welcome_keyboard(),
    )
    return WELCOME


async def funded_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'FUNDED BUY' button: form ka link dikhao (user usi menu mein rehta hai)."""
    await update.callback_query.answer()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            "💰 <b>Funded Buy</b>\n\n"
            "Please fill out this form to buy your funded challenge:\n"
            f"{FUNDED_BUY_URL}"
        ),
        parse_mode="HTML",
    )


async def already_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'ALREADY BUY UNDER YOU' button: verification flow shuru karo (Name se)."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)  # buttons hata do
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Let's get to work. First, send me your <b>full name</b>:",
        parse_mode="HTML",
    )
    return NAME


async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = clean(update.message.text)
    if context.user_data.pop("editing", False):
        return await show_summary(update, context)
    await update.message.reply_text(
        "Got it. Now send your <b>Email</b> (the one you used to sign up with your propfirm account) :-",
        parse_mode="HTML",
    )
    return EMAIL


async def get_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    email = clean(update.message.text)
    if not EMAIL_RE.match(email):
        await update.message.reply_text(
            "That doesn't look like a valid email. Please send it again (e.g. name@example.com):"
        )
        return EMAIL
    context.user_data["email"] = email
    if context.user_data.pop("editing", False):
        return await show_summary(update, context)
    await update.message.reply_text("Which Company Funded Account Did You purchase ?")
    return PROPFIRM


async def get_propfirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["propfirm"] = clean(update.message.text)
    if context.user_data.pop("editing", False):
        return await show_summary(update, context)
    await update.message.reply_text("<b>Account Size</b> :-", parse_mode="HTML")
    return SIZE


async def get_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["size"] = clean(update.message.text)
    context.user_data.pop("editing", None)
    return await show_summary(update, context)


async def support_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'Contact Support' button: support ki ID dikhao."""
    await update.callback_query.answer()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            "💬 <b>Contact Support</b>\n\n"
            "Have a question or need a hand? Our team is happy to help you 😊\n\n"
            "Contact our support team here:\n"
            f"👉 @{SUPPORT_USERNAME}"
        ),
        parse_mode="HTML",
        reply_markup=support_markup(),
    )


async def remind_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Please use the buttons above to continue 👆")


async def edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("What would you like to change?", reply_markup=edit_keyboard())
    return CONFIRM


async def back_to_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        summary_text(context.user_data), parse_mode="HTML", reply_markup=confirm_keyboard()
    )
    return CONFIRM


async def edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    field = query.data.split("_")[1]
    context.user_data["editing"] = True

    prompts = {
        "name": ("Send your correct <b>full name</b>:", NAME),
        "email": ("Send your correct <b>Email</b>:", EMAIL),
        "firm": ("Send your correct <b>Prop Firm</b>:", PROPFIRM),
        "size": ("Send your correct <b>Account Size</b>:", SIZE),
    }
    text, state = prompts[field]
    await query.edit_message_text(text, parse_mode="HTML")
    return state


async def submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    d = context.user_data
    user = query.from_user

    try:
        outcome = await asyncio.to_thread(
            save_request,
            user.id,
            user.username or "N/A",
            d["name"],
            d["email"],
            d["propfirm"],
            d["size"],
        )
    except DuplicateError as e:
        text, markup = duplicate_reply(e.kind)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        d.clear()
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Google Sheet save error: {type(e).__name__}: {e}")
        await notify_admin(context.application, f"⚠️ Sheet save failed for user {user.id}.\n{e}")
        await query.edit_message_text(
            summary_text(d) + "\n\n⚠️ Couldn't submit right now. Please tap Confirm again in a moment.",
            parse_mode="HTML",
            reply_markup=confirm_keyboard(),
        )
        return CONFIRM

    await query.edit_message_text(
        "✅ <b>Details submitted!</b>\n\n"
        f"{details_block(d)}\n\n"
        f"Thank you for joining <b>{escape(CLUB_NAME)}</b>! 🐺🙏\n\n"
        "Our team will verify your details, and once approved your private group "
        "access link will be sent to you right here.",
        parse_mode="HTML",
    )

    # Admin ko notification
    title = "📥 <b>New Verification Request</b>" if outcome == "new" else "🔁 <b>Re-submitted Request</b> (after Rejected)"
    await notify_admin(
        context.application,
        f"{title}\n\n"
        f"👤 Name: {escape(d['name'])}\n"
        f"📧 Email: {escape(d['email'])}\n"
        f"🏢 Prop Firm: {escape(d['propfirm'])}\n"
        f"💵 Account Size: {escape(d['size'])}\n"
        f"🔗 Telegram: @{escape(user.username or 'N/A')} (ID: {user.id})\n\n"
        f"Full details are in the Google Sheet. "
        f"Check them, then set Status to Approved, Rejected or Removed.",
        parse_mode="HTML",
    )

    d.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Process cancelled. Send /start to begin again.")
    return ConversationHandler.END


async def stale_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Bot restart hone ke baad purane buttons dabane par
    await update.callback_query.answer(
        "This session has expired. Please send /start to begin again.", show_alert=True
    )


def main():
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WELCOME: [
                CallbackQueryHandler(funded_buy, pattern=r"^funded_buy$"),
                CallbackQueryHandler(already_buy, pattern=r"^already_buy$"),
                CallbackQueryHandler(support_info, pattern=r"^support$"),
                MessageHandler(TEXT_ONLY, remind_buttons),
            ],
            NAME: [MessageHandler(TEXT_ONLY, get_name)],
            EMAIL: [MessageHandler(TEXT_ONLY, get_email)],
            PROPFIRM: [MessageHandler(TEXT_ONLY, get_propfirm)],
            SIZE: [MessageHandler(TEXT_ONLY, get_size)],
            CONFIRM: [
                CallbackQueryHandler(submit, pattern=r"^confirm$"),
                CallbackQueryHandler(edit_menu, pattern=r"^edit$"),
                CallbackQueryHandler(back_to_summary, pattern=r"^back$"),
                CallbackQueryHandler(edit_field, pattern=r"^edit_(name|email|firm|size)$"),
                MessageHandler(TEXT_ONLY, remind_buttons),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,  # beech mein /start dabane par dobara shuru ho jaye
    )

    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(stale_button))

    logger.info("Bot shuru ho gaya hai...")
    application.run_polling()


if __name__ == "__main__":
    main()
