import os
import re
import sys
import time
import json
import logging
import asyncio
import random
import feedparser
import speech_recognition as sr
from pydub import AudioSegment
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from flask import Flask, request
from pyrogram import Client, filters, errors
from pyrogram.enums import ChatType, ChatMemberStatus, ParseMode
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions,
    ChatMember,
)
from FrozenMusic.infra.concurrency.ci import deterministic_privilege_validator
from gtts import gTTS

# Load environment variables
load_dotenv()

API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "5268762773"))
LOG_CHANNEL_ID = int(os.getenv("-1002107533268", None)) # Log channel for admin actions
RSS_FEED_URL = os.environ.get("RSS_FEED_URL", "https://www.youtube.com/feeds/videos.xml?channel_id=UC-K20bY-dK_9e17W3K-252A")
RSS_CHANNEL_ID = int(os.getenv("RSS_CHANNEL_ID", None))

# Initialize the bot client
session_name = os.environ.get("SESSION_NAME", "help_bot")
bot = Client(session_name, bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# Define bot name for dynamic use
BOT_NAME = os.environ.get("BOT_NAME", "Frozen Help Bot")
BOT_LINK = os.environ.get("BOT_LINK", f"https://t.me/{bot.get_me().username}")

# In-memory storage for user stats and RSS feed
user_stats = {}
last_rss_entry_link = ""
premium_users = set()

# A simple FAQ dictionary
FAQ_DATA = {
    "rules": "ग्रुप के नियम:\n1. कोई स्पैमिंग नहीं\n2. कोई गाली-गलौज नहीं\n3. केवल ग्रुप से संबंधित बातें।",
    "help": "मैं आपकी मदद कैसे कर सकता हूँ? `/help` कमांड का प्रयोग करें या नीचे दिए गए बटन पर क्लिक करें।",
    "contact": "एडमिन से संपर्क करने के लिए @Frozensupport1 पर मैसेज करें।",
}

# New In-memory storage for new features
warn_counts = {}
user_message_timestamps = {}
scheduled_messages = []
user_reputation = {}
auto_delete_timers = {}

# --- Flood Control Settings (New) ---
FLOOD_THRESHOLD = 5 # max messages
FLOOD_TIME_WINDOW = 3 # in seconds
FLOOD_MUTE_TIME = 10 # in minutes

# Helper functions
def to_bold_unicode(text: str) -> str:
    bold_text = ""
    for char in text:
        if 'A' <= char <= 'Z':
            bold_text += chr(ord('𝗔') + (ord(char) - ord('A')))
        elif 'a' <= char <= 'z':
            bold_text += chr(ord('𝗮') + (ord('char') - ord('a')))
        else:
            bold_text += char
    return bold_text

async def extract_target_user(message: Message):
    if message.reply_to_message:
        return message.reply_to_message.from_user
    
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("❌ कृपया किसी यूज़र को रिप्लाई करें या उसका @username/user_id दें।")
        return None

    target = parts[1]
    if target.startswith("@"):
        target = target[1:]
    try:
        user = await message._client.get_users(target)
        return user
    except:
        await message.reply("❌ यह यूज़र नहीं मिला।")
        return None

# --- Admin Action Log (Step 9) ---
async def log_admin_action(action: str, admin: str, target: str):
    if LOG_CHANNEL_ID:
        log_message = f"🛡️ **एडमिन लॉग**\n\n**कार्य:** {action}\n**एडमिन:** {admin}\n**लक्ष्य:** {target}"
        try:
            await bot.send_message(LOG_CHANNEL_ID, log_message)
        except Exception as e:
            print(f"Failed to send log to channel: {e}")

# --- Welcome/Onboarding Feature ---
@bot.on_message(filters.new_chat_members)
async def welcome_new_member(client, message):
    chat_id = message.chat.id
    for member in message.new_chat_members:
        if member.is_bot:
            continue
        
        user_name = member.first_name
        welcome_text = (
            f"👋 **{user_name}** का स्वागत है! 🎉\n\n"
            "कृपया ग्रुप के इन नियमों का पालन करें:\n"
            "1. किसी भी तरह की स्पैमिंग न करें।\n"
            "2. अभद्र भाषा का प्रयोग न करें।\n"
            "3. केवल ग्रुप से संबंधित बातें करें।"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ मैंने नियम पढ़ लिए हैं", callback_data="rules_accepted")]
        ])
        await client.send_message(chat_id, welcome_text, reply_markup=keyboard)

@bot.on_callback_query(filters.regex("rules_accepted"))
async def handle_rules_accepted(client, callback_query):
    user_id = callback_query.from_user.id
    message = callback_query.message
    
    if len(message.entities) > 1 and message.entities[1].type == "text_mention":
        joined_user_id = message.entities[1].user.id
        if joined_user_id != user_id:
            await callback_query.answer("❌ आप किसी और के लिए यह बटन नहीं दबा सकते।", show_alert=True)
            return

    await callback_query.answer("✅ धन्यवाद! आप अब ग्रुप में भाग ले सकते हैं।", show_alert=False)
    try:
        await callback_query.message.delete()
    except Exception as e:
        print(f"Error deleting welcome message: {e}")

# --- Moderation Commands ---
@bot.on_message(filters.group & filters.command("mute"))
async def mute_user(client, message):
    if not await deterministic_privilege_validator(message):
        return await message.reply("❌ आप यह कमांड इस्तेमाल नहीं कर सकते, क्योंकि आप एडमिन नहीं हैं।")
    
    target_user_id = await extract_target_user(message)
    if not target_user_id:
        return
    try:
        await client.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target_user_id.id,
            permissions=ChatPermissions(can_send_messages=False)
        )
        await message.reply(f"🔇 यूज़र को सफलतापूर्वक म्यूट कर दिया गया है।")
        await log_admin_action("Mute", message.from_user.first_name, target_user_id.first_name)
    except Exception as e:
        await message.reply(f"❌ यूज़र को म्यूट करने में एक समस्या आई।\nError: {e}")

@bot.on_message(filters.group & filters.command("unmute"))
async def unmute_user(client, message):
    if not await deterministic_privilege_validator(message):
        return await message.reply("❌ आप यह कमांड इस्तेमाल नहीं कर सकते, क्योंकि आप एडमिन नहीं हैं।")
        
    target_user_id = await extract_target_user(message)
    if not target_user_id:
        return
    try:
        await client.unban_chat_member(chat_id=message.chat.id, user_id=target_user_id.id)
        await message.reply(f"🔊 यूज़र को सफलतापूर्वक अनम्यूट कर दिया गया है।")
        await log_admin_action("Unmute", message.from_user.first_name, target_user_id.first_name)
    except Exception as e:
        await message.reply(f"❌ यूज़र को अनम्यूट करने में एक समस्या आई।\nError: {e}")

@bot.on_message(filters.group & filters.command("tmute"))
async def tmute_user(client, message):
    if not await deterministic_privilege_validator(message):
        return await message.reply("❌ आप यह कमांड इस्तेमाल नहीं कर सकते, क्योंकि आप एडमिन नहीं हैं।")
        
    parts = message.text.split()
    if len(parts) < 3:
        return await message.reply("❌ सही इस्तेमाल: `/tmute <reply_to_user> <minutes>`")
    target_user = await extract_target_user(message)
    if not target_user:
        return
    try:
        mute_minutes = int(parts[2])
    except (IndexError, ValueError):
        return await message.reply("❌ कृपया समय मिनटों में एक संख्या के रूप में दें।")

    mute_end_date = datetime.now(timezone.utc) + timedelta(minutes=mute_minutes)
    try:
        await client.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target_user.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=mute_end_date
        )
        await message.reply(f"⏱️ यूज़र को {mute_minutes} मिनट के लिए म्यूट कर दिया गया है।")
        await log_admin_action(f"Temporary Mute ({mute_minutes} mins)", message.from_user.first_name, target_user.first_name)
    except Exception as e:
        await message.reply(f"❌ यूज़र को अस्थायी रूप से म्यूट करने में एक समस्या आई।\nError: {e}")

@bot.on_message(filters.group & filters.command("kick"))
async def kick_user(client, message):
    if not await deterministic_privilege_validator(message):
        return await message.reply("❌ आप यह कमांड इस्तेमाल नहीं कर सकते, क्योंकि आप एडमिन नहीं हैं।")
        
    target_user = await extract_target_user(message)
    if not target_user:
        return
    try:
        await client.kick_chat_member(chat_id=message.chat.id, user_id=target_user.id)
        await message.reply("🚪 यूज़र को सफलतापूर्वक ग्रुप से किक कर दिया गया है।")
        await log_admin_action("Kick", message.from_user.first_name, target_user.first_name)
    except Exception as e:
        await message.reply(f"❌ यूज़र को किक करने में एक समस्या आई।\nError: {e}")

@bot.on_message(filters.group & filters.command("ban"))
async def ban_user(client, message):
    if not await deterministic_privilege_validator(message):
        return await message.reply("❌ आप यह कमांड इस्तेमाल नहीं कर सकते, क्योंकि आप एडमिन नहीं हैं।")
        
    target_user = await extract_target_user(message)
    if not target_user:
        return
    try:
        await client.ban_chat_member(chat_id=message.chat.id, user_id=target_user.id)
        await message.reply("🚫 यूज़र को सफलतापूर्वक ग्रुप से बैन कर दिया गया है।")
        await log_admin_action("Ban", message.from_user.first_name, target_user.first_name)
    except Exception as e:
        await message.reply(f"❌ यूज़र को बैन करने में एक समस्या आई।\nError: {e}")

@bot.on_message(filters.group & filters.command("unban"))
async def unban_user(client, message):
    if not await deterministic_privilege_validator(message):
        return await message.reply("❌ आप यह कमांड इस्तेमाल नहीं कर सकते, क्योंकि आप एडमिन नहीं हैं।")
        
    target_user = await extract_target_user(message)
    if not target_user:
        return
    try:
        await client.unban_chat_member(chat_id=message.chat.id, user_id=target_user.id)
        await message.reply("✅ यूज़र को सफलतापूर्वक अनबैन कर दिया गया है।")
        await log_admin_action("Unban", message.from_user.first_name, target_user.first_name)
    except Exception as e:
        await message.reply(f"❌ यूज़र को अनबैन करने में एक समस्या आई।\nError: {e}")

# --- New Advanced Help System ---
@bot.on_message(filters.command(["start", "help"]))
async def start_and_help_handler(_, message):
    user_id = message.from_user.id
    raw_name = message.from_user.first_name or ""
    styled_name = to_bold_unicode(raw_name)
    user_link = f"[{styled_name}](tg://user?id={user_id})"

    help_text = to_bold_unicode("Help")
    add_me_text = to_bold_unicode("Add Me")
    updates_text = to_bold_unicode("Updates")
    support_text = to_bold_unicode("Support")

    caption = (
        f"👋 **Welcome!**\n\n"
        f"This is an advanced group management assistant.\n\n"
        f"🛠️ **Admin Commands:** Mute, Unmute, Tmute, Kick, Ban, Unban\n\n"
        f"🛡️ **Anti-Abuse Filters:** This bot automatically handles spam links, forwards, and profanity.\n\n"
        f"๏ Click **Help** below to see all commands."
    )
    buttons = [
        [
            InlineKeyboardButton(f"➕ {add_me_text}", url=f"{BOT_LINK}?startgroup=true"),
            InlineKeyboardButton(f"📢 {updates_text}", url="https://t.me/vibeshiftbots")
        ],
        [
            InlineKeyboardButton(f"💬 {support_text}", url="https://t.me/Frozensupport1"),
            InlineKeyboardButton(f"❓ {help_text}", callback_data="show_help")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(buttons)
    await message.reply_animation(
        animation="https://frozen-imageapi.lagendplayersyt.workers.dev/file/2e483e17-05cb-45e2-b166-1ea476ce9521.mp4",
        caption=caption,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )

@bot.on_callback_query(filters.regex("^show_help$"))
async def show_help_callback(_, callback_query):
    text = ">📜 *Choose a category to explore commands:*"
    buttons = [
        [InlineKeyboardButton("🛡️ Admin Tools", callback_data="help_admin")],
        [InlineKeyboardButton("❤️ Fun & Games", callback_data="help_fun")],
        [InlineKeyboardButton("🏠 Home", callback_data="go_back")]
    ]
    reply_markup = InlineKeyboardMarkup(buttons)
    await callback_query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)

@bot.on_callback_query(filters.regex("^help_admin$"))
async def help_admin_callback(_, callback_query):
    text = (
        "🛡️ *Admin & Moderation Commands*\n\n"
        ">➜ `/mute <@user or reply>`\n"
        "   • Mute a user indefinitely.\n\n"
        ">➜ `/unmute <@user or reply>`\n"
        "   • Unmute a previously muted user.\n\n"
        ">➜ `/tmute <@user or reply> <minutes>`\n"
        "   • Temporarily mute a user for a set duration.\n\n"
        ">➜ `/kick <@user or reply>`\n"
        "   • Kick a user from the group.\n\n"
        ">➜ `/ban <@user or reply>`\n"
        "   • Ban a user permanently.\n\n"
        ">➜ `/unban <@user or reply>`\n"
        "   • Unban a previously banned user."
    )
    buttons = [[InlineKeyboardButton("🔙 Back", callback_data="show_help")]]
    reply_markup = InlineKeyboardMarkup(buttons)
    await callback_query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)

@bot.on_callback_query(filters.regex("^help_fun$"))
async def help_fun_callback(_, callback_query):
    text = (
        "❤️ *Fun & Engagement Commands*\n\n"
        ">➜ `/poll <question> <option1> <option2> ...`\n"
        "   • Create a poll with multiple options.\n\n"
        ">➜ `/couple`\n"
        "   • Find a random couple from the group members.\n\n"
        ">➜ `/remindme in <time> <message>`\n"
        "   • Set a personal reminder. (Example: `/remindme in 1h to eat lunch`)\n\n"
        ">➜ `/dice`\n"
        "   • Roll a dice."
    )
    buttons = [[InlineKeyboardButton("🔙 Back", callback_data="show_help")]]
    reply_markup = InlineKeyboardMarkup(buttons)
    await callback_query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)

@bot.on_callback_query(filters.regex("^go_back$"))
async def go_back_callback(_, callback_query):
    user_id = callback_query.from_user.id
    raw_name = callback_query.from_user.first_name or ""
    styled_name = to_bold_unicode(raw_name)
    user_link = f"[{styled_name}](tg://user?id={user_id})"

    help_text = to_bold_unicode("Help")
    add_me_text = to_bold_unicode("Add Me")
    updates_text = to_bold_unicode("Updates")
    support_text = to_bold_unicode("Support")

    caption = (
        f"👋 **Welcome!**\n\n"
        f"This is an advanced group management assistant.\n\n"
        f"🛠️ **Admin Commands:** Mute, Unmute, Tmute, Kick, Ban, Unban\n\n"
        f"🛡️ **Anti-Abuse Filters:** This bot automatically handles spam links, forwards, and profanity.\n\n"
        f"๏ Click **Help** below to see all commands."
    )
    buttons = [
        [
            InlineKeyboardButton(f"➕ {add_me_text}", url=f"{BOT_LINK}?startgroup=true"),
            InlineKeyboardButton(f"📢 {updates_text}", url="https://t.me/vibeshiftbots")
        ],
        [
            InlineKeyboardButton(f"💬 {support_text}", url="https://t.me/Frozensupport1"),
            InlineKeyboardButton(f"❓ {help_text}", callback_data="show_help")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(buttons)
    await callback_query.message.edit_caption(
        caption=caption,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )

# --- Anti-Abuse & Security (Step 3) ---
@bot.on_message(filters.group & ~filters.me & ~filters.via_bot)
async def anti_abuse_filter(client, message):
    user_status = (await client.get_chat_member(message.chat.id, message.from_user.id)).status
    if user_status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
        return

    # Flood Control Logic
    user_id = message.from_user.id
    current_time = time.time()
    
    if user_id not in user_message_timestamps:
        user_message_timestamps[user_id] = []
    
    user_message_timestamps[user_id].append(current_time)
    
    user_message_timestamps[user_id] = [ts for ts in user_message_timestamps[user_id] if current_time - ts <= FLOOD_TIME_WINDOW]
    
    if len(user_message_timestamps[user_id]) > FLOOD_THRESHOLD:
        try:
            await client.restrict_chat_member(
                chat_id=message.chat.id,
                user_id=user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=datetime.now(timezone.utc) + timedelta(minutes=FLOOD_MUTE_TIME)
            )
            await message.reply(f"🔇 **{message.from_user.first_name}** को स्पैमिंग के कारण {FLOOD_MUTE_TIME} मिनट के लिए म्यूट कर दिया गया है।")
            await log_admin_action("Flood Mute", "Automatic", message.from_user.first_name)
        except Exception:
            pass
        return

    if message.forward_from or message.forward_from_chat:
        try:
            await message.delete()
        except Exception:
            pass

    if re.search(r'(https?://\S+|t\.me/\S+)', message.text or ''):
        try:
            await message.delete()
            await client.send_message(message.chat.id, f"❌ **{message.from_user.first_name}**, ग्रुप में लिंक भेजने की अनुमति नहीं है।")
        except Exception:
            pass

    profanity_list = ["fuck", "bitch", "cunt", "chutiya", "randi"]
    if any(word in (message.text or '').lower() for word in profanity_list):
        try:
            await message.delete()
            await client.send_message(message.chat.id, f"❌ **{message.from_user.first_name}**, ग्रुप में ऐसी भाषा का प्रयोग न करें।")
        except Exception:
            pass

# --- Automations & Workflows (Step 4) ---
@bot.on_message(filters.group & filters.text & ~filters.via_bot & filters.regex(r'(?i)^(hi|hello|namaste|rules|help)$'))
async def automation_handler(client, message):
    if not message.text:
        return
    
    text = message.text.lower()
    
    if "hi" in text or "hello" in text or "namaste" in text:
        await message.reply(f"नमस्ते, **{message.from_user.first_name}**! 👋\nग्रुप में आपका स्वागत है।")

    elif "rules" in text:
        await message.reply("ग्रुप के नियम जानने के लिए `/help` कमांड का प्रयोग करें।")

    elif "help" in text:
        await message.reply("मैं आपकी मदद कैसे कर सकता हूँ? `/help` कमांड का प्रयोग करें या नीचे दिए गए बटन पर क्लिक करें।",
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❓ Help Menu", callback_data="show_help")]]))

# --- New Features from Step 5 & 6 ---

@bot.on_message(filters.group & filters.command("poll"))
async def poll_command(_, message):
    args = message.text.split()[1:]
    if len(args) < 3:
        await message.reply("❌ कृपया एक सवाल और कम से कम दो विकल्प दें।\nसही इस्तेमाल: `/poll आपका सवाल? ऑप्शन1 ऑप्शन2 ...`")
        return

    question = args[0]
    options = args[1:]
    
    try:
        await bot.send_poll(
            chat_id=message.chat.id,
            question=question,
            options=options,
            is_anonymous=False
        )
        await message.delete()
    except Exception as e:
        await message.reply(f"❌ पोल बनाने में एक समस्या आई।\nError: {e}")

@bot.on_message(filters.group & filters.command("remindme"))
async def remindme_command(client, message):
    parts = message.text.split()
    if len(parts) < 4 or not parts[2].endswith(('m', 'h', 's')):
        await message.reply("❌ सही इस्तेमाल: `/remindme in <समय> <मैसेज>`\nउदाहरण: `/remindme in 10m to drink water`")
        return

    try:
        time_unit = parts[2][-1]
        time_value = int(parts[2][:-1])
        reminder_text = " ".join(parts[3:])
    except (IndexError, ValueError):
        await message.reply("❌ कृपया सही समय और मैसेज दें।")
        return
        
    if time_unit == 's':
        seconds = time_value
    elif time_unit == 'm':
        seconds = time_value * 60
    elif time_unit == 'h':
        seconds = time_value * 3600
    else:
        await message.reply("❌ अमान्य समय इकाई। केवल 's' (सेकंड), 'm' (मिनट), 'h' (घंटे) का उपयोग करें।")
        return
    
    if seconds > 3600 * 24:
        await message.reply("❌ मैं 24 घंटे से ज़्यादा का रिमाइंडर नहीं लगा सकता।")
        return

    await message.reply(f"⏰ आपका रिमाइंडर **{time_value}{time_unit}** में सेट हो गया है।")
    await asyncio.sleep(seconds)
    await message.reply(f"🔔 **रिमाइंडर:**\n\n**{message.from_user.first_name}**, आपको याद दिलाया जा रहा है:\n\n`{reminder_text}`")


@bot.on_message(filters.group & filters.command("couple"))
async def couple_command(client, message):
    try:
        members = []
        async for member in client.get_chat_members(message.chat.id):
            if not member.user.is_bot:
                members.append(member.user)
        
        if len(members) < 2:
            await message.reply("❌ इस कमांड के लिए कम से कम 2 सदस्य होने चाहिए।")
            return

        couple = random.sample(members, 2)
        
        caption = (
            f"❤️ **Group Couple of the Day** ❤️\n\n"
            f"**{couple[0].first_name}** 💘 **{couple[1].first_name}**"
        )
        
        await message.reply(caption)
    except Exception as e:
        await message.reply(f"❌ इस कमांड को चलाने में एक समस्या आई।\nError: {e}")

@bot.on_message(filters.group & filters.command("dice"))
async def dice_command(client, message):
    await client.send_dice(message.chat.id)

# --- New Features from Step 7, 8 & 9 ---
@bot.on_message(filters.group & filters.command("tts"))
async def tts_command(client, message):
    text = " ".join(message.command[1:])
    if not text:
        return await message.reply("❌ कृपया कोई टेक्स्ट दें।\nसही इस्तेमाल: `/tts नमस्ते, आप कैसे हैं?`")
    
    try:
        tts = gTTS(text=text, lang='hi', slow=False)
        tts.save("tts.mp3")
        await client.send_audio(chat_id=message.chat.id, audio="tts.mp3", caption=f"टेक्स्ट-टू-स्पीच द्वारा भेजा गया:\n`{text}`")
        os.remove("tts.mp3")
    except Exception as e:
        await message.reply(f"❌ ऑडियो बनाने में एक समस्या आई।\nError: {e}")

@bot.on_message(filters.group & filters.command("faq"))
async def faq_command(_, message):
    args = message.text.split()
    if len(args) < 2:
        faq_list = ", ".join(FAQ_DATA.keys())
        await message.reply(f"❌ कृपया एक FAQ चुनें।\nउपलब्ध FAQ: `{faq_list}`\nसही इस्तेमाल: `/faq rules`")
        return
    
    query = args[1].lower()
    if query in FAQ_DATA:
        await message.reply(FAQ_DATA[query])
    else:
        await message.reply(f"❌ `{query}` के लिए कोई FAQ नहीं मिला।")

# --- New Features from Step 10, 11 & 12 ---
@bot.on_message(filters.group & ~filters.via_bot)
async def update_stats(_, message):
    if not message.from_user:
        return
    user_id = message.from_user.id
    if user_id in user_stats:
        user_stats[user_id]['messages'] += 1
    else:
        user_stats[user_id] = {'messages': 1, 'name': message.from_user.first_name}

@bot.on_message(filters.group & filters.command("stats"))
async def stats_command(_, message):
    sorted_stats = sorted(user_stats.items(), key=lambda item: item[1]['messages'], reverse=True)
    
    stats_text = "📊 **Group Analytics: Top 5 Senders**\n\n"
    rank = 1
    for user_id, data in sorted_stats[:5]:
        stats_text += f"{rank}. **{data['name']}**: {data['messages']} messages\n"
        rank += 1
    
    if not sorted_stats:
        stats_text = "❌ अभी तक कोई डेटा नहीं है। कुछ मैसेज भेजने के बाद फिर से कोशिश करें।"

    await message.reply(stats_text)

@bot.on_message(filters.group & filters.command("say"))
async def say_command(client, message):
    if not await deterministic_privilege_validator(message):
        return await message.reply("❌ आप यह कमांड इस्तेमाल नहीं कर सकते।")
    
    text = message.text.split(" ", 1)
    if len(text) < 2:
        return await message.reply("❌ कृपया मुझे कुछ बोलने के लिए दें।\nसही इस्तेमाल: `/say आपका मैसेज`")
    
    await message.delete()
    await client.send_message(message.chat.id, text[1])

# --- RSS Feed Check on a timer ---
async def check_rss_feed_periodically():
    global last_rss_entry_link
    while True:
        try:
            feed = feedparser.parse(RSS_FEED_URL)
            if not feed.entries:
                print("RSS feed has no entries.")
            else:
                new_entries = []
                for entry in reversed(feed.entries):
                    if entry.link == last_rss_entry_link:
                        break
                    new_entries.append(entry)
                
                if new_entries:
                    for entry in new_entries:
                        video_title = entry.title
                        video_link = entry.link
                        message_text = f"📢 **New YouTube Video!**\n\n**{video_title}**\n{video_link}"
                        await bot.send_message(RSS_CHANNEL_ID, message_text)
                    last_rss_entry_link = new_entries[-1].link
        except Exception as e:
            print(f"Error checking RSS feed: {e}")
        
        await asyncio.sleep(3600) # Check every 1 hour

@bot.on_message(filters.group & filters.command("start_rss"))
async def start_rss_command(client, message):
    if not await deterministic_privilege_validator(message):
        return await message.reply("❌ आप यह कमांड इस्तेमाल नहीं कर सकते।")
    
    if not RSS_CHANNEL_ID:
        return await message.reply("❌ कृपया अपनी `.env` फ़ाइल में `RSS_CHANNEL_ID` जोड़ें।")

    asyncio.create_task(check_rss_feed_periodically())
    await message.reply("✅ RSS फ़ीड चेकर शुरू हो गया है।")

# --- New Features from Step 14, 15, 16 & 17 ---

# Step 14: Backup & Persistence
@bot.on_message(filters.command("backup") & filters.user(OWNER_ID))
async def backup_settings(_, message):
    try:
        backup_data = {
            "faq_data": FAQ_DATA,
            "user_stats": user_stats,
            "premium_users": list(premium_users),
            "warn_counts": warn_counts,
            "scheduled_messages": scheduled_messages,
            "user_reputation": user_reputation
        }
        with open("bot_backup.json", "w") as f:
            json.dump(backup_data, f, indent=4)
        await message.reply_document(document="bot_backup.json", caption="✅ बॉट सेटिंग्स का सफलतापूर्वक बैकअप लिया गया है।")
    except Exception as e:
        await message.reply(f"❌ बैकअप लेने में एक समस्या आई।\nError: {e}")

@bot.on_message(filters.command("restore") & filters.user(OWNER_ID))
async def restore_settings(_, message):
    if not message.reply_to_message or not message.reply_to_message.document:
        return await message.reply("❌ कृपया बैकअप फ़ाइल पर रिप्लाई करें।")
    
    try:
        file_path = await message.reply_to_message.download()
        with open(file_path, "r") as f:
            restored_data = json.load(f)
        
        FAQ_DATA.clear()
        FAQ_DATA.update(restored_data.get("faq_data", {}))
        user_stats.clear()
        user_stats.update(restored_data.get("user_stats", {}))
        premium_users.clear()
        premium_users.update(set(restored_data.get("premium_users", [])))
        warn_counts.clear()
        warn_counts.update(restored_data.get("warn_counts", {}))
        scheduled_messages.clear()
        scheduled_messages.extend(restored_data.get("scheduled_messages", []))
        user_reputation.clear()
        user_reputation.update(restored_data.get("user_reputation", {}))

        await message.reply("✅ बॉट सेटिंग्स सफलतापूर्वक रिस्टोर हो गई हैं।")
    except Exception as e:
        await message.reply(f"❌ फ़ाइल रिस्टोर करने में एक समस्या आई।\nError: {e}")

# Step 15: Privacy & Compliance
@bot.on_message(filters.command("mydata"))
async def my_data_command(_, message):
    user_id = message.from_user.id
    if user_id in user_stats:
        await message.reply(f"📊 आपके डेटा के अनुसार, आपने अभी तक {user_stats[user_id]['messages']} मैसेज भेजे हैं। यदि आप चाहते हैं कि यह डेटा डिलीट कर दिया जाए, तो `/deletedata` का प्रयोग करें।")
    else:
        await message.reply("❌ आपके बारे में कोई डेटा संग्रहीत नहीं है।")

@bot.on_message(filters.command("deletedata"))
async def delete_my_data_command(_, message):
    user_id = message.from_user.id
    if user_id in user_stats:
        del user_stats[user_id]
        await message.reply("✅ आपका डेटा सफलतापूर्वक डिलीट कर दिया गया है।")
    else:
        await message.reply("❌ आपका डेटा पहले से ही डिलीटेड है।")

# Step 16: Monetization
@bot.on_message(filters.command("premium"))
async def premium_command(_, message):
    if message.from_user.id in premium_users:
        await message.reply("✅ आप पहले से ही प्रीमियम सदस्य हैं।")
    else:
        await message.reply("✨ **प्रीमियम सदस्यता प्राप्त करें!**\n\nप्रीमियम सुविधाओं को अनलॉक करने के लिए [यहाँ क्लिक करें](https://example.com/premium)।\n\nउदाहरण के लिए: विशेष टैग, तेज़ AI रिस्पांस, और बहुत कुछ!")

@bot.on_message(filters.command("add_premium") & filters.user(OWNER_ID))
async def add_premium(_, message):
    parts = message.text.split()
    if len(parts) < 2:
        return await message.reply("❌ कृपया यूज़र ID दें।")
    
    try:
        user_id = int(parts[1])
        premium_users.add(user_id)
        await message.reply(f"✅ यूज़र `{user_id}` को प्रीमियम में जोड़ा गया।")
    except (IndexError, ValueError):
        await message.reply("❌ कृपया एक मान्य यूज़र ID दें।")

# Step 17: Cross-Group Bridging
@bot.on_message(filters.group & filters.command("forward_message"))
async def forward_to_other_group(client, message):
    if not await deterministic_privilege_validator(message):
        return await message.reply("❌ आप यह कमांड इस्तेमाल नहीं कर सकते।")

    parts = message.text.split()
    if len(parts) < 2:
        return await message.reply("❌ सही इस्तेमाल: `/forward_message <group_id or username> <reply_to_message>`")

    target_chat = parts[1]
    if not message.reply_to_message:
        return await message.reply("❌ कृपया उस मैसेज पर रिप्लाई करें जिसे आप फॉरवर्ड करना चाहते हैं।")
    
    try:
        await message.reply_to_message.forward(target_chat)
        await message.reply("✅ मैसेज सफलतापूर्वक फॉरवर्ड कर दिया गया है।")
    except Exception as e:
        await message.reply(f"❌ मैसेज फॉरवर्ड करने में एक समस्या आई।\nError: {e}")

# --- New Features: Warning System & Scheduled Messages ---

@bot.on_message(filters.group & filters.command("warn"))
async def warn_user_command(client, message):
    if not await deterministic_privilege_validator(message):
        return await message.reply("❌ आप यह कमांड इस्तेमाल नहीं कर सकते।")

    target_user = await extract_target_user(message)
    if not target_user:
        return

    reason = "No reason provided."
    if len(message.command) > 1:
        reason = " ".join(message.command[1:])

    if target_user.id not in warn_counts:
        warn_counts[target_user.id] = 0
    
    warn_counts[target_user.id] += 1
    
    if warn_counts[target_user.id] >= 3:
        try:
            await client.ban_chat_member(message.chat.id, target_user.id)
            await message.reply(f"⚠️ **{target_user.first_name}** को 3 चेतावनियों के बाद बैन कर दिया गया है।")
            await log_admin_action("Ban after 3 warns", message.from_user.first_name, target_user.first_name)
            del warn_counts[target_user.id]
        except Exception as e:
            await message.reply(f"❌ यूज़र को बैन करने में एक समस्या आई।\nError: {e}")
    else:
        await message.reply(f"⚠️ **{target_user.first_name}** को चेतावनी दी गई है।\n**कारण:** {reason}\n**कुल चेतावनी:** {warn_counts[target_user.id]}/3")
        await log_admin_action("Warn", message.from_user.first_name, target_user.first_name)

@bot.on_message(filters.group & filters.command("resetwarns"))
async def reset_warns_command(client, message):
    if not await deterministic_privilege_validator(message):
        return await message.reply("❌ आप यह कमांड इस्तेमाल नहीं कर सकते।")
    
    target_user = await extract_target_user(message)
    if not target_user:
        return
    
    if target_user.id in warn_counts:
        del warn_counts[target_user.id]
        await message.reply("✅ यूज़र की चेतावनियाँ सफलतापूर्वक रीसेट कर दी गई हैं।")
    else:
        await message.reply("❌ इस यूज़र के पास कोई चेतावनी नहीं है।")

@bot.on_message(filters.group & filters.command("schedule"))
async def schedule_message_command(client, message):
    if not await deterministic_privilege_validator(message):
        return await message.reply("❌ आप यह कमांड इस्तेमाल नहीं कर सकते।")
        
    parts = message.text.split(" ", 2)
    if len(parts) < 3:
        return await message.reply("❌ सही इस्तेमाल: `/schedule <समय> <मैसेज>`\nउदाहरण: `/schedule 10m Hello everyone!`")
        
    time_str = parts[1]
    message_text = parts[2]
    
    try:
        time_value = int(time_str[:-1])
        time_unit = time_str[-1].lower()
    except (IndexError, ValueError):
        return await message.reply("❌ कृपया समय और मैसेज सही फ़ॉर्मेट में दें।")

    if time_unit == 's':
        delay = time_value
    elif time_unit == 'm':
        delay = time_value * 60
    elif time_unit == 'h':
        delay = time_value * 3600
    else:
        return await message.reply("❌ अमान्य समय इकाई। केवल 's' (सेकंड), 'm' (मिनट), 'h' (घंटे) का उपयोग करें।")

    send_time = datetime.now(timezone.utc) + timedelta(seconds=delay)
    
    scheduled_messages.append({
        "chat_id": message.chat.id,
        "text": message_text,
        "send_time": send_time.isoformat()
    })
    
    await message.reply(f"✅ आपका मैसेज **{send_time.strftime('%H:%M:%S')}** पर भेजने के लिए शेड्यूल हो गया है।")

# Background task to send scheduled messages
async def send_scheduled_messages():
    while True:
        messages_to_send = []
        now = datetime.now(timezone.utc)
        
        for msg in scheduled_messages:
            send_time = datetime.fromisoformat(msg["send_time"])
            if now >= send_time:
                messages_to_send.append(msg)
        
        for msg in messages_to_send:
            try:
                await bot.send_message(msg["chat_id"], msg["text"])
                scheduled_messages.remove(msg)
            except Exception as e:
                print(f"Failed to send scheduled message: {e}")
                scheduled_messages.remove(msg)
        
        await asyncio.sleep(5)

# --- NEW: AI-Powered Chat (Simple, Rule-based) ---
@bot.on_message(filters.group & filters.text & filters.regex(r"(?i)^(hello bot|namaste bot|hi bot)$"))
async def ai_hello(_, message):
    await message.reply("नमस्ते! मैं आपके सवाल का जवाब देने के लिए यहाँ हूँ। आप मुझसे कुछ भी पूछ सकते हैं।")

@bot.on_message(filters.group & filters.text & filters.regex(r"(?i)^(tum kon ho|what are you|who are you)$"))
async def ai_who_are_you(_, message):
    await message.reply(f"मैं **{BOT_NAME}** हूँ, आपके ग्रुप को मैनेज करने के लिए एक एडवांस बॉट।")

# --- NEW: Reputation System ---
@bot.on_message(filters.group & filters.command("rep"))
async def give_rep_command(_, message):
    if not message.reply_to_message:
        return await message.reply("❌ किसी यूज़र को +rep देने के लिए उसके मैसेज पर रिप्लाई करें।")
    
    target_user = message.reply_to_message.from_user
    sender_user = message.from_user

    if target_user.id == sender_user.id:
        return await message.reply("❌ आप खुद को +rep नहीं दे सकते।")

    if target_user.id not in user_reputation:
        user_reputation[target_user.id] = 0
    
    user_reputation[target_user.id] += 1
    
    await message.reply(f"✅ **{target_user.first_name}** को एक प्रतिष्ठा अंक (+1 rep) मिला। अब उनके पास {user_reputation[target_user.id]} अंक हैं।")

@bot.on_message(filters.group & filters.command("reps"))
async def show_reps_command(_, message):
    sorted_reps = sorted(user_reputation.items(), key=lambda item: item[1], reverse=True)
    
    reps_text = "✨ **Group Reputation Leaderboard**\n\n"
    if not sorted_reps:
        reps_text = "❌ अभी तक कोई प्रतिष्ठा अंक नहीं है।"
    else:
        for user_id, rep_count in sorted_reps[:5]:
            try:
                user = await bot.get_users(user_id)
                reps_text += f"**{user.first_name}**: {rep_count} अंक\n"
            except:
                pass

    await message.reply(reps_text)

# --- NEW: Ticket System ---
@bot.on_message(filters.private & filters.command("ticket"))
async def ticket_command(_, message):
    ticket_text = message.text.split(" ", 1)
    if len(ticket_text) < 2:
        return await message.reply("❌ कृपया अपना सवाल लिखें।\nसही इस्तेमाल: `/ticket आपका सवाल`")

    user_name = message.from_user.first_name
    ticket_message = f"🚨 **नया सपोर्ट टिकट!**\n\n**भेजने वाला:** {user_name} (`{message.from_user.id}`)\n\n**सवाल:**\n`{ticket_text[1]}`"
    
    await bot.send_message(OWNER_ID, ticket_message)
    await message.reply("✅ आपका टिकट सफलतापूर्वक भेज दिया गया है। जल्द ही आपसे संपर्क किया जाएगा।")

# --- NEW: Voice to Text ---
@bot.on_message(filters.group & filters.command("vtt"))
async def voice_to_text_command(client, message):
    if not message.reply_to_message or not message.reply_to_message.voice:
        return await message.reply("❌ कृपया एक वॉइस मैसेज पर रिप्लाई करें।")

    try:
        await message.reply("🔄 ट्रांसक्राइब किया जा रहा है... कृपया प्रतीक्षा करें।")
        voice_file_path = await message.reply_to_message.download()

        audio = AudioSegment.from_ogg(voice_file_path)
        audio.export("voice.wav", format="wav")

        recognizer = sr.Recognizer()
        with sr.AudioFile("voice.wav") as source:
            audio_data = recognizer.record(source)
            text = recognizer.recognize_google(audio_data, language="hi-IN")
            await message.reply(f"🎤 **टेक्स्ट:** `{text}`")

    except sr.UnknownValueError:
        await message.reply("❌ वॉइस को टेक्स्ट में बदल नहीं सका। कृपया स्पष्ट बोलें।")
    except Exception as e:
        await message.reply(f"❌ वॉइस को टेक्स्ट में बदलने में एक समस्या आई।\nError: {e}")
    finally:
        if os.path.exists(voice_file_path):
            os.remove(voice_file_path)
        if os.path.exists("voice.wav"):
            os.remove("voice.wav")

# --- NEW: Admin Broadcast ---
@bot.on_message(filters.user(OWNER_ID) & filters.command("broadcast"))
async def broadcast_message_command(client, message):
    broadcast_text = message.text.split(" ", 1)
    if len(broadcast_text) < 2:
        return await message.reply("❌ कृपया वह मैसेज दें जिसे आप ब्रॉडकास्ट करना चाहते हैं।")
    
    success_count = 0
    failure_count = 0
    
    async for dialog in client.get_dialogs():
        if dialog.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL]:
            try:
                await client.send_message(dialog.chat.id, broadcast_text[1])
                success_count += 1
            except Exception:
                failure_count += 1
    
    await message.reply(f"✅ मैसेज सफलतापूर्वक भेजा गया।\nसफलता: {success_count}\nविफलता: {failure_count}")

# --- NEW: Tag All ---
@bot.on_message(filters.group & filters.command("tagall"))
async def tag_all_command(client, message):
    if not await deterministic_privilege_validator(message):
        return await message.reply("❌ आप यह कमांड इस्तेमाल नहीं कर सकते।")

    members = []
    async for member in client.get_chat_members(message.chat.id):
        if not member.user.is_bot:
            members.append(f"[{member.user.first_name}](tg://user?id={member.user.id})")
    
    if not members:
        return await message.reply("❌ इस ग्रुप में टैग करने के लिए कोई सदस्य नहीं है।")

    text = " ".join(members)
    await message.reply(f"👥 सभी को टैग किया जा रहा है:\n\n{text}", disable_web_page_preview=True)

# --- NEW: Auto-Delete Message ---
@bot.on_message(filters.group & filters.command("autodelete"))
async def autodelete_message(client, message):
    if not await deterministic_privilege_validator(message):
        return await message.reply("❌ आप यह कमांड इस्तेमाल नहीं कर सकते।")
    
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].endswith(('m', 'h', 's')):
        return await message.reply("❌ सही इस्तेमाल: `/autodelete <time>`")
        
    time_str = parts[1]
    
    try:
        time_value = int(time_str[:-1])
        time_unit = time_str[-1].lower()
    except (IndexError, ValueError):
        return await message.reply("❌ कृपया समय सही फ़ॉर्मेट में दें।")
        
    if time_unit == 's':
        delay = time_value
    elif time_unit == 'm':
        delay = time_value * 60
    elif time_unit == 'h':
        delay = time_value * 3600
    else:
        return await message.reply("❌ अमान्य समय इकाई। केवल 's' (सेकंड), 'm' (मिनट), 'h' (घंटे) का उपयोग करें।")
    
    if delay > 3600:
        return await message.reply("❌ मैं 1 घंटे से ज़्यादा का ऑटो-डिलीट टाइमर नहीं लगा सकता।")

    sent_message = await message.reply(f"✅ यह मैसेज {time_value}{time_unit} के बाद अपने आप डिलीट हो जाएगा।")
    auto_delete_timers[sent_message.chat.id] = {
        "message_id": sent_message.id,
        "delete_time": datetime.now(timezone.utc) + timedelta(seconds=delay)
    }

async def check_auto_delete():
    while True:
        now = datetime.now(timezone.utc)
        messages_to_delete = []
        for chat_id, data in auto_delete_timers.items():
            if now >= data["delete_time"]:
                messages_to_delete.append((chat_id, data["message_id"]))
        
        for chat_id, message_id in messages_to_delete:
            try:
                await bot.delete_messages(chat_id, message_id)
                del auto_delete_timers[chat_id]
            except Exception as e:
                print(f"Failed to delete message: {e}")
        
        await asyncio.sleep(5)

# --- NEW: Advanced Polling ---
@bot.on_message(filters.group & filters.command("stoppoll"))
async def stop_poll_command(client, message):
    if not await deterministic_privilege_validator(message):
        return await message.reply("❌ आप यह कमांड इस्तेमाल नहीं कर सकते।")
    
    if not message.reply_to_message or not message.reply_to_message.poll:
        return await message.reply("❌ कृपया उस पोल पर रिप्लाई करें जिसे आप बंद करना चाहते हैं।")
    
    try:
        await client.stop_poll(message.chat.id, message.reply_to_message.id)
    except Exception as e:
        await message.reply(f"❌ पोल को बंद करने में एक समस्या आई।\nError: {e}")

# --- NEW: User Info ---
@bot.on_message(filters.group & filters.command("info"))
async def user_info_command(_, message):
    if not message.reply_to_message:
        return await message.reply("❌ कृपया एक यूज़र के मैसेज पर रिप्लाई करें।")
        
    user = message.reply_to_message.from_user
    
    user_info = f"👤 **यूज़र जानकारी**\n\n"
    user_info += f"**नाम:** {user.first_name}\n"
    if user.username:
        user_info += f"**यूज़रनेम:** @{user.username}\n"
    user_info += f"**आईडी:** `{user.id}`\n"
    user_info += f"**पर्मलिंक:** [Link](tg://user?id={user.id})\n"
    
    if user.id in user_stats:
        user_info += f"**कुल मैसेज:** {user_stats[user.id]['messages']}\n"
    if user.id in user_reputation:
        user_info += f"**प्रतिष्ठा:** {user_reputation[user.id]} अंक\n"
    if user.id in warn_counts:
        user_info += f"**चेतावनी:** {warn_counts[user.id]}/3\n"
    
    await message.reply(user_info, disable_web_page_preview=True)

# --- NEW: Chat Info ---
@bot.on_message(filters.group & filters.command("chatinfo"))
async def chat_info_command(client, message):
    chat = message.chat
    
    chat_info = f"ℹ️ **ग्रुप जानकारी**\n\n"
    chat_info += f"**नाम:** {chat.title}\n"
    if chat.username:
        chat_info += f"**यूज़रनेम:** @{chat.username}\n"
    chat_info += f"**आईडी:** `{chat.id}`\n"
    
    try:
        member_count = await client.get_chat_members_count(chat.id)
        chat_info += f"**सदस्य:** {member_count}\n"
    except Exception as e:
        print(f"Failed to get member count: {e}")
        
    await message.reply(chat_info, disable_web_page_preview=True)


if __name__ == "__main__":
    print("Bot started. Press Ctrl+C to stop.")
    asyncio.get_event_loop().create_task(send_scheduled_messages())
    asyncio.get_event_loop().create_task(check_auto_delete())
    bot.run()
