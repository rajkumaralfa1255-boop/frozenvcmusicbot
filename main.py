import os
import re
import sys
import time
import json
import logging
import asyncio
import random
import requests
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
OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY", None)

# Initialize the bot client
session_name = os.environ.get("SESSION_NAME", "help_bot")
bot = Client(session_name, bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# Define bot name for dynamic use
BOT_NAME = os.environ.get("BOT_NAME", "Frozen Help Bot")
BOT_LINK = os.environ.get("BOT_LINK", f"https://t.me/{bot.get_me().username}")

# In-memory storage for various data
user_stats = {}
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
link_whitelist = set()
restricted_file_types = set()

# Load data from file if it exists
def load_data():
    global notes_data, gban_list
    try:
        with open("bot_data.json", "r") as f:
            data = json.load(f)
            notes_data = data.get("notes_data", {})
            gban_list.update(data.get("gban_list", []))
            print("Data loaded successfully.")
    except (FileNotFoundError, json.JSONDecodeError):
        print("No existing data file found or file is empty.")

# Save data to file
def save_data():
    data = {
        "notes_data": notes_data,
        "gban_list": list(gban_list)
    }
    with open("bot_data.json", "w") as f:
        json.dump(data, f, indent=4)
    print("Data saved successfully.")

# Auto-mute on low messages settings
LOW_MSG_MUTE_THRESHOLD = 5
LOW_MSG_MUTE_TIME = 300 # 5 minutes

# Pre-defined game data
TRUTH_QUESTIONS = ["क्या आपने कभी अपने दोस्त को झूठ बोला है?", "आपकी सबसे अजीब आदत क्या है?", "आपकी सबसे बड़ी डर क्या है?", "आपने अपने जीवन में सबसे अजीब काम क्या किया है?"]
DARE_CHALLENGES = ["अपनी प्रोफ़ाइल फ़ोटो 1 घंटे के लिए बदलें।", "ग्रुप में एक जोक सुनाएं।", "1 मिनट तक अपनी नाक पर अपनी उंगली रखें।", "ग्रुप में एक अजीबोगरीब आवाज़ निकालें।"]
TRIVIA_QUESTIONS = {
    "भारत की राजधानी क्या है?": "दिल्ली",
    "सूर्य से सबसे निकटतम ग्रह कौन सा है?": "बुध",
    "राष्ट्रीय गान किसने लिखा था?": "रवींद्रनाथ टैगोर",
    "सबसे बड़ा महासागर कौन सा है?": "प्रशांत महासागर",
}
trivia_game = {}

# --- Helper functions ---
async def is_admin_or_owner(message: Message):
    if message.from_user.id == OWNER_ID:
        return True
    
    try:
        chat_member: ChatMember = await message._client.get_chat_member(
            chat_id=message.chat.id,
            user_id=message.from_user.id
        )
        return chat_member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
    except Exception:
        return False

def to_bold_unicode(text: str) -> str:
    bold_text = ""
    for char in text:
        if 'A' <= char <= 'Z':
            bold_text += chr(ord('𝗔') + (ord(char) - ord('A')))
        elif 'a' <= char <= 'z':
            bold_text += chr(ord('𝗮') + (ord(char) - ord('a')))
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
    except Exception:
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

@bot.on_message(filters.group & filters.command("setphotowelcome"))
async def set_photo_welcome(client, message):
    if not await is_admin_or_owner(message):
        return await message.reply("❌ आप यह कमांड इस्तेमाल नहीं कर सकते।")
    
    if not message.reply_to_message or not message.reply_to_message.photo:
        return await message.reply("❌ कृपया एक फ़ोटो पर रिप्लाई करें।")

    caption = message.text.split(" ", 1)
    if len(caption) < 2:
        return await message.reply("❌ कृपया फ़ोटो के साथ वेलकम टेक्स्ट भी दें।")

    custom_welcome_messages[message.chat.id] = {"photo": message.reply_to_message.photo.file_id, "caption": caption[1]}
    await message.reply("✅ फ़ोटो के साथ वेलकम मैसेज सेट हो गया है।")

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
    if message.from_user and message.from_user.id in gban_list:
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

    # Link filter with whitelist
    if message.text and re.search(r'(https?://\S+|t\.me/\S+)', message.text):
        is_whitelisted = False
        for domain in link_whitelist:
            if domain in message.text:
                is_whitelisted = True
                break
        
        if not is_whitelisted:
            try:
                await message.delete()
                await client.send_message(message.chat.id, f"❌ **{message.from_user.first_name}**, ग्रुप में लिंक भेजने की अनुमति नहीं है।")
            except Exception:
                pass
    
    # File type restriction
    if message.document:
        file_name, file_extension = os.path.splitext(message.document.file_name.lower())
        if file_extension in restricted_file_types:
            try:
                await message.delete()
                await client.send_message(message.chat.id, f"❌ **{message.from_user.first_name}**, इस प्रकार की फ़ाइलें भेजने की अनुमति नहीं है।")
            except Exception:
                pass

    if message.forward_from or message.forward_from_chat:
        try:
            await message.delete()
        except Exception:
            pass

    profanity_list = ["fuck", "bitch", "cunt", "chutiya", "randi"]
    if any(word in (message.text or '').lower() for word in profanity_list):
        try:
            await message.delete()
            await client.send_message(message.chat.id, f"❌ **{message.from_user.first_name}**, ग्रुप में ऐसी भाषा का प्रयोग न करें।")
        except Exception:
            pass

@bot.on_message(filters.group & filters.command("whitelist"))
async def add_whitelist_domain(client, message):
    if not await is_admin_or_owner(message):
        return await message.reply("❌ आप यह कमांड इस्तेमाल नहीं कर सकते।")
    
    parts = message.text.split()
    if len(parts) < 2:
        return await message.reply("❌ सही इस्तेमाल: `/whitelist <domain>`")
    
    domain = parts[1].replace('https://', '').replace('http://', '').strip('/')
    link_whitelist.add(domain)
    await message.reply(f"✅ `{domain}` को लिंक की अनुमति वाली लिस्ट में जोड़ा गया है।")

@bot.on_message(filters.group & filters.command("restrictfiletype"))
async def restrict_file_type(client, message):
    if not await is_admin_or_owner(message):
        return await message.reply("❌ आप यह कमांड इस्तेमाल नहीं कर सकते।")
    
    parts = message.text.split()
    if len(parts) < 2:
        return await message.reply("❌ सही इस्तेमाल: `/restrictfiletype <.ext>`")
        
    file_extension = parts[1].lower()
    if not file_extension.startswith('.'):
        file_extension = '.' + file_extension
        
    restricted_file_types.add(file_extension)
    await message.reply(f"✅ `{file_extension}` फ़ाइल टाइप को प्रतिबंधित कर दिया गया है।")

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

# --- New Games and Fun ---
@bot.on_message(filters.group & filters.command("truth"))
async def truth_game(_, message):
    question = random.choice(TRUTH_QUESTIONS)
    await message.reply(f"💡 **Truth**: {question}")

@bot.on_message(filters.group & filters.command("dare"))
async def dare_game(_, message):
    challenge = random.choice(DARE_CHALLENGES)
    await message.reply(f"🔥 **Dare**: {challenge}")

@bot.on_message(filters.group & filters.command("trivia"))
async def start_trivia(_, message):
    if message.chat.id in trivia_game:
        return await message.reply("❌ एक क्विज़ पहले से ही चल रही है।")
    
    question = random.choice(list(TRIVIA_QUESTIONS.keys()))
    trivia_game[message.chat.id] = {"question": question, "answer": TRIVIA_QUESTIONS[question]}
    
    await message.reply(f"🧠 **Trivia**: {question}\n\nआपका उत्तर इस पर रिप्लाई करके दें।")

@bot.on_message(filters.group & filters.text & filters.reply & filters.regex(r'^(?i)\S+'))
async def check_trivia_answer(_, message):
    if not message.reply_to_message or message.chat.id not in trivia_game:
        return
    
    if message.reply_to_message.from_user.id != bot.me.id:
        return
        
    if "Trivia" not in message.reply_to_message.text:
        return
        
    if message.text.lower() == trivia_game[message.chat.id]["answer"].lower():
        await message.reply(f"🎉 **सही जवाब!** **{message.from_user.first_name}** ने सही जवाब दिया।")
        del trivia_game[message.chat.id]
    else:
        await message.reply("❌ **गलत जवाब।** फिर से कोशिश करें।")

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

@bot.on_message(filters.group & filters.command("getfile"))
async def get_file_from_sticker(client, message):
    if not message.reply_to_message or not message.reply_to_message.sticker:
        return await message.reply("❌ कृपया एक स्टिकर पर रिप्लाई करें।")

    try:
        file_path = await message.reply_to_message.download()
        await message.reply_document(document=file_path)
        os.remove(file_path)
    except Exception as e:
        await message.reply(f"❌ स्टिकर को फ़ाइल में बदलने में समस्या आई।\nError: {e}")

@bot.on_message(filters.group & filters.command("gadminbroadcast") & filters.user(OWNER_ID))
async def group_admin_broadcast(client, message):
    broadcast_text = message.text.split(" ", 1)
    if len(broadcast_text) < 2:
        return await message.reply("❌ कृपया वह मैसेज दें जिसे आप ब्रॉडकास्ट करना चाहते हैं।")
    
    success_count = 0
    failure_count = 0
    
    async for dialog in client.get_dialogs():
        if dialog.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL]:
            try:
                chat_member = await client.get_chat_member(dialog.chat.id, bot.me.id)
                if chat_member.status == ChatMemberStatus.ADMINISTRATOR:
                    await client.send_message(dialog.chat.id, broadcast_text[1])
                    success_count += 1
            except Exception:
                failure_count += 1
    
    await message.reply(f"✅ मैसेज सफलतापूर्वक भेजा गया।\nसफलता: {success_count}\nविफलता: {failure_count}")

@bot.on_message(filters.group & filters.command("backup") & filters.user(OWNER_ID))
async def backup_data(_, message):
    save_data()
    await message.reply("✅ डेटा का बैकअप सफलतापूर्वक ले लिया गया है।")

@bot.on_message(filters.group & filters.command("restore") & filters.user(OWNER_ID))
async def restore_data(_, message):
    load_data()
    await message.reply("✅ डेटा सफलतापूर्वक रीस्टोर कर दिया गया है।")

@bot.on_message(filters.group & filters.command("settitle"))
async def set_group_title(client, message):
    if not await is_admin_or_owner(message):
        return await message.reply("❌ आप यह कमांड इस्तेमाल नहीं कर सकते।")
    
    new_title = message.text.split(" ", 1)
    if len(new_title) < 2:
        return await message.reply("❌ कृपया एक नया शीर्षक दें।")

    try:
        await client.set_chat_title(message.chat.id, new_title[1])
        await message.reply("✅ ग्रुप का शीर्षक बदल दिया गया है।")
    except Exception as e:
        await message.reply(f"❌ शीर्षक बदलने में एक समस्या आई।\nError: {e}")

@bot.on_message(filters.group & filters.command("setphoto"))
async def set_group_photo(client, message):
    if not await is_admin_or_owner(message):
        return await message.reply("❌ आप यह कमांड इस्तेमाल नहीं कर सकते।")
    
    if not message.reply_to_message or not message.reply_to_message.photo:
        return await message.reply("❌ कृपया एक फ़ोटो पर रिप्लाई करें।")

    try:
        photo_path = await message.reply_to_message.download()
        await client.set_chat_photo(message.chat.id, photo=photo_path)
        await message.reply("✅ ग्रुप की फ़ोटो बदल दी गई है।")
        os.remove(photo_path)
    except Exception as e:
        await message.reply(f"❌ फ़ोटो बदलने में एक समस्या आई।\nError: {e}")

# The bot will now start and load the data.
if __name__ == "__main__":
    load_data()
    print("Bot started. Press Ctrl+C to stop.")
    bot.run()
