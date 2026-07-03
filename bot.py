from telethon import TelegramClient, events, Button
from telethon.tl.custom.message import Message as _TLMessage
import asyncio
import itertools
import aiohttp
import aiofiles
import os
import random
import time
import json
import re
import requests

# Keep one polling process and one shared Telethon session regardless of cwd.
_instance_lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.bot.lock')
_instance_lock = open(_instance_lock_path, 'a+b')
if os.path.getsize(_instance_lock_path) == 0:
    _instance_lock.write(b'\0')
    _instance_lock.flush()
_instance_lock.seek(0)
try:
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(_instance_lock.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(_instance_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError as exc:
    raise RuntimeError("Another bot.py instance is already running.") from exc

# ── Our modules ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━───────
import config
from config import (
    API_ID, API_HASH, BOT_TOKEN, TG_API,
    BOT_BRAND, OWNER_NAME, OWNER_USERNAME, OWNER_ID, DEV_LINE,
    PREMIUM_FILE, SITES_FILE, PROXY_FILE, USER_PROXY_FILE, USER_POOL_FILE,
    KEYS_FILE, USER_ACCESS_FILE, WORKING_PROXY_FILE,
    ADMIN_IDS, ADMIN_ID, _DEFAULT_ADMINS, _save_admin_ids,
    KEY_ADMIN_IDS, _save_key_admin_ids, SILENT_ADMIN_IDS,
    TIER_LIMITS, KEY_PREFIX, PLAN_TIERS, VALID_TIERS,
    MASS_WORKERS, LOG_CHANNEL_ID,
)
from emojis import pe, SEP
from keyboards import (
    _raw_post, raw_send, raw_edit, nav_edit,
    rows_main, rows_gates, rows_proxy, rows_admin, rows_admin_users,
    rows_admin_sites, rows_admin_proxy_pool, rows_stop, kb_stop,
)
from storage import (
    user_proxies, user_pool_enabled,
    load_user_proxies, save_user_proxies,
    load_user_pool, save_user_pool,
    _keys_data, _user_access,
    load_keys, save_keys, load_user_access, save_user_access,
    get_user_proxy, get_user_proxy_list, set_user_proxy, set_user_proxies, remove_user_proxy,
    get_file_lines, load_premium_users, load_sites, load_proxies,
    is_admin, is_key_admin, is_premium,
    get_user_tier, get_user_limit, time_remaining,
    generate_key, set_user_access, revoke_user_access,
    get_proxies_for_user, extract_cc, make_progress_bar,
    _now_utc, _save_working_proxy,
)
from bin_db import get_bin_info, load_bins
from cards import build_result_card, checker_line
from proxy_utils import (
    check_card_with_retry, test_proxy, test_site, get_proxy_ip,
    clear_session_bad_sites, clear_error_log,
)

from keyboards import _send_notification, _pin_message_botapi

from datetime import datetime

# ── Bot client ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━────────
_session_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'checker_bot')
bot = TelegramClient(_session_path, API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# ── Global session state ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━────────────
active_sessions: dict = {}
pending_checks:  dict = {}

# ═══════════════════════════════════════════════════════════════════════════════
# TELETHON PATCHES — disable link previews, strip invalid tg-emoji gracefully
# ═══════════════════════════════════════════════════════════════════════════════

_orig_send_message = bot.send_message
_orig_edit_message = bot.edit_message

import re as _re

def _strip_tg_emoji(text):
    if not text:
        return text
    return _re.sub(r'<tg-emoji[^>]*>([^<]*)</tg-emoji>', r'\1', text)

def _is_doc_invalid(e):
    s = str(e).upper()
    return 'DOCUMENT_INVALID' in s or 'FILE_REFERENCE_INVALID' in s

_orig_tl_edit = _TLMessage.edit

async def _safe_tl_edit(self, *args, **kwargs):
    kwargs.setdefault('link_preview', False)
    try:
        return await _orig_tl_edit(self, *args, **kwargs)
    except Exception as e:
        if _is_doc_invalid(e):
            new_args = list(args)
            if new_args and isinstance(new_args[0], str):
                new_args[0] = _strip_tg_emoji(new_args[0])
            if 'text' in kwargs:
                kwargs['text'] = _strip_tg_emoji(kwargs['text'])
            if 'message' in kwargs and isinstance(kwargs['message'], str):
                kwargs['message'] = _strip_tg_emoji(kwargs['message'])
            return await _orig_tl_edit(self, *new_args, **kwargs)
        raise

_TLMessage.edit = _safe_tl_edit

async def _send_message_no_preview(*args, **kwargs):
    kwargs.setdefault('link_preview', False)
    try:
        return await _orig_send_message(*args, **kwargs)
    except Exception as e:
        if _is_doc_invalid(e):
            args = list(args)
            if len(args) > 1 and isinstance(args[1], str):
                args[1] = _strip_tg_emoji(args[1])
            if 'message' in kwargs:
                kwargs['message'] = _strip_tg_emoji(kwargs['message'])
            return await _orig_send_message(*args, **kwargs)
        raise

async def _edit_message_no_preview(*args, **kwargs):
    kwargs.setdefault('link_preview', False)
    try:
        return await _orig_edit_message(*args, **kwargs)
    except Exception as e:
        if _is_doc_invalid(e):
            args = list(args)
            if len(args) > 2 and isinstance(args[2], str):
                args[2] = _strip_tg_emoji(args[2])
            if 'text' in kwargs:
                kwargs['text'] = _strip_tg_emoji(kwargs['text'])
            return await _orig_edit_message(*args, **kwargs)
        raise

bot.send_message = _send_message_no_preview
bot.edit_message = _edit_message_no_preview

# ═══════════════════════════════════════════════════════════════════════════════
# BOT-DEPENDENT HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def get_display_name(uid):
    try:
        entity = await bot.get_entity(uid)
        name = (entity.first_name or "").strip()
        if getattr(entity, 'last_name', None):
            name = f"{name} {entity.last_name}".strip()
        return name or f"User {uid}"
    except:
        return f"User {uid}"

async def get_user_info(uid):
    try:
        entity = await bot.get_entity(uid)
        name = (entity.first_name or "").strip()
        if getattr(entity, 'last_name', None):
            name = f"{name} {entity.last_name}".strip()
        name = name or f"User {uid}"
        username = getattr(entity, 'username', None)
        return name, username
    except:
        return f"User {uid}", None

async def get_tg_username(uid):
    """Return @username if set, else ID:uid string."""
    try:
        entity = await bot.get_entity(uid)
        uname  = getattr(entity, 'username', None)
        return f"@{uname}" if uname else f"ID:{uid}"
    except:
        return f"ID:{uid}"


def _is_3ds(msg: str) -> bool:
    m = msg.lower()
    return any(x in m for x in ('3d secure', '3ds', 'authentication required', 'otp required'))

def _is_insuf(msg: str) -> bool:
    return 'insufficient' in msg.lower()


async def forward_to_log_channel(result, bin_info, user_id, checker_name):
    if not LOG_CHANNEL_ID:
        return
    try:
        log_chat = int(LOG_CHANNEL_ID)
    except (ValueError, TypeError):
        return
    msg = build_result_card(result, bin_info, user_id, checker_name, show_uid=True)
    await bot.send_message(log_chat, msg, parse_mode='html')

async def send_realtime_hit(user_id, result, hit_type):
    bin_info       = await get_bin_info(result['card'].split('|')[0])
    result['bin_info'] = bin_info
    name, username = await get_user_info(user_id)
    has_username   = bool(username)
    checker_name   = name if has_username else str(user_id)
    msg    = build_result_card(result, bin_info, user_id, checker_name, show_uid=False)
    msg_id = await asyncio.to_thread(_send_notification, user_id, msg)
    if msg_id and hit_type == "Charged":
        await asyncio.to_thread(_pin_message_botapi, user_id, msg_id)
    asyncio.create_task(forward_to_log_channel(result, bin_info, user_id, checker_name))


async def send_insufficient_log(user_id, result):
    from cards import _clean_response
    card     = result['card']
    resp_msg = _clean_response(result.get('message', ''))
    bin_info = await get_bin_info(card.split('|')[0]) if card else None
    await bot.send_message(
        user_id,
        pe(f"💸 <b>Insufficient Funds</b>\n"
           f"<b>{SEP}</b>\n"
           f"🃏 <b>Card</b>  »  <tg-spoiler>{card}</tg-spoiler>\n"
           f"💬 <b>Reason</b>  »  {resp_msg}"),
        parse_mode='html'
    )
    name, uname = await get_user_info(user_id)
    cname = name if uname else str(user_id)
    asyncio.create_task(forward_to_log_channel(result, bin_info or ("","","","","",""), user_id, cname))




# ═══════════════════════════════════════════════════════════════════════════════
# MASS CHECK ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

async def update_mass_progress(user_id, message_id, results, checked, last_res=None):
    bar    = make_progress_bar(checked, results['total'])
    latest = ""
    if last_res:
        st    = last_res['status']
        msg_r = last_res.get('message', '') or ''
        if st == 'Charged':
            se = "💎"; label = "CHARGED"
        elif st == 'Approved':
            se = "✅"; label = "APPROVED"
        elif _is_insuf(msg_r):
            se = "💸"; label = "Insufficient"
        elif _is_3ds(msg_r):
            se = "⚠️"; label = "3DS"
        else:
            se = "🚫"; label = "Declined"
        reason = msg_r[:45] if msg_r else label
        t = round(time.time() - results.get('last_card_time', time.time()), 2)
        latest = (
            f"\n<b>{SEP}</b>\n"
            f"⚡ <b>Last Card</b>\n"
            f"{se}  <tg-spoiler>{last_res['card']}</tg-spoiler>\n"
            f"💫  {reason}  ·  {t}s"
        )
    text = pe(
        f"<b>🔥 Mass Check — Running</b>\n"
        f"<b>{SEP}</b>\n"
        f"📋 <b>Total</b>    »  {results['total']}\n"
        f"☄️ <b>Checked</b>  »  {checked}\n"
        f"💎 <b>Charged</b>  »  {len(results['charged'])}\n"
        f"✅ <b>Approved</b> »  {len(results['approved'])}\n"
        f"⚠️ <b>3DS</b>      »  {len(results.get('tds', []))}\n"
        f"<code>{bar}</code>"
        f"{latest}"
    )
    await raw_edit(user_id, message_id, text, rows_stop())

def _file_row(label, r):
    gate  = r.get('gateway', 'Shopify')
    price = r.get('price', '-')
    bi    = r.get('bin_info')
    if bi:
        brand, btype, level, bank, country, flag = bi
        bank_line = f"  Bank    : {bank} | {country} {flag} | {brand} {btype} {level}\n"
    else:
        bank_line = ""
    return (
        f"  [{label}]\n"
        f"  CC      : {r['card']}\n"
        f"  Gateway : {gate}\n"
        f"  Amount  : {price}\n"
        f"  Message : {r.get('message','')[:80]}\n"
        + bank_line +
        f"  {'─'*36}\n"
    )


async def send_final_results(user_id, results):
    elapsed  = int(time.time() - results['start_time'])
    h, m, s  = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
    bar      = make_progress_bar(results['total'], results['total'])
    ch_count = len(results['charged'])
    ap_count = len(results['approved'])
    td_count = len(results.get('tds', []))
    cname    = await get_display_name(user_id)
    summary  = pe(
        f"<b>🔥 Mass Check — Done</b>\n"
        f"<b>{SEP}</b>\n"
        f"📋 <b>Total</b>    »  {results['total']}\n"
        f"💎 <b>Charged</b>  »  {ch_count}\n"
        f"✅ <b>Approved</b> »  {ap_count}\n"
        f"⚠️ <b>3DS</b>      »  {td_count}\n"
        f"<code>{bar}</code>\n"
        f"⏱️ <b>Time</b>     »  {h}h {m}m {s}s\n"
        f"<b>{SEP}</b>\n"
        f"{checker_line(user_id, cname)}\n"
        f"{DEV_LINE}"
    )
    await bot.send_message(user_id, summary, parse_mode='html')

    # ── Charged file (premium hits) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    if results['charged']:
        D = "─" * 44
        lines = [f"{D}\n  {BOT_BRAND}  ◈  💎 CHARGED HITS\n{D}\n\n"]
        for r in results['charged']:
            lines.append(_file_row("💎 CHARGED", r))
        lines.append(f"\n  Charged  »  {ch_count}\n{D}\n")
        async with aiofiles.open("charged.txt", 'w') as f:
            await f.write("".join(lines))
        await bot.send_file(user_id, "charged.txt",
                            caption=pe(f"💎 <b>Charged Hits  »  {ch_count}</b>\n{DEV_LINE}"),
                            parse_mode='html')
        try: os.remove("charged.txt")
        except: pass

    # ── Approved / 3DS combined file ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━─────────────
    combo = results['approved'] + results.get('tds', [])
    if combo:
        D = "─" * 44
        lines = [f"{D}\n  {BOT_BRAND}  ◈  HITS FILE\n{D}\n\n"]
        if results['approved']:
            lines.append(f"  ── ✅ APPROVED  ({ap_count}) ────────────────────\n\n")
            for r in results['approved']:
                lines.append(_file_row("✅ APPROVED", r))
        if results.get('tds'):
            lines.append(f"\n  ── ⚠️  3DS  ({td_count}) ──────────────────────\n\n")
            for r in results['tds']:
                lines.append(_file_row("⚠️ 3DS", r))
        lines.append(f"\n{D}\n  Approved: {ap_count}  ·  3DS: {td_count}\n{D}\n")
        async with aiofiles.open("approved.txt", 'w') as f:
            await f.write("".join(lines))
        caption = pe(
            f"✅ <b>Approved File</b>\n"
            f"✅ <b>Approved</b>  »  {ap_count}  ·  ⚠️ <b>3DS</b>  »  {td_count}\n"
            f"{DEV_LINE}"
        )
        await bot.send_file(user_id, "approved.txt", caption=caption, parse_mode='html')
        try: os.remove("approved.txt")
        except: pass

    # ── Failed cards file (error.txt) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    error_path = os.path.join(os.path.dirname(__file__), 'error.txt')
    try:
        async with aiofiles.open(error_path, 'r') as f:
            err_content = await f.read()
        err_lines = [l for l in err_content.strip().splitlines() if l.strip()]
        if err_lines:
            await bot.send_file(
                user_id, error_path,
                caption=pe(
                    f"❌ <b>Failed Cards  »  {len(err_lines)}</b>\n"
                    f"⚠️ Cards that errored after all retries\n"
                    f"{DEV_LINE}"
                ),
                parse_mode='html'
            )
    except FileNotFoundError:
        pass
    except Exception:
        pass

async def run_mass_check(user_id, cards, progress_msg_id):
    session_key = f"{user_id}_{progress_msg_id}"
    clear_session_bad_sites()   # reset bad-site list for this session
    clear_error_log()           # reset failed-cards log for this session
    active_sessions[session_key] = {'paused': False}
    all_results = {
        'charged': [], 'approved': [], 'dead': [], 'tds': [],
        'total': len(cards), 'start_time': time.time(), 'last_card_time': time.time(),
    }
    # Load all proxies once and cycle through ALL of them in order
    proxy_pool  = list(get_proxies_for_user(user_id) or load_proxies())
    proxy_iter  = itertools.cycle(proxy_pool) if proxy_pool else None
    proxy_lock  = asyncio.Lock()

    async def _next_proxy():
        if not proxy_iter:
            return None
        async with proxy_lock:
            return next(proxy_iter)

    try:
        queue       = asyncio.Queue()
        last_update = [time.time()]
        for c in cards:
            queue.put_nowait(c)

        async def worker():
            while not queue.empty() and session_key in active_sessions:
                sess = active_sessions.get(session_key)
                if not sess:
                    break
                while sess.get('paused', False):
                    await asyncio.sleep(1)
                    sess = active_sessions.get(session_key)
                    if not sess:
                        return
                try:
                    card = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                cur_sites  = load_sites()
                start_proxy = await _next_proxy()
                if not cur_sites or not proxy_pool:
                    break
                t0  = time.time()
                res = await check_card_with_retry(card, cur_sites, proxy_pool, max_retries=1, start_proxy=start_proxy)
                res['time'] = round(time.time() - t0, 2)
                all_results['last_card_time'] = t0
                rmsg = res.get('message', '')
                if res['status'] == 'Charged':
                    all_results['charged'].append(res)
                    await send_realtime_hit(user_id, res, 'Charged')
                elif res['status'] == 'Approved':
                    if _is_3ds(rmsg):
                        all_results['tds'].append(res)
                    else:
                        all_results['approved'].append(res)
                        await send_realtime_hit(user_id, res, 'Approved')
                else:
                    if _is_insuf(rmsg):
                        all_results['approved'].append(res)
                        await send_insufficient_log(user_id, res)
                    elif _is_3ds(rmsg):
                        all_results['tds'].append(res)
                    else:
                        all_results['dead'].append(res)
                queue.task_done()
                checked = (len(all_results['charged'])
                           + len(all_results['approved'])
                           + len(all_results['dead'])
                           + len(all_results['tds']))
                now = time.time()
                if now - last_update[0] >= 1.0:
                    last_update[0] = now
                    if session_key in active_sessions:
                        try:
                            await update_mass_progress(user_id, progress_msg_id,
                                                       all_results, checked, res)
                        except:
                            pass

        workers = [asyncio.create_task(worker()) for _ in range(MASS_WORKERS)]
        while workers:
            if session_key not in active_sessions:
                for w in workers:
                    if not w.done():
                        w.cancel()
                break
            done, pending = await asyncio.wait(workers, timeout=1.0)
            workers = list(pending)
    except Exception as e:
        await bot.send_message(user_id, pe(f"⚠️ Error: {e}"), parse_mode='html')
    finally:
        if session_key in active_sessions:
            del active_sessions[session_key]
        try:
            await asyncio.to_thread(_raw_post, f"{TG_API}/deleteMessage",
                {"chat_id": user_id, "message_id": progress_msg_id})
        except:
            pass
        await send_final_results(user_id, all_results)

# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN PANEL HELPER
# ═══════════════════════════════════════════════════════════════════════════════

def _admin_panel_text():
    pcount  = len(load_premium_users())
    scount  = len(load_sites())
    prcount = len(load_proxies())
    return pe(
        f"<b>👑 Admin Panel</b>\n"
        f"<b>{SEP}</b>\n"
        f"<blockquote>"
        f"🟢 <b>Status</b>   »  Online\n"
        f"👤 <b>Users</b>    »  {pcount}\n"
        f"🌐 <b>Sites</b>    »  {scount}\n"
        f"🔑 <b>Proxies</b>  »  {prcount}"
        f"</blockquote>\n"
        f"<b>{SEP}</b>\n"
        f"{DEV_LINE}"
    )

# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════


# ── /start ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━────────────
@bot.on(events.NewMessage(pattern='/start'))
async def start(event):
    uid      = event.sender_id
    chat_id  = event.chat_id
    in_group = (chat_id != uid)

    if not in_group:
        try:
            rm_msg = await bot.send_message(uid, "\u200b", buttons=Button.clear())
            await asyncio.sleep(0.3)
            await bot.delete_messages(uid, rm_msg.id)
        except Exception:
            try:
                from keyboards import _http_session
                await asyncio.to_thread(
                    _http_session.post,
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    data={
                        "chat_id": str(uid),
                        "text": "\u200b",
                        "reply_markup": json.dumps({"remove_keyboard": True}),
                    },
                    timeout=8,
                )
            except Exception:
                pass
    try:
        sender    = await event.get_sender()
        username  = f"@{sender.username}" if sender.username else f"ID:{uid}"
        firstname = sender.first_name or "User"
    except:
        username  = f"ID:{uid}"
        firstname = "User"
    tier = get_user_tier(uid)
    trem = time_remaining(uid)
    if is_admin(uid):           status_line = "Admin"
    elif is_key_admin(uid):     status_line = "Key Admin"
    elif tier == "auth":        status_line = f"Auth — {trem} left"   if trem else "Auth Expired"
    elif tier == "grant":       status_line = f"Grant — {trem} left"  if trem else "Grant Expired"
    elif tier in PLAN_TIERS:
        info = PLAN_TIERS[tier]
        status_line = f"{info['emoji']} {info['label']} — {trem} left" if trem else f"{info['emoji']} {info['label']} Expired"
    elif tier:                  status_line = f"{tier.capitalize()} — {trem} left" if trem else f"{tier.capitalize()} Expired"
    else:                       status_line = "No Access"
    lim  = get_user_limit(uid)
    text = pe(
        f"<b>⚡ Welcome, {firstname}!</b>\n"
        f"<b>{SEP}</b>\n"
        f"<blockquote>"
        f"👤 <b>User</b>    »  {username}\n"
        f"🔑 <b>ID</b>      »  <code>{uid}</code>\n"
        f"💎 <b>Status</b>  »  {status_line}\n"
        f"📋 <b>Limit</b>   »  {lim if lim else 'N/A'} cards"
        f"</blockquote>\n"
        f"<b>{SEP}</b>\n"
        f"Select an option below to get started."
    )
    dest = chat_id if in_group else uid
    await raw_send(dest, text, rows_main(),
                   reply_to=event.message.id if in_group else None)

# ── /sh — single check ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@bot.on(events.NewMessage(pattern=r'^/sh\s+'))
async def single_check(event):
    uid = event.sender_id
    if not is_premium(uid):
        await event.reply(pe("❌ <b>Access Denied.</b> Only premium users can use this bot."), parse_mode='html')
        return
    sites   = load_sites()
    proxies = get_proxies_for_user(uid) or load_proxies()
    if not sites:
        await event.reply(pe("❌ No sites available. Contact admin."), parse_mode='html'); return
    if not proxies:
        await event.reply(pe("❌ <b>No proxy set!</b>\n\nAdd your proxy first:\n<code>/setproxy ip:port</code>\nor\n<code>/setproxy ip:port:user:pass</code>"), parse_mode='html'); return
    cards = extract_cc(event.message.text.split(' ', 1)[1].strip())
    if not cards:
        await event.reply(pe("❌ Invalid format. Use: <code>/sh card|mm|yy|cvv</code>"), parse_mode='html'); return
    card = cards[0]
    smsg = await event.reply(
        pe(f"⚡ <b>Checking...</b>\n"
           f"<b>{SEP}</b>\n"
           f"🃏 <tg-spoiler><code>{card}</code></tg-spoiler>"),
        parse_mode='html',
    )
    try:
        t0 = time.time()
        (result, bin_info), (name, username) = await asyncio.gather(
            asyncio.gather(
                check_card_with_retry(card, sites, proxies, max_retries=3),
                get_bin_info(card.split('|')[0]),
            ),
            get_user_info(uid),
        )
        has_username = bool(username)
        cname        = name if has_username else str(uid)
        result['time'] = round(time.time() - t0, 2)
        resp = build_result_card(result, bin_info, uid, cname, show_uid=False)
        await raw_edit(uid, smsg.id, resp, [])
        if result.get('status') in ('Charged', 'Approved'):
            asyncio.create_task(forward_to_log_channel(result, bin_info, uid, cname))
        if result.get('status') == 'Charged':
            await asyncio.to_thread(_pin_message_botapi, uid, smsg.id)

    except Exception as e:
        await smsg.edit(pe(f"❌ Error: {e}"), parse_mode='html')

# ── /setproxy — per-user proxy ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━──────
@bot.on(events.NewMessage(pattern=r'^/setproxy(\s+[\s\S]+)?$'))
async def setproxy_command(event):
    uid = event.sender_id
    if not is_premium(uid):
        await event.reply(pe("❌ <b>Access Denied.</b>"), parse_mode='html'); return
    raw = event.message.text[len('/setproxy'):].strip()
    if not raw:
        curr_list = get_user_proxy_list(uid)
        curr_disp = f"{len(curr_list)} proxy(ies)" if curr_list else "Not set"
        pool      = load_proxies()
        await event.reply(
            pe(f"<b>⚙️ Your Proxies</b>\n<b>{SEP}</b>\n"
               f"🔌 <b>Personal:</b> {curr_disp}\n"
               f"📋 <b>Pool:</b> {len(pool)} proxies\n<b>{SEP}</b>\n"
               f"<b>Add proxies (one per line — all become yours, rotate automatically):</b>\n"
               f"<code>/setproxy proxy1:port</code>\n"
               f"<code>/setproxy\nproxy1:port\nproxy2:port:user:pass\nproxy3:port</code>\n\n"
               f"Clear: <code>/clearuserproxy</code>"),
            parse_mode='html',
        )
        return
    lines = [l.strip() for l in raw.split('\n') if l.strip()]
    if not lines:
        await event.reply(pe("❌ No valid proxy found."), parse_mode='html'); return
    existing = get_user_proxy_list(uid)
    merged   = existing + [p for p in lines if p not in existing]
    set_user_proxies(uid, merged)
    added = len(merged) - len(existing)
    await event.reply(pe(
        f"✅ <b>Proxies Saved!</b>\n"
        f"🔌 <b>Total personal proxies:</b> {len(merged)}\n"
        f"📥 <b>Added:</b> {added}   🔁 Rotating automatically"
    ), parse_mode='html')

@bot.on(events.NewMessage(pattern=r'^/clearuserproxy$'))
async def clearuserproxy_command(event):
    uid = event.sender_id
    if not is_premium(uid):
        await event.reply(pe("❌ <b>Access Denied.</b>"), parse_mode='html'); return
    remove_user_proxy(uid)
    await event.reply(pe("✅ <b>Your proxy cleared!</b> Will use proxy pool now."), parse_mode='html')

# ── .txt file auto-detection ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━────────
@bot.on(events.NewMessage(func=lambda e: e.file and e.file.name and e.file.name.endswith('.txt') and not e.via_bot_id))
async def txt_detected(event):
    uid = event.sender_id
    if not is_premium(uid):
        return
    fp = await event.download_media()
    try:
        async with aiofiles.open(fp, 'r', encoding='utf-8', errors='ignore') as f:
            content = await f.read()
    finally:
        try: os.remove(fp)
        except: pass
    cards = extract_cc(content)
    if not cards:
        await event.reply(pe("❌ No valid cards found in this file."), parse_mode='html'); return
    proxies = get_proxies_for_user(uid) or load_proxies()
    if not proxies:
        await event.reply(pe("❌ <b>No proxy set!</b>\n\nAdd your proxy first:\n<code>/setproxy ip:port</code>\nor\n<code>/setproxy ip:port:user:pass</code>"), parse_mode='html'); return
    limit = get_user_limit(uid)
    if len(cards) > limit:
        cards = cards[:limit]
        await event.reply(
            pe(f"⚠️ <b>File trimmed to {limit} cards</b> (your {get_user_tier(uid)} plan limit)."),
            parse_mode='html',
        )
    pending_checks[uid] = {'cards': cards}
    preview_lines = "\n".join(
        [f'⭐ <tg-spoiler>{c}</tg-spoiler>' for c in cards[:3]]
    )
    more = f"\n<i>And {len(cards)-3} more...</i>" if len(cards) > 3 else ""
    text = pe(f"{preview_lines}{more}\n\n<b>🔥 TAP BELOW TO CHECK</b>")
    await raw_send(
        uid, text,
        [[{"text": "💳  Check this CC", "callback_data": f"start_check_{uid}"}]],
        reply_to=event.message.id,
    )

# ── /msh & /chk — mass check ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━───────
@bot.on(events.NewMessage(pattern=r'^/(msh|chk)$'))
async def mass_check_cmd(event):
    uid = event.sender_id
    if not is_premium(uid):
        await event.reply(pe("❌ <b>Access Denied.</b>"), parse_mode='html'); return
    if not event.reply_to_msg_id:
        await event.reply(pe("⚡ Reply to a <code>.txt</code> file, or just send the file directly!"), parse_mode='html'); return
    reply = await event.get_reply_message()
    if not reply.file or not reply.file.name.endswith('.txt'):
        await event.reply(pe("❌ Please reply to a <code>.txt</code> file."), parse_mode='html'); return
    sites   = load_sites()
    proxies = get_proxies_for_user(uid) or load_proxies()
    if not sites:
        await event.reply(pe("❌ No sites available."), parse_mode='html'); return
    if not proxies:
        await event.reply(pe("❌ <b>No proxy set!</b>\n\nAdd your proxy first:\n<code>/setproxy ip:port</code>\nor\n<code>/setproxy ip:port:user:pass</code>"), parse_mode='html'); return
    fp = await reply.download_media()
    async with aiofiles.open(fp, 'r', encoding='utf-8', errors='ignore') as f:
        content = await f.read()
    cards = extract_cc(content)
    try: os.remove(fp)
    except: pass
    if not cards:
        await event.reply(pe("❌ No valid cards found."), parse_mode='html'); return
    limit = get_user_limit(uid)
    if len(cards) > limit:
        cards = cards[:limit]
        await event.reply(
            pe(f"⚠️ <b>File trimmed to {limit} cards</b> (your {get_user_tier(uid)} plan limit)."),
            parse_mode='html',
        )
    text = pe(
        f"<b>🔥 Mass Check — Starting</b>\n"
        f"<b>{SEP}</b>\n"
        f"📋 <b>Total</b>    »  {len(cards)}\n"
        f"☄️ <b>Checked</b>  »  0\n"
        f"💎 <b>Charged</b>  »  0\n"
        f"✅ <b>Approved</b> »  0\n"
        f"⚠️ <b>3DS</b>      »  0\n"
        f"<code>{make_progress_bar(0, len(cards))}</code>"
    )
    msg_id = await raw_send(uid, text, rows_stop(), reply_to=event.message.id)
    if msg_id:
        asyncio.create_task(run_mass_check(uid, cards, msg_id))

# ── Proxy pool commands ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━─────────────
@bot.on(events.NewMessage(pattern=r'^/addproxy'))
async def add_proxy_command(event):
    uid = event.sender_id
    if not is_admin(uid):
        await event.reply(pe("❌ <b>Admin only.</b>"), parse_mode='html'); return
    content = event.message.text[len('/addproxy'):].strip()
    if not content:
        await event.reply(pe(
            f"❌ <b>Usage:</b>\n<b>{SEP}</b>\n"
            f"<code>/addproxy ip:port</code>\n"
            f"<code>/addproxy ip:port:user:pass</code>\n"
            f"<code>/addproxy socks5://ip:port</code>\n"
            f"<code>/addproxy http://user:pass@ip:port</code>\n\n"
            f"Multiple proxies — one per line:\n"
            f"<code>/addproxy\nip:port\nip:port:user:pass</code>"
        ), parse_mode='html'); return
    new  = [l.strip() for l in content.split('\n') if l.strip()] if '\n' in content else [content.strip()]
    curr = load_proxies()
    added = [p for p in new if p not in curr]
    dups  = len(new) - len(added)
    if not added:
        await event.reply(pe("⚠️ All proxies already in pool."), parse_mode='html'); return
    async with aiofiles.open(PROXY_FILE, 'a') as f:
        for p in added: await f.write(f"{p}\n")
    dup_note = f"\n⚠️ {dups} duplicate(s) skipped." if dups else ""
    await event.reply(pe(
        f"✅ <b>Added {len(added)} {'proxy' if len(added)==1 else 'proxies'} to pool!</b>{dup_note}\n"
        f"📋 <b>Pool now:</b> {len(curr)+len(added)} proxies"
    ), parse_mode='html')

@bot.on(events.NewMessage(pattern=r'^/rmproxy\s+'))
async def remove_single_proxy(event):
    uid = event.sender_id
    if not is_premium(uid):
        await event.reply(pe("❌ <b>Access Denied.</b>"), parse_mode='html'); return
    p    = event.message.text.split(' ', 1)[1].strip()
    curr = load_proxies()
    if p not in curr:
        await event.reply(pe(f"❌ Proxy not found: <code>{p}</code>"), parse_mode='html'); return
    async with aiofiles.open(PROXY_FILE, 'w') as f:
        for x in curr:
            if x != p: await f.write(f"{x}\n")
    await event.reply(pe(f"✅ <b>Proxy removed!</b>\n<code>{p}</code>"), parse_mode='html')

@bot.on(events.NewMessage(pattern=r'^/clearproxy$'))
async def clear_all_proxies(event):
    uid = event.sender_id
    if not is_premium(uid):
        await event.reply(pe("❌ <b>Access Denied.</b>"), parse_mode='html'); return
    curr = load_proxies()
    if not curr:
        await event.reply(pe("❌ proxy.txt is already empty."), parse_mode='html'); return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bk = f"proxy_backup_{uid}_{ts}.txt"
    async with aiofiles.open(bk, 'w') as f:
        for p in curr: await f.write(f"{p}\n")
    await event.reply(pe(f"📋 <b>Backup — {len(curr)} proxies:</b>"), file=bk, parse_mode='html')
    try: os.remove(bk)
    except: pass
    async with aiofiles.open(PROXY_FILE, 'w') as f: await f.write("")
    await event.reply(pe(f"✅ <b>Cleared all {len(curr)} proxies!</b>"), parse_mode='html')

@bot.on(events.NewMessage(pattern=r'^/getproxy$'))
async def get_all_proxies(event):
    uid = event.sender_id
    if not is_premium(uid):
        await event.reply(pe("❌ <b>Access Denied.</b>"), parse_mode='html'); return
    curr = load_proxies()
    if not curr:
        await event.reply(pe("❌ No proxies in proxy.txt."), parse_mode='html'); return
    if len(curr) <= 50:
        lines = "\n".join([f"{i+1}. <code>{p}</code>" for i, p in enumerate(curr)])
        await event.reply(pe(f"<b>📋 Proxies ({len(curr)}):</b>\n\n{lines}"), parse_mode='html')
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fn = f"proxies_{uid}_{ts}.txt"
        async with aiofiles.open(fn, 'w') as f:
            for i, p in enumerate(curr): await f.write(f"{i+1}. {p}\n")
        await event.reply(pe(f"<b>📋 Total Proxies: {len(curr)}</b>"), file=fn, parse_mode='html')
        try: os.remove(fn)
        except: pass

@bot.on(events.NewMessage(pattern=r'^/getworkingproxy$'))
async def get_working_proxies(event):
    uid = event.sender_id
    if not is_admin(uid):
        await event.reply(pe("❌ <b>Admin only.</b>"), parse_mode='html'); return
    if not os.path.exists(WORKING_PROXY_FILE):
        await event.reply(pe("❌ No working proxies saved yet. They are collected automatically on each hit."), parse_mode='html'); return
    proxies = [l.strip() for l in open(WORKING_PROXY_FILE) if l.strip()]
    if not proxies:
        await event.reply(pe("❌ working_proxies.txt is empty."), parse_mode='html'); return
    if len(proxies) <= 30:
        lines = "\n".join(f"<code>{p}</code>" for p in proxies)
        await event.reply(pe(f"<b>✅ Working Proxies ({len(proxies)}):</b>\n\n{lines}"), parse_mode='html')
    else:
        await event.reply(pe(f"<b>✅ Working Proxies: {len(proxies)}</b>"), file=WORKING_PROXY_FILE, parse_mode='html')

@bot.on(events.NewMessage(pattern=r'^/clearworkingproxy$'))
async def clear_working_proxies(event):
    uid = event.sender_id
    if not is_admin(uid):
        await event.reply(pe("❌ <b>Admin only.</b>"), parse_mode='html'); return
    try:
        open(WORKING_PROXY_FILE, 'w').close()
        await event.reply(pe("✅ <b>Working proxies list cleared.</b>"), parse_mode='html')
    except Exception as e:
        await event.reply(pe(f"❌ Error: {e}"), parse_mode='html')

@bot.on(events.NewMessage(pattern=r'^/getuserproxy(\s+\d+)?$'))
async def get_user_proxy_cmd(event):
    uid = event.sender_id
    if not is_admin(uid):
        await event.reply(pe("❌ <b>Admin only.</b>"), parse_mode='html'); return
    parts = event.message.text.strip().split()
    if len(parts) < 2:
        try:
            data = json.load(open(USER_PROXY_FILE)) if os.path.exists(USER_PROXY_FILE) else {}
        except:
            data = {}
        if not data:
            await event.reply(pe("❌ No user proxies saved."), parse_mode='html'); return
        lines = "\n".join(f"{uid}: {proxy}" for uid, proxy in data.items())
        ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
        fn    = f"user_proxies_{ts}.txt"
        with open(fn, 'w') as f: f.write(lines)
        await event.reply(pe(f"<b>👤 User Proxies ({len(data)} users):</b>"), file=fn, parse_mode='html')
        try: os.remove(fn)
        except: pass
    else:
        target = int(parts[1])
        proxy  = get_user_proxy(target)
        if proxy:
            await event.reply(pe(f"<b>👤 Proxy for <code>{target}</code>:</b>\n<code>{proxy}</code>"), parse_mode='html')
        else:
            await event.reply(pe(f"❌ No proxy set for <code>{target}</code>."), parse_mode='html')

@bot.on(events.NewMessage(pattern=r'^/chkproxy\s+'))
async def check_single_proxy(event):
    uid = event.sender_id
    if not is_premium(uid):
        await event.reply(pe("❌ <b>Access Denied.</b>"), parse_mode='html'); return
    proxy = event.message.text.split(' ', 1)[1].strip()
    smsg  = await event.reply(pe(f"⚡ Testing <code>{proxy}</code>..."), parse_mode='html')
    r = await test_proxy(proxy)
    if r['status'] == 'alive':
        ip      = await get_proxy_ip(proxy)
        ip_line = f"\n🌐 <b>IP:</b> <code>{ip}</code>" if ip else ""
        await smsg.edit(pe(f"✅ <b>Proxy ALIVE!</b>{ip_line}\n<code>{proxy}</code>"), parse_mode='html')
    else:
        await smsg.edit(pe(f"❌ <b>Proxy DEAD!</b>\n<code>{proxy}</code>"), parse_mode='html')

# ── Site commands ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━─────
@bot.on(events.NewMessage(pattern='/site'))
async def site_command(event):
    uid = event.sender_id
    if not is_premium(uid):
        await event.reply(pe("❌ <b>Access Denied.</b>"), parse_mode='html'); return
    sites   = load_sites()
    proxies = get_proxies_for_user(uid) or load_proxies()
    if not sites:
        await event.reply(pe("❌ sites.txt is empty."), parse_mode='html'); return
    if not proxies:
        await event.reply(pe("❌ No proxies available."), parse_mode='html'); return
    smsg = await event.reply(pe(f"🔥 Checking {len(sites)} sites..."), parse_mode='html')
    alive, dead = [], []
    for i in range(0, len(sites), 10):
        batch   = sites[i:i+10]
        results = await asyncio.gather(*[test_site(s, random.choice(proxies)) for s in batch])
        for r in results:
            (alive if r['status'] == 'alive' else dead).append(r['site'])
        await smsg.edit(pe(f"🔥 Checking sites...\n✅ Alive: {len(alive)} | ❌ Dead: {len(dead)}"), parse_mode='html')
    async with aiofiles.open(SITES_FILE, 'w') as f:
        for s in alive: await f.write(f"{s}\n")
    await smsg.edit(pe(f"✅ <b>Site Check Done!</b>\n\n✅ Alive: {len(alive)}\n❌ Removed: {len(dead)}"), parse_mode='html')

@bot.on(events.NewMessage(pattern=r'^/(rm|rmsite)\s+'))
async def remove_site_command(event):
    uid = event.sender_id
    if not is_premium(uid):
        await event.reply(pe("❌ <b>Access Denied.</b>"), parse_mode='html'); return
    url  = event.message.text.split(' ', 1)[1].strip()
    curr = load_sites()
    if url not in curr:
        await event.reply(pe(f"❌ Site not found."), parse_mode='html'); return
    async with aiofiles.open(SITES_FILE, 'w') as f:
        for s in curr:
            if s != url: await f.write(f"{s}\n")
    await event.reply(pe(f"✅ <b>Site removed!</b>"), parse_mode='html')

@bot.on(events.NewMessage(pattern='/proxy'))
async def proxy_command(event):
    uid = event.sender_id
    if not is_premium(uid):
        await event.reply(pe("❌ <b>Access Denied.</b>"), parse_mode='html'); return
    proxies = load_proxies()
    if not proxies:
        await event.reply(pe("❌ proxy.txt is empty."), parse_mode='html'); return
    smsg = await event.reply(pe(f"🔥 Checking {len(proxies)} proxies..."), parse_mode='html')
    alive, dead = [], []
    for i in range(0, len(proxies), 50):
        results = await asyncio.gather(*[test_proxy(p) for p in proxies[i:i+50]])
        for r in results:
            (alive if r['status'] == 'alive' else dead).append(r['proxy'])
        await smsg.edit(pe(f"🔥 Checking proxies...\n✅ Alive: {len(alive)} | ❌ Dead: {len(dead)}"), parse_mode='html')
    async with aiofiles.open(PROXY_FILE, 'w') as f:
        for p in alive: await f.write(f"{p}\n")
    await smsg.edit(pe(f"✅ <b>Proxy Check Done!</b>\n✅ Alive: {len(alive)}\n❌ Removed: {len(dead)}"), parse_mode='html')







@bot.on(events.NewMessage(pattern=r'^/myplan$'))
async def myplan_command(event):
    uid  = event.sender_id
    tier = get_user_tier(uid)
    trem = time_remaining(uid)
    lim  = get_user_limit(uid)
    if is_admin(uid):
        await event.reply(
            pe(f"<b>👑 Admin Panel</b>\n<b>{SEP}</b>\n⭐ Unlimited access\n🔥 Limit: 5000 cards/file"),
            parse_mode='html',
        ); return
    if not tier:
        await event.reply(
            pe(f"❌ <b>No Active Plan</b>\n<b>{SEP}</b>\nRedeem a key: <code>/redeem {KEY_PREFIX}-XXXX</code>\nContact admin for access."),
            parse_mode='html',
        ); return
    acc = _user_access.get(uid, {})
    exp = acc.get('expires_at', '')[:10]
    await event.reply(pe(
        f"<b>📋 My Plan</b>\n"
        f"<b>{SEP}</b>\n"
        f"💎 <b>Tier:</b> {tier.capitalize()}\n"
        f"📅 <b>Expires:</b> {exp}\n"
        f"⏳ <b>Remaining:</b> {trem or 'Expired'}\n"
        f"📋 <b>Limit:</b> {lim} cards/file\n"
        f"<b>{SEP}</b>\n"
        f"{DEV_LINE}"
    ), parse_mode='html')

# ── Redeemable access keys ────────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern=r'^/genkey(?:\s+.*)?$'))
async def genkey_command(event):
    uid = event.sender_id
    if not (is_admin(uid) or is_key_admin(uid)):
        await event.reply(pe("❌ <b>Admin only.</b>"), parse_mode='html')
        return

    parts = event.message.text.split()
    if len(parts) not in (2, 3):
        tiers = ", ".join(sorted(VALID_TIERS))
        await event.reply(pe(
            f"❌ <b>Usage:</b> <code>/genkey [tier] [days]</code>\n"
            f"💎 <b>Tiers:</b> <code>{tiers}</code>\n"
            f"ℹ️ Days are optional; example: <code>/genkey silver</code>"
        ), parse_mode='html')
        return

    tier = parts[1].lower()
    if tier not in VALID_TIERS:
        tiers = ", ".join(sorted(VALID_TIERS))
        await event.reply(
            pe(f"❌ Invalid tier. Choose: <code>{tiers}</code>"),
            parse_mode='html',
        )
        return

    try:
        days = int(parts[2]) if len(parts) == 3 else int(PLAN_TIERS[tier]['days'])
        if days <= 0 or days > 3650:
            raise ValueError
    except (TypeError, ValueError):
        await event.reply(
            pe("❌ Days must be a number from <code>1</code> to <code>3650</code>."),
            parse_mode='html',
        )
        return

    key = generate_key()
    while key in _keys_data:
        key = generate_key()
    _keys_data[key] = {
        "tier": tier,
        "plan_days": days,
        "created_by": uid,
        "created_at": _now_utc().isoformat(),
        "redeemed_by": None,
        "redeemed_at": None,
    }
    save_keys()

    info = PLAN_TIERS[tier]
    await event.reply(pe(
        f"✅ <b>Access Key Generated</b>\n"
        f"<b>{SEP}</b>\n"
        f"🔑 <code>{key}</code>\n"
        f"💎 <b>Plan:</b> {info['label']}\n"
        f"📅 <b>Duration:</b> {days} day{'s' if days != 1 else ''}\n"
        f"<b>{SEP}</b>\n"
        f"Redeem with: <code>/redeem {key}</code>"
    ), parse_mode='html')


@bot.on(events.NewMessage(pattern=r'^/redeem(?:\s+.*)?$'))
async def redeem_command(event):
    uid = event.sender_id
    parts = event.message.text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        await event.reply(
            pe(f"❌ <b>Usage:</b> <code>/redeem {KEY_PREFIX}-XXXX</code>"),
            parse_mode='html',
        )
        return

    key = parts[1].strip()
    key_data = _keys_data.get(key)
    if not isinstance(key_data, dict):
        await event.reply(pe("❌ <b>Invalid access key.</b>"), parse_mode='html')
        return
    if key_data.get("redeemed_by") is not None:
        await event.reply(pe("❌ <b>This key has already been redeemed.</b>"), parse_mode='html')
        return

    tier = str(key_data.get("tier", "")).lower()
    try:
        days = int(key_data.get("plan_days", 0))
    except (TypeError, ValueError):
        days = 0
    if tier not in VALID_TIERS or days <= 0:
        await event.reply(pe("❌ <b>This access key is invalid.</b>"), parse_mode='html')
        return

    set_user_access(uid, tier, days, granted_by=f"key:{key}")
    key_data["redeemed_by"] = uid
    key_data["redeemed_at"] = _now_utc().isoformat()
    save_keys()

    info = PLAN_TIERS[tier]
    await event.reply(pe(
        f"✅ <b>Key Redeemed Successfully!</b>\n"
        f"<b>{SEP}</b>\n"
        f"💎 <b>Plan:</b> {info['label']}\n"
        f"📅 <b>Duration:</b> {days} day{'s' if days != 1 else ''}\n"
        f"📋 <b>Limit:</b> {TIER_LIMITS[tier]} cards/file"
    ), parse_mode='html')


# ── Test commands (admin only) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━──────
@bot.on(events.NewMessage(pattern=r'^/testcards$'))
async def testcards_command(event):
    uid = event.sender_id
    if uid != ADMIN_ID:
        await event.reply(pe("❌ <b>Admin only.</b>"), parse_mode='html'); return
    fake_bin = ("VISA", "Credit", "Classic", "Test Bank", "United States", "🇺🇸")
    statuses = [
        ("Charged",  "Payment captured successfully — $1.00 auth charge",   "Shopify Payments", "$1.00"),
        ("Approved", "Card approved — 3DS not required",                     "Shopify Payments", "$0.00"),
        ("OTP",      "3D Secure authentication required by issuing bank",    "Shopify Payments", "-"),
        ("Declined", "Your card was declined. Please try a different card.", "Shopify Payments", "-"),
    ]
    await event.reply(pe(f"<b>🧪 Test Cards Preview</b> — {len(statuses)} result types"), parse_mode='html')
    for status, message, gateway, price in statuses:
        fake_result = {
            'status': status, 'message': message,
            'card': "4111111111111111|12|2026|123",
            'gateway': gateway, 'price': price,
        }
        card_msg = build_result_card(fake_result, fake_bin, uid, "VXO Checker")
        await raw_send(uid, card_msg, [])
        await asyncio.sleep(0.4)

@bot.on(events.NewMessage(pattern=r'^/testnotif$'))
async def testnotif_command(event):
    uid = event.sender_id
    if not is_admin(uid):
        await event.reply(pe("❌ <b>Admin only.</b>"), parse_mode='html'); return
    fake_result = {
        'status': 'Charged', 'message': 'TEST — ORDER PLACED',
        'card': '4111111111111111|12|2026|123',
        'gateway': 'Shopify Payments', 'price': '$1.00',
        'receipt_url': '', 'proxy': '',
    }
    fake_bin  = ("VISA", "Credit", "Classic", "Test Bank", "United States", "🇺🇸")
    card_text = build_result_card(fake_result, fake_bin, uid, "VXO Checker")
    notif = pe(
        f"<b>🔥 TEST NOTIFICATION — {BOT_BRAND}</b>\n"
        f"<b>{'═'*24}</b>\n"
        f"<b>By:</b> <a href='tg://user?id={uid}'>Admin</a> (<code>{uid}</code>)\n"
        f"<b>{'─'*24}</b>\n"
    ) + "\n" + card_text

    admin_targets = list((ADMIN_IDS | _DEFAULT_ADMINS) - {uid, 1001003902149848})
    results = {}
    for t in admin_targets:
        r = _raw_post(f"{TG_API}/sendMessage", {
            "chat_id": t, "text": notif,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        })
        results[t] = "✅" if r.get("ok") else f"❌ {r.get('description','err')}"

    admin_lines = "\n".join(f"<code>{t}</code>: {s}" for t, s in results.items())
    await event.reply(pe(
        f"<b>📡 Admin Results:</b>\n{admin_lines}"
    ), parse_mode='html')




# ── /grant & /auth — grant timed access (admin) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━───
@bot.on(events.NewMessage(pattern=r'^/grant(\s+.*)?$'))
async def grant_command(event):
    if not is_admin(event.sender_id):
        await event.reply(pe("❌ <b>Admin only.</b>"), parse_mode='html'); return
    parts = event.message.text.split()
    if len(parts) < 3:
        await event.reply(pe("❌ Usage: <code>/grant [user_id] [days]</code>"), parse_mode='html'); return
    try:
        target_uid = int(parts[1]); days = int(parts[2])
        if days < 1: raise ValueError
    except:
        await event.reply(pe("❌ Invalid. Example: <code>/grant 123456789 2</code>"), parse_mode='html'); return
    set_user_access(target_uid, "grant", days)
    trem = time_remaining(target_uid)
    await event.reply(pe(
        f"✅ <b>Grant Issued!</b>\n"
        f"<b>{SEP}</b>\n"
        f"👤 <b>User:</b> <code>{target_uid}</code>\n"
        f"💎 <b>Plan:</b> {days} {'Day' if days == 1 else 'Days'}\n"
        f"⏳ <b>Expires in:</b> {trem}\n"
        f"📋 <b>Limit:</b> {TIER_LIMITS['grant']} cards/file"
    ), parse_mode='html')
@bot.on(events.NewMessage(pattern=r'^/auth(\s+.*)?$'))
async def auth_command(event):
    if not is_admin(event.sender_id):
        await event.reply(pe("❌ <b>Admin only.</b>"), parse_mode='html'); return
    parts = event.message.text.split()
    if len(parts) < 3:
        await event.reply(pe("❌ Usage: <code>/auth [user_id] [days]</code>"), parse_mode='html'); return
    try:
        target_uid = int(parts[1]); days = int(parts[2])
        if days < 1: raise ValueError
    except:
        await event.reply(pe("❌ Invalid. Example: <code>/auth 123456789 2</code>"), parse_mode='html'); return
    set_user_access(target_uid, "auth", days)
    trem = time_remaining(target_uid)
    await event.reply(pe(
        f"✅ <b>Auth Issued!</b>\n"
        f"<b>{SEP}</b>\n"
        f"👤 <b>User:</b> <code>{target_uid}</code>\n"
        f"💎 <b>Plan:</b> {days} {'Day' if days == 1 else 'Days'}\n"
        f"⏳ <b>Expires in:</b> {trem}\n"
        f"📋 <b>Limit:</b> {TIER_LIMITS['auth']} cards/file"
    ), parse_mode='html')
@bot.on(events.NewMessage(pattern=r'^/revoke(\s+.*)?$'))
async def revoke_command(event):
    if not is_admin(event.sender_id):
        await event.reply(pe("❌ <b>Admin only.</b>"), parse_mode='html'); return
    parts = event.message.text.split()
    if len(parts) < 2:
        await event.reply(pe("❌ Usage: <code>/revoke [user_id]</code>"), parse_mode='html'); return
    try: target_uid = int(parts[1])
    except:
        await event.reply(pe("❌ Invalid user ID."), parse_mode='html'); return
    if target_uid not in _user_access:
        await event.reply(pe(f"⚠️ User <code>{target_uid}</code> has no timed access."), parse_mode='html'); return
    revoke_user_access(target_uid)
    await event.reply(pe(f"✅ <b>Revoked!</b>\n<code>{target_uid}</code> access removed."), parse_mode='html')





# ── /admin — dashboard ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@bot.on(events.NewMessage(pattern=r'^/admin$'))
async def admin_panel_cmd(event):
    if not is_admin(event.sender_id):
        await event.reply(pe("❌ <b>Admin only.</b>"), parse_mode='html'); return
    await raw_send(event.sender_id, _admin_panel_text(), rows_admin())

@bot.on(events.NewMessage(pattern=r'^/setadmin(\s+.*)?$'))
async def setadmin_command(event):
    uid = event.sender_id
    if not is_admin(uid):
        await event.reply(pe("❌ <b>Admin only.</b>"), parse_mode='html'); return
    parts = event.message.text.strip().split()
    current_list = "\n".join(f"• <code>{a}</code>" for a in sorted(ADMIN_IDS))
    if len(parts) < 2:
        await event.reply(pe(
            f"<b>👑 Admin Management</b>\n<b>{SEP}</b>\n"
            f"<b>Current admins:</b>\n{current_list}\n<b>{SEP}</b>\n"
            f"<b>Add:</b> <code>/setadmin add [user_id]</code>\n"
            f"<b>Remove:</b> <code>/setadmin rm [user_id]</code>"
        ), parse_mode='html'); return
    action = parts[1].lower()
    if action in ("add", "rm", "remove") and len(parts) >= 3:
        try:
            target = int(parts[2])
        except ValueError:
            await event.reply(pe("❌ <b>Invalid user ID.</b>"), parse_mode='html'); return
        if action == "add":
            ADMIN_IDS.add(target)
            _save_admin_ids(ADMIN_IDS)
            _register_commands()
            await event.reply(pe(f"✅ <b>Admin added:</b> <code>{target}</code>"), parse_mode='html')
        else:
            if target in _DEFAULT_ADMINS:
                await event.reply(pe("❌ <b>Cannot remove a default admin.</b>"), parse_mode='html'); return
            ADMIN_IDS.discard(target)
            _save_admin_ids(ADMIN_IDS)
            _register_commands()
            await event.reply(pe(f"✅ <b>Admin removed:</b> <code>{target}</code>"), parse_mode='html')
    else:
        await event.reply(pe(
            f"<b>👑 Admin Management</b>\n<b>{SEP}</b>\n"
            f"<b>Add:</b> <code>/setadmin add [user_id]</code>\n"
            f"<b>Remove:</b> <code>/setadmin rm [user_id]</code>"
        ), parse_mode='html')



# ── Premium management (legacy) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━─────
@bot.on(events.NewMessage(pattern=r'^/addpremium\s+'))
async def add_premium_command(event):
    if not is_admin(event.sender_id):
        await event.reply(pe("❌ <b>Admin only.</b>"), parse_mode='html'); return
    new_id = event.message.text.split(' ', 1)[1].strip()
    if not new_id.isdigit():
        await event.reply(pe("❌ Usage: <code>/addpremium 123456789</code>"), parse_mode='html'); return
    curr = load_premium_users()
    if new_id in curr:
        await event.reply(pe(f"⚠️ User <code>{new_id}</code> already premium."), parse_mode='html'); return
    async with aiofiles.open(PREMIUM_FILE, 'a') as f: await f.write(f"{new_id}\n")
    await event.reply(pe(f"✅ <b>Premium Added!</b>\n👑 User <code>{new_id}</code> now has access."), parse_mode='html')

@bot.on(events.NewMessage(pattern=r'^/rmpremium\s+'))
async def remove_premium_command(event):
    if not is_admin(event.sender_id):
        await event.reply(pe("❌ <b>Admin only.</b>"), parse_mode='html'); return
    rm_id = event.message.text.split(' ', 1)[1].strip()
    curr  = load_premium_users()
    if rm_id not in curr:
        await event.reply(pe(f"❌ User <code>{rm_id}</code> not in list."), parse_mode='html'); return
    async with aiofiles.open(PREMIUM_FILE, 'w') as f:
        for u in curr:
            if u != rm_id: await f.write(f"{u}\n")
    await event.reply(pe(f"🚫 <b>Premium Removed!</b>\n❌ User <code>{rm_id}</code> removed."), parse_mode='html')

@bot.on(events.NewMessage(pattern=r'^/listpremium$'))
async def list_premium_command(event):
    if not is_admin(event.sender_id):
        await event.reply(pe("❌ <b>Admin only.</b>"), parse_mode='html'); return
    curr = load_premium_users()
    if not curr:
        await event.reply(pe("📋 No premium users found."), parse_mode='html'); return
    lines = "\n".join([f"{i+1}. <code>{u}</code>" for i, u in enumerate(curr)])
    await event.reply(pe(f"<b>📋 Premium Users ({len(curr)}):</b>\n\n{lines}"), parse_mode='html')

# ── Site management ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━───
@bot.on(events.NewMessage(pattern=r'^/addsite\s+'))
async def add_site_command(event):
    if not is_admin(event.sender_id):
        await event.reply(pe("❌ <b>Admin only.</b>"), parse_mode='html'); return
    new_site = event.message.text.split(' ', 1)[1].strip()
    if not new_site.startswith('http'):
        await event.reply(pe("❌ URL must start with http."), parse_mode='html'); return
    curr = load_sites()
    if new_site in curr:
        await event.reply(pe("⚠️ Site already exists."), parse_mode='html'); return
    async with aiofiles.open(SITES_FILE, 'a') as f: await f.write(f"{new_site}\n")
    await event.reply(pe(f"✅ <b>Site Added!</b>\n<code>{new_site}</code>"), parse_mode='html')

@bot.on(events.NewMessage(pattern=r'^/listsites$'))
async def list_sites_command(event):
    if not is_admin(event.sender_id):
        await event.reply(pe("❌ <b>Admin only.</b>"), parse_mode='html'); return
    curr = load_sites()
    if not curr:
        await event.reply(pe("📋 No sites found."), parse_mode='html'); return
    if len(curr) <= 30:
        lines = "\n".join([f"{i+1}. <code>{s}</code>" for i, s in enumerate(curr)])
        await event.reply(pe(f"<b>🌐 Sites ({len(curr)}):</b>\n\n{lines}"), parse_mode='html')
    else:
        fn = f"sites_list_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        async with aiofiles.open(fn, 'w') as f:
            for i, s in enumerate(curr): await f.write(f"{i+1}. {s}\n")
        await event.reply(pe(f"<b>🌐 Total Sites: {len(curr)}</b>"), file=fn, parse_mode='html')
        try: os.remove(fn)
        except: pass

@bot.on(events.NewMessage(pattern=r'^/broadcast\s+'))
async def broadcast_command(event):
    if not is_admin(event.sender_id):
        await event.reply(pe("❌ <b>Admin only.</b>"), parse_mode='html'); return
    msg   = event.message.text.split(' ', 1)[1].strip()
    users = load_premium_users()
    if not users:
        await event.reply(pe("❌ No premium users."), parse_mode='html'); return
    bc = pe(
        f"<b>⚡ {BOT_BRAND}</b>\n<b>{SEP}</b>\n"
        f"<b>📡 Admin Broadcast</b>\n{msg}\n"
        f"<b>{SEP}</b>\n{DEV_LINE}"
    )
    smsg = await event.reply(pe(f"🚀 Broadcasting to {len(users)} users..."), parse_mode='html')
    sent, failed = 0, 0
    for uid_str in users:
        try:
            await bot.send_message(int(uid_str), bc, parse_mode='html')
            sent += 1
        except:
            failed += 1
        await asyncio.sleep(0.1)
    await smsg.edit(pe(f"🚀 <b>Broadcast Done!</b>\n✅ Sent: {sent} | ❌ Failed: {failed}"), parse_mode='html')



# ═══════════════════════════════════════════════════════════════════════════════
# CALLBACK QUERY HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

@bot.on(events.CallbackQuery(pattern=b"gates"))
async def cb_gates(event):
    uid = event.sender_id
    if not is_premium(uid):
        await event.answer("❌ Premium required!", alert=True); return
    text = pe(
        f"<b>💳 Shopify Checker</b>\n"
        f"<b>{SEP}</b>\n"
        f"⚡ <b>Single</b>  »  <code>/sh card|mm|yy|cvv</code>\n\n"
        f"⚡ <b>Mass</b>    »  Reply to .txt with <code>/msh</code>\n"
        f"                 or send a .txt file directly"
    )
    await event.answer()
    await nav_edit(event.chat_id, event.message_id, text, rows_gates())

@bot.on(events.CallbackQuery(pattern=b"my_plan_btn"))
async def cb_my_plan_btn(event):
    uid  = event.sender_id
    if not is_premium(uid):
        await event.answer("❌ Premium required!", alert=True); return
    tier = get_user_tier(uid)
    trem = time_remaining(uid)
    lim  = get_user_limit(uid)
    await event.answer()
    if is_admin(uid):
        text = pe(
            f"<b>👑 Admin Access</b>\n"
            f"<b>{SEP}</b>\n"
            f"⭐ <b>Tier:</b> Admin\n"
            f"🔥 <b>Limit:</b> 5000 cards/file\n"
            f"⚡ <b>Status:</b> Unlimited\n"
            f"<b>{SEP}</b>\n"
            f"{DEV_LINE}"
        )
    else:
        acc = _user_access.get(uid, {})
        exp = acc.get('expires_at', '')[:10]
        text = pe(
            f"<b>🏧 My Plan</b>\n"
            f"<b>{SEP}</b>\n"
            f"💎 <b>Tier:</b> {(tier or 'free').capitalize()}\n"
            f"📅 <b>Expires:</b> {exp or 'N/A'}\n"
            f"⏳ <b>Remaining:</b> {trem or 'Expired'}\n"
            f"📋 <b>Limit:</b> {lim} cards/file\n"
            f"<b>{SEP}</b>\n"
            f"{DEV_LINE}"
        )
    await nav_edit(event.chat_id, event.message_id, text,
                   [[{"text": "↪️  Back", "callback_data": "gates"}]])

@bot.on(events.CallbackQuery(pattern=b"manage_proxy"))
async def cb_manage_proxy(event):
    uid = event.sender_id
    if not is_premium(uid):
        await event.answer("❌ Premium required!", alert=True); return
    user_list  = get_user_proxy_list(uid)
    pool       = load_proxies()
    if user_list:
        proxy_status = f"✅ <b>{len(user_list)} personal proxy(ies)</b> — rotating 🔁"
    else:
        proxy_status = "❌ <b>No Personal Proxies Set</b>"
    text = pe(
        f"<b>🔌 Proxy Settings</b>\n"
        f"<b>{SEP}</b>\n"
        f"{proxy_status}\n\n"
        f"📋 <b>Pool:</b> {len(pool)} proxies\n"
        f"<b>{SEP}</b>\n"
        f"<b>👩‍💻 Add proxies (all become yours, rotate per card):</b>\n"
        f"<code>/setproxy proxy1:port</code>\n"
        f"<code>/setproxy\nproxy1:port\nproxy2:port</code>\n\n"
        f"Clear all: <code>/clearuserproxy</code>"
    )
    await event.answer()
    await nav_edit(event.chat_id, event.message_id, text, rows_proxy(uid))

@bot.on(events.CallbackQuery(pattern=b"back_start"))
async def cb_back_start(event):
    uid = event.sender_id
    try:
        sender    = await bot.get_entity(uid)
        username  = f"@{sender.username}" if sender.username else f"ID:{uid}"
        firstname = sender.first_name or "User"
    except:
        username  = f"ID:{uid}"
        firstname = "User"
    tier = get_user_tier(uid)
    trem = time_remaining(uid)
    if is_admin(uid):           status_line = "Admin"
    elif is_key_admin(uid):     status_line = "Key Admin"
    elif tier == "auth":        status_line = f"Auth — {trem} left"   if trem else "Auth Expired"
    elif tier == "grant":       status_line = f"Grant — {trem} left"  if trem else "Grant Expired"
    elif tier in PLAN_TIERS:
        info = PLAN_TIERS[tier]
        status_line = f"{info['emoji']} {info['label']} — {trem} left" if trem else f"{info['emoji']} {info['label']} Expired"
    elif tier:                  status_line = f"{tier.capitalize()} — {trem} left" if trem else f"{tier.capitalize()} Expired"
    else:                       status_line = "No Access"
    lim  = get_user_limit(uid)
    text = pe(
        f"<b>⚡ Welcome, {firstname}!</b>\n"
        f"<b>{SEP}</b>\n"
        f"<blockquote>"
        f"👤 <b>User</b>    »  {username}\n"
        f"🔑 <b>ID</b>      »  <code>{uid}</code>\n"
        f"💎 <b>Status</b>  »  {status_line}\n"
        f"📋 <b>Limit</b>   »  {lim if lim else 'N/A'} cards"
        f"</blockquote>\n"
        f"<b>{SEP}</b>\n"
        f"Select an option below to get started."
    )
    await event.answer()
    await nav_edit(event.chat_id, event.message_id, text, rows_main())

@bot.on(events.CallbackQuery(pattern=b"close"))
async def cb_close(event):
    try: await event.delete()
    except: await event.answer("✅ Closed")

@bot.on(events.CallbackQuery(pattern=b"toggle_pool"))
async def cb_toggle_pool(event):
    uid     = event.sender_id
    current = user_pool_enabled.get(uid, True)
    user_pool_enabled[uid] = not current
    save_user_pool()
    state = "ON ✅" if not current else "OFF 🚀"
    await event.answer(f"Proxy Pool {state}", alert=False)
    user_list = get_user_proxy_list(uid)
    pool      = load_proxies()
    if user_list:
        proxy_status = f"✅ <b>{len(user_list)} personal proxy(ies)</b> — rotating 🔁"
    else:
        proxy_status = "❌ <b>No Personal Proxies Set</b>"
    text = pe(
        f"<b>🔌 Proxy Settings</b>\n"
        f"<b>{SEP}</b>\n"
        f"{proxy_status}\n\n"
        f"📋 <b>Pool:</b> {len(pool)} proxies\n"
        f"<b>{SEP}</b>\n"
        f"<b>👩‍💻 Add proxies (rotate per card):</b>\n"
        f"<code>/setproxy proxy1:port</code>\n"
        f"<code>/setproxy\nproxy1:port\nproxy2:port</code>\n\n"
        f"Clear all: <code>/clearuserproxy</code>"
    )
    await nav_edit(event.chat_id, event.message_id, text, rows_proxy(uid))

@bot.on(events.CallbackQuery(pattern=b"test_proxy_btn"))
async def cb_test_proxy(event):
    uid       = event.sender_id
    user_list = get_user_proxy_list(uid)
    # Admins fallback to pool if they have no personal list
    if not user_list and is_admin(uid):
        pool = load_proxies()
        if pool: user_list = pool[:20]
    if not user_list:
        await event.answer("❌ No proxies to test!", alert=True); return
    await event.answer(f"⚡ Testing {len(user_list)} proxies...", alert=False)
    msg = await bot.send_message(uid, pe(
        f"<b>📡 Testing {len(user_list)} proxy(ies)...</b>\n"
        f"<i>Please wait, this may take a moment.</i>"
    ), parse_mode='html')
    results = await asyncio.gather(*[test_proxy(p) for p in user_list])
    alive_proxies = [user_list[i] for i, r in enumerate(results) if r['status'] == 'alive']
    dead_proxies  = [user_list[i] for i, r in enumerate(results) if r['status'] != 'alive']
    # Auto-remove dead proxies from user's list (only if they are personal proxies)
    personal = get_user_proxy_list(uid)
    if dead_proxies and personal:
        updated = [p for p in personal if p not in dead_proxies]
        set_user_proxies(uid, updated)
    lines = [f"✅ <b>{len(alive_proxies)} alive</b>   ❌ <b>{len(dead_proxies)} dead (removed)</b>"]
    if alive_proxies:
        lines.append("\n<b>✅ Alive:</b>")
        for p in alive_proxies[:15]:
            lines.append(f"<code>{p}</code>")
        if len(alive_proxies) > 15:
            lines.append(f"<i>... +{len(alive_proxies)-15} more</i>")
    if dead_proxies:
        lines.append("\n<b>❌ Removed:</b>")
        for p in dead_proxies[:5]:
            lines.append(f"<code>{p}</code>")
        if len(dead_proxies) > 5:
            lines.append(f"<i>... +{len(dead_proxies)-5} more removed</i>")
    await msg.edit(pe(
        f"<b>📡 Proxy Test Report</b>\n"
        f"<b>{SEP}</b>\n"
        + "\n".join(lines)
    ), parse_mode='html')

@bot.on(events.CallbackQuery(pattern=b"remove_proxy_btn"))
async def cb_remove_proxy(event):
    uid       = event.sender_id
    user_list = get_user_proxy_list(uid)
    if user_list:
        remove_user_proxy(uid)
        await event.answer("✅ All personal proxies removed!", alert=False)
        await bot.send_message(uid, pe(
            f"🗑️ <b>Removed all {len(user_list)} personal proxy(ies).</b>"
        ), parse_mode='html')
        return
    proxies = load_proxies()
    if not proxies:
        await event.answer("❌ No proxies!", alert=True); return
    removed = proxies[0]
    async with aiofiles.open(PROXY_FILE, 'w') as f:
        for p in proxies[1:]: await f.write(f"{p}\n")
    await event.answer("✅ Removed!", alert=False)
    await bot.send_message(uid, pe(f"🗑️ <b>Proxy removed from pool:</b>\n<code>{removed}</code>"), parse_mode='html')

@bot.on(events.CallbackQuery(pattern=b"stop_mass"))
async def cb_stop_mass(event):
    uid = event.sender_id
    sk  = f"{uid}_{event.message_id}"
    if sk in active_sessions:
        del active_sessions[sk]
        await event.answer("🛑 Stopping...")
        try: await event.edit(pe("🚫 <b>Check stopped by user.</b>"), parse_mode='html')
        except: pass
    else:
        await event.answer("Already stopped.", alert=False)

@bot.on(events.CallbackQuery(pattern=rb"start_check_(\d+)"))
async def cb_start_check(event):
    uid  = event.sender_id
    data = pending_checks.get(uid)
    if not data:
        await event.answer("❌ Session expired. Send the file again.", alert=True); return
    cards = data['cards']
    del pending_checks[uid]
    await event.answer(f"⚡ Starting check for {len(cards)} cards!")
    queued_msg_id = event.message_id
    try:
        await event.edit(pe(f"<b>🎯 Queued {len(cards)} cards!</b>\n⚡ Starting..."), parse_mode='html')
    except:
        pass
    sites   = load_sites()
    proxies = get_proxies_for_user(uid) or load_proxies()
    if not sites:
        await bot.send_message(uid, pe("❌ No sites configured. Contact admin."), parse_mode='html'); return
    if not proxies:
        await bot.send_message(uid, pe("❌ <b>No proxy set!</b>\n\nAdd your proxy first:\n<code>/setproxy ip:port</code>\nor\n<code>/setproxy ip:port:user:pass</code>"), parse_mode='html'); return
    text = pe(
        f"<b>🔥 Mass Check — Starting</b>\n"
        f"<b>{SEP}</b>\n"
        f"📋 <b>Total</b>    »  {len(cards)}\n"
        f"☄️ <b>Checked</b>  »  0\n"
        f"💎 <b>Charged</b>  »  0\n"
        f"✅ <b>Approved</b> »  0\n"
        f"⚠️ <b>3DS</b>      »  0\n"
        f"<code>{make_progress_bar(0, len(cards))}</code>"
    )
    try:
        await asyncio.to_thread(_raw_post, f"{TG_API}/deleteMessage",
            {"chat_id": uid, "message_id": queued_msg_id})
    except:
        pass
    msg_id = await raw_send(uid, text, rows_stop())
    if msg_id:
        asyncio.create_task(run_mass_check(uid, cards, msg_id))

@bot.on(events.CallbackQuery(pattern=b"pause"))
async def pause_handler(event):
    sk = f"{event.sender_id}_{event.message_id}"
    if sk in active_sessions:
        active_sessions[sk]['paused'] = True
        await event.answer("⏸️ Paused")

@bot.on(events.CallbackQuery(pattern=b"resume"))
async def resume_handler(event):
    sk = f"{event.sender_id}_{event.message_id}"
    if sk in active_sessions:
        active_sessions[sk]['paused'] = False
        await event.answer("▶️ Resumed")

@bot.on(events.CallbackQuery(pattern=b"stop"))
async def stop_handler(event):
    sk = f"{event.sender_id}_{event.message_id}"
    if sk in active_sessions:
        del active_sessions[sk]
        await event.answer("🛑 Stopped")
        try: await event.edit(pe("🚫 <b>Checking stopped.</b>"), parse_mode='html')
        except: pass

# ── Admin panel callbacks ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━───────────
@bot.on(events.CallbackQuery(pattern=b"admin_panel"))
async def cb_admin_panel(event):
    if not is_admin(event.sender_id):
        await event.answer("❌ Admin only!", alert=True); return
    await event.answer()
    await nav_edit(event.chat_id, event.message_id, _admin_panel_text(), rows_admin())

@bot.on(events.CallbackQuery(pattern=b"admin_users"))
async def cb_admin_users(event):
    if not is_admin(event.sender_id):
        await event.answer("❌ Admin only!", alert=True); return
    pcount = len(load_premium_users())
    text   = pe(
        f"<b>👑 User Management</b>\n"
        f"<b>{SEP}</b>\n"
        f"<b>Premium Users:</b> {pcount}\n"
        f"<b>{SEP}</b>\n"
        f"<b>Commands:</b>\n"
        f"<code>/addpremium [ID]</code> — Add user\n"
        f"<code>/rmpremium [ID]</code> — Remove user\n"
        f"<code>/listpremium</code> — See full list"
    )
    await event.answer()
    await nav_edit(event.chat_id, event.message_id, text, rows_admin_users())

@bot.on(events.CallbackQuery(pattern=b"admin_sites"))
async def cb_admin_sites(event):
    if not is_admin(event.sender_id):
        await event.answer("❌ Admin only!", alert=True); return
    scount = len(load_sites())
    text   = pe(
        f"<b>🌐 Site Management</b>\n"
        f"<b>{SEP}</b>\n"
        f"<b>Total Sites:</b> {scount}\n"
        f"<b>{SEP}</b>\n"
        f"<b>Commands:</b>\n"
        f"<code>/addsite [url]</code> — Add site\n"
        f"<code>/rmsite [url]</code> — Remove site\n"
        f"<code>/listsites</code> — See all sites"
    )
    await event.answer()
    await nav_edit(event.chat_id, event.message_id, text, rows_admin_sites())

@bot.on(events.CallbackQuery(pattern=b"admin_proxy_pool"))
async def cb_admin_proxy_pool(event):
    if not is_admin(event.sender_id):
        await event.answer("❌ Admin only!", alert=True); return
    pool   = load_proxies()
    sample = pool[0] if pool else "None"
    text   = pe(
        f"<b>⚙️ Proxy Pool Management</b>\n"
        f"<b>{SEP}</b>\n"
        f"<b>Pool Size:</b> {len(pool)} proxies\n"
        f"<b>Sample:</b> <code>{sample}</code>\n"
        f"<b>{SEP}</b>\n"
        f"<b>Commands:</b>\n"
        f"<code>/addproxy [proxy]</code> — Add proxy\n"
        f"<code>/rmproxy [proxy]</code> — Remove proxy\n"
        f"<code>/clearproxy</code> — Clear all\n"
        f"<code>/getproxy</code> — Get pool file"
    )
    await event.answer()
    await nav_edit(event.chat_id, event.message_id, text, rows_admin_proxy_pool())

@bot.on(events.CallbackQuery(pattern=b"admin_broadcast_info"))
async def cb_admin_broadcast_info(event):
    if not is_admin(event.sender_id):
        await event.answer("❌ Admin only!", alert=True); return
    pcount = len(load_premium_users())
    await event.answer(f"📡 Send: /broadcast [message]\nWill reach {pcount} premium users.", alert=True)

@bot.on(events.CallbackQuery(pattern=b"admin_list_users"))
async def cb_admin_list_users(event):
    if not is_admin(event.sender_id):
        await event.answer("❌ Admin only!", alert=True); return
    curr = load_premium_users()
    if not curr:
        await event.answer("📋 No premium users.", alert=True); return
    lines = "\n".join([f"{i+1}. <code>{u}</code>" for i, u in enumerate(curr[:50])])
    note  = f"\n<i>...and {len(curr)-50} more</i>" if len(curr) > 50 else ""
    text  = pe(f"<b>👑 Premium Users ({len(curr)}):</b>\n\n{lines}{note}\n\n<b>{SEP}</b>\n{DEV_LINE}")
    await event.answer()
    await nav_edit(event.chat_id, event.message_id, text, rows_admin_users())

@bot.on(events.CallbackQuery(pattern=b"admin_list_sites_cb"))
async def cb_admin_list_sites(event):
    if not is_admin(event.sender_id):
        await event.answer("❌ Admin only!", alert=True); return
    sites = load_sites()
    if not sites:
        await event.answer("📋 No sites.", alert=True); return
    lines = "\n".join([f"{i+1}. <code>{s}</code>" for i, s in enumerate(sites[:30])])
    note  = f"\n<i>...and {len(sites)-30} more. Use /listsites for full list.</i>" if len(sites) > 30 else ""
    text  = pe(f"<b>🌐 Sites ({len(sites)}):</b>\n\n{lines}{note}\n\n<b>{SEP}</b>\n{DEV_LINE}")
    await event.answer()
    await nav_edit(event.chat_id, event.message_id, text, rows_admin_sites())

@bot.on(events.CallbackQuery(pattern=b"admin_list_proxy_cb"))
async def cb_admin_list_proxy(event):
    if not is_admin(event.sender_id):
        await event.answer("❌ Admin only!", alert=True); return
    pool = load_proxies()
    if not pool:
        await event.answer("📋 Pool is empty.", alert=True); return
    lines = "\n".join([f"{i+1}. <code>{p}</code>" for i, p in enumerate(pool[:20])])
    note  = f"\n<i>...and {len(pool)-20} more. Use /getproxy for full list.</i>" if len(pool) > 20 else ""
    text  = pe(f"<b>⚙️ Proxy Pool ({len(pool)}):</b>\n\n{lines}{note}\n\n<b>{SEP}</b>\n{DEV_LINE}")
    await event.answer()
    await nav_edit(event.chat_id, event.message_id, text, rows_admin_proxy_pool())

@bot.on(events.CallbackQuery(pattern=b"admin_add_user_info"))
async def cb_admin_add_user_info(event):
    if not is_admin(event.sender_id): await event.answer("❌ Admin only!", alert=True); return
    await event.answer("✅ Send: /addpremium [user_id]", alert=True)

@bot.on(events.CallbackQuery(pattern=b"admin_rm_user_info"))
async def cb_admin_rm_user_info(event):
    if not is_admin(event.sender_id): await event.answer("❌ Admin only!", alert=True); return
    await event.answer("🔥 Send: /rmpremium [user_id]", alert=True)

@bot.on(events.CallbackQuery(pattern=b"admin_add_site_info"))
async def cb_admin_add_site_info(event):
    if not is_admin(event.sender_id): await event.answer("❌ Admin only!", alert=True); return
    await event.answer("✅ Send: /addsite [url]", alert=True)

@bot.on(events.CallbackQuery(pattern=b"admin_rm_site_info"))
async def cb_admin_rm_site_info(event):
    if not is_admin(event.sender_id): await event.answer("❌ Admin only!", alert=True); return
    await event.answer("🔥 Send: /rmsite [url]", alert=True)

@bot.on(events.CallbackQuery(pattern=b"admin_add_proxy_info"))
async def cb_admin_add_proxy_info(event):
    if not is_admin(event.sender_id): await event.answer("❌ Admin only!", alert=True); return
    await event.answer("✅ Send: /addproxy [ip:port or ip:port:user:pass]", alert=True)

@bot.on(events.CallbackQuery(pattern=b"admin_clear_proxy_cb"))
async def cb_admin_clear_proxy(event):
    if not is_admin(event.sender_id): await event.answer("❌ Admin only!", alert=True); return
    try:
        async with aiofiles.open(PROXY_FILE, 'w') as f: await f.write("")
        await event.answer("🔥 Proxy pool cleared!", alert=True)
    except:
        await event.answer("❌ Failed to clear pool.", alert=True)
    pool = load_proxies()
    text = pe(
        f"<b>⚙️ Proxy Pool Management</b>\n"
        f"<b>{SEP}</b>\n"
        f"<b>Pool Size:</b> {len(pool)} proxies\n"
        f"<b>{SEP}</b>\n"
        f"<b>Commands:</b>\n"
        f"<code>/addproxy [proxy]</code> — Add proxy\n"
        f"<code>/rmproxy [proxy]</code> — Remove proxy\n"
        f"<code>/clearproxy</code> — Clear all\n"
        f"<code>/getproxy</code> — Get pool file"
    )
    await nav_edit(event.chat_id, event.message_id, text, rows_admin_proxy_pool())




# ═══════════════════════════════════════════════════════════════════════════════
# BOT COMMAND MENU REGISTRATION
# ═══════════════════════════════════════════════════════════════════════════════

def _register_commands():
    user_cmds = [
        {"command": "start",          "description": "Start the bot & open your dashboard"},
        {"command": "sh",             "description": "Single check: /sh card|mm|yy|cvv"},
        {"command": "msh",            "description": "Mass check (reply to .txt file or send file)"},
        {"command": "redeem",         "description": "Redeem an access key"},
        {"command": "myplan",         "description": "Check your current plan & remaining time"},
        {"command": "setproxy",       "description": "Set proxy: /setproxy ip:port[:user:pass]"},
        {"command": "clearuserproxy", "description": "Remove your personal proxy"},
        {"command": "chkproxy",       "description": "Test a proxy: /chkproxy ip:port"},
    ]
    admin_cmds = user_cmds + [
        {"command": "admin",          "description": "Open admin panel"},
        {"command": "genkey",         "description": "Generate key: /genkey [tier] [days]"},
        {"command": "grant",          "description": "Grant access: /grant [user_id] [days]"},
        {"command": "auth",           "description": "Auth access: /auth [user_id] [days]"},
        {"command": "revoke",         "description": "Revoke access: /revoke [user_id]"},
        {"command": "addpremium",     "description": "Add permanent premium: /addpremium [id]"},
        {"command": "rmpremium",      "description": "Remove premium: /rmpremium [id]"},
        {"command": "listpremium",    "description": "List premium users"},
        {"command": "addsite",        "description": "Add site: /addsite [url]"},
        {"command": "rmsite",         "description": "Remove site: /rmsite [url]"},
        {"command": "listsites",      "description": "List all sites"},
        {"command": "addproxy",       "description": "Add proxy to pool: /addproxy [ip:port]"},
        {"command": "rmproxy",        "description": "Remove from pool: /rmproxy [proxy]"},
        {"command": "clearproxy",     "description": "Clear entire proxy pool"},
        {"command": "getproxy",       "description": "Get proxy pool as file"},
        {"command": "broadcast",      "description": "Broadcast: /broadcast [message]"},
        {"command": "setadmin",       "description": "Manage admins: /setadmin add/rm [user_id]"},
        {"command": "testcards",      "description": "Test all 4 result types"},
    ]
    from keyboards import _http_session
    base = f"https://api.telegram.org/bot{BOT_TOKEN}"
    try:
        _http_session.post(f"{base}/setMyCommands",
                           json={"commands": user_cmds}, timeout=5)
        _http_session.post(f"{base}/setMyCommands",
                           json={"commands": admin_cmds,
                                 "scope": {"type": "chat", "chat_id": ADMIN_ID}},
                           timeout=5)
    except:
        pass


_register_commands()
_bin_count = load_bins()
print(f"VXO Bot started successfully! (BIN DB: {_bin_count:,} entries)")
bot.run_until_disconnected()
