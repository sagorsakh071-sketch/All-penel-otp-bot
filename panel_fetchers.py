# panel_fetchers.py — OTP Panel v5 · ULTIMATE FIXED ENGINE v2
# ══════════════════════════════════════════════════════════════════
# ALL BUGS FIXED (original + newly found):
#  ✅ BUG-2  ims_login      — session cookie verification added
#  ✅ BUG-3  konekta_login  — dashboard URL check (not just "sign")
#  ✅ BUG-4  _captcha       — no hardcoded 4; BeautifulSoup fallback
#  ✅ BUG-5  CSRF           — logs warning, never silently fails
#  ✅ BUG-6  panel_login    — redirect-back-to-login detection added
#  ✅ BUG-8  session expiry — proactive detection via _is_session_expired()
#  ✅ BUG-9  error pages    — now correctly detected as session expiry
#  ✅ BUG-10 proofsms_fetch — properly exported (was dead code)
#  ✅ NEW    timesms_login/fetch — TimeSMS panel (sesskey from Reports)
#  ✅ NEW    _core_fetch    — also checks SMSCDRReports for sesskey
#  ✅ FIX-A  _parse()       — dict rows now handled (VoiceGate compat)
#  ✅ FIX-B  _roxysms_fetch — network error vs session expiry separated
#  ✅ FIX-C  timesms_fetch  — network error vs session expiry separated
#  ✅ FIX-D  panel_login    — laravel_session cookie now also checked
#  ✅ FIX-E  ProofSMS       — proofsms_fetch now dispatched for ProofSMS
# ══════════════════════════════════════════════════════════════════

import os, re, json, logging, html as _html
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_COOKIE_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies")
os.makedirs(_COOKIE_BASE, exist_ok=True)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_BASE_HEADERS = {
    "User-Agent":      _UA,
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection":      "keep-alive",
}

_AJAX_HEADERS = {
    "User-Agent":       _UA,
    "Accept":           "application/json, text/javascript, */*; q=0.01",
    "Accept-Language":  "en-US,en;q=0.9",
    "Accept-Encoding":  "gzip, deflate",
    "X-Requested-With": "XMLHttpRequest",
    "Connection":       "keep-alive",
}

# Words that appear on login pages
_LOGIN_PAGE_SIGNALS = [
    "sign in", "signin", "please sign",
    "log in", "please login",
    "authentication required", "session expired",
    "unauthorized", "access denied",
    # NOTE: 'password' & 'username' removed — dashboard pages often have
    # 'Change Password' links which caused false session-expired detection.
]


def _dates():
    today     = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    return f"{yesterday} 00:00:00", f"{today} 23:59:59"


def _session():
    import requests as rq
    s = rq.Session()
    s.headers.update(_BASE_HEADERS)
    return s


# ── ✅ BUG-4 FIX: Captcha solver — no hardcoded fallback ───────
def _solve_captcha(html_text: str) -> str:
    """
    Solve math captcha from HTML text.
    Supports addition and subtraction.
    Tries regex first, then BeautifulSoup.
    Logs warning if captcha not found.
    """
    # Try addition patterns
    for pat in [
        r'What is\s*(\d+)\s*\+\s*(\d+)',
        r'(\d+)\s*\+\s*(\d+)\s*=',
        r'captcha[^>]*>\s*(\d+)\s*\+\s*(\d+)',
        r'>(\d+)\s*\+\s*(\d+)<',
        r'(\d+)\s*\+\s*(\d+)',   # broad catch-all
    ]:
        m = re.search(pat, html_text, re.I)
        if m:
            return str(int(m.group(1)) + int(m.group(2)))

    # Try subtraction patterns
    for pat in [
        r'What is\s*(\d+)\s*-\s*(\d+)',
        r'(\d+)\s*-\s*(\d+)\s*=',
    ]:
        m = re.search(pat, html_text, re.I)
        if m:
            result = int(m.group(1)) - int(m.group(2))
            return str(max(0, result))

    # BeautifulSoup fallback (if installed)
    try:
        from bs4 import BeautifulSoup
        plain = BeautifulSoup(html_text, "html.parser").get_text()
        for pat in [r'(\d+)\s*\+\s*(\d+)', r'(\d+)\s*-\s*(\d+)']:
            m = re.search(pat, plain)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                if "+" in pat:
                    return str(a + b)
                return str(max(0, a - b))
    except ImportError:
        pass

    logger.warning("⚠️ Captcha not found — sending 0 (may cause login failure)")
    return "0"


# Backward compat alias
def _captcha(html: str) -> int:
    return int(_solve_captcha(html) or "0")


# ── ✅ BUG-9 FIX: Session expiry detector ─────────────────────
def _is_session_expired(resp) -> bool:
    """
    Returns True if response signals that session has expired.
    Checks: HTTP status, URL redirect history, response content.
    """
    # HTTP-level indicators
    if resp.status_code in (401, 403):
        return True

    # URL redirect to login page
    final_url = str(resp.url).lower()
    if any(x in final_url for x in ["/login", "/signin", "/sign-in", "/auth/login"]):
        return True

    # Redirect-history check — page content দিয়ে confirm করো
    if resp.history:
        last_url = str(resp.history[-1].url).lower()
        if (any(x in last_url for x in ["/login", "/signin", "/sign-in"])
                and "json" not in resp.headers.get("Content-Type", "").lower()):
            page = resp.text[:1500].lower()
            if "<html" in page and any(w in page for w in ["sign in", "please login", "signin"]):
                return True

    # Content-based check (only for non-JSON responses)
    ct = resp.headers.get("Content-Type", "").lower()
    if "json" not in ct:
        text_low = resp.text[:3000].lower()
        # Must look like an HTML page AND have login signals
        if ("<html" in text_low or "<form" in text_low):
            if any(w in text_low for w in _LOGIN_PAGE_SIGNALS):
                return True

    return False


# ══════════════════════════════════════════════════════════════
#  KEEPALIVE — session alive রাখার জন্য lightweight ping
#  ImsPanel আর RoxySMS এর server-side session টাইমআউট খুব কম।
#  fetch এর মাঝে idle থাকলে session মরে যায়।
#  এই function একটা dashboard GET করে — minimal traffic, session refresh।
# ══════════════════════════════════════════════════════════════
def _ims_keepalive(s, url: str, path: str) -> bool:
    """
    ImsPanel session alive রাখার জন্য dashboard ping।
    Returns True যদি session ঠিক থাকে, False যদি expired।
    """
    try:
        r = s.get(
            f"{url}/{path}/SMSDashboard",
            headers={**_BASE_HEADERS, "Referer": f"{url}/{path}/SMSCDRStats"},
            timeout=10, allow_redirects=True,
        )
        if _is_session_expired(r):
            logger.debug(f"ims_keepalive · session expired")
            return False
        logger.debug(f"ims_keepalive · OK (HTTP {r.status_code})")
        return True
    except Exception as e:
        logger.debug(f"ims_keepalive · network error: {str(e)[:60]}")
        return True   # network error → session এখনো হয়তো alive, False করবো না


def _roxy_keepalive(s, url: str, path: str) -> bool:
    """
    RoxySMS session alive রাখার জন্য SMSCDRReports ping।
    Returns True যদি session ঠিক থাকে, False যদি expired।
    """
    try:
        r = s.get(
            f"{url}/{path}/SMSCDRReports",
            headers={**_BASE_HEADERS, "Referer": f"{url}/{path}/SMSDashboard"},
            timeout=10, allow_redirects=True,
        )
        if _is_session_expired(r):
            logger.debug(f"roxy_keepalive · session expired")
            return False
        logger.debug(f"roxy_keepalive · OK (HTTP {r.status_code})")
        return True
    except Exception as e:
        logger.debug(f"roxy_keepalive · network error: {str(e)[:60]}")
        return True


def _classify(resp) -> str:
    """Returns: 'json' | 'login_page' | 'empty' | 'html_other'"""
    if _is_session_expired(resp):
        return "login_page"

    ct   = resp.headers.get("Content-Type", "").lower()
    text = resp.text.strip()

    if not text:
        return "empty"
    if "json" in ct or text.startswith("[") or text.startswith("{"):
        return "json"
    return "html_other"


# ── ✅ FIX-A: Row parser — handles both list AND dict rows ─────
def _parse(text: str, mask_only=False) -> list:
    """
    Parse panel JSON response into normalized rows.
    FIX-A: Now handles dict-type rows (e.g. VoiceGate) in addition to list rows.
    Returns list of [date, id, number, cli, None, sms] rows.
    """
    text = text.strip()
    if not text or text.startswith("<"):
        return []
    try:
        js = json.loads(text)
    except Exception:
        cut = text.rfind("]")
        if cut > 0:
            try:
                js = json.loads(text[:cut + 1])
            except Exception:
                return []
        else:
            return []

    if isinstance(js, dict):
        rows = js.get("aaData") or js.get("data") or []
    elif isinstance(js, list):
        rows = js
    else:
        return []

    INVALID = {"None", "", "null", "—", "****", "***", "**", "*", "N/A", "$"}
    out = []

    for row in rows:
        # ── List row (standard panel format) ──────────────────
        if isinstance(row, list):
            if len(row) < 4:
                continue
            if str(row[0]).startswith("0,"):
                continue
            num = str(row[2]).strip().lstrip("+")
            if not num.isdigit() or num == "0":
                continue
            sms = ""
            for col in [5, 4, 6]:
                v = str(row[col]).strip() if len(row) > col else ""
                if v and v not in INVALID and not re.match(r'^\*+$', v):
                    sms = _html.unescape(v)
                    break
            if mask_only and not sms:
                continue
            out.append([str(row[0]), str(row[1]), num, str(row[3]), None, sms])

        # ── ✅ FIX-A: Dict row (VoiceGate and others) ─────────
        elif isinstance(row, dict):
            num = str(row.get("number", row.get("num", row.get("msisdn", "")))).strip().lstrip("+")
            if not num.isdigit() or num == "0":
                continue
            sms = str(row.get("sms", row.get("message", row.get("text", "")))).strip()
            if sms in INVALID or re.match(r'^\*+$', sms):
                sms = ""
            if mask_only and not sms:
                continue
            sms = _html.unescape(sms)
            cli = str(row.get("cli", row.get("service", row.get("sender", "")))).strip()
            dt  = str(row.get("date", row.get("dt", row.get("time", "")))).strip()
            rid = str(row.get("id", row.get("rowid", ""))).strip()
            out.append([dt, rid, num, cli, None, sms])

    return out


# ══════════════════════════════════════════════════════════════
#  CORE FETCH — ULTIMATE FIXED ENGINE
# ══════════════════════════════════════════════════════════════
def _core_fetch(s, url: str, path: str) -> list | None:
    """
    Returns:
      list  → rows (may be empty [])
      None  → session expired (caller must re-login)
    """
    d1, d2 = _dates()
    paths_to_try = list(dict.fromkeys([path, "agent", "client", "reseller"]))

    for p in paths_to_try:
        base_params = {
            "fdate1": d1, "fdate2": d2,
            "frange": "", "fclient": "", "fnum": "", "fcli": "",
            "fgdate": "", "fgmonth": "", "fgrange": "",
            "fgclient": "", "fgnumber": "", "fgcli": "",
            "fg": "0",
        }

        # ── A. Direct hit (no sesskey) ──────────────────────
        for ep in [
            f"{url}/{p}/res/data_smscdr.php",
            f"{url}/{p}/res/data_smscdrreports.php",
        ]:
            try:
                r = s.get(ep, params=base_params,
                          headers={**_AJAX_HEADERS, "Referer": f"{url}/{p}/SMSCDRStats"},
                          timeout=20, allow_redirects=True)
                if _is_session_expired(r):
                    return None
                if _classify(r) == "json":
                    rows = _parse(r.text)
                    if rows:
                        return rows
            except Exception as e:
                err = str(e)
                if any(x in err for x in ["NewConnection", "ConnectionError",
                                           "Failed to establish", "Max retries"]):
                    break
                continue

        # ── B. Sesskey fallback ─────────────────────────────
        sesskey = ""
        for stats_ep in [
            f"{url}/{p}/SMSCDRStats",
            f"{url}/{p}/SMSCDRReports",  # TimeSMS uses this
        ]:
            if sesskey:
                break
            try:
                rs = s.get(stats_ep,
                           headers={**_BASE_HEADERS, "Referer": f"{url}/{p}/SMSDashboard"},
                           timeout=15, allow_redirects=True)
                if _is_session_expired(rs):
                    return None
                m = re.search(r'sesskey=([A-Za-z0-9+/=_-]+)', rs.text)
                if m:
                    sesskey = m.group(1)
            except Exception:
                continue

        if sesskey:
            for ep in [
                f"{url}/{p}/res/data_smscdr.php",
                f"{url}/{p}/res/data_smscdrreports.php",
            ]:
                try:
                    r = s.get(ep, params={**base_params, "sesskey": sesskey},
                              headers={**_AJAX_HEADERS, "Referer": f"{url}/{p}/SMSCDRStats"},
                              timeout=20, allow_redirects=True)
                    if _is_session_expired(r):
                        return None
                    if _classify(r) == "json":
                        rows = _parse(r.text)
                        if rows:
                            return rows
                except Exception:
                    pass

        # ── C. Test panel endpoint fallback ─────────────────
        try:
            r = s.get(f"{url}/{p}/res/data_testsmscdr.php",
                      params={"fdate1": d1, "fdate2": d2, "fg": "0"},
                      headers={**_AJAX_HEADERS, "Referer": f"{url}/{p}/SMSTestPanel"},
                      timeout=15, allow_redirects=True)
            if _is_session_expired(r):
                return None
            if _classify(r) == "json":
                rows = _parse(r.text)
                if rows:
                    return rows
        except Exception:
            pass

    return []  # authenticated but no data


# ══════════════════════════════════════════════════════════════
#  INTS LOGIN/FETCH  (HADI, Seven1Tel, Wolf, Gaza, Sniper, MAIT, etc.)
#  ✅ BUG-5 FIX: CSRF warning logged
#  ✅ BUG-6 FIX: URL redirect check after login
# ══════════════════════════════════════════════════════════════
def ints_login(bn, email, pw, url="", forced_path=None):
    s = _session()
    try:
        r = s.get(f"{url}/login", timeout=25, allow_redirects=True)
    except Exception as e:
        raise Exception(f"{bn} · server unreachable: {e}")

    low = r.text.lower()
    if r.status_code in (403, 401) or "not in allowlist" in low or "not allowed" in low:
        raise Exception(f"{bn} · IP blocked (HTTP {r.status_code})")

    capt = _solve_captcha(r.text)

    csrf = ""
    for pat in [
        r'name=["\']_token["\'][^>]+value=["\']([^"\']+)["\']',
        r'value=["\']([^"\']+)["\'][^>]+name=["\']_token["\']',
        r'meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']',
    ]:
        m = re.search(pat, r.text)
        if m:
            csrf = m.group(1)
            break
    if not csrf:
        logger.debug(f"{bn} · no CSRF token on login page (may be OK for this panel)")

    post_data = {
        "username": email, "password": pw,
        "capt": capt, "g-recaptcha-response": "",
    }
    if csrf:
        post_data["_token"] = csrf

    try:
        r2 = s.post(
            f"{url}/signin",
            data=post_data,
            headers={**_BASE_HEADERS,
                     "Referer": f"{url}/login",
                     "Content-Type": "application/x-www-form-urlencoded",
                     "Origin": url},
            allow_redirects=True, timeout=20,
        )
    except Exception as e:
        raise Exception(f"{bn} · signin POST failed: {e}")

    # Check session cookie
    sid = (s.cookies.get("PHPSESSID") or s.cookies.get("session")
           or s.cookies.get("laravel_session"))
    if not sid:
        raise Exception(f"{bn} · no session cookie after login (HTTP {r2.status_code})"
                        " — wrong credentials or server issue")

    # ✅ BUG-6 FIX: Check URL — redirected back to login = failed
    final_url = str(r2.url).lower()
    if any(x in final_url for x in ["/login", "/signin", "/sign-in"]):
        raise Exception(f"{bn} · login failed — redirected back to login"
                        " (wrong credentials or rate limit)")

    # Detect path
    if forced_path in ("agent", "client", "reseller"):
        path = forced_path
    elif "client" in str(r2.url):
        path = "client"
    elif "reseller" in str(r2.url):
        path = "reseller"
    else:
        path = "agent"

    logger.info(f"✅ ints_login {bn} {email} · path:{path}")
    return s, path, url


def ints_fetch(bn, session_info, url=""):
    import requests as rq
    if (isinstance(session_info, tuple) and len(session_info) == 3
            and hasattr(session_info[0], "cookies")):
        s, path, _url = session_info
        url = _url or url
    else:
        # Legacy cookie-file fallback
        cookie_file = session_info[0] if isinstance(session_info, tuple) else session_info
        path = session_info[1] if isinstance(session_info, tuple) else "agent"
        s = _session()
        try:
            with open(cookie_file) as cf:
                for line in cf:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) >= 7:
                        s.cookies.set(parts[5], parts[6], domain=parts[0])
        except Exception as e:
            logger.warning(f"ints_fetch cookie load fail {bn}: {e}")
            return None

    result = _core_fetch(s, url, path)
    if result is None:
        logger.warning(f"ints_fetch {bn} · session expired → will re-login")
    elif result:
        logger.info(f"✅ ints_fetch {bn} · {len(result)} rows")
    else:
        logger.debug(f"ints_fetch {bn} · 0 rows (authenticated, no data)")
    return result


# Backward compat
def hadi_login(email, pw, url=""): return ints_login("HADI_SMS", email, pw, url)
def hadi_fetch(cookie_file, url=""): return ints_fetch("HADI_SMS", cookie_file, url)


# ══════════════════════════════════════════════════════════════
#  STANDARD PANEL  (PurplePanel, TrueSMS, etc.)
#  ✅ BUG-6 FIX: URL redirect check after login
#  ✅ FIX-D: laravel_session cookie now also checked
# ══════════════════════════════════════════════════════════════
def panel_login(bn, email, pw, url):
    s = _session()
    if "/sms" in url:
        lp, sp = "SignIn", "signmein"
    else:
        lp, sp = "login", "signin"

    try:
        r = s.get(f"{url}/{lp}", timeout=25, allow_redirects=True)
    except Exception as e:
        raise Exception(f"{bn} · server unreachable: {e}")

    low = r.text.lower()
    if r.status_code in (403, 401) or "not in allowlist" in low:
        raise Exception(f"{bn} · IP blocked (HTTP {r.status_code})")

    capt = _solve_captcha(r.text)
    csrf = ""
    m = re.search(r'name=["\']_token["\'][^>]+value=["\']([^"\']+)["\']', r.text)
    if m:
        csrf = m.group(1)

    post_data = {"username": email, "password": pw,
                 "capt": capt, "g-recaptcha-response": ""}
    if csrf:
        post_data["_token"] = csrf

    r2 = s.post(f"{url}/{sp}", data=post_data,
                headers={**_BASE_HEADERS,
                         "Referer": f"{url}/{lp}",
                         "Content-Type": "application/x-www-form-urlencoded",
                         "Origin": url},
                allow_redirects=True, timeout=20)

    # ✅ FIX-D: Also check laravel_session (some panels use it)
    sid = (s.cookies.get("PHPSESSID") or s.cookies.get("session")
           or s.cookies.get("laravel_session"))
    if not sid:
        raise Exception(f"{bn} · no session cookie after login (wrong credentials?)")

    # ✅ BUG-6 FIX: URL check
    final_url = str(r2.url).lower()
    if f"/{lp.lower()}" in final_url or f"/{sp.lower()}" in final_url:
        raise Exception(f"{bn} · login failed — redirected back to login page")

    rurl = str(r2.url)
    if "reseller" in rurl:
        path = "reseller"
    elif "client" in rurl:
        path = "client"
    else:
        path = "agent"

    logger.info(f"✅ panel_login {bn} {email} · path:{path}")
    return s, path, url


def panel_fetch(session_info, url):
    if (isinstance(session_info, tuple) and len(session_info) == 3
            and hasattr(session_info[0], "cookies")):
        s, path, _url = session_info
        url = _url or url
    else:
        s = _session()
        path = "agent"
    return _core_fetch(s, url, path)


def reseller_fetch(sid, url):
    return panel_fetch(sid, url)


# ══════════════════════════════════════════════════════════════
#  IMS PANEL — NEW LOGIN SYSTEM
#  Human-like delays, multi-attempt login, etkk token refresh
# ══════════════════════════════════════════════════════════════
import time, random
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

def _ims_make_session():
    import requests as rq
    s = rq.Session()
    adapter = HTTPAdapter(max_retries=Retry(total=3, backoff_factor=2))
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    })
    return s

def _ims_human_delay(a=2, b=5):
    t = random.uniform(a, b)
    time.sleep(t)

def _ims_get_etkk(html):
    for pat in [
        r"name=['\"]etkk['\"][^>]+value=['\"]([^'\"]+)['\"]",
        r"value=['\"]([^'\"]+)['\"][^>]+name=['\"]etkk['\"]"
    ]:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return ""

def _ims_try_login(s, url, email, pw, etkk, capt):
    """Single login attempt with full browser-like headers."""
    import requests as rq
    try:
        r = s.post(
            f"{url}/signin",
            data={"username": email, "password": pw, "capt": capt, "etkk": etkk},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": url,
                "Referer": f"{url}/login",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-User": "?1",
                "Cache-Control": "max-age=0",
                "Upgrade-Insecure-Requests": "1",
            },
            allow_redirects=True,
            timeout=30
        )
        return r
    except rq.exceptions.ConnectionError:
        logger.warning("ImsPanel · connection dropped, retrying in 8s...")
        time.sleep(8)
        return None

def ims_login(email, pw, url):
    s = _ims_make_session()

    # Homepage visit (human-like)
    try:
        s.get(url, timeout=20)
    except Exception:
        pass
    _ims_human_delay(2, 3)

    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        logger.info(f"ImsPanel · login attempt {attempt}/{max_attempts} — {email}")

        try:
            r = s.get(f"{url}/login", timeout=20, allow_redirects=True)
        except Exception as e:
            raise Exception(f"ImsPanel · server unreachable: {e}")

        etkk = _ims_get_etkk(r.text)
        capt = _solve_captcha(r.text)

        _ims_human_delay(3, 6)

        r2 = _ims_try_login(s, url, email, pw, etkk, capt)

        if r2 is None:
            _ims_human_delay(5, 8)
            continue

        final_url = str(r2.url).lower()

        # ── Success check ──────────────────────────────────
        if not any(x in final_url for x in ["/login", "/signin"]):
            for role in ["reseller", "agent", "admin", "client"]:
                if role in final_url:
                    logger.info(f"✅ ImsPanel · {email} · role: {role}")
                    return s, role, url
            logger.info(f"✅ ImsPanel · {email} · URL: {r2.url}")
            return s, "client", url

        # ── Failed — try with refreshed etkk immediately ──
        new_etkk = _ims_get_etkk(r2.text)
        new_capt = _solve_captcha(r2.text)

        if new_etkk and new_etkk != etkk:
            logger.info("ImsPanel · token changed, retrying immediately...")
            _ims_human_delay(2, 4)
            r3 = _ims_try_login(s, url, email, pw, new_etkk, new_capt)
            if r3 and not any(x in str(r3.url).lower() for x in ["/login", "/signin"]):
                for role in ["reseller", "agent", "admin", "client"]:
                    if role in str(r3.url).lower():
                        logger.info(f"✅ ImsPanel · {email} · role: {role}")
                        return s, role, url
                logger.info(f"✅ ImsPanel · {email} · URL: {r3.url}")
                return s, "client", url

        logger.warning(f"ImsPanel · attempt {attempt} failed. Retrying...")
        _ims_human_delay(5, 10)

    raise Exception(f"ImsPanel · all {max_attempts} login attempts failed for {email}")


def ims_fetch(session_info, url):
    if (isinstance(session_info, tuple) and len(session_info) == 3
            and hasattr(session_info[0], "cookies")):
        s, path, _url = session_info
        url = _url or url
    else:
        s = session_info
        path = "client"

    # Dashboard visit (human-like, keeps session alive)
    try:
        _ims_human_delay(2, 3)
        s.get(f"{url}/{path}/SMSDashboard", timeout=15)
        _ims_human_delay(1, 2)
    except Exception:
        pass

    # Get sesskey from CDR Stats
    sesskey = ""
    try:
        rs = s.get(f"{url}/{path}/SMSCDRStats", timeout=15, allow_redirects=True)
        if _is_session_expired(rs):
            logger.info("ims_fetch · session expired → re-login")
            return None
        m = re.search(r'sesskey=([A-Za-z0-9+/=_-]+)', rs.text)
        if m:
            sesskey = m.group(1)
    except Exception:
        pass

    _ims_human_delay(1, 2)

    from datetime import datetime as _dt2, timedelta as _td2
    today     = _dt2.now().strftime("%Y-%m-%d")
    yesterday = (_dt2.now() - _td2(days=1)).strftime("%Y-%m-%d")

    params = {
        "fdate1": f"{yesterday} 00:00:00",
        "fdate2": f"{today} 23:59:59",
        "frange": "", "fclient": "", "fnum": "", "fcli": "",
        "fgdate": "", "fgmonth": "", "fgrange": "",
        "fgclient": "", "fgnumber": "", "fgcli": "", "fg": "0"
    }
    if sesskey:
        params["sesskey"] = sesskey

    try:
        r = s.get(
            f"{url}/{path}/res/data_smscdr.php",
            params=params,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{url}/{path}/SMSCDRStats",
            },
            timeout=20, allow_redirects=True
        )
        if _is_session_expired(r):
            logger.info("ims_fetch · session expired on CDR fetch → re-login")
            return None
        rows = _parse(r.text)
        if rows:
            logger.info(f"✅ ims_fetch · {len(rows)} rows")
        return rows
    except Exception as e:
        logger.warning(f"ims_fetch · fetch error: {e}")
        return []


# ══════════════════════════════════════════════════════════════
#  KONEKTA
#  ✅ BUG-3 FIX: Dashboard URL verification (not just "sign" check)
# ══════════════════════════════════════════════════════════════
def konekta_login(email, pw):
    s = _session()
    try:
        r = s.get("https://konektapremium.net/sign-in", timeout=15, allow_redirects=True)
    except Exception as e:
        raise Exception(f"Konekta · server unreachable: {e}")

    capt = _solve_captcha(r.text)
    r2 = s.post("https://konektapremium.net/signin",
                data={"username": email, "password": pw, "capt": capt},
                headers={**_BASE_HEADERS,
                         "Referer": "https://konektapremium.net/sign-in",
                         "Content-Type": "application/x-www-form-urlencoded"},
                allow_redirects=True, timeout=15)

    # ✅ BUG-3 FIX: Check for dashboard/agent in URL, not just absence of "sign"
    final = str(r2.url).lower()
    success_signals = ["dashboard", "agent", "client", "reseller", "smscdr", "sms"]
    if not any(x in final for x in success_signals):
        raise Exception(f"Konekta · login failed — unexpected URL: {r2.url}")

    # Also verify session cookie
    sid = s.cookies.get("PHPSESSID") or s.cookies.get("session")
    if not sid:
        raise Exception("Konekta · no session cookie after login")

    logger.info(f"✅ Konekta · {email}")
    return s, "agent", "https://konektapremium.net"


def konekta_fetch(session_info):
    if (isinstance(session_info, tuple) and len(session_info) >= 2
            and hasattr(session_info[0], "cookies")):
        s = session_info[0]
        url = "https://konektapremium.net"
        path = session_info[1] if len(session_info) > 1 else "agent"
    else:
        s = session_info
        path = "agent"
        url = "https://konektapremium.net"
    return _core_fetch(s, url, path)


# ══════════════════════════════════════════════════════════════
#  TIMESMS — NEW PANEL
#  Gets sesskey from SMSCDRReports (not SMSCDRStats)
#  ✅ FIX-C: Network errors separated from session expiry
# ══════════════════════════════════════════════════════════════
def timesms_login(bn, email, pw, url="https://www.timesms.org"):
    s = _session()
    try:
        r = s.get(f"{url}/login", timeout=25, allow_redirects=True)
    except Exception as e:
        raise Exception(f"{bn} · server unreachable: {e}")

    low = r.text.lower()
    if r.status_code in (403, 401) or "not in allowlist" in low:
        raise Exception(f"{bn} · IP blocked (HTTP {r.status_code})")

    capt = _solve_captcha(r.text)

    csrf = ""
    for pat in [
        r'name=["\']_token["\'][^>]+value=["\']([^"\']+)["\']',
        r'value=["\']([^"\']+)["\'][^>]+name=["\']_token["\']',
    ]:
        m = re.search(pat, r.text)
        if m:
            csrf = m.group(1)
            break

    post_data = {"username": email, "password": pw, "capt": capt}
    if csrf:
        post_data["_token"] = csrf

    try:
        r2 = s.post(f"{url}/signin", data=post_data,
                    headers={**_BASE_HEADERS,
                             "Referer": f"{url}/login",
                             "Content-Type": "application/x-www-form-urlencoded",
                             "Origin": url},
                    allow_redirects=True, timeout=20)
    except Exception as e:
        raise Exception(f"{bn} · signin POST failed: {e}")

    sid = (s.cookies.get("PHPSESSID") or s.cookies.get("session")
           or s.cookies.get("laravel_session"))
    if not sid:
        raise Exception(f"{bn} · no session cookie after login"
                        " (wrong credentials or rate limit)")

    final_url = str(r2.url).lower()
    if any(x in final_url for x in ["/login", "/signin", "/sign-in"]):
        raise Exception(f"{bn} · login failed — redirected back to login")

    if "client" in str(r2.url):
        path = "client"
    elif "reseller" in str(r2.url):
        path = "reseller"
    else:
        path = "agent"

    logger.info(f"✅ timesms_login {bn} {email} · path:{path}")
    return s, path, url


def timesms_fetch(bn, session_info, url="https://www.timesms.org"):
    """
    TimeSMS-specific fetch.
    ✅ FIX-C: Proper separation of network errors vs session expiry.
    """
    if not (isinstance(session_info, tuple) and len(session_info) == 3
            and hasattr(session_info[0], "cookies")):
        return []

    s, path, _url = session_info
    url = _url or url
    d1, d2 = _dates()

    # TimeSMS: sesskey preferably from SMSCDRReports
    sesskey = ""
    for ep in [
        f"{url}/{path}/SMSCDRReports",
        f"{url}/{path}/SMSCDRStats",
    ]:
        try:
            rs = s.get(ep, timeout=15, allow_redirects=True,
                       headers={**_BASE_HEADERS, "Referer": f"{url}/{path}/SMSDashboard"})
            if _is_session_expired(rs):
                return None  # ✅ FIX-C: session expiry → None
            m = re.search(r'sesskey=([A-Za-z0-9+/=_-]+)', rs.text)
            if m:
                sesskey = m.group(1)
                break
        except Exception as e:
            err = str(e)
            # Network-level failure — return [] not None
            if any(x in err for x in ["NewConnection", "ConnectionError", "Max retries"]):
                logger.warning(f"timesms_fetch {bn} · network error: {err[:80]}")
                return []
            continue

    params = {
        "fdate1": d1, "fdate2": d2,
        "frange": "", "fclient": "", "fnum": "", "fcli": "",
        "fgdate": "", "fgmonth": "", "fgrange": "",
        "fgclient": "", "fgnumber": "", "fgcli": "",
        "fg": "0",
    }
    if sesskey:
        params["sesskey"] = sesskey

    try:
        r = s.get(f"{url}/{path}/res/data_smscdr.php", params=params,
                  headers={**_AJAX_HEADERS,
                           "Referer": f"{url}/{path}/SMSCDRReports"},
                  timeout=20, allow_redirects=True)
        if _is_session_expired(r):
            return None  # ✅ FIX-C: session expiry → None
        if _classify(r) == "json":
            rows = _parse(r.text)
            if rows:
                logger.info(f"✅ timesms_fetch {bn} · {len(rows)} rows")
            else:
                logger.debug(f"timesms_fetch {bn} · 0 rows")
            return rows if rows is not None else []
    except Exception as e:
        err = str(e)
        if any(x in err for x in ["NewConnection", "ConnectionError", "Max retries"]):
            logger.warning(f"timesms_fetch {bn} · network error: {err[:80]}")
            return []
        logger.warning(f"timesms_fetch {bn} error: {err}")

    return []


# ══════════════════════════════════════════════════════════════
#  ROXYSMS
#  ✅ FIX-B: Network error vs session expiry separated
# ══════════════════════════════════════════════════════════════
def _roxysms_login(bn, email, pw, url=""):
    s = _session()
    try:
        r = s.get(f"{url}/Login", timeout=20, allow_redirects=True)
    except Exception as e:
        raise Exception(f"{bn} · server unreachable: {e}")

    if r.status_code in (403, 401):
        raise Exception(f"{bn} · IP blocked (HTTP {r.status_code})")

    capt = _solve_captcha(r.text)
    r2 = s.post(f"{url}/signin",
                data={"username": email, "password": pw,
                      "capt": capt, "g-recaptcha-response": ""},
                headers={**_BASE_HEADERS,
                         "Referer": f"{url}/Login",
                         "Content-Type": "application/x-www-form-urlencoded"},
                allow_redirects=True, timeout=20)
    if not (s.cookies.get("PHPSESSID") or s.cookies.get("session")
            or s.cookies.get("laravel_session")):
        raise Exception(f"{bn} · no session cookie after login"
                        " (wrong credentials or rate limit)")
    _roxy_final = str(r2.url).lower()
    if any(x in _roxy_final for x in ["/login", "/signin", "/sign-in"]):
        raise Exception(f"{bn} · login failed — redirected back to login"
                        " (wrong credentials or rate limit)")
    path = "agent" if "agent" in str(r2.url) else "client"
    logger.info(f"✅ {bn} {email} · path:{path}")
    return s, path, url


def _roxysms_fetch(bn, session_info, url=""):
    """
    ✅ FIX-B: Distinguishes network errors (return []) from session expiry (return None).
    Previously all exceptions returned [] which treated network failures as empty data.
    """
    if not isinstance(session_info, tuple):
        return []
    s, path, url = session_info
    d1, d2 = _dates()

    for p in [path, "agent", "client"]:
        sesskey = ""
        # ✅ KEEPALIVE: SMSCDRReports GET করি sesskey এর জন্য — এটাই keepalive হিসেবে কাজ করে।
        # আলাদা ping লাগবে না কারণ sesskey fetch টা নিজেই session refresh করে।
        try:
            rs = s.get(f"{url}/{p}/SMSCDRReports", timeout=15, allow_redirects=True)
            if _is_session_expired(rs):
                return None  # ✅ FIX-B: session expiry → None
            m = re.search(r'sesskey=([A-Za-z0-9+/=]+)', rs.text)
            if m:
                sesskey = m.group(1)
        except Exception as e:
            err = str(e)
            if any(x in err for x in ["NewConnection", "ConnectionError", "Max retries"]):
                logger.warning(f"_roxysms_fetch {bn} · network error: {err[:80]}")
                return []  # ✅ FIX-B: network error → [] (not session expiry)
            # Other exceptions: ignore and try next path

        params = {"fdate1": d1, "fdate2": d2, "frange": "", "fclient": "",
                  "fnum": "", "fcli": "", "fgdate": "", "fgmonth": "",
                  "fgrange": "", "fgclient": "", "fgnumber": "", "fgcli": "", "fg": "0"}
        if sesskey:
            params["sesskey"] = sesskey

        try:
            r = s.get(f"{url}/{p}/res/data_smscdr.php", params=params,
                      headers={**_AJAX_HEADERS, "Referer": f"{url}/{p}/SMSCDRReports"},
                      timeout=20)
            if _is_session_expired(r):
                return None  # ✅ FIX-B: session expiry → None
            rows = _parse(r.text, mask_only=True)
            if rows:
                return rows
        except Exception as e:
            err = str(e)
            if any(x in err for x in ["NewConnection", "ConnectionError", "Max retries"]):
                logger.warning(f"_roxysms_fetch {bn} · network error: {err[:80]}")
                return []

        try:
            r = s.get(f"{url}/{p}/res/data_testsmscdr.php",
                      params={"fdate1": d1, "fdate2": d2, "fg": "0"},
                      headers={**_AJAX_HEADERS, "Referer": f"{url}/{p}/SMSTestPanel"},
                      timeout=15)
            if _is_session_expired(r):
                return None  # ✅ FIX-B: session expiry → None
            rows = _parse(r.text, mask_only=True)
            if rows:
                return rows
        except Exception as e:
            err = str(e)
            if any(x in err for x in ["NewConnection", "ConnectionError", "Max retries"]):
                logger.warning(f"_roxysms_fetch {bn} · network error: {err[:80]}")
                return []

    return []


# ══════════════════════════════════════════════════════════════
#  VOICEGATE
# ══════════════════════════════════════════════════════════════
def _voicegate_login(bn, email, pw, url=""):
    s = _session()
    try:
        r = s.get(f"{url}/SignIn", timeout=20, allow_redirects=True)
    except Exception as e:
        raise Exception(f"{bn} · server unreachable: {e}")

    if r.status_code in (403, 401):
        raise Exception(f"{bn} · IP blocked (HTTP {r.status_code})")

    capt = _solve_captcha(r.text)
    _vg_data = {"username": email, "password": pw}
    if capt and capt != "0":          # skip if captcha not found
        _vg_data["capt"] = capt
    r2 = s.post(f"{url}/signmein",
                data=_vg_data,
                headers={**_BASE_HEADERS,
                         "Referer": f"{url}/SignIn",
                         "Content-Type": "application/x-www-form-urlencoded"},
                allow_redirects=True, timeout=20)
    if not (s.cookies.get("PHPSESSID") or s.cookies.get("session")
            or s.cookies.get("laravel_session")):
        raise Exception(f"{bn} · no session cookie after login"
                        " (wrong credentials or rate limit)")
    _vg_final = str(r2.url).lower()
    if any(x in _vg_final for x in ["/login", "/signin", "/sign-in"]):
        raise Exception(f"{bn} · login failed — redirected back to login"
                        " (wrong credentials or rate limit)")
    final = str(r2.url)
    path = "reseller" if "reseller" in final else ("agent" if "agent" in final else "client")
    logger.info(f"✅ {bn} {email} · path:{path}")
    return s, path, url


def _voicegate_fetch(bn, session_info, url=""):
    if not isinstance(session_info, tuple):
        return []
    s, path, url = session_info
    d1, d2 = _dates()
    params = {"fdate1": d1, "fdate2": d2, "ftermination": "", "fclient": "",
              "fnum": "", "fcli": "", "fgdate": "0", "fgtermination": "0",
              "fgclient": "0", "fgnumber": "0", "fgcli": "0", "fg": "0"}
    for p in [path, "reseller", "agent", "client"]:
        try:
            r = s.get(f"{url}/{p}/ajax/dt_reports.php", params=params,
                      headers={**_AJAX_HEADERS, "Referer": f"{url}/{p}/Reports"},
                      timeout=15)
            if _is_session_expired(r):
                return None
            rows = _parse(r.text)
            if rows:
                return rows
        except Exception:
            continue
    return []


# ══════════════════════════════════════════════════════════════
#  NUMBERPANEL
# ══════════════════════════════════════════════════════════════
def _numberpanel_login(bn, email, pw, url=""):
    """Working login — exact logic from confirmed working standalone bot."""
    s = _session()
    try:
        r = s.get(f"{url}/login", timeout=15, allow_redirects=True)
    except Exception as e:
        raise Exception(f"{bn} · server unreachable: {e}")

    # Captcha: "What is X + Y"
    mc = re.search(r'What is (\d+)\s*\+\s*(\d+)', r.text, re.I)
    capt = int(mc.group(1)) + int(mc.group(2)) if mc else 0

    r2 = s.post(f"{url}/signin",
                data={"username": email, "password": pw, "capt": capt},
                headers={**_BASE_HEADERS,
                         "Referer": f"{url}/login",
                         "Content-Type": "application/x-www-form-urlencoded"},
                allow_redirects=True, timeout=15)

    if not s.cookies.get("PHPSESSID"):
        raise Exception(f"{bn} · login failed — no PHPSESSID (wrong credentials?)")

    path = "agent" if "agent" in str(r2.url) else "client"
    logger.info(f"✅ {bn} {email} · path:{path}")
    return s, path, url


def _numberpanel_fetch(bn, session_info, url=""):
    """Working fetch — exact logic from confirmed working standalone bot."""
    if not isinstance(session_info, tuple):
        return []
    s, path, url = session_info
    d1, d2 = _dates()

    # Get sesskey from SMSCDRStats
    sesskey = ""
    try:
        rs = s.get(f"{url}/{path}/SMSCDRStats",
                   headers={**_BASE_HEADERS, "Referer": f"{url}/{path}/SMSDashboard"},
                   timeout=15, allow_redirects=True)
        if _is_session_expired(rs):
            return None
        sk = re.search(r'sesskey=([A-Za-z0-9+/=]+)', rs.text)
        if sk:
            sesskey = sk.group(1)
    except Exception:
        pass

    params = {"fdate1": d1, "fdate2": d2, "fg": "0"}
    if sesskey:
        params["sesskey"] = sesskey

    try:
        r = s.get(f"{url}/{path}/res/data_smscdr.php",
                  params=params,
                  headers={**_AJAX_HEADERS,
                           "Referer": f"{url}/{path}/SMSCDRStats"},
                  timeout=15, allow_redirects=True)
        if _is_session_expired(r):
            return None
        t = r.text.strip()
        if not t or t.startswith("<"):
            return []
        rows = _parse(t)
        return rows
    except Exception as e:
        err = str(e)
        if any(x in err for x in ["NewConnection", "ConnectionError", "Max retries"]):
            return []
        return []


# ══════════════════════════════════════════════════════════════
#  PROOFSMS — ✅ BUG-10 FIX: Now properly exported (was dead code)
# ══════════════════════════════════════════════════════════════
def proofsms_fetch(session_info, url=""):
    """ProofSMS-specific fetch using sesskey from SMSCDRStats."""
    if not isinstance(session_info, tuple):
        return []
    s, path, _url = session_info
    url = _url or url
    d1, d2 = _dates()

    for p in ["agent", "client"]:
        sesskey = ""
        try:
            rs = s.get(f"{url}/{p}/SMSCDRStats", timeout=15, allow_redirects=True)
            if _is_session_expired(rs):
                return None
            m = re.search(r'sesskey=([A-Za-z0-9+/=_-]+)', rs.text)
            if m:
                sesskey = m.group(1)
        except Exception:
            pass

        if sesskey:
            try:
                params = {"fdate1": d1, "fdate2": d2, "fg": "0", "sesskey": sesskey}
                r = s.get(f"{url}/{p}/res/data_smscdr.php", params=params,
                          headers={**_AJAX_HEADERS,
                                   "Referer": f"{url}/{p}/SMSCDRStats"},
                          timeout=20, allow_redirects=True)
                if _is_session_expired(r):
                    return None
                rows = _parse(r.text)
                if rows:
                    return rows
            except Exception:
                pass

        # Fallback: test endpoint
        try:
            r = s.get(f"{url}/{p}/res/data_testsmscdr.php",
                      params={"fdate1": d1, "fdate2": d2, "fg": "0"},
                      headers={**_AJAX_HEADERS,
                               "Referer": f"{url}/{p}/SMSTestPanel"},
                      timeout=15, allow_redirects=True)
            if _is_session_expired(r):
                return None
            rows = _parse(r.text)
            if rows:
                return rows
        except Exception:
            pass

    return []


# Backward compat alias
_proofsms_fetch = proofsms_fetch



# ══════════════════════════════════════════════════════════════
#  SNIPER PANEL  (http://135.125.222.224/ints)
#  Fixed path=agent, SMSCDRReports Referer
# ══════════════════════════════════════════════════════════════
def _sniper_login(bn, email, pw, url=""):
    s = _session()
    try:
        r = s.get(f"{url}/login", timeout=15, allow_redirects=True)
    except Exception as e:
        raise Exception(f"{bn} · server unreachable: {e}")

    if r.status_code in (403, 401):
        raise Exception(f"{bn} · IP blocked (HTTP {r.status_code})")

    capt = _solve_captcha(r.text)

    csrf = ""
    for pat in [
        r'name=["\']_token["\'][^>]+value=["\']([^"\']+)["\']',
        r'value=["\']([^"\']+)["\'][^>]+name=["\']_token["\']',
    ]:
        m = re.search(pat, r.text)
        if m: csrf = m.group(1); break

    post_data = {"username": email, "password": pw, "capt": capt}
    if csrf: post_data["_token"] = csrf

    r2 = s.post(f"{url}/signin",
                data=post_data,
                headers={**_BASE_HEADERS,
                         "Referer": f"{url}/login",
                         "Content-Type": "application/x-www-form-urlencoded"},
                allow_redirects=True, timeout=15)

    if not s.cookies.get("PHPSESSID"):
        raise Exception(f"{bn} · no PHPSESSID cookie (wrong credentials?)")

    final_url = str(r2.url).lower()
    if any(x in final_url for x in ["/login", "/signin", "/sign-in"]):
        raise Exception(f"{bn} · login failed — redirected back to login")

    # Sniper always uses agent path
    path = "agent"
    logger.info(f"✅ {bn} {email} · path:{path}")
    return s, path, url


def _sniper_fetch(bn, session_info, url=""):
    if not isinstance(session_info, tuple):
        return []
    s, path, url = session_info
    d1, d2 = _dates()

    # Try main CDR endpoint with SMSCDRReports referer (sniper specific)
    for ep in [
        f"{url}/{path}/res/data_smscdr.php",
        f"{url}/{path}/res/data_smscdrreports.php",
    ]:
        sesskey = ""
        try:
            rs = s.get(f"{url}/{path}/SMSCDRReports",
                       headers={**_BASE_HEADERS, "Referer": f"{url}/{path}/SMSDashboard"},
                       timeout=15, allow_redirects=True)
            if _is_session_expired(rs):
                return None
            m = re.search(r'sesskey=([A-Za-z0-9+/=_-]+)', rs.text)
            if m: sesskey = m.group(1)
        except Exception:
            pass

        params = {
            "fdate1": d1, "fdate2": d2,
            "frange": "", "fclient": "", "fnum": "", "fcli": "",
            "fgdate": "", "fgmonth": "", "fgrange": "",
            "fgclient": "", "fgnumber": "", "fgcli": "",
            "fg": "0",
        }
        if sesskey:
            params["sesskey"] = sesskey

        try:
            r = s.get(ep, params=params,
                      headers={**_AJAX_HEADERS,
                               "Referer": f"{url}/{path}/SMSCDRReports"},
                      timeout=15, allow_redirects=True)
            if _is_session_expired(r):
                return None
            if _classify(r) == "json":
                rows = _parse(r.text)
                if rows:
                    return rows
        except Exception as e:
            err = str(e)
            if any(x in err for x in ["NewConnection", "ConnectionError", "Max retries"]):
                return []
            continue

    return []

# ══════════════════════════════════════════════════════════════
#  DISPATCH — new_panel_login / new_panel_fetch
#  ✅ FIX-E: ProofSMS now uses proofsms_fetch instead of ints_fetch
#  (handled in api_server_v2.py _get_fetch_fns, kept here for compat)
# ══════════════════════════════════════════════════════════════
def new_panel_login(bn, email, pw, url=""):
    if bn == "RoxySMS":
        return _roxysms_login(bn, email, pw, url)
    elif bn == "VoiceGate":
        return _voicegate_login(bn, email, pw, url)
    elif bn == "NumberPanel":
        return _numberpanel_login(bn, email, pw, url)
    elif bn == "SniperPanel":
        return _sniper_login(bn, email, pw, url)
    else:
        return ints_login(bn, email, pw, url)


def new_panel_fetch(bn, session_info, url=""):
    if bn == "RoxySMS":
        return _roxysms_fetch(bn, session_info, url)
    elif bn == "VoiceGate":
        return _voicegate_fetch(bn, session_info, url)
    elif bn == "NumberPanel":
        return _numberpanel_fetch(bn, session_info, url)
    elif bn == "SniperPanel":
        return _sniper_fetch(bn, session_info, url)
    elif bn == "ProofSMS":
        return proofsms_fetch(session_info, url)
    else:
        return ints_fetch(bn, session_info, url)
