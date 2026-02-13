"""
FinSplit Telegram Bot

Commands:
    /start      — Open the web app
    /balance    — Show daily allowance
    /spent <amount> <desc> — Quick-add expense

Setup:
    1. Create bot with @BotFather, get BOT_TOKEN
    2. Set env vars: BOT_TOKEN, WEBAPP_URL (your public HTTPS URL)
    3. Run: python bot.py
"""
import os
import asyncio
import hmac
import hashlib
import json
from urllib.parse import parse_qs

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

# ── Config ─────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
WEBAPP_URL = os.environ.get('WEBAPP_URL', 'http://localhost:5001')


# ── Helpers ────────────────────────────────────────────────────

def validate_init_data(init_data: str, bot_token: str) -> dict | None:
    """Validate Telegram WebApp initData using HMAC-SHA256.
    Returns parsed user data if valid, None otherwise."""
    parsed = parse_qs(init_data)
    if 'hash' not in parsed:
        return None

    received_hash = parsed.pop('hash')[0]

    # Build check string: sorted key=value pairs
    data_check = '\n'.join(
        f'{k}={v[0]}' for k, v in sorted(parsed.items())
    )

    # HMAC key = HMAC-SHA256("WebAppData", bot_token)
    secret = hmac.new(b'WebAppData', bot_token.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed, received_hash):
        return None

    # Parse user JSON
    if 'user' in parsed:
        return json.loads(parsed['user'][0])
    return {}


def get_daily_allowance_text():
    """Get daily allowance by importing from the Flask app context."""
    from app import app, calculate_daily_allowance
    with app.app_context():
        a = calculate_daily_allowance()

    status_emoji = {
        'comfortable': '🟢',
        'tight': '🟡',
        'over': '🔴',
    }
    emoji = status_emoji.get(a['status'], '⚪')

    lines = [
        f"{emoji} **Daily Allowance: ${a['daily_allowance']:.2f}**",
        '',
        f"Income: ${a['monthly_income']:.0f}",
        f"Fixed payments: ${a['fixed_payments_total']:.0f}",
        f"Investments: ${a['investment_contributions']:.0f}",
        f"Already spent: ${a['already_spent']:.0f}",
        f"Remaining: ${a['remaining']:.0f}",
        f"Days left: {a['days_remaining']}",
    ]
    return '\n'.join(lines)


def quick_add_expense(amount: float, description: str):
    """Add an expense transaction from Telegram."""
    from app import app, db
    from models import Transaction
    from datetime import date

    with app.app_context():
        t = Transaction(
            type='expense',
            amount=amount,
            currency='USD',
            category='Other',
            description=description,
            date=date.today(),
        )
        db.session.add(t)
        db.session.commit()
        return t.id


# ── Bot Handlers ──────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[
        InlineKeyboardButton(
            text='Open FinSplit',
            web_app=WebAppInfo(url=WEBAPP_URL)
        )
    ]]
    await update.message.reply_text(
        'Welcome to FinSplit! Open the app to manage your finances.',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = get_daily_allowance_text()
    except Exception as e:
        text = f'Error: {e}'
    await update.message.reply_text(text, parse_mode='Markdown')


async def spent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or len(args) < 1:
        await update.message.reply_text(
            'Usage: /spent <amount> <description>\nExample: /spent 15 Coffee'
        )
        return

    try:
        amount = float(args[0])
    except ValueError:
        await update.message.reply_text('Amount must be a number. Example: /spent 15 Coffee')
        return

    description = ' '.join(args[1:]) if len(args) > 1 else ''

    try:
        tid = quick_add_expense(amount, description)
        text = get_daily_allowance_text()
        await update.message.reply_text(
            f'Added expense: ${amount:.2f} {description}\n\n{text}',
            parse_mode='Markdown'
        )
    except Exception as e:
        await update.message.reply_text(f'Error adding expense: {e}')


# ── Main ──────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        print('Set BOT_TOKEN environment variable first.')
        print('Get one from @BotFather on Telegram.')
        return

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('balance', balance))
    app.add_handler(CommandHandler('spent', spent))

    print(f'Bot started. WebApp URL: {WEBAPP_URL}')
    app.run_polling()


if __name__ == '__main__':
    main()
