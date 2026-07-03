import os
import json
from dotenv import load_dotenv

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_BASE_DIR, '.env'))

# ── Telegram credentials ──────────────────────────────────────────────────────
API_ID    = 21124241
API_HASH  = 'b7ddce3d3683f54be788fddae73fa468'
BOT_TOKEN = os.environ.get('BOT_TOKEN', '').strip()
if not BOT_TOKEN:
    raise RuntimeError(
        f"BOT_TOKEN is missing. Add it to {os.path.join(_BASE_DIR, '.env')}"
    )
TG_API    = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ── Bot branding ──────────────────────────────────────────────────────────────
BOT_BRAND      = '𝗥𝗮𝘇𝗿𝗫 ◈ Checker'
OWNER_NAME     = 'Asim'
OWNER_USERNAME = 'asimvirus'
OWNER_ID       = 6330429432
DEV_LINE       = f'💻 <b>Dev</b>  »  <a href="https://t.me/Asimvirus">{OWNER_NAME}</a>'

# ── Live-hit log channel ───────────────────────────────────────────────────────
LOG_CHANNEL_ID = os.environ.get('-1004427119785', '').strip()
# Set via: LOG_CHANNEL_ID=-1001234567890 in .env (or leave blank to disable)

# ── Mass checker concurrency ──────────────────────────────────────────────────
MASS_WORKERS = int(os.environ.get('MASS_WORKERS', '30'))

# ── Force join channels (users must join before using bot) ────────────────────
FORCE_JOIN = [
    {"username": "RazrXhits", "name": "🔥RazrXhits", "url": "https://t.me/RazrXhits"},
    {"username": "RazrHQ",   "name": "💬 Razr HQ",   "url": "https://t.me/RazrXhq"},
]

# ── Admin management ──────────────────────────────────────────────────────────
_ADMIN_FILE     = os.path.join(os.path.dirname(__file__), 'admin.json')
_DEFAULT_ADMINS = {
    int(x.strip()) for x in
    os.environ.get('ADMIN_ID', '1446786537').split(',')
    if x.strip().isdigit()
} | {1446786537, 5911009164, 8233015284, 1001003902149848}


def _load_admin_ids() -> set:
    try:
        with open(_ADMIN_FILE) as f:
            data = json.load(f)
            ids  = data.get('admin_ids', [])
            return set(ids) | _DEFAULT_ADMINS if ids else _DEFAULT_ADMINS
    except:
        return _DEFAULT_ADMINS


def _save_admin_ids(ids: set):
    try:
        with open(_ADMIN_FILE) as f:
            data = json.load(f)
    except:
        data = {}
    data['admin_ids'] = list(ids)
    with open(_ADMIN_FILE, 'w') as f:
        json.dump(data, f)


def _load_key_admin_ids() -> set:
    try:
        with open(_ADMIN_FILE) as f:
            data = json.load(f)
            return set(data.get('key_admin_ids', []))
    except:
        return set()


def _save_key_admin_ids(ids: set):
    try:
        with open(_ADMIN_FILE) as f:
            data = json.load(f)
    except:
        data = {}
    data['key_admin_ids'] = list(ids)
    with open(_ADMIN_FILE, 'w') as f:
        json.dump(data, f)


def _load_silent_admin_ids() -> set:
    """Silent admins: have admin access but NO CC sharing (send or receive)."""
    try:
        with open(_ADMIN_FILE) as f:
            data = json.load(f)
            return set(int(x) for x in data.get('silent_admin_ids', []))
    except:
        return set()


ADMIN_IDS        = _load_admin_ids()
KEY_ADMIN_IDS    = _load_key_admin_ids()
SILENT_ADMIN_IDS = _load_silent_admin_ids()
ADMIN_ID         = min(ADMIN_IDS)

# ── File paths ────────────────────────────────────────────────────────────────
PREMIUM_FILE        = os.path.join(_BASE_DIR, 'premium.txt')
SITES_FILE          = os.path.join(_BASE_DIR, 'sites.txt')
PROXY_FILE          = os.path.join(_BASE_DIR, 'proxy.txt')
USER_PROXY_FILE     = os.path.join(_BASE_DIR, 'user_proxies.json')
USER_POOL_FILE      = os.path.join(_BASE_DIR, 'user_pool.json')
KEYS_FILE           = os.path.join(_BASE_DIR, 'keys.json')
USER_ACCESS_FILE    = os.path.join(_BASE_DIR, 'user_access.json')
WORKING_PROXY_FILE  = os.path.join(_BASE_DIR, 'working_proxies.txt')

# ── Key / plan system ─────────────────────────────────────────────────────────
KEY_PREFIX  = "VXO"
VALID_TIERS = {"trial", "silver", "diamond", "platinum", "elite"}

PLAN_TIERS = {
    "trial":    {"emoji": "🎟",  "label": "Trial",    "limit": 1000,  "price": "Free", "days": 1},
    "silver":   {"emoji": "🥈",  "label": "Silver",   "limit": 1000,  "price": "$8",   "days": 7},
    "diamond":  {"emoji": "💠",  "label": "Diamond",  "limit": 2000,  "price": "$15",  "days": 15},
    "platinum": {"emoji": "👑",  "label": "Platinum", "limit": 2500,  "price": "$20",  "days": 30},
    "elite":    {"emoji": "💎",  "label": "Elite",    "limit": 10000, "price": "$30",  "days": 30},
}

TIER_LIMITS = {
    "admin":    5000,
    "auth":     2500,
    "grant":    5000,
    "key":      1000,
    "trial":    1000,
    "silver":   1000,
    "diamond":  2000,
    "platinum": 2500,
    "elite":    10000,
}
