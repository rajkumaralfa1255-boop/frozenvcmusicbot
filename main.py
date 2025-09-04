import os
import re
import sys
import time
import json
import logging
import asyncio
import random
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
from gtts import gTTS

# Load environment variables
load_dotenv()

API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "5268762773"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", None))
RSS_FEED_URL = os.environ.get("RSS_FEED_URL", "https://www.youtube.com/feeds/videos.xml?channel_id=UC-K20bY-dK_9e17W3K-252A")
RSS_CHANNEL_ID = int(os.getenv("RSS_CHANNEL_ID", None))

# Initialize the bot client
session_name = os.environ.get("SESSION_NAME", "help_bot")
bot = Client(session_name, bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# Define bot name for dynamic use
BOT_NAME = os.environ.get("BOT_NAME", "Frozen Help Bot")
BOT_LINK = os.environ.get("BOT_LINK", f"https://t.me/{bot.get_me().username}")

# In-memory storage for user stats, notes, and other data
user_stats = {}
last_rss_entry_link = ""
premium_users = set()
FAQ_DATA = {
    "rules": "ग्रुप के नियम:\n1. कोई स्पैमिंग नहीं\n2. कोई गाली-गलौज नहीं\n3. केवल ग्रुप से संबंधित बातें।",
    "help": "मैं आपकी मदद कैसे कर सकता हूँ? `/help` कमांड का प्रयोग करें या नीचे दिए गए बटन पर क्लिक करें।",
    "contact": "एडमिन से संपर्क करने के लिए @Frozensupport1 पर मैसेज करें।",
}

warn_counts = {}
user_message_timestamps = {}
scheduled_messages = []
user_reputation = {}
auto_delete_timers = {}
notes_data = {}
gban_list = set()
custom_welcome_messages = {}

# Auto-mute on low messages settings
LOW_MSG_MUTE_THRESHOLD = 5
LOW_MSG_MUTE_TIME = 300 # 5 minutes

# Pre-defined quotes list
QUOTES = [
    "सपने वो नहीं होते जो हम सोते हुए देखते हैं, सपने वो होते हैं जो हमें सोने नहीं देते। - अब्दुल कलाम",
    "जो लोग खुद से प्यार करते हैं, वे दुनिया को बदलने की शक्ति रखते हैं। - महात्मा गांधी",
    "सफलता की खुशी का अनुभव करने से पहले, इंसान को असफलता का अनुभव करना चाहिए। - डॉ. ए.पी.जे. अब्दुल कलाम",
    "अगर तुम सूरज की तरह चमकना चाहते हो, तो पहले सूरज की तरह जलना सीखो। - डॉ. ए.पी.जे. अब्दुल कलाम",
    "कर्मभूमि पर फल के लिए श्रम सबको करना पड़ता है, भगवान सिर्फ लकीरें देता है, रंग हमें खुद भरना पड़ता है। - अज्ञात",
]

# --- Helper functions ---

# Recreating the privilege validator function
async def is_admin_or_owner(message: Message):
    if message.from_user.id == OWNER_ID:
        return True
    
    chat_member: ChatMember = await message._client.get_chat_member(
        chat_id=message.chat.id,
        user_id=message.from_user.id
    )
    return chat_member.status in [
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER
    ]

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

async def log_admin_action(action: str, admin: str, target: str):
    if LOG_CHANNEL_ID:
        log_message = f"🛡️ **एडमिन लॉग**\n\n**कार्य:** {action}\n**एडमिन:** {admin}\n**लक्ष्य:** {target}"
        try:
            await bot.send_message(LOG_CHANNEL_ID, log_message)
        except Exception as e:
            print(f"Failed to send log to channel: {e}")

# --- New Enhanced UI for Start/Help ---
@bot.on_message(filters.command(["start", "help"]))
async def start_and_help_handler(_, message):
    user_id = message.from_user.id
    raw_name = message.from_user.first_name or ""
    styled_name = to_bold_unicode(raw_name)

    caption = (
        f"👋 **नमस्ते {styled_name}!**\n\n"
        f"मैं एक एडवांस ग्रुप मैनेजमेंट असिस्टेंट हूँ।\n"
        f"मैं आपके ग्रुप को साफ, सुरक्षित और व्यवस्थित रखने में मदद करता हूँ।\n\n"
        f"मेरे सभी फ़ीचर्स को एक्सप्लोर करने के लिए, नीचे दिए गए **Help** बटन पर क्लिक करें।\n\n"
        f"**Developer:** [Shubham](tg://user?id={OWNER_ID})"
    )
    buttons = [
        [
            InlineKeyboardButton("➕ मुझे ग्रुप में जोड़ें", url=f"https://t.me/{bot.get_me().username}?startgroup=true"),
            InlineKeyboardButton("📢 अपडेट्स", url="https://t.me/vibeshiftbots")
        ],
        [
            InlineKeyboardButton("❓ Help", callback_data="show_help"),
            InlineKeyboardButton("💬 सपोर्ट", url="https://t.me/Frozensupport1")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(buttons)
    await message.reply_animation(
        animation="https://frozen-imageapi.lagendplayersyt.workers.dev/file/2e483e17-05cb-45e2-b166-1ea476ce9521.mp4",
        caption=caption,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )

@bot.on_callback_query(filters.regex("show_help"))
async def show_help_callback(_, callback_query):
    text = "**📚 कमांड्स का मेनू**\n\nनीचे दिए गए बटन्स से आप कमांड्स को कैटेगरी के अनुसार देख सकते हैं।"
    buttons = [
        [InlineKeyboardButton("🛡️ एडमिन कमांड्स", callback_data="help_admin"),
         InlineKeyboardButton("🚀 यूटिलिटी कमांड्स", callback_data="help_utility")],
        [InlineKeyboardButton("😄 मनोरंजन कमांड्स", callback_data="help_fun"),
         InlineKeyboardButton("ℹ️ जानकारी कमांड्स", callback_data="help_info")],
        [InlineKeyboardButton("🏠 मुख्य पेज पर वापस", callback_data="go_back")]
    ]
    reply_markup = InlineKeyboardMarkup(buttons)
    await callback_query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)

@bot.on_callback_query(filters.regex("help_admin"))
async def help_admin_callback(_, callback_query):
    text = (
        "🛡️ **एडमिन और मॉडरेसन कमांड्स**\n\n"
        "`/mute <reply> or <username>`: सदस्य को हमेशा के लिए म्यूट करें।\n"
        "`/tmute <reply> <time>`: सदस्य को कुछ देर के लिए म्यूट करें।\n"
        "`/unmute <reply>`: सदस्य को अनम्यूट करें।\n"
        "`/ban <reply>`: सदस्य को ग्रुप से बैन करें।\n"
        "`/unban <reply>`: सदस्य को अनबैन करें।\n"
        "`/kick <reply>`: सदस्य को ग्रुप से किक करें।\n"
        "`/warn <reply>`: सदस्य को चेतावनी दें (3 चेतावनियों के बाद बैन)।\n"
        "`/resetwarns <reply>`: सदस्य की चेतावनियों को रीसेट करें।\n"
        "`/del`: मैसेज डिलीट करें।\n"
        "`/setwelcome`: कस्टम वेलकम मैसेज सेट करें।\n"
        "`/autodelete <time>`: मैसेज को ऑटो-डिलीट करें।"
    )
    buttons = [[InlineKeyboardButton("🔙 वापस", callback_data="show_help")]]
    reply_markup = InlineKeyboardMarkup(buttons)
    await callback_query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)

@bot.on_callback_query(filters.regex("help_utility"))
async def help_utility_callback(_, callback_query):
    text = (
        "🚀 **यूटिलिटी कमांड्स**\n\n"
        "`/tts <text>`: टेक्स्ट को ऑडियो में बदलें।\n"
        "`/vtt`: वॉइस मैसेज को टेक्स्ट में बदलें।\n"
        "`/schedule <time> <text>`: मैसेज को शेड्यूल करें।\n"
        "`/broadcast <message>`: सभी ग्रुप्स में ब्रॉडकास्ट करें (केवल मालिक)।\n"
        "`/gban <reply>`: सदस्य को बॉट के सभी ग्रुप्स से बैन करें (केवल मालिक)।\n"
        "`/ungban <reply>`: सदस्य को बॉट के सभी ग्रुप्स से अनबैन करें (केवल मालिक)।"
    )
    buttons = [[InlineKeyboardButton("🔙 वापस", callback_data="show_help")]]
    reply_markup = InlineKeyboardMarkup(buttons)
    await callback_query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)

@bot.on_callback_query(filters.regex("help_fun"))
async def help_fun_callback(_, callback_query):
    text = (
        "😄 **मनोरंजन कमांड्स**\n\n"
        "`/poll <question> <options>`: एक पोल बनाएं।\n"
        "`/couple`: ग्रुप का 'Couple of the Day' चुनें।\n"
        "`/dice`: एक डाइस रोल करें।\n"
        "`/rep <reply>`: किसी सदस्य की प्रतिष्ठा बढ़ाएं।\n"
        "`/reps`: सबसे ज़्यादा प्रतिष्ठा वाले सदस्यों को देखें।\n"
        "`/quote`: एक प्रेरणादायक कोट पाएं।"
    )
    buttons = [[InlineKeyboardButton("🔙 वापस", callback_data="show_help")]]
    reply_markup = InlineKeyboardMarkup(buttons)
    await callback_query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)

@bot.on_callback_query(filters.regex("help_info"))
async def help_info_callback(_, callback_query):
    text = (
        "ℹ️ **जानकारी कमांड्स**\n\n"
        "`/stats`: ग्रुप के टॉप मैसेज सेंडर्स देखें।\n"
        "`/admins`: ग्रुप के सभी एडमिन को लिस्ट करें।\n"
        "`/info <reply>`: किसी सदस्य के बारे में जानकारी पाएं।\n"
        "`/chatinfo`: ग्रुप के बारे में जानकारी पाएं।\n"
        "`/ping`: बॉट की गति (speed) को चेक करें।"
    )
    buttons = [[InlineKeyboardButton("🔙 वापस", callback_data="show_help")]]
    reply_markup = InlineKeyboardMarkup(buttons)
    await callback_query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)

@bot.on_callback_query(filters.regex("go_back"))
async def go_back_callback(_, callback_query):
    user_id = callback_query.from_user.id
    raw_name = callback_query.from_user.first_name or ""
    styled_name = to_bold_unicode(raw_name)

    caption = (
        f"👋 **नमस्ते {styled_name}!**\n\n"
        f"मैं एक एडवांस ग्रुप मैनेजमेंट असिस्टेंट हूँ।\n"
        f"मैं आपके ग्रुप को साफ, सुरक्षित और व्यवस्थित रखने में मदद करता हूँ।\n\n"
        f"मेरे सभी फ़ीचर्स को एक्सप्लोर करने के लिए, नीचे दिए गए **Help** बटन पर क्लिक करें।\n\n"
        f"**Developer:** [Shubham](tg://user?id={OWNER_ID})"
    )
    buttons = [
        [
            InlineKeyboardButton("➕ मुझे ग्रुप में जोड़ें", url=f"https://t.me/{bot.get_me().username}?startgroup=true"),
            InlineKeyboardButton("📢 अपडेट्स", url="https://t.me/vibeshiftbots")
        ],
        [
            InlineKeyboardButton("❓ Help", callback_data="show_help"),
            InlineKeyboardButton("💬 सपोर्ट", url="https://t.me/Frozensupport1")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(buttons)
    await callback_query.message.edit_caption(
        caption=caption,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )
# --- Welcome/Onboarding Feature ---
@bot.on_message(filters.new_chat_members)
async def welcome_new_member(client, message):
    chat_id = message.chat.id
    for member in message.new_chat_members:
        if member.is_bot:
            continue
        
        if chat_id in custom_welcome_messages:
            welcome_text = custom_welcome_messages[chat_id]
        else:
            welcome_text = (
                f"👋 **{member.first_name}** का स्वागत है! 🎉\n\n"
                "कृपया ग्रुप के इन नियमों का पालन करें:\n"
                "1. कोई स्पैमिंग नहीं\n"
                "2. कोई गाली-गलौज नहीं\n"
                "3. केवल ग्रुप से संबंधित बातें करें।"
            )
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ मैंने नियम पढ़ लिए हैं", callback_data="rules_accepted")]
        ])
        await client.send_message(chat_id, welcome_text, reply_markup=keyboard)

@bot.on_message(filters.group & filters.command("setwelcome"))
async def set_welcome_message(client, message):
    if not await is_admin_or_owner(message):
        return await message.reply("❌ आप यह कमांड इस्तेमाल नहीं कर सकते।")
    
    parts = message.text.split(" ", 1)
    if len(parts) < 2:
        return await message.reply("❌ कृपया वह मैसेज दें जिसे आप वेलकम मैसेज बनाना चाहते हैं।")
    
    custom_welcome_messages[message.chat.id] = parts[1]
    await message.reply("✅ कस्टम वेलकम मैसेज सेट हो गया है।")

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
    if not await is_admin_or_owner(message):
        return await message.reply("❌ आप यह कमांड इस्तेमाल नहीं कर सकते, क्योंकि आप एडमिन नहीं हैं।")
    
    target_user = await extract_target_user(message)
    if not target_user:
        return
    try:
        await client.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target_user.id,
            permissions=ChatPermissions(can_send_messages=False)
        )
        await message.reply(f"🔇 यूज़र को सफलतापूर्वक म्यूट कर दिया गया है।")
        await log_admin_action("Mute", message.from_user.first_name, target_user.first_name)
    except Exception as e:
        await message.reply(f"❌ यूज़र को म्यूट करने में एक समस्या आई।\nError: {e}")

@bot.on_message(filters.group & filters.command("unmute"))
async def unmute_user(client, message):
    if not await is_admin_or_owner(message):
        return await message.reply("❌ आप यह कमांड इस्तेमाल नहीं कर सकते, क्योंकि आप एडमिन नहीं हैं।")
        
    target_user = await extract_target_user(message)
    if not target_user:
        return
    try:
        await client.unban_chat_member(chat_id=message.chat.id, user_id=target_user.id)
        await message.reply(f"🔊 यूज़र को सफलतापूर्वक अनम्यूट कर दिया गया है।")
        await log_admin_action("Unmute", message.from_user.first_name, target_user.first_name)
    except Exception as e:
        await message.reply(f"❌ यूज़र को अनम्यूट करने में एक समस्या आई।\nError: {e}")

@bot.on_message(filters.group & filters.command("tmute"))
async def tmute_user(client, message):
    if not await is_admin_or_owner(message):
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
    if not await is_admin_or_owner(message):
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
    if not await is_admin_or_owner(message):
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
    if not await is_admin_or_owner(message):
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

@bot.on_message(filters.group & filters.command("del"))
async def delete_message(client, message):
    if not await is_admin_or_owner(message):
        return await message.reply("❌ आप यह कमांड इस्तेमाल नहीं कर सकते।")
    
    if not message.reply_to_message:
        return await message.reply("❌ कृपया उस मैसेज पर रिप्लाई करें जिसे आप डिलीट करना चाहते हैं।")
    
    try:
        await client.delete_messages(message.chat.id, [message.reply_to_message.id, message.id])
    except Exception as e:
        await message.reply(f"❌ मैसेज डिलीट करने में एक समस्या आई।\nError: {e}")

# --- Anti-Abuse & Security ---
@bot.on_message(filters.group & ~filters.me & ~filters.via_bot)
async def anti_abuse_filter(client, message):
    if message.from_user.id in gban_list:
        try:
            await client.ban_chat_member(message.chat.id, message.from_user.id)
            return
        except Exception:
            pass

    user_status = (await client.get_chat_member(message.chat.id, message.from_user.id)).status
    if user_status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
        return

    # Auto-mute for low message count
    user_id = message.from_user.id
    if user_id not in user_stats or user_stats[user_id].get('messages', 0) < LOW_MSG_MUTE_THRESHOLD:
        try:
            await client.restrict_chat_member(
                chat_id=message.chat.id,
                user_id=user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=datetime.now(timezone.utc) + timedelta(seconds=LOW_MSG_MUTE_TIME)
            )
            await message.reply(f"🔇 **{message.from_user.first_name}** को कुछ देर के लिए म्यूट कर दिया गया है, क्योंकि आपके मैसेज बहुत कम हैं। ग्रुप में भाग लेने के लिए और मैसेज भेजें।")
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

# --- Automations & Workflows ---
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

# --- New Features ---
@bot.on_message(filters.group & filters.command("quote"))
async def send_random_quote(_, message):
    quote = random.choice(QUOTES)
    await message.reply(f"💬 **Quote of the Day**\n\n{quote}")

@bot.on_message(filters.group & filters.command("gban") & filters.user(OWNER_ID))
async def global_ban(client, message):
    target_user = await extract_target_user(message)
    if not target_user:
        return
    
    gban_list.add(target_user.id)
    await message.reply(f"🚫 **{target_user.first_name}** को सभी ग्रुप्स से बैन कर दिया गया है।")

@bot.on_message(filters.group & filters.command("ungban") & filters.user(OWNER_ID))
async def global_unban(client, message):
    target_user = await extract_target_user(message)
    if not target_user:
        return
    
    if target_user.id in gban_list:
        gban_list.remove(target_user.id)
        await message.reply(f"✅ **{target_user.first_name}** को ग्लोबल बैन से हटा दिया गया है।")
    else:
        await message.reply("❌ यह सदस्य ग्लोबल बैन लिस्ट में नहीं है।")

@bot.on_message(filters.group & filters.command("save"))
async def save_note(_, message):
    parts = message.text.split(" ", 2)
    if len(parts) < 3:
        return await message.reply("❌ सही इस्तेमाल: `/save <note_name> <text>`")
    
    note_name = parts[1].lower()
    note_text = parts[2]
    
    if note_name in notes_data:
        return await message.reply("❌ इस नाम का नोट पहले से मौजूद है।")
        
    notes_data[note_name] = note_text
    await message.reply(f"✅ नोट `{note_name}` सफलतापूर्वक सेव हो गया है।")

@bot.on_message(filters.group & filters.command("notes"))
async def get_notes(_, message):
    if not notes_data:
        return await message.reply("❌ अभी तक कोई नोट सेव नहीं हुआ है।")
    
    notes_list = "\n".join(notes_data.keys())
    await message.reply(f"📝 **उपलब्ध नोट्स:**\n\n`{notes_list}`")

@bot.on_message(filters.group & filters.command("getnotes"))
async def get_note(_, message):
    parts = message.text.split(" ", 1)
    if len(parts) < 2:
        return await message.reply("❌ सही इस्तेमाल: `/getnotes <note_name>`")
    
    note_name = parts[1].lower()
    
    if note_name not in notes_data:
        return await message.reply("❌ इस नाम का कोई नोट नहीं मिला।")
        
    await message.reply(notes_data[note_name])

@bot.on_message(filters.group & filters.command("deletenote"))
async def delete_note(client, message):
    if not await is_admin_or_owner(message):
        return await message.reply("❌ आप यह कमांड इस्तेमाल नहीं कर सकते।")
    
    parts = message.text.split(" ", 1)
    if len(parts) < 2:
        return await message.reply("❌ सही इस्तेमाल: `/deletenote <note_name>`")
    
    note_name = parts[1].lower()
    
    if note_name not in notes_data:
        return await message.reply("❌ इस नाम का कोई नोट नहीं मिला।")
        
    del notes_data[note_name]
    await message.reply(f"✅ नोट `{note_name}` सफलतापूर्वक डिलीट हो गया है।")

@bot.on_message(filters.group & filters.command("admins"))
async def list_admins(client, message):
    admins = []
    async for member in client.get_chat_members(message.chat.id, filter="administrators"):
        admins.append(f"**{member.user.first_name}** (`{member.user.id}`)")
        
    if not admins:
        return await message.reply("❌ इस ग्रुप में कोई एडमिन नहीं है।")
        
    admins_text = "🛡️ **ग्रुप एडमिन:**\n\n" + "\n".join(admins)
    await message.reply(admins_text)

@bot.on_message(filters.group & filters.command("ping"))
async def ping_command(_, message):
    start_time = time.time()
    await message.reply(" pong!")
    end_time = time.time()
    latency = round((end_time - start_time) * 1000)
    await message.edit_text(f"🚀 **Pong!**\n`{latency}ms`")

# --- All other features (already implemented) ---
# ... (all other commands from the previous update go here)
# Since the code block is too large, I'm showing a placeholder. You should paste all the previous code here.

# --- Voice to Text (VTT) ---
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

# ... (All other command handlers like poll, remindme, couple, etc.)

if __name__ == "__main__":
    print("Bot started. Press Ctrl+C to stop.")
    asyncio.get_event_loop().create_task(send_scheduled_messages())
    asyncio.get_event_loop().create_task(check_auto_delete())
    bot.run()
