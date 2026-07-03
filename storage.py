import os
import re
import json
import string
import secrets
from datetime import datetime, timedelta, timezone

from config import (
    PREMIUM_FILE, SITES_FILE, PROXY_FILE, USER_PROXY_FILE, USER_POOL_FILE,
    KEYS_FILE, USER_ACCESS_FILE, WORKING_PROXY_FILE,
    ADMIN_IDS, _DEFAULT_ADMINS, KEY_ADMIN_IDS, KEY_PREFIX, TIER_LIMITS,
)

# ── Per-user proxy storage (each user stores a LIST of personal proxies) ──────
user_proxies: dict = {}


def _to_list(val) -> list:
    """Normalise old string values → list."""
    if val is None:
        return []
    if isinstance(val, list):
        return [p for p in val if p]
    return [val] if val else []


def load_user_proxies():
    global user_proxies
    if os.path.exists(USER_PROXY_FILE):
        try:
            with open(USER_PROXY_FILE, 'r') as f:
                raw = json.load(f)
                user_proxies = {int(k): _to_list(v) for k, v in raw.items()}
        except:
            user_proxies = {}


def save_user_proxies():
    try:
        with open(USER_PROXY_FILE, 'w') as f:
            json.dump({str(k): v for k, v in user_proxies.items()}, f)
    except:
        pass


def get_user_proxy(uid):
    """Return the first personal proxy (or None) — backward-compat."""
    lst = user_proxies.get(uid, [])
    return lst[0] if lst else None


def get_user_proxy_list(uid) -> list:
    """Return the full list of personal proxies for this user."""
    return list(user_proxies.get(uid, []))


def set_user_proxy(uid, proxy):
    """Set a single personal proxy (replaces the list)."""
    user_proxies[uid] = [proxy] if proxy else []
    save_user_proxies()


def set_user_proxies(uid, proxies: list):
    """Set the full list of personal proxies."""
    user_proxies[uid] = [p for p in proxies if p]
    save_user_proxies()


def remove_user_proxy(uid):
    user_proxies.pop(uid, None)
    save_user_proxies()


# ── Per-user proxy pool toggle ────────────────────────────────────────────────
user_pool_enabled: dict = {}


def load_user_pool():
    global user_pool_enabled
    if os.path.exists(USER_POOL_FILE):
        try:
            with open(USER_POOL_FILE, 'r') as f:
                user_pool_enabled = {int(k): v for k, v in json.load(f).items()}
        except:
            user_pool_enabled = {}


def save_user_pool():
    try:
        with open(USER_POOL_FILE, 'w') as f:
            json.dump({str(k): v for k, v in user_pool_enabled.items()}, f)
    except:
        pass


# ── Key / access storage ──────────────────────────────────────────────────────
_keys_data:   dict = {}
_user_access: dict = {}


def load_keys():
    global _keys_data
    if os.path.exists(KEYS_FILE):
        try:
            with open(KEYS_FILE, 'r') as f:
                _keys_data = json.load(f)
        except:
            _keys_data = {}


def save_keys():
    try:
        with open(KEYS_FILE, 'w') as f:
            json.dump(_keys_data, f, indent=2)
    except:
        pass


def load_user_access():
    global _user_access
    if os.path.exists(USER_ACCESS_FILE):
        try:
            with open(USER_ACCESS_FILE, 'r') as f:
                _user_access = {int(k): v for k, v in json.load(f).items()}
        except:
            _user_access = {}


def save_user_access():
    try:
        with open(USER_ACCESS_FILE, 'w') as f:
            json.dump({str(k): v for k, v in _user_access.items()}, f, indent=2)
    except:
        pass


# ── File-line helpers ─────────────────────────────────────────────────────────
def get_file_lines(fp):
    if not os.path.exists(fp):
        return []
    try:
        with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
            return [l.strip() for l in f if l.strip()]
    except:
        return []


def load_premium_users(): return get_file_lines(PREMIUM_FILE)
def load_sites():         return get_file_lines(SITES_FILE)
def load_proxies():       return get_file_lines(PROXY_FILE)


# ── Access helpers ────────────────────────────────────────────────────────────
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS or uid in _DEFAULT_ADMINS


def is_key_admin(uid: int) -> bool:
    """Key admin: can gen/grant keys but gets NO CC hit notifications."""
    return uid in KEY_ADMIN_IDS and uid not in ADMIN_IDS and uid not in _DEFAULT_ADMINS


def _now_utc():
    return datetime.now(timezone.utc)


def is_access_valid(uid: int) -> bool:
    acc = _user_access.get(uid)
    if not acc:
        return False
    try:
        exp = datetime.fromisoformat(acc['expires_at'])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return _now_utc() < exp
    except:
        return False


def is_premium(uid: int) -> bool:
    if is_admin(uid):           return True
    if is_access_valid(uid):    return True
    if str(uid) in load_premium_users(): return True
    return False


def get_user_tier(uid: int) -> str | None:
    if is_admin(uid):           return "admin"
    if is_access_valid(uid):    return _user_access[uid].get('tier', 'key')
    if str(uid) in load_premium_users(): return "admin"
    return None


def get_user_limit(uid: int) -> int:
    tier = get_user_tier(uid)
    return TIER_LIMITS.get(tier, 0)


def time_remaining(uid: int) -> str | None:
    acc = _user_access.get(uid)
    if not acc:
        return None
    try:
        exp = datetime.fromisoformat(acc['expires_at'])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        delta = exp - _now_utc()
        if delta.total_seconds() <= 0:
            return None
        d = delta.days
        h = delta.seconds // 3600
        m = (delta.seconds % 3600) // 60
        if d > 0: return f"{d}d {h}h {m}m"
        if h > 0: return f"{h}h {m}m"
        return f"{m}m"
    except:
        return None


# ── Key management ────────────────────────────────────────────────────────────
def generate_key() -> str:
    chars = string.ascii_letters + string.digits
    rand  = ''.join(secrets.choice(chars) for _ in range(20))
    return f"{KEY_PREFIX}-{rand}"


def set_user_access(uid: int, tier: str, plan_days: int, granted_by="admin"):
    expires = (_now_utc() + timedelta(days=plan_days)).isoformat()
    _user_access[uid] = {
        "tier":       tier,
        "expires_at": expires,
        "plan_days":  plan_days,
        "granted_by": granted_by,
        "granted_at": _now_utc().isoformat(),
    }
    save_user_access()


def revoke_user_access(uid: int):
    _user_access.pop(uid, None)
    save_user_access()


def get_proxies_for_user(uid: int) -> list:
    user_list = get_user_proxy_list(uid)
    pool      = load_proxies()
    pool_on   = user_pool_enabled.get(uid, True)
    if is_admin(uid):
        if user_list:
            return (user_list + pool) if pool_on else user_list
        return pool
    if not user_list:
        return []
    return (user_list + pool) if pool_on else user_list


# ── Misc helpers ──────────────────────────────────────────────────────────────
def extract_cc(text: str) -> list:
    matches = re.findall(r'(\d{15,16})\|(\d{2})\|(\d{2,4})\|(\d{3,4})', text)
    cards = []
    for card, month, year, cvv in matches:
        if len(year) == 2:
            year = '20' + year
        cards.append(f"{card}|{month}|{year}|{cvv}")
    return cards


def make_progress_bar(current, total, width=20) -> str:
    if total == 0:
        return f"[{'░'*width}] 0/0 (0%)"
    filled = int(width * current / total)
    pct    = int(100 * current / total)
    return f"[{'█'*filled}{'░'*(width-filled)}] {current}/{total} ({pct}%)"


def _save_working_proxy(proxy: str, user_id: int, card: str):
    if not proxy:
        return
    try:
        existing = set()
        if os.path.exists(WORKING_PROXY_FILE):
            with open(WORKING_PROXY_FILE, 'r') as f:
                existing = {l.strip() for l in f if l.strip()}
        if proxy not in existing:
            with open(WORKING_PROXY_FILE, 'a') as f:
                f.write(proxy + '\n')
    except Exception:
        pass


# ── Dead site indicators ──────────────────────────────────────────────────────
_DEAD_INDICATORS = (
    'receipt id is empty', 'handle is empty', 'product id is empty',
    'tax amount is empty', 'payment method identifier is empty',
    'invalid url', 'error in 1st req', 'error in 1 req',
    'cloudflare', 'connection failed', 'timed out', 'access denied',
    'tlsv1 alert', 'ssl routines', 'could not resolve', 'domain name not found',
    'name or service not known', 'openssl ssl_connect', 'empty reply from server',
    'httperror504', 'http error', 'timeout', 'unreachable', 'ssl error',
    '502', '503', '504', 'bad gateway', 'service unavailable', 'gateway timeout',
    'network error', 'connection reset', 'failed to detect product',
    'failed to create checkout', 'failed to tokenize card',
    'failed to get proposal data', 'submit rejected', 'handle error', 'http 404',
    'delivery_delivery_line_detail_changed', 'delivery_address2_required',
    'url rejected', 'malformed input', 'amount_too_small', 'amount too small',
    'site dead', 'captcha_required', 'captcha required', 'site errors', 'failed',
    'all products sold out', 'no_session_token', 'tokenize_fail',
)


def is_dead_site_error(msg: str) -> bool:
    if not msg:
        return True
    return any(k in str(msg).lower() for k in _DEAD_INDICATORS)


# ── Initialise on import ──────────────────────────────────────────────────────
load_user_proxies()
load_user_pool()
load_keys()
load_user_access()
