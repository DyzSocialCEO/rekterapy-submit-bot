import requests
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
import os
import asyncio
from datetime import datetime, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes
)

# Environment variables
BOT_TOKEN = os.getenv('BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
ADMIN_ID = int(os.getenv('ADMIN_ID'))

# Conversation states
STORY_TYPE, WALLET, CONTRACT, AMOUNT, STORY = range(5)

# Database connection
def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

# Initialize database
def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS submissions (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            username VARCHAR(255),
            story_type VARCHAR(20) NOT NULL,
            wallet_address VARCHAR(255) NOT NULL,
            contract_address VARCHAR(255),
            amount VARCHAR(100),
            story TEXT,
            status VARCHAR(20) DEFAULT 'pending',
            submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

# Check rate limit
def check_rate_limit(wallet_address):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT COUNT(*) as count 
        FROM submissions 
        WHERE LOWER(wallet_address) = LOWER(%s) 
        AND submitted_at > %s
    ''', (wallet_address, datetime.now() - timedelta(days=1)))
    result = cursor.fetchone()
    conn.close()
    return result['count'] > 0

def validate_submission_token(token):
    """Validate token with the mini app API"""
    try:
        # Replace with your actual Vercel app URL
        API_URL = "https://your-app-url.vercel.app/api/verify-token"
        
        response = requests.get(f"{API_URL}?token={token}")
        
        if response.status_code == 200:
            data = response.json()
            return data.get('valid', False), data
        return False, None
    except Exception as e:
        print(f"Token validation error: {e}")
        return False, None
    
def validate_submission_token(token):
    """Validate token with the mini app API"""
    try:
        # Your actual Rekterapy app URL
        API_URL = "https://app.rekterapy.com/api/verify-token"
        
        response = requests.get(f"{API_URL}?token={token}")
        
        if response.status_code == 200:
            data = response.json()
            return data.get('valid', False), data
        return False, None
    except Exception as e:
        print(f"Token validation error: {e}")
        return False, None

# async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Check if user came with a token
    if not context.args:
        # No token - direct access denied
        await update.message.reply_text(
            "⛔ *Access Denied*\n\n"
            "This bot can only be accessed through the official Rekterapy app.\n\n"
            "👉 Open @RekTerapyFM_Bot and click 'Share Your Story' to submit.",
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    
    # Get the token
    token = context.args[0]
    
    # Validate with API
    is_valid, user_data = validate_submission_token(token)
    
    if not is_valid:
        await update.message.reply_text(
            "⛔ *Invalid or Expired Token*\n\n"
            "Your access token is invalid or has expired.\n\n"
            "Please go back to @RekTerapyFM_Bot and click 'Share Your Story' again.\n\n"
            "_Tokens expire after 1 hour for security._",
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    
    # Store validated user info for later use
    context.user_data['validated'] = True
    context.user_data['app_user_id'] = user_data.get('user_id')
    
    welcome_text = f"""
🎭 *Welcome to Rekterapy Story Submission*

🏆 *WIN 5000 STARS WEEKLY!* 

Submit your best crypto story - wins or losses!

✅ *What Makes a Winning Story:*
- Authentic & verifiable (we check on-chain!)
- Emotional impact & lessons learned  
- Specific details (dates, amounts, tx hash)
- Helps the community learn

⚠️ *INSTANT BAN for:*
- Fake stories or stolen content
- Wrong wallet/CA addresses
- Multiple accounts or spam
- AI-generated content

📝 *Rules:*
- One submission per wallet per WEEK
- All stories are manually verified
- Winners announced every Sunday
- False info = permanent ban

*Choose your story type:*
    """
    
    keyboard = [
        [
            InlineKeyboardButton("📉 REKT Story", callback_data="type_rekt"),
            InlineKeyboardButton("🚀 MOON Story", callback_data="type_moon")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        welcome_text, 
        parse_mode='Markdown',
        reply_markup=reply_markup
    )
    return STORY_TYPE

# Handle story type selection
async def story_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    story_type = query.data.replace('type_', '')
    context.user_data['story_type'] = story_type
    
    if story_type == 'rekt':
        prompt = "📉 *REKT STORY SUBMISSION*\n\nLet's document your loss for the community.\n\nFirst, what's your *wallet address*?"
    else:
        prompt = "🚀 *MOON STORY SUBMISSION*\n\nLet's celebrate your win!\n\nFirst, what's your *wallet address*?"
    
    await query.edit_message_text(prompt, parse_mode='Markdown')
    return WALLET

# Collect wallet
async def collect_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallet = update.message.text.strip()
    
    if len(wallet) < 20:
        await update.message.reply_text("⚠️ Please enter a valid wallet address:")
        return WALLET
    
    if check_rate_limit(wallet):
        await update.message.reply_text(
            "⏰ You've already submitted today. One submission per wallet per 24 hours!\n\n"
            "Come back tomorrow to share another story! 🙏"
        )
        return ConversationHandler.END
    
    context.user_data['wallet'] = wallet
    await update.message.reply_text(
        "✅ Wallet saved!\n\n"
        "Now, the *contract address* of the token:",
        parse_mode='Markdown'
    )
    return CONTRACT

# Collect contract
async def collect_contract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contract = update.message.text.strip()
    context.user_data['contract'] = contract
    
    if context.user_data['story_type'] == 'rekt':
        prompt = "How much did you *lose*? (e.g., '$5000' or '2 ETH'):"
    else:
        prompt = "How much did you *gain*? (e.g., '$50000' or '10x'):"
    
    await update.message.reply_text(prompt, parse_mode='Markdown')
    return AMOUNT

# Collect amount
async def collect_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount = update.message.text.strip()
    context.user_data['amount'] = amount
    
    if context.user_data['story_type'] == 'rekt':
        prompt = (
            "💔 *Tell us your REKT story* (max 750 chars):\n\n"
            "_How did it happen? What went wrong? Share the pain!_"
        )
    else:
        prompt = (
            "🎉 *Tell us your MOON story* (max 750 chars):\n\n"
            "_How did you spot it? When did you buy? How did you win?_"
        )
    
    await update.message.reply_text(prompt, parse_mode='Markdown')
    return STORY

# Collect story and save
async def collect_story(update: Update, context: ContextTypes.DEFAULT_TYPE):
    story = update.message.text.strip()
    
    if len(story) > 750:
        await update.message.reply_text(
            f"⚠️ Your story is {len(story)} characters. Please shorten to 750 or less:"
        )
        return STORY
    
    user = update.effective_user
    story_type = context.user_data['story_type']
    
    # Save to database
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO submissions 
        (user_id, username, story_type, wallet_address, contract_address, amount, story)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    ''', (
        user.id,
        user.username or user.first_name,
        story_type,
        context.user_data['wallet'],
        context.user_data['contract'],
        context.user_data['amount'],
        story
    ))
    submission_id = cursor.fetchone()['id']
    conn.commit()
    conn.close()
    
    # Emoji based on type
    emoji = "📉" if story_type == 'rekt' else "🚀"
    type_text = "REKT" if story_type == 'rekt' else "MOON"
    
    # Send to admin
    admin_text = f"""
{emoji} *NEW {type_text} STORY* #{submission_id}

👤 User: @{user.username or 'No username'} ({user.id})
💳 Wallet: `{context.user_data['wallet']}`
📜 Contract: `{context.user_data['contract']}`
💰 Amount: {context.user_data['amount']}

📖 *Story:*
{story}
    """
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{submission_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{submission_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=admin_text,
        parse_mode='Markdown',
        reply_markup=reply_markup
    )
    
    # Confirm to user
    if story_type == 'rekt':
        confirm = (
            "📉 *REKT Story Submitted!*\n\n"
            "Your loss has been documented. If approved, you're eligible for the $100 monthly REKT award!\n\n"
            "Stay strong, degen. We've all been there. 🫂"
        )
    else:
        confirm = (
            "🚀 *MOON Story Submitted!*\n\n"
            "Your win has been recorded! If approved, you're eligible for the $100 monthly MOON award!\n\n"
            "Congrats on the gains! 🎉"
        )
    
    await update.message.reply_text(confirm, parse_mode='Markdown')
    
    context.user_data.clear()
    return ConversationHandler.END

# Cancel
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❌ Submission cancelled.\n\n"
        "Come back anytime to share your story!"
    )
    context.user_data.clear()
    return ConversationHandler.END

# Admin: View pending by type
async def admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT * FROM submissions 
        WHERE status = 'pending'
        ORDER BY submitted_at DESC 
        LIMIT 10
    ''')
    
    submissions = cursor.fetchall()
    conn.close()
    
    if not submissions:
        await update.message.reply_text("No pending submissions.")
        return
    
    for sub in submissions:
        emoji = "📉" if sub['story_type'] == 'rekt' else "🚀"
        type_text = sub['story_type'].upper()
        
        text = f"""
{emoji} *{type_text} Submission #{sub['id']}*
👤 User: {sub['username']} ({sub['user_id']})
💳 Wallet: `{sub['wallet_address']}`
📜 Contract: `{sub['contract_address']}`
💰 Amount: {sub['amount']}
📅 Submitted: {sub['submitted_at'].strftime('%Y-%m-%d %H:%M')}

📖 Story:
{sub['story']}
        """
        
        keyboard = [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve_{sub['id']}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject_{sub['id']}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            text,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

# Admin: Get approved stories
async def admin_approved(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT * FROM submissions 
        WHERE status = 'approved'
        ORDER BY reviewed_at DESC 
        LIMIT 20
    ''')
    
    submissions = cursor.fetchall()
    conn.close()
    
    if not submissions:
        await update.message.reply_text("No approved submissions yet.")
        return
    
    response = "*APPROVED STORIES (Copy for app):*\n\n"
    
    for sub in submissions:
        emoji = "📉" if sub['story_type'] == 'rekt' else "🚀"
        response += f"""
---
{emoji} #{sub['id']} ({sub['story_type'].upper()})
Wallet: {sub['wallet_address'][:6]}...{sub['wallet_address'][-4:]}
Amount: {sub['amount']}
Story: {sub['story']}
---

"""
    
    await update.message.reply_text(response, parse_mode='Markdown')

# Handle review buttons
async def handle_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorized!", show_alert=True)
        return
    
    action, submission_id = query.data.split('_')
    
    conn = get_db()
    cursor = conn.cursor()
    
    new_status = 'approved' if action == 'approve' else 'rejected'
    
    cursor.execute('''
        UPDATE submissions 
        SET status = %s, reviewed_at = %s 
        WHERE id = %s
    ''', (new_status, datetime.now(), submission_id))
    
    conn.commit()
    conn.close()
    
    await query.answer(f"Submission {new_status}!")
    
    await query.edit_message_text(
        query.message.text + f"\n\n✅ *STATUS: {new_status.upper()}*",
        parse_mode='Markdown'
    )

    # Simple health check server for Render
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Bot is running!')
    
    def log_message(self, format, *args):
        return  # Disable logging

def run_health_server():
    port = int(os.getenv('PORT', 10000))
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    print(f"Health check server running on port {port}")
    server.serve_forever()

def main():
    init_db()
    
    # Start health check server in background
    health_thread = Thread(target=run_health_server, daemon=True)
    health_thread.start()
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            STORY_TYPE: [CallbackQueryHandler(story_type_selected)],
            WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_wallet)],
            CONTRACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_contract)],
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_amount)],
            STORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_story)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler('pending', admin_pending))
    app.add_handler(CommandHandler('approved', admin_approved))
    app.add_handler(CallbackQueryHandler(handle_review))
    
    print("Bot started successfully!")
    app.run_polling()

if __name__ == '__main__':
    main()