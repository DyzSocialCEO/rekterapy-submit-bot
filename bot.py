import requests
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
import os
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

# Conversation states - User
STORY_TYPE, WALLET, CONTRACT, AMOUNT, STORY, CONFIRM = range(6)

# Conversation states - Admin scoring
ADMIN_SCORING = 10
ADMIN_BROADCAST = 11

# Scoring criteria
CRITERIA = ['authenticity', 'emotional', 'lesson', 'detail', 'storytelling']
CRITERIA_NAMES = {
    'authenticity': '🔍 Authenticity',
    'emotional': '💔 Emotional Impact',
    'lesson': '📚 Lesson Learned',
    'detail': '📋 Detail Quality',
    'storytelling': '✍️ Storytelling'
}

# Rejection reasons
REJECTION_REASONS = {
    'ai': '🤖 AI-Generated Content',
    'fake': '🚫 Fake/Unverifiable Story',
    'copied': '📋 Copied/Stolen Content',
    'invalid': '💳 Invalid Wallet/Contract',
    'duplicate': '🔄 Duplicate Submission',
    'loweffort': '📝 Too Low Effort',
    'inappropriate': '⚠️ Inappropriate Content',
    'multiaccounts': '👤 Multiple Account Abuse'
}

# Database connection
def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

# Initialize database
def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    # Users table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id BIGINT PRIMARY KEY,
            username VARCHAR(255),
            total_moondust BIGINT DEFAULT 0,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Submissions table
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
            rejection_reason VARCHAR(100),
            score_authenticity INT DEFAULT 0,
            score_emotional INT DEFAULT 0,
            score_lesson INT DEFAULT 0,
            score_detail INT DEFAULT 0,
            score_storytelling INT DEFAULT 0,
            total_moondust INT DEFAULT 0,
            week_number INT,
            submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TIMESTAMP
        )
    ''')
    
    # Champions table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS champions (
            id SERIAL PRIMARY KEY,
            week_number INT UNIQUE,
            user_id BIGINT,
            username VARCHAR(255),
            submission_id INT,
            story_preview TEXT,
            total_moondust INT,
            announced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()

# Get current week number
def get_week_number():
    now = datetime.utcnow()
    return now.isocalendar()[1]

# Check if submissions are open (Sunday 00:00 - Friday 23:59 UTC)
def is_submissions_open():
    now = datetime.utcnow()
    # Monday = 0, Sunday = 6
    # Open: Sunday (6) 00:00 to Friday (4) 23:59
    if now.weekday() == 5:  # Saturday - closed for review
        return False
    return True

# Get time until submissions close
def get_time_until_close():
    now = datetime.utcnow()
    # Find next Friday 23:59
    days_until_friday = (4 - now.weekday()) % 7
    if days_until_friday == 0 and now.hour >= 23 and now.minute >= 59:
        days_until_friday = 7
    close_time = now.replace(hour=23, minute=59, second=0) + timedelta(days=days_until_friday)
    diff = close_time - now
    return diff.days, diff.seconds // 3600

# Ensure user exists
def ensure_user(user_id, username):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO users (telegram_id, username)
        VALUES (%s, %s)
        ON CONFLICT (telegram_id) DO UPDATE SET username = %s
    ''', (user_id, username, username))
    conn.commit()
    conn.close()

# Check rate limit by Telegram user ID
def check_user_rate_limit(user_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT COUNT(*) as count 
        FROM submissions 
        WHERE user_id = %s 
        AND submitted_at > %s
    ''', (user_id, datetime.now() - timedelta(days=1)))
    result = cursor.fetchone()
    conn.close()
    return result['count'] > 0

# Check rate limit by wallet address
def check_wallet_rate_limit(wallet_address):
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

# Validate wallet address
def is_valid_wallet(wallet):
    if len(wallet) < 26 or len(wallet) > 128:
        return False
    return all(c.isalnum() or c in '-_' for c in wallet)

# Validate contract address
def is_valid_contract(contract):
    if len(contract) < 26 or len(contract) > 128:
        return False
    return all(c.isalnum() or c in '-_' for c in contract)

# Add moondust to user
def add_moondust(user_id, amount):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE users SET total_moondust = total_moondust + %s
        WHERE telegram_id = %s
    ''', (amount, user_id))
    conn.commit()
    conn.close()

# ==================== USER COMMANDS ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.user_data.clear()
    
    ensure_user(user.id, user.username or user.first_name)
    
    # Check if submissions are open
    if not is_submissions_open():
        await update.message.reply_text(
            "⏰ Submissions are closed!\n\n"
            "Saturday is review day. Winners announced at 20:00 UTC.\n\n"
            "New week starts Sunday 00:00 UTC. Come back then! 🙏"
        )
        return ConversationHandler.END
    
    # Check rate limit
    if check_user_rate_limit(user.id):
        await update.message.reply_text(
            "⏰ You've already submitted a story today!\n\n"
            "One submission per account per 24 hours.\n\n"
            "Come back tomorrow to share another story! 🙏"
        )
        return ConversationHandler.END
    
    days, hours = get_time_until_close()
    
    welcome_text = f"""🎭 Welcome to Rekterapy Story Submission

🏆 WIN 5000 STARS WEEKLY!

Submit your best crypto story - wins or losses!

⏰ Week closes in: {days} days, {hours} hours

✅ What Makes a Winning Story:
- Authentic & verifiable (we check on-chain!)
- Emotional impact & lessons learned  
- Specific details (dates, amounts, tx hash)
- Helps the community learn

⚠️ INSTANT BAN for:
- Fake stories or stolen content
- Wrong wallet/CA addresses
- Multiple accounts or spam
- AI-generated content

📝 Scoring (Max 5000 Moondust):
- Authenticity: up to 1000
- Emotional Impact: up to 1000
- Lesson Learned: up to 1000
- Detail Quality: up to 1000
- Storytelling: up to 1000

💡 Commands: /cancel to exit, /back to go back

Choose your story type:"""
    
    keyboard = [
        [
            InlineKeyboardButton("📉 REKT Story", callback_data="type_rekt"),
            InlineKeyboardButton("🚀 MOON Story", callback_data="type_moon")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)
    return STORY_TYPE

async def story_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    story_type = query.data.replace('type_', '')
    context.user_data['story_type'] = story_type
    
    if story_type == 'rekt':
        prompt = "📉 REKT STORY SUBMISSION\n\nLet's document your loss for the community.\n\nFirst, what's your wallet address?\n\n(/cancel to exit | /back to go back)"
    else:
        prompt = "🚀 MOON STORY SUBMISSION\n\nLet's celebrate your win!\n\nFirst, what's your wallet address?\n\n(/cancel to exit | /back to go back)"
    
    await query.edit_message_text(prompt)
    return WALLET

async def back_to_story_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("📉 REKT Story", callback_data="type_rekt"),
            InlineKeyboardButton("🚀 MOON Story", callback_data="type_moon")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("⬅️ Choose your story type:", reply_markup=reply_markup)
    return STORY_TYPE

async def back_to_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⬅️ What's your wallet address?")
    return WALLET

async def back_to_contract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⬅️ What's the contract address?")
    return CONTRACT

async def back_to_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    story_type = context.user_data.get('story_type', 'rekt')
    if story_type == 'rekt':
        await update.message.reply_text("⬅️ How much did you lose?")
    else:
        await update.message.reply_text("⬅️ How much did you gain?")
    return AMOUNT

async def back_to_story(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⬅️ Tell us your story (20-750 chars):")
    return STORY

async def collect_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallet = update.message.text.strip()
    
    if not is_valid_wallet(wallet):
        await update.message.reply_text(
            "⚠️ Invalid wallet address!\n\n"
            "Must be 26-128 characters. Try again:"
        )
        return WALLET
    
    if check_wallet_rate_limit(wallet):
        await update.message.reply_text(
            "⚠️ This wallet already submitted today!\n\n"
            "One submission per wallet per 24 hours. 🙏"
        )
        return ConversationHandler.END
    
    context.user_data['wallet'] = wallet
    await update.message.reply_text("✅ Wallet saved!\n\nNow, the contract address:")
    return CONTRACT

async def collect_contract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contract = update.message.text.strip()
    
    if not is_valid_contract(contract):
        await update.message.reply_text(
            "⚠️ Invalid contract address!\n\n"
            "Must be 26-128 characters. Try again:"
        )
        return CONTRACT
    
    context.user_data['contract'] = contract
    
    story_type = context.user_data['story_type']
    if story_type == 'rekt':
        await update.message.reply_text("✅ Contract saved!\n\nHow much did you lose? (e.g., '$5000' or '2 ETH'):")
    else:
        await update.message.reply_text("✅ Contract saved!\n\nHow much did you gain? (e.g., '$50000' or '10x'):")
    return AMOUNT

async def collect_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount = update.message.text.strip()
    
    if len(amount) < 1 or len(amount) > 50:
        await update.message.reply_text("⚠️ Please enter a valid amount (1-50 chars):")
        return AMOUNT
    
    context.user_data['amount'] = amount
    
    story_type = context.user_data['story_type']
    if story_type == 'rekt':
        await update.message.reply_text("✅ Amount saved!\n\n💔 Tell us your REKT story (20-750 chars):\n\nWhat happened? Share the pain!")
    else:
        await update.message.reply_text("✅ Amount saved!\n\n🎉 Tell us your MOON story (20-750 chars):\n\nHow did you win?")
    return STORY

async def collect_story(update: Update, context: ContextTypes.DEFAULT_TYPE):
    story = update.message.text.strip()
    
    if len(story) > 750:
        await update.message.reply_text(f"⚠️ Too long ({len(story)} chars). Max 750. Shorten it:")
        return STORY
    
    if len(story) < 20:
        await update.message.reply_text("⚠️ Too short! At least 20 characters:")
        return STORY
    
    context.user_data['story'] = story
    
    # Show confirmation
    story_type = context.user_data['story_type']
    emoji = "📉" if story_type == 'rekt' else "🚀"
    type_text = "REKT" if story_type == 'rekt' else "MOON"
    
    wallet = context.user_data['wallet']
    contract = context.user_data['contract']
    wallet_short = f"{wallet[:8]}...{wallet[-6:]}"
    contract_short = f"{contract[:8]}...{contract[-6:]}"
    story_preview = story[:150] + "..." if len(story) > 150 else story
    
    confirm_text = f"""📋 CONFIRM YOUR SUBMISSION

{emoji} Type: {type_text}
💳 Wallet: {wallet_short}
📜 Contract: {contract_short}
💰 Amount: {context.user_data['amount']}

📖 Story:
{story_preview}

Is everything correct?"""
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Submit", callback_data="confirm_yes"),
            InlineKeyboardButton("❌ Cancel", callback_data="confirm_no")
        ],
        [InlineKeyboardButton("⬅️ Edit Story", callback_data="confirm_back")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(confirm_text, reply_markup=reply_markup)
    return CONFIRM

async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    action = query.data
    
    if action == "confirm_no":
        context.user_data.clear()
        await query.edit_message_text("❌ Cancelled. Send /start to try again.")
        return ConversationHandler.END
    
    elif action == "confirm_back":
        await query.edit_message_text("⬅️ Tell us your story (20-750 chars):")
        return STORY
    
    elif action == "confirm_yes":
        user = query.from_user
        story_type = context.user_data['story_type']
        week_num = get_week_number()
        
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO submissions 
            (user_id, username, story_type, wallet_address, contract_address, amount, story, week_number)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        ''', (
            user.id,
            user.username or user.first_name,
            story_type,
            context.user_data['wallet'],
            context.user_data['contract'],
            context.user_data['amount'],
            context.user_data['story'],
            week_num
        ))
        submission_id = cursor.fetchone()['id']
        conn.commit()
        conn.close()
        
        # Notify admin
        emoji = "📉" if story_type == 'rekt' else "🚀"
        type_text = "REKT" if story_type == 'rekt' else "MOON"
        
        admin_text = f"""{emoji} NEW {type_text} STORY #{submission_id}

👤 @{user.username or 'No username'} ({user.id})
💳 {context.user_data['wallet']}
📜 {context.user_data['contract']}
💰 {context.user_data['amount']}

📖 Story:
{context.user_data['story']}"""
        
        keyboard = [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"review_approve_{submission_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"review_reject_{submission_id}")
            ],
            [InlineKeyboardButton("⏭️ Skip", callback_data=f"review_skip_{submission_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text, reply_markup=reply_markup)
        
        # Confirm to user
        await query.edit_message_text(
            f"✅ Story #{submission_id} Submitted!\n\n"
            "Awaiting review. You'll be notified when scored.\n\n"
            "Good luck! 🍀"
        )
        
        context.user_data.clear()
        return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled. Send /start to begin again.")
    return ConversationHandler.END

# ==================== USER INFO COMMANDS ====================

async def mystats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username)
    
    conn = get_db()
    cursor = conn.cursor()
    
    # Get user moondust
    cursor.execute('SELECT total_moondust FROM users WHERE telegram_id = %s', (user.id,))
    user_data = cursor.fetchone()
    total_moondust = user_data['total_moondust'] if user_data else 0
    
    # Get submission stats
    cursor.execute('''
        SELECT 
            COUNT(*) as total,
            COUNT(CASE WHEN status = 'approved' THEN 1 END) as approved,
            COUNT(CASE WHEN status = 'rejected' THEN 1 END) as rejected,
            COUNT(CASE WHEN status = 'pending' THEN 1 END) as pending
        FROM submissions WHERE user_id = %s
    ''', (user.id,))
    stats = cursor.fetchone()
    
    # Get rank
    cursor.execute('''
        SELECT COUNT(*) + 1 as rank FROM users 
        WHERE total_moondust > (SELECT total_moondust FROM users WHERE telegram_id = %s)
    ''', (user.id,))
    rank_data = cursor.fetchone()
    rank = rank_data['rank'] if rank_data else 0
    
    # Check if user is a champion
    cursor.execute('SELECT COUNT(*) as wins FROM champions WHERE user_id = %s', (user.id,))
    wins = cursor.fetchone()['wins']
    
    conn.close()
    
    trophy = "🏆 " if wins > 0 else ""
    
    text = f"""📊 YOUR STATS

{trophy}@{user.username or user.first_name}

✨ Total Moondust: {total_moondust:,}
📈 Leaderboard Rank: #{rank}
🏆 Championship Wins: {wins}

📝 Submissions:
- Total: {stats['total']}
- Approved: {stats['approved']}
- Rejected: {stats['rejected']}
- Pending: {stats['pending']}"""
    
    await update.message.reply_text(text)

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT telegram_id, username, total_moondust 
        FROM users 
        ORDER BY total_moondust DESC 
        LIMIT 10
    ''')
    top_users = cursor.fetchall()
    
    # Get user rank
    cursor.execute('''
        SELECT COUNT(*) + 1 as rank FROM users 
        WHERE total_moondust > (SELECT COALESCE(total_moondust, 0) FROM users WHERE telegram_id = %s)
    ''', (user.id,))
    rank_data = cursor.fetchone()
    user_rank = rank_data['rank'] if rank_data else 0
    
    cursor.execute('SELECT total_moondust FROM users WHERE telegram_id = %s', (user.id,))
    user_moondust = cursor.fetchone()
    user_moondust = user_moondust['total_moondust'] if user_moondust else 0
    
    conn.close()
    
    medals = ['🥇', '🥈', '🥉']
    
    text = "🏆 MOONDUST LEADERBOARD\n\n"
    
    for i, u in enumerate(top_users):
        medal = medals[i] if i < 3 else f"{i+1}."
        name = u['username'] or 'Anonymous'
        text += f"{medal} @{name} — {u['total_moondust']:,} Moondust\n"
    
    text += f"\n━━━━━━━━━━━━━━━\n"
    text += f"Your rank: #{user_rank} ({user_moondust:,} Moondust)"
    
    await update.message.reply_text(text)

async def champions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT * FROM champions 
        ORDER BY week_number DESC 
        LIMIT 10
    ''')
    champs = cursor.fetchall()
    conn.close()
    
    if not champs:
        await update.message.reply_text("🏆 HALL OF CHAMPIONS\n\nNo champions yet! Be the first!")
        return
    
    text = "⭐ HALL OF CHAMPIONS\n\n"
    
    for c in champs:
        preview = c['story_preview'][:50] + "..." if len(c['story_preview'] or '') > 50 else c['story_preview']
        text += f"""🏆 Week {c['week_number']} | @{c['username']}
   "{preview}"
   Score: {c['total_moondust']:,} | Prize: 5000⭐

"""
    
    await update.message.reply_text(text)

async def week_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    week_num = get_week_number()
    is_open = is_submissions_open()
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) as count FROM submissions WHERE week_number = %s', (week_num,))
    submissions = cursor.fetchone()['count']
    conn.close()
    
    if is_open:
        days, hours = get_time_until_close()
        status = f"🟢 OPEN\n\n⏰ Closes in: {days} days, {hours} hours"
    else:
        status = "🔴 CLOSED\n\n📊 Review in progress. Results at 20:00 UTC!"
    
    text = f"""📅 WEEK {week_num} STATUS

{status}

📝 Submissions this week: {submissions}

💡 Submit your story with /start"""
    
    await update.message.reply_text(text)

# ==================== ADMIN COMMANDS ====================

async def admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT * FROM submissions 
        WHERE status = 'pending'
        ORDER BY submitted_at ASC 
        LIMIT 10
    ''')
    submissions = cursor.fetchall()
    conn.close()
    
    if not submissions:
        await update.message.reply_text("✅ No pending submissions!")
        return
    
    await update.message.reply_text(f"📋 {len(submissions)} pending submissions:\n")
    
    for sub in submissions:
        emoji = "📉" if sub['story_type'] == 'rekt' else "🚀"
        
        text = f"""{emoji} #{sub['id']} | @{sub['username']}
💳 {sub['wallet_address'][:20]}...
💰 {sub['amount']}

📖 {sub['story'][:200]}{'...' if len(sub['story']) > 200 else ''}"""
        
        keyboard = [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"review_approve_{sub['id']}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"review_reject_{sub['id']}")
            ],
            [InlineKeyboardButton("⏭️ Skip", callback_data=f"review_skip_{sub['id']}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(text, reply_markup=reply_markup)

async def admin_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    conn = get_db()
    cursor = conn.cursor()
    
    week_num = get_week_number()
    
    cursor.execute('SELECT COUNT(*) as count FROM submissions WHERE status = %s', ('pending',))
    pending = cursor.fetchone()['count']
    
    cursor.execute('SELECT COUNT(*) as count FROM submissions WHERE week_number = %s', (week_num,))
    this_week = cursor.fetchone()['count']
    
    cursor.execute('SELECT COUNT(*) as count FROM submissions WHERE week_number = %s AND status = %s', (week_num, 'approved'))
    approved_week = cursor.fetchone()['count']
    
    conn.close()
    
    text = f"""📊 ADMIN STATUS

📅 Week: {week_num}
⏰ Submissions: {'OPEN' if is_submissions_open() else 'CLOSED'}

📋 Pending: {pending}
📝 This week: {this_week}
✅ Approved this week: {approved_week}

Commands:
/pending - Review submissions
/stats - Full statistics
/champion - Set weekly winner"""
    
    await update.message.reply_text(text)

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('SELECT COUNT(*) as count FROM users')
    total_users = cursor.fetchone()['count']
    
    cursor.execute('SELECT COUNT(*) as count FROM submissions')
    total_subs = cursor.fetchone()['count']
    
    cursor.execute('SELECT COALESCE(SUM(total_moondust), 0) as total FROM users')
    total_moondust = cursor.fetchone()['total']
    
    cursor.execute('SELECT COUNT(*) as count FROM champions')
    total_champions = cursor.fetchone()['count']
    
    conn.close()
    
    text = f"""📈 FULL STATISTICS

👥 Total Users: {total_users}
📝 Total Submissions: {total_subs}
✨ Total Moondust Given: {total_moondust:,}
🏆 Championships Held: {total_champions}"""
    
    await update.message.reply_text(text)

async def admin_review_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorized!", show_alert=True)
        return
    
    await query.answer()
    
    parts = query.data.split('_')
    action = parts[1]
    submission_id = int(parts[2])
    
    if action == "skip":
        await query.edit_message_text(query.message.text + "\n\n⏭️ Skipped for later")
        return
    
    elif action == "reject":
        # Show rejection reasons
        keyboard = []
        for key, reason in REJECTION_REASONS.items():
            keyboard.append([InlineKeyboardButton(reason, callback_data=f"reject_{key}_{submission_id}")])
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f"review_back_{submission_id}")])
        
        await query.edit_message_text(
            query.message.text + "\n\n❌ Select rejection reason:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    elif action == "approve":
        # Start scoring
        context.user_data['scoring_submission'] = submission_id
        context.user_data['scores'] = {}
        context.user_data['current_criteria'] = 0
        context.user_data['original_message'] = query.message.text
        
        criteria = CRITERIA[0]
        name = CRITERIA_NAMES[criteria]
        
        keyboard = [
            [
                InlineKeyboardButton("200", callback_data=f"score_{criteria}_200"),
                InlineKeyboardButton("400", callback_data=f"score_{criteria}_400"),
                InlineKeyboardButton("600", callback_data=f"score_{criteria}_600"),
                InlineKeyboardButton("800", callback_data=f"score_{criteria}_800"),
                InlineKeyboardButton("1000", callback_data=f"score_{criteria}_1000")
            ],
            [InlineKeyboardButton("❌ Cancel Scoring", callback_data="score_cancel")]
        ]
        
        await query.edit_message_text(
            f"📊 SCORING #{submission_id}\n\n{name}:\n\nSelect score (200-1000):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    elif action == "back":
        # Go back to approve/reject
        keyboard = [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"review_approve_{submission_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"review_reject_{submission_id}")
            ],
            [InlineKeyboardButton("⏭️ Skip", callback_data=f"review_skip_{submission_id}")]
        ]
        
        # Remove the rejection prompt
        original = query.message.text.split("\n\n❌")[0]
        await query.edit_message_text(original, reply_markup=InlineKeyboardMarkup(keyboard))
        return

async def handle_rejection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorized!", show_alert=True)
        return
    
    await query.answer()
    
    parts = query.data.split('_')
    reason_key = parts[1]
    submission_id = int(parts[2])
    reason_text = REJECTION_REASONS.get(reason_key, 'Unknown')
    
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        UPDATE submissions 
        SET status = 'rejected', rejection_reason = %s, reviewed_at = %s
        WHERE id = %s
        RETURNING user_id
    ''', (reason_text, datetime.now(), submission_id))
    
    result = cursor.fetchone()
    user_id = result['user_id'] if result else None
    
    conn.commit()
    conn.close()
    
    # Notify user
    if user_id:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"❌ Your story #{submission_id} was rejected.\n\nReason: {reason_text}\n\nYou can submit a new story tomorrow."
            )
        except:
            pass
    
    await query.edit_message_text(
        query.message.text.split("\n\n❌")[0] + f"\n\n❌ REJECTED: {reason_text}"
    )

async def handle_scoring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorized!", show_alert=True)
        return
    
    await query.answer()
    
    if query.data == "score_cancel":
        context.user_data.clear()
        await query.edit_message_text(query.message.text + "\n\n❌ Scoring cancelled. Story still pending.")
        return
    
    if query.data == "score_back":
        current = context.user_data.get('current_criteria', 0)
        if current > 0:
            context.user_data['current_criteria'] = current - 1
            criteria = CRITERIA[current - 1]
            # Remove last score
            if criteria in context.user_data['scores']:
                del context.user_data['scores'][criteria]
        
        criteria = CRITERIA[context.user_data['current_criteria']]
        name = CRITERIA_NAMES[criteria]
        
        keyboard = [
            [
                InlineKeyboardButton("200", callback_data=f"score_{criteria}_200"),
                InlineKeyboardButton("400", callback_data=f"score_{criteria}_400"),
                InlineKeyboardButton("600", callback_data=f"score_{criteria}_600"),
                InlineKeyboardButton("800", callback_data=f"score_{criteria}_800"),
                InlineKeyboardButton("1000", callback_data=f"score_{criteria}_1000")
            ],
            [
                InlineKeyboardButton("⬅️ Back", callback_data="score_back"),
                InlineKeyboardButton("❌ Cancel", callback_data="score_cancel")
            ]
        ]
        
        submission_id = context.user_data['scoring_submission']
        await query.edit_message_text(
            f"📊 SCORING #{submission_id}\n\n{name}:\n\nSelect score (200-1000):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    if query.data.startswith("score_confirm"):
        # Final save
        submission_id = context.user_data['scoring_submission']
        scores = context.user_data['scores']
        total = sum(scores.values())
        
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE submissions 
            SET status = 'approved',
                score_authenticity = %s,
                score_emotional = %s,
                score_lesson = %s,
                score_detail = %s,
                score_storytelling = %s,
                total_moondust = %s,
                reviewed_at = %s
            WHERE id = %s
            RETURNING user_id, username, story
        ''', (
            scores.get('authenticity', 0),
            scores.get('emotional', 0),
            scores.get('lesson', 0),
            scores.get('detail', 0),
            scores.get('storytelling', 0),
            total,
            datetime.now(),
            submission_id
        ))
        
        result = cursor.fetchone()
        user_id = result['user_id']
        
        # Add moondust to user
        cursor.execute('''
            UPDATE users SET total_moondust = total_moondust + %s
            WHERE telegram_id = %s
        ''', (total, user_id))
        
        conn.commit()
        conn.close()
        
        # Notify user
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"✅ Your story #{submission_id} was approved!\n\n"
                     f"✨ You earned {total:,} Moondust!\n\n"
                     f"Breakdown:\n"
                     f"🔍 Authenticity: {scores.get('authenticity', 0)}\n"
                     f"💔 Emotional: {scores.get('emotional', 0)}\n"
                     f"📚 Lesson: {scores.get('lesson', 0)}\n"
                     f"📋 Detail: {scores.get('detail', 0)}\n"
                     f"✍️ Storytelling: {scores.get('storytelling', 0)}\n\n"
                     f"Check /leaderboard to see your rank!"
            )
        except:
            pass
        
        context.user_data.clear()
        
        await query.edit_message_text(
            query.message.text + f"\n\n✅ APPROVED: {total:,} Moondust"
        )
        return
    
    if query.data == "score_redo":
        context.user_data['scores'] = {}
        context.user_data['current_criteria'] = 0
        
        criteria = CRITERIA[0]
        name = CRITERIA_NAMES[criteria]
        
        keyboard = [
            [
                InlineKeyboardButton("200", callback_data=f"score_{criteria}_200"),
                InlineKeyboardButton("400", callback_data=f"score_{criteria}_400"),
                InlineKeyboardButton("600", callback_data=f"score_{criteria}_600"),
                InlineKeyboardButton("800", callback_data=f"score_{criteria}_800"),
                InlineKeyboardButton("1000", callback_data=f"score_{criteria}_1000")
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="score_cancel")]
        ]
        
        submission_id = context.user_data['scoring_submission']
        await query.edit_message_text(
            f"📊 SCORING #{submission_id}\n\n{name}:\n\nSelect score (200-1000):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    # Regular score selection
    parts = query.data.split('_')
    criteria = parts[1]
    score = int(parts[2])
    
    context.user_data['scores'][criteria] = score
    current = context.user_data['current_criteria']
    
    if current < len(CRITERIA) - 1:
        # Next criteria
        context.user_data['current_criteria'] = current + 1
        next_criteria = CRITERIA[current + 1]
        name = CRITERIA_NAMES[next_criteria]
        
        keyboard = [
            [
                InlineKeyboardButton("200", callback_data=f"score_{next_criteria}_200"),
                InlineKeyboardButton("400", callback_data=f"score_{next_criteria}_400"),
                InlineKeyboardButton("600", callback_data=f"score_{next_criteria}_600"),
                InlineKeyboardButton("800", callback_data=f"score_{next_criteria}_800"),
                InlineKeyboardButton("1000", callback_data=f"score_{next_criteria}_1000")
            ],
            [
                InlineKeyboardButton("⬅️ Back", callback_data="score_back"),
                InlineKeyboardButton("❌ Cancel", callback_data="score_cancel")
            ]
        ]
        
        submission_id = context.user_data['scoring_submission']
        progress = f"({current + 2}/{len(CRITERIA)})"
        
        await query.edit_message_text(
            f"📊 SCORING #{submission_id} {progress}\n\n{name}:\n\nSelect score (200-1000):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        # Show confirmation
        scores = context.user_data['scores']
        total = sum(scores.values())
        
        summary = f"""📊 SCORE SUMMARY

🔍 Authenticity: {scores.get('authenticity', 0)}
💔 Emotional: {scores.get('emotional', 0)}
📚 Lesson: {scores.get('lesson', 0)}
📋 Detail: {scores.get('detail', 0)}
✍️ Storytelling: {scores.get('storytelling', 0)}

━━━━━━━━━━━━━━━
✨ TOTAL: {total:,} Moondust

Confirm?"""
        
        keyboard = [
            [
                InlineKeyboardButton("✅ Confirm", callback_data="score_confirm"),
                InlineKeyboardButton("🔄 Redo", callback_data="score_redo")
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="score_cancel")]
        ]
        
        await query.edit_message_text(summary, reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_set_champion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    week_num = get_week_number()
    
    conn = get_db()
    cursor = conn.cursor()
    
    # Find highest scoring approved submission this week
    cursor.execute('''
        SELECT * FROM submissions 
        WHERE week_number = %s AND status = 'approved'
        ORDER BY total_moondust DESC, submitted_at ASC
        LIMIT 1
    ''', (week_num,))
    
    winner = cursor.fetchone()
    
    if not winner:
        await update.message.reply_text("❌ No approved submissions this week!")
        conn.close()
        return
    
    # Check if champion already set
    cursor.execute('SELECT * FROM champions WHERE week_number = %s', (week_num,))
    existing = cursor.fetchone()
    
    if existing:
        await update.message.reply_text(
            f"⚠️ Week {week_num} champion already set!\n\n"
            f"🏆 @{existing['username']} — {existing['total_moondust']:,} Moondust"
        )
        conn.close()
        return
    
    # Set champion
    story_preview = winner['story'][:100]
    
    cursor.execute('''
        INSERT INTO champions (week_number, user_id, username, submission_id, story_preview, total_moondust)
        VALUES (%s, %s, %s, %s, %s, %s)
    ''', (week_num, winner['user_id'], winner['username'], winner['id'], story_preview, winner['total_moondust']))
    
    conn.commit()
    conn.close()
    
    # Notify winner
    try:
        await context.bot.send_message(
            chat_id=winner['user_id'],
            text=f"🏆🎉 CONGRATULATIONS! 🎉🏆\n\n"
                 f"You are the Week {week_num} CHAMPION!\n\n"
                 f"Your story scored {winner['total_moondust']:,} Moondust!\n\n"
                 f"⭐ 5000 Telegram Stars coming your way!\n\n"
                 f"Thank you for sharing your story! 🙏"
        )
    except:
        pass
    
    await update.message.reply_text(
        f"🏆 WEEK {week_num} CHAMPION SET!\n\n"
        f"Winner: @{winner['username']}\n"
        f"Score: {winner['total_moondust']:,} Moondust\n"
        f"Story #{winner['id']}\n\n"
        f"User has been notified. Send them 5000⭐!"
    )

async def admin_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /undo <submission_id>")
        return
    
    try:
        submission_id = int(args[0])
    except:
        await update.message.reply_text("Invalid ID. Usage: /undo <submission_id>")
        return
    
    conn = get_db()
    cursor = conn.cursor()
    
    # Get current submission
    cursor.execute('SELECT * FROM submissions WHERE id = %s', (submission_id,))
    sub = cursor.fetchone()
    
    if not sub:
        await update.message.reply_text(f"❌ Submission #{submission_id} not found!")
        conn.close()
        return
    
    # If was approved, remove moondust from user
    if sub['status'] == 'approved' and sub['total_moondust'] > 0:
        cursor.execute('''
            UPDATE users SET total_moondust = total_moondust - %s
            WHERE telegram_id = %s
        ''', (sub['total_moondust'], sub['user_id']))
    
    # Reset to pending
    cursor.execute('''
        UPDATE submissions 
        SET status = 'pending', 
            rejection_reason = NULL,
            score_authenticity = 0,
            score_emotional = 0,
            score_lesson = 0,
            score_detail = 0,
            score_storytelling = 0,
            total_moondust = 0,
            reviewed_at = NULL
        WHERE id = %s
    ''', (submission_id,))
    
    conn.commit()
    conn.close()
    
    await update.message.reply_text(
        f"✅ Submission #{submission_id} reset to pending.\n\n"
        f"Previous status: {sub['status']}\n"
        f"Moondust removed: {sub['total_moondust']}"
    )

# ==================== HEALTH CHECK ====================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Bot is running!')
    
    def log_message(self, format, *args):
        return

def run_health_server():
    port = int(os.getenv('PORT', 10000))
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    print(f"Health check server running on port {port}")
    server.serve_forever()

# ==================== MAIN ====================

def main():
    init_db()
    
    health_thread = Thread(target=run_health_server, daemon=True)
    health_thread.start()
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    # User conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            STORY_TYPE: [CallbackQueryHandler(story_type_selected, pattern="^type_")],
            WALLET: [
                CommandHandler('back', back_to_story_type),
                MessageHandler(filters.TEXT & ~filters.COMMAND, collect_wallet)
            ],
            CONTRACT: [
                CommandHandler('back', back_to_wallet),
                MessageHandler(filters.TEXT & ~filters.COMMAND, collect_contract)
            ],
            AMOUNT: [
                CommandHandler('back', back_to_contract),
                MessageHandler(filters.TEXT & ~filters.COMMAND, collect_amount)
            ],
            STORY: [
                CommandHandler('back', back_to_amount),
                MessageHandler(filters.TEXT & ~filters.COMMAND, collect_story)
            ],
            CONFIRM: [
                CommandHandler('back', back_to_story),
                CallbackQueryHandler(handle_confirmation, pattern="^confirm_")
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    app.add_handler(conv_handler)
    
    # User commands
    app.add_handler(CommandHandler('mystats', mystats))
    app.add_handler(CommandHandler('leaderboard', leaderboard))
    app.add_handler(CommandHandler('top', leaderboard))
    app.add_handler(CommandHandler('champions', champions))
    app.add_handler(CommandHandler('halloffame', champions))
    app.add_handler(CommandHandler('week', week_status))
    
    # Admin commands
    app.add_handler(CommandHandler('pending', admin_pending))
    app.add_handler(CommandHandler('status', admin_status))
    app.add_handler(CommandHandler('stats', admin_stats))
    app.add_handler(CommandHandler('champion', admin_set_champion))
    app.add_handler(CommandHandler('undo', admin_undo))
    
    # Admin callback handlers
    app.add_handler(CallbackQueryHandler(admin_review_action, pattern="^review_"))
    app.add_handler(CallbackQueryHandler(handle_rejection, pattern="^reject_"))
    app.add_handler(CallbackQueryHandler(handle_scoring, pattern="^score_"))
    
    print("Bot started successfully!")
    app.run_polling()

if __name__ == '__main__':
    main()