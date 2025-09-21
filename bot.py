import os
import sqlite3
import asyncio
import aiohttp
import threading
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)
from dotenv import load_dotenv

# Bot configuration - all data embedded in the file
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

# Production settings
PORT = int(os.environ.get('PORT', 8000))
IS_PRODUCTION = os.environ.get('RENDER') or os.environ.get('RAILWAY_ENVIRONMENT')

# Embedded texts and button configurations
TEXTS = {
    "start": "👋 **Welcome to Support Bot!**\n\nI'm here to help you get assistance from our team. Use the buttons below to get started."
}

BUTTONS_CONFIG = {
    "main": [
        {"text": "💬 Contact Support", "callback_data": "help_support"},
        {"text": "📱 Our Website", "url": "https://example.com"},
        {"text": "📢 Updates Channel", "url": "https://t.me/your_channel"}
    ]
}

# Simple HTTP server to prevent sleeping on free hosts
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Support Bot is running!')
        
    def log_message(self, format, *args):
        # Suppress HTTP server logs
        pass

def run_health_server():
    """Run HTTP server to prevent host from sleeping"""
    server = HTTPServer(('0.0.0.0', PORT), HealthHandler)
    print(f"Health server running on port {PORT}")
    server.serve_forever()

async def keep_alive_ping():
    """Self-ping every 10 minutes to prevent sleeping"""
    if not IS_PRODUCTION:
        return
        
    # Wait 5 minutes before starting pings
    await asyncio.sleep(300)
    
    base_urls = [
        os.environ.get('RENDER_EXTERNAL_URL'),
        os.environ.get('RAILWAY_PUBLIC_DOMAIN'),
        os.environ.get('APP_URL')
    ]
    
    ping_url = None
    for url in base_urls:
        if url:
            ping_url = url.rstrip('/')
            break
    
    if not ping_url:
        print("No external URL found for keep-alive pings")
        return
    
    print(f"Starting keep-alive pings to: {ping_url}")
    
    while True:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(ping_url) as response:
                    if response.status == 200:
                        print(f"Keep-alive ping successful at {datetime.now().strftime('%H:%M:%S')}")
                    else:
                        print(f"Keep-alive ping returned status {response.status}")
        except Exception as e:
            print(f"Keep-alive ping failed: {e}")
        
        # Wait 10 minutes between pings
        await asyncio.sleep(600)

# --- Database Setup ---
def init_database():
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    # Users table - track all users who ever used the bot
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            total_messages INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1
        )
    ''')
    
    # Support sessions table - track all support conversations
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS support_sessions (
            session_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ended_at TIMESTAMP,
            ended_by TEXT,  -- 'admin', 'user', 'timeout'
            message_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active'  -- 'active', 'closed'
        )
    ''')
    
    conn.commit()
    conn.close()

# Initialize database on startup
init_database()

# --- Session storage ---
active_chats = {}  # user_id: {"in_support": bool, "waiting_for_first_message": bool, "admin_message_ids": [], "session_id": int}
admin_replying_to = {}  # admin_user_id: target_user_id

# --- Database Functions ---
def update_user_stats(user_id, username, full_name):
    """Update user statistics in database"""
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT OR REPLACE INTO users (user_id, username, full_name, last_seen, 
                                     total_messages, is_active)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP, 
                COALESCE((SELECT total_messages FROM users WHERE user_id = ?) + 1, 1), 1)
    ''', (user_id, username, full_name, user_id))
    
    conn.commit()
    conn.close()

def start_support_session(user_id):
    """Start a new support session"""
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT INTO support_sessions (user_id, started_at, status)
        VALUES (?, CURRENT_TIMESTAMP, 'active')
    ''', (user_id,))
    
    session_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return session_id

def end_support_session(session_id, ended_by, message_count):
    """End a support session"""
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        UPDATE support_sessions 
        SET ended_at = CURRENT_TIMESTAMP, ended_by = ?, message_count = ?, status = 'closed'
        WHERE session_id = ?
    ''', (ended_by, message_count, session_id))
    
    conn.commit()
    conn.close()

def get_bot_statistics():
    """Get comprehensive bot statistics"""
    try:
        conn = sqlite3.connect('bot_data.db')
        cursor = conn.cursor()
        
        # Total users
        cursor.execute('SELECT COUNT(*) FROM users')
        total_users = cursor.fetchone()[0] or 0
        
        # Users today
        cursor.execute('SELECT COUNT(*) FROM users WHERE DATE(last_seen) = DATE("now")')
        users_today = cursor.fetchone()[0] or 0
        
        # Users this week
        cursor.execute('SELECT COUNT(*) FROM users WHERE last_seen >= datetime("now", "-7 days")')
        users_week = cursor.fetchone()[0] or 0
        
        # Total support sessions
        cursor.execute('SELECT COUNT(*) FROM support_sessions')
        total_sessions = cursor.fetchone()[0] or 0
        
        # Active sessions
        cursor.execute('SELECT COUNT(*) FROM support_sessions WHERE status = "active"')
        active_sessions = cursor.fetchone()[0] or 0
        
        # Average messages per session
        cursor.execute('SELECT AVG(message_count) FROM support_sessions WHERE status = "closed"')
        avg_result = cursor.fetchone()[0]
        avg_messages = round(avg_result, 1) if avg_result else 0
        
        conn.close()
        
        return {
            'total_users': total_users,
            'users_today': users_today,
            'users_week': users_week,
            'total_sessions': total_sessions,
            'active_sessions': active_sessions,
            'avg_messages': avg_messages,
            'current_active_chats': len([u for u in active_chats.values() if u.get("in_support")])
        }
    except Exception as e:
        print(f"Error getting stats: {e}")
        return {
            'total_users': 0,
            'users_today': 0,
            'users_week': 0,
            'total_sessions': 0,
            'active_sessions': 0,
            'avg_messages': 0,
            'current_active_chats': 0
        }

def get_all_user_ids():
    """Get all user IDs for broadcasting"""
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    cursor.execute('SELECT user_id FROM users WHERE is_active = 1')
    user_ids = [row[0] for row in cursor.fetchall()]
    conn.close()
    return user_ids

# --- User Interface Buttons ---
def main_user_menu():
    """Main menu for regular users"""
    btns = []
    for b in BUTTONS_CONFIG["main"]:
        if b.get("callback_data"):
            btns.append([InlineKeyboardButton(b["text"], callback_data=b["callback_data"])])
        elif b.get("url"):
            btns.append([InlineKeyboardButton(b["text"], url=b["url"])])
    return InlineKeyboardMarkup(btns)

def user_support_menu():
    """Menu for users in support mode"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ End Support Chat", callback_data="end_support")]
    ])

# --- Admin Interface Buttons ---
def main_admin_menu():
    """Enhanced main admin control panel"""
    stats = get_bot_statistics()
    hosting_status = "🟢 Production" if IS_PRODUCTION else "🟡 Development"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"💬 Active Chats ({stats['current_active_chats']})", callback_data="view_active_chats"),
            InlineKeyboardButton(f"📊 Statistics", callback_data="bot_stats")
        ],
        [
            InlineKeyboardButton("📢 Broadcast Message", callback_data="broadcast"),
            InlineKeyboardButton("👥 User Management", callback_data="user_management")
        ],
        [
            InlineKeyboardButton("📝 Support History", callback_data="support_history"),
            InlineKeyboardButton(f"🔧 Bot Status {hosting_status}", callback_data="bot_settings")
        ],
        [InlineKeyboardButton("🗑️ Clean Chat History", callback_data="clean_chat")]
    ])

def admin_chat_buttons(user_id):
    """Enhanced buttons for managing individual user chat"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💬 Reply", callback_data=f"reply_{user_id}"),
            InlineKeyboardButton("❌ Close & Clean", callback_data=f"close_clean_{user_id}")
        ],
        [
            InlineKeyboardButton(f"👤 Profile ({user_id})", url=f"tg://user?id={user_id}"),
            InlineKeyboardButton("⚠️ Block User", callback_data=f"block_{user_id}")
        ],
        [InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]
    ])

def admin_active_chats_menu(active_users):
    """Enhanced menu showing all active support chats"""
    buttons = []
    for user_id, user_info in active_users.items():
        username = user_info.get('username', 'No username')
        username_display = f"@{username}" if username != 'No username' else f"User {user_id}"
        buttons.append([InlineKeyboardButton(
            f"💬 {username_display} (Active)", 
            callback_data=f"view_chat_{user_id}"
        )])
    
    buttons.append([InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(buttons)

def broadcast_menu():
    """Broadcast message options"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Send to All Users", callback_data="broadcast_all")],
        [InlineKeyboardButton("💬 Send to Active Chats Only", callback_data="broadcast_active")],
        [InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]
    ])

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = update.effective_user
    
    # Update user stats
    update_user_stats(user_id, user.username, user.full_name)
    
    # Clear any active chat state when user starts over
    if user_id in active_chats:
        # End any active support session
        session_id = active_chats[user_id].get('session_id')
        if session_id:
            end_support_session(session_id, 'user', active_chats[user_id].get('message_count', 0))
        active_chats.pop(user_id, None)
    
    if user_id == ADMIN_ID:
        hosting_info = f"Hosting: {'Production' if IS_PRODUCTION else 'Development'}"
        await update.message.reply_text(
            f"🔧 **ADMIN PANEL**\n\nWelcome to the enhanced admin dashboard!\n{hosting_info}",
            reply_markup=main_admin_menu(),
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(TEXTS["start"], reply_markup=main_user_menu())

# --- Enhanced User Support Flow ---
async def start_support_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initialize support chat for user"""
    if update.callback_query:
        user = update.callback_query.from_user
        query = update.callback_query
        await query.answer()
    else:
        user = update.message.from_user
    
    user_id = user.id
    
    # Start support session in database
    session_id = start_support_session(user_id)
    
    # Set user in support mode
    active_chats[user_id] = {
        "in_support": True, 
        "waiting_for_first_message": True,
        "username": user.username,
        "full_name": user.full_name,
        "admin_message_ids": [],  # Track admin messages for deletion
        "session_id": session_id,
        "message_count": 0
    }
    
    message_text = (
        "💬 **SUPPORT CHAT STARTED**\n\n"
        "You are now connected to support. Our admin will respond as soon as possible.\n\n"
        "📝 **Send your message below:**"
    )
    
    if update.callback_query:
        await query.edit_message_text(message_text, reply_markup=user_support_menu(), parse_mode='Markdown')
    else:
        await context.bot.send_message(chat_id=user_id, text=message_text, reply_markup=user_support_menu(), parse_mode='Markdown')

async def handle_user_support_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle messages from users in support mode"""
    user = update.message.from_user
    user_id = user.id
    message_text = update.message.text
    
    if user_id not in active_chats or not active_chats[user_id].get("in_support"):
        # User not in support mode
        await update.message.reply_text(
            "👋 Please use the support button to start a conversation:",
            reply_markup=main_user_menu()
        )
        return
    
    # Update message count
    active_chats[user_id]["message_count"] += 1
    update_user_stats(user_id, user.username, user.full_name)
    
    # Check if this is the first message
    if active_chats[user_id].get("waiting_for_first_message"):
        active_chats[user_id]["waiting_for_first_message"] = False
        
        username_display = f"@{user.username}" if user.username else "No username"
        admin_message = (
            f"🆘 **NEW SUPPORT REQUEST**\n\n"
            f"👤 User: {username_display}\n"
            f"🆔 ID: `{user_id}`\n"
            f"📝 Name: {user.full_name}\n"
            f"🕐 Time: {datetime.now().strftime('%H:%M:%S')}\n\n"
            f"💬 **First Message:**\n{message_text}"
        )
        
        admin_msg = await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_message,
            reply_markup=admin_chat_buttons(user_id),
            parse_mode='Markdown'
        )
        
        # Store admin message ID for potential deletion
        active_chats[user_id]["admin_message_ids"].append(admin_msg.message_id)
        
        await update.message.reply_text(
            "✅ **Message sent to admin!**\n\nStay here, admin will respond soon, if you left the chat the conversation will stop.",
            reply_markup=user_support_menu(),
            parse_mode='Markdown'
        )
    else:
        # Continue conversation
        admin_msg = await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"💬 **User {user_id}:** {message_text}",
            reply_markup=admin_chat_buttons(user_id),
            parse_mode='Markdown'
        )
        
        # Store admin message ID
        active_chats[user_id]["admin_message_ids"].append(admin_msg.message_id)
        
        await update.message.reply_text(
            "📤 **Message sent!**",
            reply_markup=user_support_menu(),
            parse_mode='Markdown'
        )

# --- Enhanced Admin Handlers ---
async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle enhanced admin panel navigation"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    query = update.callback_query
    
    try:
        await query.answer()
    except:
        pass
    
    if query.data == "admin_panel":
        try:
            await query.edit_message_text(
                "🔧 **ADMIN PANEL**\n\nChoose an option:",
                reply_markup=main_admin_menu(),
                parse_mode='Markdown'
            )
        except Exception:
            # If message can't be edited, send new one
            await query.message.reply_text(
                "🔧 **ADMIN PANEL**\n\nChoose an option:",
                reply_markup=main_admin_menu(),
                parse_mode='Markdown'
            )
    
    elif query.data == "view_active_chats":
        active_users = {uid: info for uid, info in active_chats.items() if info.get("in_support")}
        if not active_users:
            text = "📭 **NO ACTIVE CHATS**\n\nNo users are currently in support mode."
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]])
        else:
            text = f"💬 **ACTIVE SUPPORT CHATS** ({len(active_users)})\n\nSelect a chat to manage:"
            markup = admin_active_chats_menu(active_users)
        
        try:
            await query.edit_message_text(text, reply_markup=markup, parse_mode='Markdown')
        except:
            await query.message.reply_text(text, reply_markup=markup, parse_mode='Markdown')
    
    elif query.data == "bot_stats":
        stats = get_bot_statistics()
        hosting_status = "🟢 Production (24/7)" if IS_PRODUCTION else "🟡 Development"
        
        stats_text = (
            f"📊 **ENHANCED BOT STATISTICS**\n\n"
            f"👥 **Users:**\n"
            f"• Total Users: {stats['total_users']}\n"
            f"• Today: {stats['users_today']}\n"
            f"• This Week: {stats['users_week']}\n\n"
            f"💬 **Support:**\n"
            f"• Active Chats: {stats['current_active_chats']}\n"
            f"• Total Sessions: {stats['total_sessions']}\n"
            f"• Avg Messages/Session: {stats['avg_messages']}\n\n"
            f"🔧 **Status:** {hosting_status}\n"
            f"🤖 **Admin:** {ADMIN_ID}"
        )
        
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="bot_stats")], 
            [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
        ])
        
        try:
            await query.edit_message_text(stats_text, reply_markup=markup, parse_mode='Markdown')
        except:
            await query.message.reply_text(stats_text, reply_markup=markup, parse_mode='Markdown')
    
    elif query.data == "broadcast":
        all_users = get_all_user_ids()
        active_chat_users = [uid for uid in active_chats.keys() if active_chats[uid].get("in_support")]
        
        text = (
            f"📢 **BROADCAST MESSAGE**\n\n"
            f"All Users: {len(all_users)}\n"
            f"Active Chats: {len(active_chat_users)}\n\n"
            f"Choose broadcast type:"
        )
        
        try:
            await query.edit_message_text(text, reply_markup=broadcast_menu(), parse_mode='Markdown')
        except:
            await query.message.reply_text(text, reply_markup=broadcast_menu(), parse_mode='Markdown')
    
    elif query.data == "clean_chat":
        # Delete recent messages in admin chat
        deleted_count = 0
        chat_id = update.effective_chat.id
        current_msg_id = query.message.message_id
        
        for i in range(1, 50):  # Try to delete last 50 messages
            try:
                await context.bot.delete_message(chat_id, current_msg_id - i)
                deleted_count += 1
            except:
                break
        
        text = (
            f"🗑️ **CHAT CLEANED**\n\n"
            f"Deleted {deleted_count} recent messages.\n\n"
            f"Note: Due to Telegram limitations, only recent messages can be deleted."
        )
        
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]])
        
        try:
            await query.edit_message_text(text, reply_markup=markup, parse_mode='Markdown')
        except:
            await query.message.reply_text(text, reply_markup=markup, parse_mode='Markdown')
    
    elif query.data.startswith("view_chat_"):
        user_id = int(query.data.split("_")[2])
        user_info = active_chats.get(user_id, {})
        username = user_info.get("username", "No username")
        full_name = user_info.get("full_name", "Unknown")
        message_count = user_info.get("message_count", 0)
        
        chat_info = (
            f"👤 **USER CHAT DETAILS**\n\n"
            f"🆔 ID: {user_id}\n"
            f"👤 Username: @{username if username else 'None'}\n"
            f"📝 Name: {full_name}\n"
            f"💬 Messages: {message_count}\n"
            f"🟢 Status: Active in support\n"
            f"🕐 Started: Recently"
        )
        
        try:
            await query.edit_message_text(chat_info, reply_markup=admin_chat_buttons(user_id), parse_mode='Markdown')
        except:
            await query.message.reply_text(chat_info, reply_markup=admin_chat_buttons(user_id), parse_mode='Markdown')

async def admin_chat_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle enhanced admin chat management"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    query = update.callback_query
    try:
        await query.answer()
    except:
        pass
    
    data = query.data
    
    if data.startswith("reply_"):
        user_id = int(data.split("_")[1])
        admin_replying_to[ADMIN_ID] = user_id
        
        reply_text = (
            f"✍️ **REPLY MODE ACTIVATED**\n\n"
            f"👤 Replying to User {user_id}\n"
            f"💬 Type your message and it will be sent instantly."
        )
        
        try:
            await query.edit_message_text(reply_text, parse_mode='Markdown')
        except:
            await query.message.reply_text(reply_text, parse_mode='Markdown')
    
    elif data.startswith("close_clean_"):
        user_id = int(data.split("_")[2])
        
        try:
            # Get chat info before deletion
            chat_info = active_chats.get(user_id, {})
            username = chat_info.get("username", "No username")
            message_count = chat_info.get("message_count", 0)
            session_id = chat_info.get("session_id")
            
            # End support session in database
            if session_id:
                end_support_session(session_id, 'admin', message_count)
            
            # Delete admin messages related to this user
            admin_message_ids = chat_info.get("admin_message_ids", [])
            deleted_count = 0
            
            for msg_id in admin_message_ids:
                try:
                    await context.bot.delete_message(ADMIN_ID, msg_id)
                    deleted_count += 1
                except Exception as delete_error:
                    print(f"Could not delete message {msg_id}: {delete_error}")
            
            # Remove user from active chats
            active_chats.pop(user_id, None)
            
            # Create summary message
            summary = (
                f"✅ **CHAT CLOSED & CLEANED**\n\n"
                f"👤 User: @{username if username != 'No username' else f'User {user_id}'}\n"
                f"🆔 ID: {user_id}\n"
                f"💬 Total Messages: {message_count}\n"
                f"🗑️ Deleted Messages: {deleted_count}\n"
                f"🕐 Closed: {datetime.now().strftime('%H:%M:%S')}"
            )
            
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]])
            
            try:
                await query.edit_message_text(summary, reply_markup=markup, parse_mode='Markdown')
            except:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=summary,
                    reply_markup=markup,
                    parse_mode='Markdown'
                )
            
            # Notify user
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="✅ **SUPPORT CHAT COMPLETED**\n\nThank you for contacting us! Your issue has been resolved.\n\nFeel free to contact us again anytime.",
                    reply_markup=main_user_menu(),
                    parse_mode='Markdown'
                )
            except Exception as notify_error:
                print(f"Could not notify user {user_id}: {notify_error}")
                
        except Exception as e:
            error_text = f"❌ **ERROR CLOSING CHAT**\n\nUser: {user_id}\nError: {str(e)}"
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]])
            
            try:
                await query.edit_message_text(error_text, reply_markup=markup, parse_mode='Markdown')
            except:
                await context.bot.send_message(chat_id=ADMIN_ID, text=error_text, reply_markup=markup, parse_mode='Markdown')

async def admin_broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle broadcast options"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    query = update.callback_query
    await query.answer()
    
    if query.data == "broadcast_all":
        all_users = get_all_user_ids()
        text = (
            f"📢 **BROADCAST TO ALL USERS**\n\n"
            f"Ready to send to {len(all_users)} total users.\n\n"
            f"💬 **Type your message now:**"
        )
        
        try:
            await query.edit_message_text(text, parse_mode='Markdown')
        except:
            await query.message.reply_text(text, parse_mode='Markdown')
        
        context.user_data["broadcast_type"] = "all"
    
    elif query.data == "broadcast_active":
        active_users = [uid for uid in active_chats.keys() if active_chats[uid].get("in_support")]
        text = (
            f"💬 **BROADCAST TO ACTIVE CHATS**\n\n"
            f"Ready to send to {len(active_users)} users in active support chats.\n\n"
            f"💬 **Type your message now:**"
        )
        
        try:
            await query.edit_message_text(text, parse_mode='Markdown')
        except:
            await query.message.reply_text(text, parse_mode='Markdown')
        
        context.user_data["broadcast_type"] = "active"

async def admin_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin replies and broadcasts"""
    if update.message.from_user.id != ADMIN_ID:
        return
    
    # Check if admin is in broadcast mode
    if context.user_data.get("broadcast_type"):
        await handle_broadcast(update, context)
        return
    
    # Regular reply handling
    target_user_id = admin_replying_to.get(ADMIN_ID)
    if not target_user_id:
        # Admin not in any mode, show admin panel
        await update.message.reply_text(
            "🔧 **ADMIN PANEL**\n\nYou're not currently replying to anyone.\nUse the buttons below or send /admin",
            reply_markup=main_admin_menu(),
            parse_mode='Markdown'
        )
        return
    
    # Send reply to user
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"👨‍💼 **Admin:** {update.message.text}",
            reply_markup=user_support_menu(),
            parse_mode='Markdown'
        )
        
        # Clear reply mode
        admin_replying_to.pop(ADMIN_ID, None)
        
        # Confirm to admin
        await update.message.reply_text(
            f"✅ **Reply sent to User {target_user_id}!**\n\nUser can continue chatting.",
            reply_markup=admin_chat_buttons(target_user_id),
            parse_mode='Markdown'
        )
        
    except Exception as e:
        await update.message.reply_text(
            f"❌ **Failed to send reply to User {target_user_id}**\n\nError: {str(e)}",
            reply_markup=admin_chat_buttons(target_user_id),
            parse_mode='Markdown'
        )

async def handle_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle broadcast message sending"""
    broadcast_type = context.user_data.get("broadcast_type")
    message_text = update.message.text
    
    if not broadcast_type:
        await update.message.reply_text("❌ No broadcast type selected.")
        return
    
    if broadcast_type == "all":
        user_ids = get_all_user_ids()
        target_desc = "all users"
    elif broadcast_type == "active":
        user_ids = [uid for uid in active_chats.keys() if active_chats[uid].get("in_support")]
        target_desc = "active chat users"
    else:
        await update.message.reply_text("❌ Invalid broadcast type.")
        return
    
    if not user_ids:
        await update.message.reply_text(
            f"❌ No {target_desc} found to send broadcast to.",
            reply_markup=main_admin_menu()
        )
        context.user_data.pop("broadcast_type", None)
        return
    
    # Send "broadcasting..." message
    status_msg = await update.message.reply_text(
        f"📡 **BROADCASTING...**\n\n"
        f"Sending to {len(user_ids)} {target_desc}...",
        parse_mode='Markdown'
    )
    
    # Send broadcast
    success_count = 0
    fail_count = 0
    
    broadcast_message = f"📢 **ADMIN BROADCAST**\n\n{message_text}"
    
    for user_id in user_ids:
        if user_id == ADMIN_ID:
            continue
        
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=broadcast_message,
                parse_mode='Markdown'
            )
            success_count += 1
        except Exception:
            fail_count += 1
    
    # Clear broadcast mode
    context.user_data.pop("broadcast_type", None)
    
    # Update status message with results
    try:
        await status_msg.edit_text(
            f"📊 **BROADCAST COMPLETE**\n\n"
            f"🎯 Target: {target_desc}\n"
            f"✅ Successfully sent: {success_count}\n"
            f"❌ Failed to send: {fail_count}\n"
            f"📝 Message preview: {message_text[:50]}{'...' if len(message_text) > 50 else ''}\n\n"
            f"Total reach: {success_count}/{len(user_ids)} users",
            reply_markup=main_admin_menu(),
            parse_mode='Markdown'
        )
    except:
        await update.message.reply_text(
            f"📊 **BROADCAST COMPLETE**\n\n"
            f"🎯 Target: {target_desc}\n"
            f"✅ Successfully sent: {success_count}\n"
            f"❌ Failed to send: {fail_count}\n"
            f"📝 Message preview: {message_text[:50]}{'...' if len(message_text) > 50 else ''}\n\n"
            f"Total reach: {success_count}/{len(user_ids)} users",
            reply_markup=main_admin_menu(),
            parse_mode='Markdown'
        )

# --- User Callback Handlers ---
async def user_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user menu callbacks"""
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    
    if query.data == "help_support":
        await start_support_chat(update, context)
    
    elif query.data == "end_support":
        # User ends support chat
        if user_id in active_chats:
            chat_info = active_chats[user_id]
            session_id = chat_info.get("session_id")
            message_count = chat_info.get("message_count", 0)
            
            # End session in database
            if session_id:
                end_support_session(session_id, 'user', message_count)
            
            active_chats.pop(user_id, None)
        
        await query.edit_message_text(
            "❌ **SUPPORT CHAT ENDED**\n\nYou have ended the support chat.\nThank you for contacting us!",
            reply_markup=main_user_menu(),
            parse_mode='Markdown'
        )
        
        # Notify admin
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"ℹ️ User {user_id} ended their support chat.",
                parse_mode='Markdown'
            )
        except:
            pass

# --- Message Handlers ---
async def handle_user_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route user messages based on their state"""
    user_id = update.message.from_user.id
    
    if user_id == ADMIN_ID:
        await admin_reply_handler(update, context)
    elif user_id in active_chats and active_chats[user_id].get("in_support"):
        await handle_user_support_message(update, context)
    else:
        # Regular user not in support mode
        update_user_stats(user_id, update.message.from_user.username, update.message.from_user.full_name)
        await update.message.reply_text(
            "👋 **Welcome!** Use the menu below:",
            reply_markup=main_user_menu(),
            parse_mode='Markdown'
        )

# Set commands
async def set_commands(app):
    await app.bot.set_my_commands([
        BotCommand("start", "Start the bot and show main menu"),
        BotCommand("admin", "Admin panel (admin only)"),
        BotCommand("stats", "Show bot statistics (admin only)")
    ])

# Enhanced admin commands
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ You don't have admin access.")
        return
    
    await update.message.reply_text(
        "🔧 **ENHANCED ADMIN PANEL**\n\nManage your bot with advanced features!",
        reply_markup=main_admin_menu(),
        parse_mode='Markdown'
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Admin only command.")
        return
    
    stats = get_bot_statistics()
    await update.message.reply_text(
        f"📊 **QUICK STATS**\n\n"
        f"👥 Total Users: {stats['total_users']}\n"
        f"💬 Active Chats: {stats['current_active_chats']}\n"
        f"📈 Today's Users: {stats['users_today']}",
        parse_mode='Markdown'
    )

# Additional admin features
async def user_management_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user management features"""
    if update.effective_user.id != ADMIN_ID:
        return
        
    query = update.callback_query
    try:
        await query.answer()
    except:
        pass
    
    if query.data == "user_management":
        try:
            conn = sqlite3.connect('bot_data.db')
            cursor = conn.cursor()
            cursor.execute('SELECT user_id, username, full_name, last_seen FROM users ORDER BY last_seen DESC LIMIT 10')
            recent_users = cursor.fetchall()
            conn.close()
            
            user_list = "👥 **RECENT USERS**\n\n"
            for user_id, username, full_name, last_seen in recent_users:
                username_display = f"@{username}" if username else "No username"
                safe_name = full_name.replace('_', '\\_').replace('*', '\\*') if full_name else "Unknown"
                user_list += f"• {safe_name} ({username_display}) - ID: {user_id}\n"
            
            if not recent_users:
                user_list += "No users found."
            
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="user_management")],
                [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
            ])
            
            try:
                await query.edit_message_text(user_list, reply_markup=markup, parse_mode='Markdown')
            except:
                await query.message.reply_text(user_list, reply_markup=markup, parse_mode='Markdown')
                
        except Exception as e:
            error_text = f"❌ **ERROR LOADING USER DATA**\n\nError: {str(e)}"
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]])
            try:
                await query.edit_message_text(error_text, reply_markup=markup, parse_mode='Markdown')
            except:
                await query.message.reply_text(error_text, reply_markup=markup, parse_mode='Markdown')
    
    elif query.data == "support_history":
        try:
            conn = sqlite3.connect('bot_data.db')
            cursor = conn.cursor()
            cursor.execute('''
                SELECT s.user_id, u.username, s.started_at, s.ended_by, s.message_count, s.status
                FROM support_sessions s 
                LEFT JOIN users u ON s.user_id = u.user_id 
                ORDER BY s.started_at DESC 
                LIMIT 10
            ''')
            sessions = cursor.fetchall()
            conn.close()
            
            history_text = "📝 **SUPPORT HISTORY**\n\n"
            for user_id, username, started_at, ended_by, msg_count, status in sessions:
                username_display = f"@{username}" if username else f"User {user_id}"
                status_emoji = "🟢" if status == "active" else "🔴"
                
                if ended_by == "admin":
                    ended_info = "closed by admin"
                elif ended_by == "user":
                    ended_info = "ended by user"
                elif ended_by == "system":
                    ended_info = "system closed"
                elif status == "active":
                    ended_info = "ongoing"
                else:
                    ended_info = "completed"
                
                try:
                    if started_at:
                        dt = datetime.fromisoformat(started_at.replace('Z', '+00:00')) if 'Z' in started_at else datetime.fromisoformat(started_at)
                        time_str = dt.strftime('%m/%d %H:%M')
                    else:
                        time_str = "unknown time"
                except:
                    time_str = "unknown time"
                
                history_text += f"{status_emoji} {username_display} - {msg_count} msgs ({ended_info}) - {time_str}\n"
            
            if not sessions:
                history_text += "No support sessions found."
            
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="support_history")],
                [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
            ])
            
            try:
                await query.edit_message_text(history_text, reply_markup=markup, parse_mode='Markdown')
            except:
                await query.message.reply_text(history_text, reply_markup=markup, parse_mode='Markdown')
                
        except Exception as e:
            error_text = f"❌ **ERROR LOADING SUPPORT HISTORY**\n\nError: {str(e)}"
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]])
            try:
                await query.edit_message_text(error_text, reply_markup=markup, parse_mode='Markdown')
            except:
                await query.message.reply_text(error_text, reply_markup=markup, parse_mode='Markdown')
    
    elif query.data == "bot_settings":
        settings_text = (
            "🔧 **BOT SETTINGS**\n\n"
            f"🤖 Bot Token: Configured\n"
            f"👨‍💼 Admin ID: {ADMIN_ID}\n"
            f"💾 Database: SQLite Active\n"
            f"📊 Analytics: Enabled\n"
            f"📢 Broadcast: Enabled\n"
            f"🔒 Error Handler: Active\n"
            f"🌐 Hosting: {'Production 24/7' if IS_PRODUCTION else 'Development'}\n\n"
            "✅ All systems operational"
        )
        
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 View Stats", callback_data="bot_stats")],
            [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
        ])
        
        try:
            await query.edit_message_text(settings_text, reply_markup=markup, parse_mode='Markdown')
        except:
            await query.message.reply_text(settings_text, reply_markup=markup, parse_mode='Markdown')

async def block_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user blocking functionality"""
    if update.effective_user.id != ADMIN_ID:
        return
        
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("block_"):
        user_id = int(query.data.split("_")[1])
        
        # Mark user as blocked in database
        conn = sqlite3.connect('bot_data.db')
        cursor = conn.cursor()
        cursor.execute('UPDATE users SET is_active = 0 WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
        
        # Remove from active chats
        if user_id in active_chats:
            active_chats.pop(user_id, None)
        
        await query.edit_message_text(
            f"🚫 **USER BLOCKED**\n\n"
            f"User {user_id} has been blocked and removed from active chats.\n"
            f"They won't receive broadcasts anymore.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"🔓 Unblock {user_id}", callback_data=f"unblock_{user_id}")],
                [InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]
            ]),
            parse_mode='Markdown'
        )
        
        # Notify the blocked user
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="🚫 You have been blocked from using this bot.",
                parse_mode='Markdown'
            )
        except:
            pass
    
    elif query.data.startswith("unblock_"):
        user_id = int(query.data.split("_")[1])
        
        # Unblock user in database
        conn = sqlite3.connect('bot_data.db')
        cursor = conn.cursor()
        cursor.execute('UPDATE users SET is_active = 1 WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
        
        await query.edit_message_text(
            f"✅ **USER UNBLOCKED**\n\n"
            f"User {user_id} has been unblocked and can use the bot again.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]
            ]),
            parse_mode='Markdown'
        )

# Enhanced error handler
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors and notify admin"""
    try:
        print(f"Exception while handling an update: {context.error}")
        
        # Notify admin about error
        if ADMIN_ID:
            error_message = (
                f"⚠️ **BOT ERROR**\n\n"
                f"Error: `{str(context.error)}`\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=error_message,
                parse_mode='Markdown'
            )
    except Exception as e:
        print(f"Error in error handler: {e}")

# Build the app
app = ApplicationBuilder().token(TOKEN).post_init(set_commands).build()

# Add error handler
app.add_error_handler(error_handler)

# Add handlers in correct order
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("admin", admin_command))
app.add_handler(CommandHandler("stats", stats_command))
app.add_handler(CommandHandler("help", lambda u, c: start(u, c)))

# Admin callbacks
app.add_handler(CallbackQueryHandler(admin_panel_callback, pattern="^(admin_panel|view_active_chats|bot_stats|broadcast|clean_chat|view_chat_)"))
app.add_handler(CallbackQueryHandler(admin_chat_callback, pattern="^(reply_|close_clean_)"))
app.add_handler(CallbackQueryHandler(admin_broadcast_callback, pattern="^(broadcast_all|broadcast_active)$"))
app.add_handler(CallbackQueryHandler(user_management_callback, pattern="^(user_management|support_history|bot_settings)$"))
app.add_handler(CallbackQueryHandler(block_user_handler, pattern="^(block_|unblock_)"))

# User callbacks
app.add_handler(CallbackQueryHandler(user_callback_handler, pattern="^(help_support|end_support)$"))

# Text message handlers
app.add_handler(MessageHandler(filters.TEXT, handle_user_messages))

# Cleanup function for graceful shutdown
def cleanup_database():
    """Clean up database connections and end active sessions"""
    try:
        conn = sqlite3.connect('bot_data.db')
        cursor = conn.cursor()
        
        # End all active sessions
        cursor.execute('''
            UPDATE support_sessions 
            SET ended_at = CURRENT_TIMESTAMP, ended_by = 'system', status = 'closed'
            WHERE status = 'active'
        ''')
        
        conn.commit()
        conn.close()
        print("💾 Database cleanup completed")
    except Exception as e:
        print(f"❌ Database cleanup error: {e}")

# Start polling with production enhancements
if __name__ == "__main__":
    try:
        print("🚀 Enhanced Support Bot Starting...")
        print(f"👨‍💼 Admin ID: {ADMIN_ID}")
        print("💾 Database initialized")
        print("📊 Analytics ready")
        print("📢 Broadcast system active")
        print("🔒 Error handling enabled")
        print("🧹 Auto-cleanup enabled")
        print(f"🌐 Mode: {'Production (24/7)' if IS_PRODUCTION else 'Development'}")
        print(f"🔌 Port: {PORT}")
        
        # Start health server for production hosting (prevents sleeping)
        if IS_PRODUCTION:
            health_thread = threading.Thread(target=run_health_server, daemon=True)
            health_thread.start()
            print("❤️ Health server started")
            
            # Start keep-alive ping system
            loop = asyncio.get_event_loop()
            loop.create_task(keep_alive_ping())
            print("📡 Keep-alive system started")
        
        print("🤖 Bot is ready!")
        
        # Start the bot
        app.run_polling()
        
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped by user")
        cleanup_database()
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        cleanup_database()
    finally:
        print("👋 Bot shutdown complete")