"""
otp_bot_v2.py — Multi-User OTP Forwarder with Subscription System
==================================================================
Admin থেকে সব control করা যাবে।
User আলাদাভাবে নিজের panels, groups, template manage করবে।

REQUIREMENTS:
  pip install python-telegram-bot==20.* requests
  panel_fetchers.py একই folder এ থাকতে হবে
"""

import re, time, threading, logging, json, sqlite3, asyncio
import urllib.request
from datetime import datetime as _dt, timezone as _UTC, timedelta
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)

BOT_TOKEN  = "8513071962:AAEsrTjTvgBU100zhoU3KWBKZ8hI6fYR640"
ADMIN_IDS  = [7095358778]

DB_FILE = "otp_bot.db"

_BOT_START_TIME   = _dt.now(_UTC.utc)
_GRACE_PERIOD_SEC = 300
_OTP_MAX_AGE_SEC  = 50

TEMPLATE_EDITOR_URL = "https://your-railway-app.up.railway.app/template_editor.html"

DEFAULT_TEMPLATE = {
    "text": "{sender} | {flag} {country}\n📟 Number: {number_masked}",
    "buttons": [
        {"type": "otp", "label": "{otp}", "value": "{otp}"},
        {"type": "sep"},
        {"type": "link", "label": "📟 NUMBER", "value": "https://t.me/EANG_HUB_NBER_BOT"},
        {"type": "link", "label": "💬 SUPPORT", "value": "https://t.me/eng_hub_otp_group"}
    ]
}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("OTPBot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

_db_lock = threading.Lock()

def db_conn():
    c = sqlite3.connect(DB_FILE, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c

def db_init():
    with db_conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id      TEXT PRIMARY KEY,
            username     TEXT DEFAULT '',
            plan         TEXT DEFAULT 'weekly',
            expire_date  TEXT NOT NULL,
            panel_limit  INTEGER DEFAULT 3,
            active       INTEGER DEFAULT 1,
            warned       INTEGER DEFAULT 0,
            created_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS panels (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    TEXT NOT NULL,
            name       TEXT NOT NULL,
            url        TEXT NOT NULL,
            ptype      TEXT NOT NULL,
            fp         TEXT DEFAULT NULL,
            enabled    INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(user_id, name),
            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS accounts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            panel_id   INTEGER NOT NULL,
            username   TEXT NOT NULL,
            password   TEXT NOT NULL,
            active     INTEGER DEFAULT 1,
            FOREIGN KEY(panel_id) REFERENCES panels(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS chat_ids (
            user_id  TEXT NOT NULL,
            chat_id  TEXT NOT NULL,
            PRIMARY KEY(user_id, chat_id)
        );
        CREATE TABLE IF NOT EXISTS settings (
            user_id TEXT NOT NULL,
            key     TEXT NOT NULL,
            value   TEXT NOT NULL,
            PRIMARY KEY(user_id, key)
        );
        """)

def get_user(user_id):
    with db_conn() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?",
                         (str(user_id),)).fetchone()

def get_all_users():
    with db_conn() as c:
        return c.execute(
            "SELECT * FROM users ORDER BY active DESC, expire_date ASC"
        ).fetchall()

def add_user(user_id, username, plan, days, panel_limit):
    expire = (_dt.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with db_conn() as c:
        c.execute("""
            INSERT OR REPLACE INTO users
            (user_id, username, plan, expire_date, panel_limit, active, warned)
            VALUES (?,?,?,?,?,1,0)
        """, (str(user_id), username, plan, expire, panel_limit))

def extend_user(user_id, days):
    u = get_user(user_id)
    if not u:
        return False
    try:
        current = _dt.strptime(u["expire_date"], "%Y-%m-%d %H:%M:%S")
    except Exception:
        current = _dt.now()
    if current < _dt.now():
        current = _dt.now()
    new_exp = (current + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with db_conn() as c:
        c.execute("UPDATE users SET expire_date=?, active=1, warned=0 WHERE user_id=?",
                  (new_exp, str(user_id)))
    return True

def block_user(user_id):
    with db_conn() as c:
        c.execute("UPDATE users SET active=0 WHERE user_id=?", (str(user_id),))

def delete_user(user_id):
    with db_conn() as c:
        c.execute("DELETE FROM users WHERE user_id=?", (str(user_id),))

def is_user_active(user_id):
    u = get_user(user_id)
    if not u:
        return False
    if not u["active"]:
        return False
    try:
        exp = _dt.strptime(u["expire_date"], "%Y-%m-%d %H:%M:%S")
        if _dt.now() > exp:
            return False
    except Exception:
        return False
    return True

def days_remaining(user_id):
    u = get_user(user_id)
    if not u:
        return 0
    try:
        exp = _dt.strptime(u["expire_date"], "%Y-%m-%d %H:%M:%S")
        diff = (exp - _dt.now()).total_seconds()
        return max(0, int(diff / 86400))
    except Exception:
        return 0

def get_panels(user_id):
    with db_conn() as c:
        return c.execute(
            "SELECT * FROM panels WHERE user_id=? ORDER BY name",
            (str(user_id),)
        ).fetchall()

def get_panel_by_id(panel_id):
    with db_conn() as c:
        return c.execute("SELECT * FROM panels WHERE id=?", (panel_id,)).fetchone()

def get_accounts(panel_id):
    with db_conn() as c:
        return c.execute(
            "SELECT * FROM accounts WHERE panel_id=? AND active=1", (panel_id,)
        ).fetchall()

def get_all_accounts(panel_id):
    with db_conn() as c:
        return c.execute(
            "SELECT * FROM accounts WHERE panel_id=?", (panel_id,)
        ).fetchall()

def get_chat_ids(user_id):
    with db_conn() as c:
        return [r["chat_id"] for r in c.execute(
            "SELECT chat_id FROM chat_ids WHERE user_id=?", (str(user_id),)
        )]

def get_user_setting(user_id, key, default=None):
    with db_conn() as c:
        r = c.execute(
            "SELECT value FROM settings WHERE user_id=? AND key=?",
            (str(user_id), key)
        ).fetchone()
        return r["value"] if r else default

def set_user_setting(user_id, key, value):
    with db_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO settings(user_id,key,value) VALUES(?,?,?)",
            (str(user_id), key, value)
        )

BUILTIN_PANELS = {
    "ChoiceSMS":   {"url": "http://51.77.52.79/ints",          "ptype": "ints"},
    "FlynSMS":     {"url": "http://91.232.105.47/ints",        "ptype": "ints"},
    "Gaza":        {"url": "http://144.217.71.192/ints",       "ptype": "ints", "fp": "agent"},
    "GoatPanel":   {"url": "http://167.114.117.67/ints",       "ptype": "ints"},
    "HADI_SMS":    {"url": "http://2.59.169.96/ints",          "ptype": "ints"},
    "ImsPanel":    {"url": "https://www.imssms.org",           "ptype": "ims"},
    "KmSms":       {"url": "http://54.36.173.235/ints",        "ptype": "ints"},
    "Konekta":     {"url": "https://konektapremium.net",       "ptype": "konekta"},
    "MsiSMS":      {"url": "http://145.239.130.45/ints",       "ptype": "ints"},
    "NumberPanel": {"url": "http://51.89.99.105/NumberPanel",  "ptype": "numberpanel"},
    "ProofSMS":    {"url": "http://217.182.195.194/ints",      "ptype": "proofsms"},
    "PurplePanel": {"url": "http://85.195.94.50/sms",          "ptype": "standard"},
    "RoxySMS":     {"url": "http://www.roxysms.net",           "ptype": "roxy"},
    "Seven1Tel":   {"url": "http://94.23.120.156/ints",        "ptype": "ints"},
    "SharkSMS":    {"url": "http://65.109.111.158/ints",       "ptype": "ints"},
    "TrueSMS":     {"url": "https://truesms.net",              "ptype": "standard"},
    "VoiceGate":   {"url": "http://51.89.7.175/sms",           "ptype": "voicegate"},
    "Wolf":        {"url": "http://213.32.24.208/ints",        "ptype": "ints"},
    "GreenSMS":    {"url": "http://139.99.9.4/ints",           "ptype": "ints"},
    "MarkOI":      {"url": "http://51.75.144.178/ints",        "ptype": "ints"},
    "FireSMS":     {"url": "http://54.39.104.241/ints",        "ptype": "ints"},
    "SniperPanel": {"url": "http://135.125.222.224/ints",      "ptype": "ints"},
    "MAIT":        {"url": "http://168.119.13.175/ints",       "ptype": "ints", "fp": "agent"},
    "TimeSMS":     {"url": "https://www.timesms.org",          "ptype": "timesms"},
}

PANEL_TYPES = ["ints","ims","konekta","standard","proofsms",
               "roxy","voicegate","numberpanel","timesms"]

_SLOW      = {"SharkSMS","KmSms","MsiSMS","GoatPanel","Wolf","Gaza"}
_MEDIUM    = {"MarkOI","SniperPanel","MAIT"}
_KEEPALIVE = {"ImsPanel","RoxySMS"}

COUNTRY_DATA = {
    "1876":("🇯🇲","JM","Jamaica"),   "1868":("🇹🇹","TT","Trinidad"),
    "1246":("🇧🇧","BB","Barbados"),  "1242":("🇧🇸","BS","Bahamas"),
    "1":   ("🇺🇸","US","USA"),
    "77":  ("🇰🇿","KZ","Kazakhstan"),"76":("🇰🇿","KZ","Kazakhstan"),
    "79":  ("🇷🇺","RU","Russia"),    "7": ("🇷🇺","RU","Russia"),
    "880": ("🇧🇩","BD","Bangladesh"),"852":("🇭🇰","HK","Hong Kong"),
    "886": ("🇹🇼","TW","Taiwan"),
    "960": ("🇲🇻","MV","Maldives"),  "961":("🇱🇧","LB","Lebanon"),
    "962": ("🇯🇴","JO","Jordan"),    "963":("🇸🇾","SY","Syria"),
    "964": ("🇮🇶","IQ","Iraq"),      "965":("🇰🇼","KW","Kuwait"),
    "966": ("🇸🇦","SA","Saudi Arabia"),"967":("🇾🇪","YE","Yemen"),
    "968": ("🇴🇲","OM","Oman"),      "970":("🇵🇸","PS","Palestine"),
    "971": ("🇦🇪","AE","UAE"),       "972":("🇮🇱","IL","Israel"),
    "973": ("🇧🇭","BH","Bahrain"),   "974":("🇶🇦","QA","Qatar"),
    "977": ("🇳🇵","NP","Nepal"),     "992":("🇹🇯","TJ","Tajikistan"),
    "993": ("🇹🇲","TM","Turkmenistan"),"994":("🇦🇿","AZ","Azerbaijan"),
    "995": ("🇬🇪","GE","Georgia"),   "996":("🇰🇬","KG","Kyrgyzstan"),
    "998": ("🇺🇿","UZ","Uzbekistan"),
    "81":  ("🇯🇵","JP","Japan"),     "82":("🇰🇷","KR","South Korea"),
    "84":  ("🇻🇳","VN","Vietnam"),   "86":("🇨🇳","CN","China"),
    "90":  ("🇹🇷","TR","Turkey"),    "91":("🇮🇳","IN","India"),
    "92":  ("🇵🇰","PK","Pakistan"),  "93":("🇦🇫","AF","Afghanistan"),
    "94":  ("🇱🇰","LK","Sri Lanka"), "95":("🇲🇲","MM","Myanmar"),
    "98":  ("🇮🇷","IR","Iran"),
    "60":  ("🇲🇾","MY","Malaysia"),  "61":("🇦🇺","AU","Australia"),
    "62":  ("🇮🇩","ID","Indonesia"), "63":("🇵🇭","PH","Philippines"),
    "65":  ("🇸🇬","SG","Singapore"), "66":("🇹🇭","TH","Thailand"),
    "20":  ("🇪🇬","EG","Egypt"),     "27":("🇿🇦","ZA","South Africa"),
    "212": ("🇲🇦","MA","Morocco"),   "213":("🇩🇿","DZ","Algeria"),
    "216": ("🇹🇳","TN","Tunisia"),   "218":("🇱🇾","LY","Libya"),
    "234": ("🇳🇬","NG","Nigeria"),   "254":("🇰🇪","KE","Kenya"),
    "30":  ("🇬🇷","GR","Greece"),    "31":("🇳🇱","NL","Netherlands"),
    "32":  ("🇧🇪","BE","Belgium"),   "33":("🇫🇷","FR","France"),
    "34":  ("🇪🇸","ES","Spain"),     "36":("🇭🇺","HU","Hungary"),
    "39":  ("🇮🇹","IT","Italy"),     "40":("🇷🇴","RO","Romania"),
    "41":  ("🇨🇭","CH","Switzerland"),"43":("🇦🇹","AT","Austria"),
    "44":  ("🇬🇧","GB","UK"),        "45":("🇩🇰","DK","Denmark"),
    "46":  ("🇸🇪","SE","Sweden"),    "47":("🇳🇴","NO","Norway"),
    "48":  ("🇵🇱","PL","Poland"),    "49":("🇩🇪","DE","Germany"),
    "351": ("🇵🇹","PT","Portugal"),  "353":("🇮🇪","IE","Ireland"),
    "358": ("🇫🇮","FI","Finland"),   "380":("🇺🇦","UA","Ukraine"),
    "420": ("🇨🇿","CZ","Czech Republic"),
    "52":  ("🇲🇽","MX","Mexico"),    "55":("🇧🇷","BR","Brazil"),
    "221": ("🇸🇳","SN","Senegal"),   "229":("🇧🇯","BJ","Benin"),
    "233": ("🇬🇭","GH","Ghana"),     "237":("🇨🇲","CM","Cameroon"),
    "251": ("🇪🇹","ET","Ethiopia"),  "252":("🇸🇴","SO","Somalia"),
    "253": ("🇩🇯","DJ","Djibouti"), "255":("🇹🇿","TZ","Tanzania"),
    "256": ("🇺🇬","UG","Uganda"),   "257":("🇧🇮","BI","Burundi"),
    "258": ("🇲🇿","MZ","Mozambique"),"260":("🇿🇲","ZM","Zambia"),
    "261": ("🇲🇬","MG","Madagascar"),"263":("🇿🇼","ZW","Zimbabwe"),
    "264": ("🇳🇦","NA","Namibia"),  "265":("🇲🇼","MW","Malawi"),
    "266": ("🇱🇸","LS","Lesotho"),  "267":("🇧🇼","BW","Botswana"),
    "268": ("🇸🇿","SZ","Eswatini"), "269":("🇰🇲","KM","Comoros"),
    "241": ("🇬🇦","GA","Gabon"),    "242":("🇨🇬","CG","Congo"),
    "243": ("🇨🇩","CD","DR Congo"), "244":("🇦🇴","AO","Angola"),
    "245": ("🇬🇼","GW","Guinea-Bissau"),"248":("🇸🇨","SC","Seychelles"),
    "249": ("🇸🇩","SD","Sudan"),    "250":("🇷🇼","RW","Rwanda"),
    "220": ("🇬🇲","GM","Gambia"),   "222":("🇲🇷","MR","Mauritania"),
    "223": ("🇲🇱","ML","Mali"),     "224":("🇬🇳","GN","Guinea"),
    "225": ("🇨🇮","CI","Ivory Coast"),"226":("🇧🇫","BF","Burkina Faso"),
    "227": ("🇳🇪","NE","Niger"),    "228":("🇹🇬","TG","Togo"),
    "230": ("🇲🇺","MU","Mauritius"),"231":("🇱🇷","LR","Liberia"),
    "232": ("🇸🇱","SL","Sierra Leone"),"235":("🇹🇩","TD","Chad"),
    "236": ("🇨🇫","CF","Central African Republic"),
    "238": ("🇨🇻","CV","Cape Verde"),"239":("🇸🇹","ST","Sao Tome"),
    "240": ("🇬🇶","GQ","Eq. Guinea"),
}

FLAG_STICKER = {
    "🇦🇫":"5291937511591925566","🇧🇩":"5291824687096027834",
    "🇨🇳":"5294068833277990704","🇩🇪":"5292013274815028523",
    "🇪🇬":"5293992082212409502","🇫🇷":"5291817660529533837",
    "🇬🇧":"5293993521026453119","🇭🇰":"5292166459118606932",
    "🇮🇳":"5291933173674957761","🇮🇩":"5291915686100012878",
    "🇮🇷":"5294220170745630736","🇮🇶":"5294325010897327367",
    "🇮🇹":"5291826830284709120","🇯🇵":"5291799063321139445",
    "🇰🇷":"5294408281723262763","🇰🇿":"5294227175837290463",
    "🇲🇾":"5291858351049696702","🇲🇻":"5292004203844097218",
    "🇳🇬":"5294456308047563965","🇳🇱":"5291917797692042265",
    "🇵🇰":"5291825606219029010","🇵🇭":"5291798075478661634",
    "🇷🇺":"5294335323113807278","🇸🇦":"5294163983983463099",
    "🇸🇬":"5294451304410663668","🇿🇦":"5294325281480266304",
    "🇹🇷":"5293993400767367408","🇹🇭":"5293994384314882755",
    "🇺🇸":"5294244076533600593","🇺🇦":"5294263837678131580",
    "🇦🇪":"5294314831824835370","🇻🇳":"5294235963340379688",
    "🇬🇷":"5291948395039054764","🇵🇱":"5292190970496963836",
    "🇧🇷":"5291892229751723900","🇲🇽":"5294535073452809778",
    "🇳🇵":"5294458756178924088","🇱🇰":"5292102670264328257",
    "🇲🇲":"5294254478944393569","🇰🇼":"5292066437920218075",
    "🇶🇦":"5292166360334357676","🇴🇲":"5291813666209946812",
    "🇾🇪":"5294058972033076492","🇯🇴":"5291988613112814801",
    "🇸🇾":"5294013428199869487","🇱🇧":"5294193108156699621",
    "🇵🇸":"5294289826525238172","🇧🇭":"5294108398516720753",
    "🇰🇬":"5292091954320922577","🇹🇯":"5294120269806328883",
    "🇹🇲":"5294098958178603764","🇺🇿":"5294217645304864345",
    "🇦🇿":"5294323533428579078","🇬🇪":"5294349389131697267",
    "🇦🇲":"5291978717508164018","🇺🇬":"5294192317882716626",
    "🇰🇪":"5292111852904416801","🇹🇿":"5292146096678658977",
    "🇬🇭":"5294347396266873249","🇪🇹":"5292245976143124155",
    "🇨🇲":"5291997306126626950","🇸🇳":"5292087023698466689",
    "🇲🇦":"5292108962391414885","🇩🇿":"5294048127240655242",
    "🇹🇳":"5294484680601521871","🇱🇾":"5291858711826946840",
    "🇸🇩":"5294177148058228060","🇷🇴":"5294107724206856227",
    "🇭🇺":"5294229581018975260","🇨🇿":"5294242852467923382",
    "🇸🇪":"5291737091238026321","🇳🇴":"5291761718580502030",
    "🇩🇰":"5294531860817268837","🇫🇮":"5294049961191690629",
    "🇦🇹":"5291975174160145850","🇨🇭":"5291791748991835084",
    "🇧🇪":"5291774466043435275","🇵🇹":"5294436555492973610",
    "🇮🇪":"5294471971793293647","🇪🇸":"5294513087515216901",
    "🇦🇺":"5294444247779399477","🇳🇿":"5294189019347833274",
    "🇹🇼":"5294095745543069603","🇰🇵":"5294193812531333564",
    "🇯🇲":"5294505107465982830","🇹🇹":"5294362935458548705",
    "🇧🇧":"5294526187165471742","🇧🇸":"5294031587321600012",
}

SVC_BTN_STICKER = {
    "WhatsApp":"5226587591318479107","Facebook":"5226800149249953341",
    "Telegram":"5229055548246231595","Discord":"5226520997850550976",
    "TikTok":"5226946891102591788","Instagram":"5229117911171370672",
    "PayPal":"5226837060198896309","Apple":"5228975653264591523",
    "Google":"5258274739041883702","Microsoft":"5282843764451195532",
    "Binance":"5199785165735367039","Twitter":"5354968347094046619",
    "ChatGPT":"5229046623304191555","SMS":"5253742260054409879",
    "1xBet":"5294049995551428114",
}

def _get_country(number):
    n = re.sub(r"\D","",str(number))
    for code in sorted(COUNTRY_DATA.keys(), key=len, reverse=True):
        if n.startswith(code):
            return COUNTRY_DATA[code]
    return ("🌍","UN","Unknown")

def _detect_otp(text):
    if not text: return None
    text = re.sub(r"<#>\s*","",str(text))
    m = re.search(r"(\d{3,4}-\d{3,4})(?!\d)", text)
    if m: return m.group(1)
    m = re.search(r"(?:^|[\s,])[#\uFF03]\s*(\d{4,8})\b", text)
    if m: return m.group(1)
    m = re.search(
        r"(?:code|otp|pin|passcode|verif\w*|codigo|كود|رمز|رقم|mot de passe|Password|Confirmation"
        r"|doğrulama|кодом|код|пароль|mã|รหัส|কোড|code de|codice|Código|Kode|kode)"
        r"[^\d]*(\d{4,8})", text, re.I)
    if m: return m.group(1)
    m = re.search(r"\b(?:is|est|are|beträgt|ist)\s+(\d{4,8})\b", text, re.I)
    if m: return m.group(1)
    m = re.search(
        r"(?:Telegram|WhatsApp|Facebook|Instagram|TikTok|Discord|Google|Apple|Binance)"
        r"[^\d]*(\d{4,8})", text, re.I)
    if m: return m.group(1)
    m = re.search(r"\b(?:use|enter|input|submit)\s+(\d{4,8})\b", text, re.I)
    if m: return m.group(1)
    m = re.search(r":\s*(\d{4,8})\b", text)
    if m: return m.group(1)
    for m in re.finditer(r"(?<![/\-\d])(\d{4,6})(?![/\-\d])", text):
        c = m.group(1)
        if re.match(r"^20[0-9]{2}$", c): continue
        if c.startswith("0") and len(c) <= 4: continue
        return c
    return None

def _detect_svc(sms, cli=""):
    SVCS = {
        "WhatsApp": ["whatsapp","wapp","wa "],
        "Facebook": ["facebook","fb "],
        "Telegram": ["telegram","tg "],
        "Instagram": ["instagram","ig "],
        "TikTok":   ["tiktok","tik tok"],
        "Google":   ["google","gmail"],
        "Discord":  ["discord"],
        "Twitter":  ["twitter","x.com"],
        "Apple":    ["apple","icloud"],
        "Binance":  ["binance"],
        "PayPal":   ["paypal"],
        "Microsoft":["microsoft","outlook"],
        "1xBet":    ["1xbet","1x bet"],
    }
    t = (sms+" "+cli).lower()
    for svc, keys in SVCS.items():
        if any(k in t for k in keys): return svc
    return cli.strip() if cli.strip() else "SMS"

def _mask_number(num_clean):
    if len(num_clean) < 7:
        return num_clean
    keep_start = len(num_clean) - 3
    if keep_start <= 4:
        return num_clean
    return num_clean[:4] + "SPYX" + num_clean[keep_start:]

LANG_DATA = {
    "EN": ("EN","English"), "AR": ("AR","Arabic"),
    "BN": ("BN","Bengali"), "CN": ("CN","Chinese"),
    "RU": ("RU","Russian"), "TR": ("TR","Turkish"),
    "FA": ("FA","Persian"), "HI": ("HI","Hindi"),
    "UR": ("UR","Urdu"),    "ID": ("ID","Indonesian"),
    "MS": ("MS","Malay"),   "TH": ("TH","Thai"),
    "VI": ("VI","Vietnamese"),"FR": ("FR","French"),
    "DE": ("DE","German"),  "ES": ("ES","Spanish"),
    "PT": ("PT","Portuguese"),"IT": ("IT","Italian"),
    "PL": ("PL","Polish"),  "UK": ("UK","Ukrainian"),
    "KO": ("KO","Korean"),  "JA": ("JA","Japanese"),
}

def _tg_raw_send_to(chat_ids_list, text, keyboard=None):
    for cid in chat_ids_list:
        payload = {"chat_id": cid, "text": text, "parse_mode": "HTML"}
        if keyboard:
            payload["reply_markup"] = json.dumps(keyboard)
        data = json.dumps(payload).encode()
        for attempt in range(4):
            try:
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    data=data, headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    r.read()
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    try:
                        body = json.loads(e.read().decode())
                        wait = int(body.get("parameters", {}).get("retry_after", 5))
                    except Exception:
                        wait = 5 * (attempt + 1)
                    time.sleep(wait)
                else:
                    log.error(f"TG forward → {cid}: {e}")
                    break
            except Exception as e:
                log.error(f"TG forward → {cid}: {e}")
                break
        time.sleep(0.05)

def _tg_notify_user(user_id, text):
    payload = {"chat_id": str(user_id), "text": text, "parse_mode": "HTML"}
    data = json.dumps(payload).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=data, headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
    except Exception as e:
        log.error(f"notify_user {user_id}: {e}")

def _render_template(tmpl, bn, number, otp, service, sms=""):
    num_clean  = re.sub(r"\D","",str(number))
    num_masked = _mask_number(num_clean)
    flag, iso, country = _get_country(number)
    fid      = FLAG_STICKER.get(flag)
    flag_tag = f'<tg-emoji emoji-id="{fid}">{flag}</tg-emoji>' if fid else flag
    svc_id   = SVC_BTN_STICKER.get(service)
    svc_icon = f'<tg-emoji emoji-id="{svc_id}">📩</tg-emoji>' if svc_id else "📩"

    lang_code = "EN"
    if re.search(r"[\u0600-\u06FF]", sms):
        if iso in ("PK","AF"): lang_code = "UR"
        elif iso == "IR":      lang_code = "FA"
        else:                  lang_code = "AR"
    elif re.search(r"[\u0980-\u09FF]", sms): lang_code = "BN"
    elif re.search(r"[\u4e00-\u9fff]", sms): lang_code = "CN"
    elif re.search(r"[\u0400-\u04FF]", sms):
        lang_code = "UK" if iso in ("UA",) else "RU"
    elif re.search(r"[\u0900-\u097F]", sms): lang_code = "HI"
    elif re.search(r"[\u0e00-\u0e7f]", sms): lang_code = "TH"
    elif re.search(r"[\uac00-\ud7af]", sms): lang_code = "KO"
    elif re.search(r"[\u3040-\u30ff]", sms): lang_code = "JA"
    elif re.search(r"\b(le|la|les|est|bonjour)\b", sms, re.I): lang_code = "FR"
    elif re.search(r"\b(der|die|das|ist|bitte)\b", sms, re.I): lang_code = "DE"

    lang_short, lang_full = LANG_DATA.get(lang_code, (lang_code, lang_code))
    country_tag = f"{flag} {iso} {country}"

    vars_ = {
        "{flag}":          flag_tag,
        "{flag_plain}":    flag,
        "{iso}":           iso,
        "{sender}":        service,
        "{number}":        num_clean,
        "{number_masked}": num_masked,
        "{otp}":           str(otp),
        "{language}":      lang_short,
        "{lang_full}":     lang_full,
        "{country}":       country,
        "{country_iso}":   iso,
        "{country_tag}":   country_tag,
        "{panel}":         bn,
        "{message}":       sms[:80] if sms else "",
        "{time}":          time.strftime("%H:%M"),
        "{svc_icon}":      svc_icon,
    }

    def subst(s):
        for k, v in vars_.items():
            s = s.replace(k, v)
        return s

    raw_text = tmpl.get("text", DEFAULT_TEMPLATE["text"])
    raw_text = raw_text.replace("\\n", "\n")
    raw_text = re.sub(r"\n(\n+)", lambda m: "\n" + "\u200b\n" * len(m.group(1)), raw_text)
    text = subst(raw_text)
    btns = tmpl.get("buttons", DEFAULT_TEMPLATE["buttons"])

    STYLE_MAP = {"primary":"primary","success":"success",
                 "danger":"danger","warn":"primary","accent":"primary","default":None}

    kb_rows = []
    cur_row = []
    for b in btns:
        if b.get("type") == "sep":
            if cur_row: kb_rows.append(cur_row); cur_row = []
            continue
        label = subst(b.get("label", service))
        if b.get("type") == "link":
            btn = {"text": label, "url": subst(b.get("value",""))}
        elif b.get("type") == "copy":
            btn = {"text": label, "copy_text": {"text": subst(b.get("value",str(otp)))}}
        else:
            btn = {"text": label, "copy_text": {"text": str(otp)}}

        raw_style = b.get("style","default")
        tg_style  = STYLE_MAP.get(raw_style)
        if tg_style:
            btn["style"] = tg_style

        sid = b.get("sticker_id") or (svc_id if b.get("type") == "otp" else None)
        if sid:
            btn["icon_custom_emoji_id"] = sid

        cur_row.append(btn)
    if cur_row:
        kb_rows.append(cur_row)

    kb = {"inline_keyboard": kb_rows} if kb_rows else None
    return text, kb

def _build_otp_msg(user_id, bn, number, otp, service, sms=""):
    raw = get_user_setting(user_id, "otp_template")
    try:
        tmpl = json.loads(raw) if raw else DEFAULT_TEMPLATE
    except Exception:
        tmpl = DEFAULT_TEMPLATE
    return _render_template(tmpl, bn, number, otp, service, sms)

import queue as _queue

_fwd_seen: dict = {}
_fwd_lock = threading.Lock()
_send_queue: _queue.Queue = _queue.Queue()

def _sender_worker():
    while True:
        try:
            user_id, chat_ids_list, text, kb = _send_queue.get(timeout=5)
        except _queue.Empty:
            continue
        try:
            _tg_raw_send_to(chat_ids_list, text, kb)
        except Exception as e:
            log.error(f"sender_worker error: {e}")
        finally:
            _send_queue.task_done()
        time.sleep(0.8)

threading.Thread(target=_sender_worker, daemon=True, name="TGSender").start()

def _forward(user_id, bn, number, otp, service, sms):
    if not is_user_active(user_id):
        return
    chat_ids_list = get_chat_ids(user_id)
    if not chat_ids_list:
        return
    num_clean = re.sub(r"\D","",str(number))
    key = f"{user_id}:{bn}:+{num_clean}:{otp}"
    now = time.time()
    with _fwd_lock:
        if now - _fwd_seen.get(key, 0) < 90: return
        _fwd_seen[key] = now
        if len(_fwd_seen) > 10000:
            cutoff = now - 60
            _fwd_seen.clear() if len(_fwd_seen) > 10000 else None
    text, kb = _build_otp_msg(user_id, bn, number, otp, service, sms)
    _send_queue.put((user_id, chat_ids_list, text, kb))
    log.info(f"✅ [{user_id}][{bn}] +{num_clean} → {otp} ({service})")

_panel_stop:      dict = {}
_running_threads: dict = {}

def _get_panel_key(user_id, panel_id):
    return f"{user_id}:{panel_id}"

def _sleep_panel(bn, ptype):
    if ptype == "timesms":   time.sleep(30)
    elif bn in _KEEPALIVE:   time.sleep(5)
    elif bn in _SLOW:        time.sleep(20)
    elif bn in _MEDIUM:      time.sleep(12)
    else:                    time.sleep(8)

def _get_fns(bn, url, ptype, fp=None):
    try:
        from panel_fetchers import (
            ints_login, ints_fetch,
            ims_login, ims_fetch,
            konekta_login, konekta_fetch,
            panel_login, panel_fetch,
            new_panel_login, new_panel_fetch,
            timesms_login, timesms_fetch,
            proofsms_fetch,
        )
        if ptype == "timesms":
            return (lambda u,p: timesms_login(bn,u,p,url),
                    lambda s: timesms_fetch(bn,s,url))
        if ptype == "ims":
            return (lambda u,p: ims_login(u,p,url),
                    lambda s: ims_fetch(s,url))
        if ptype == "konekta":
            return (lambda u,p: konekta_login(u,p),
                    lambda s: konekta_fetch(s))
        if ptype in ("roxy","voicegate","numberpanel") or bn == "SniperPanel":
            return (lambda u,p: new_panel_login(bn,u,p,url),
                    lambda s: new_panel_fetch(bn,s,url))
        if ptype == "standard":
            return (lambda u,p: panel_login(bn,u,p,url),
                    lambda s: panel_fetch(s,url))
        if ptype == "proofsms":
            return (lambda u,p: ints_login(bn,u,p,url,fp),
                    lambda s: proofsms_fetch(s,url))
        return (lambda u,p: ints_login(bn,u,p,url,fp),
                lambda s: ints_fetch(bn,s,url))
    except Exception as e:
        log.error(f"[{bn}] _get_fns error: {e}")
        return None, None

def _account_loop(user_id, panel_id, bn, url, ptype, fp,
                  username, password, stop_evt):
    import html as _html
    login_fn, fetch_fn = _get_fns(bn, url, ptype, fp)
    if not login_fn:
        log.error(f"[{user_id}][{bn}:{username}] cannot get fetch functions")
        return

    log.info(f"▶ [{user_id}][{bn}:{username}] starting")
    seen=set(); session=None; fails=0; empty_s=0

    while not stop_evt.is_set():
        if not is_user_active(user_id):
            log.info(f"[{user_id}][{bn}:{username}] user expired/blocked → stopping")
            break

        try:
            if session is None:
                try:
                    session = login_fn(username, password)
                    log.info(f"[{user_id}][{bn}:{username}] login OK")
                    fails = 0
                except Exception as e:
                    fails += 1
                    log.error(f"[{user_id}][{bn}:{username}] login fail #{fails}: {e}")
                    stop_evt.wait(min(20*(2**min(fails-1,4)), 300))
                    continue

            try:
                rows = fetch_fn(session)
            except Exception as fe:
                log.warning(f"[{user_id}][{bn}:{username}] fetch error: {fe}")
                session=None; empty_s=0; stop_evt.wait(15); continue

            if rows is None:
                log.info(f"[{user_id}][{bn}:{username}] session expired → re-login")
                session=None; empty_s=0
                stop_evt.wait(5 if bn in _KEEPALIVE else 10)
                continue

            if not rows:
                empty_s += 1
                if empty_s >= 180:
                    log.info(f"[{user_id}][{bn}:{username}] 180 empty → re-login")
                    session=None; empty_s=0
                _sleep_panel(bn, ptype)
                continue
            empty_s = 0

            for row in rows:
                if isinstance(row, list):
                    num = str(row[2]) if len(row) > 2 else ""
                    cli = str(row[3]) if len(row) > 3 else ""
                    sms = str(row[5] if len(row) > 5 else (row[4] if len(row) > 4 else ""))
                    dt  = str(row[0]) if row else ""
                elif isinstance(row, dict):
                    num = str(row.get("number", row.get("num","")))
                    cli = str(row.get("cli",    row.get("service","")))
                    sms = str(row.get("sms",    row.get("message","")))
                    dt  = str(row.get("date",   row.get("dt","")))
                else:
                    continue

                if not num: continue
                num_clean = re.sub(r"\D","",num)
                otp = _detect_otp(sms)
                if not otp: continue

                if dt and dt not in ("None","0",""):
                    _row_dt = None
                    try:
                        _ts_val = float(dt.strip())
                        if 1577836800 < _ts_val < 2051222400:
                            _row_dt = _dt.fromtimestamp(_ts_val, tz=_UTC.utc)
                    except (ValueError, OSError):
                        pass
                    if _row_dt is None:
                        for _fmt in (
                            "%Y-%m-%d %H:%M:%S","%Y-%m-%dT%H:%M:%S",
                            "%Y-%m-%d %H:%M:%S.%f","%d/%m/%Y %H:%M:%S",
                            "%Y-%m-%dT%H:%M:%S.%f","%d-%m-%Y %H:%M:%S",
                            "%m/%d/%Y %H:%M:%S",
                        ):
                            try:
                                _row_dt = _dt.strptime(
                                    dt.strip(), _fmt
                                ).replace(tzinfo=_UTC.utc)
                                break
                            except ValueError:
                                pass
                    if _row_dt is not None:
                        _now        = _dt.now(_UTC.utc)
                        _uptime_sec = (_now - _BOT_START_TIME).total_seconds()
                        _age        = (_now - _row_dt).total_seconds()
                        if _uptime_sec < _GRACE_PERIOD_SEC:
                            if _row_dt < _BOT_START_TIME:
                                continue
                        else:
                            if _age > _OTP_MAX_AGE_SEC:
                                continue

                dt_clean = re.sub(r"\s+","",dt)
                key = (f"{dt_clean}:{num_clean}:{otp}"
                       if dt_clean and dt_clean not in ("None","0","")
                       else f"{num_clean}:{otp}")
                if key in seen: continue
                seen.add(key)
                if len(seen) > 8000:
                    seen = set(list(seen)[-3000:])

                svc = _detect_svc(sms, cli)
                _forward(user_id, bn, f"+{num_clean}", otp, svc,
                         _html.unescape(sms[:120]))

        except Exception as e:
            log.error(f"[{user_id}][{bn}:{username}] loop error: {e}")
            session = None

        _sleep_panel(bn, ptype)
    log.info(f"⏹ [{user_id}][{bn}:{username}] stopped")

def start_panel(user_id, panel_row):
    panel_id = panel_row["id"]
    bn       = panel_row["name"]
    url      = panel_row["url"]
    ptype    = panel_row["ptype"]
    fp       = panel_row["fp"]
    key      = _get_panel_key(user_id, panel_id)

    stop_panel_key(key)

    accounts = get_accounts(panel_id)
    if not accounts:
        log.warning(f"[{user_id}][{bn}] no active accounts — skipped")
        return False

    stop_evt = threading.Event()
    _panel_stop[key]      = stop_evt
    _running_threads[key] = []

    for acc in accounts:
        t = threading.Thread(
            target=_account_loop,
            args=(user_id, panel_id, bn, url, ptype, fp,
                  acc["username"], acc["password"], stop_evt),
            daemon=True,
            name=f"{user_id}:{bn}:{acc['username']}"
        )
        t.start()
        _running_threads[key].append(t)
        time.sleep(0.2)

    log.info(f"✅ [{user_id}][{bn}] started {len(accounts)} account(s)")
    return True

def stop_panel_key(key):
    if key in _panel_stop:
        _panel_stop[key].set()
        del _panel_stop[key]
    if key in _running_threads:
        del _running_threads[key]

def stop_panel(user_id, panel_id):
    stop_panel_key(_get_panel_key(user_id, panel_id))

def is_running(user_id, panel_id):
    key = _get_panel_key(user_id, panel_id)
    if key not in _panel_stop or _panel_stop[key].is_set():
        return False
    return any(t.is_alive() for t in _running_threads.get(key, []))

def stop_all_user_panels(user_id):
    panels = get_panels(user_id)
    for p in panels:
        stop_panel(user_id, p["id"])

def start_all_user_panels(user_id):
    count = 0
    panels = get_panels(user_id)
    for p in panels:
        if p["enabled"] and start_panel(user_id, p):
            count += 1
    return count

def start_all_on_boot():
    count = 0
    for u in get_all_users():
        if is_user_active(u["user_id"]):
            count += start_all_user_panels(u["user_id"])
    return count

def _expiry_checker():
    while True:
        time.sleep(3600)
        try:
            users = get_all_users()
            for u in users:
                uid = u["user_id"]
                if not u["active"]:
                    continue
                dr = days_remaining(uid)
                if dr <= 3 and not u["warned"]:
                    msg = (
                        f"⚠️ <b>Subscription Warning!</b>\n\n"
                        f"আপনার subscription মাত্র <b>{dr} দিন</b> বাকি আছে।\n"
                        f"Expire হওয়ার আগেই renew করুন।\n\n"
                        f"Support: @earning_hub_otp_group"
                    )
                    _tg_notify_user(uid, msg)
                    with db_conn() as c:
                        c.execute("UPDATE users SET warned=1 WHERE user_id=?", (uid,))
                    for admin_id in ADMIN_IDS:
                        _tg_notify_user(admin_id,
                            f"⚠️ User <code>{uid}</code> (@{u['username']}) "
                            f"এর subscription <b>{dr} দিন</b> বাকি।")

                if dr == 0 and u["active"]:
                    try:
                        exp = _dt.strptime(u["expire_date"], "%Y-%m-%d %H:%M:%S")
                        if _dt.now() > exp:
                            with db_conn() as c:
                                c.execute(
                                    "UPDATE users SET active=0 WHERE user_id=?",
                                    (uid,)
                                )
                            stop_all_user_panels(uid)
                            _tg_notify_user(uid,
                                "❌ <b>Subscription Expired!</b>\n\n"
                                "আপনার subscription শেষ হয়ে গেছে।\n"
                                "Renew করতে admin এর সাথে যোগাযোগ করুন।\n\n"
                                "Support: @earning_hub_otp_group")
                            for admin_id in ADMIN_IDS:
                                _tg_notify_user(admin_id,
                                    f"❌ User <code>{uid}</code> (@{u['username']}) "
                                    f"এর subscription expire হয়েছে।")
                    except Exception:
                        pass
        except Exception as e:
            log.error(f"expiry_checker error: {e}")

threading.Thread(target=_expiry_checker, daemon=True, name="ExpiryChecker").start()

MAIN_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("📋 Panels"),
      KeyboardButton("👤 Accounts"),
      KeyboardButton("⚙️ Settings")]],
    resize_keyboard=True
)

ADMIN_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("👥 Users"),
      KeyboardButton("📊 Admin Status")],
     [KeyboardButton("📋 Panels"),
      KeyboardButton("👤 Accounts"),
      KeyboardButton("⚙️ Settings")]],
    resize_keyboard=True
)

def kb_admin_users_home():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add User",    callback_data="au:add"),
         InlineKeyboardButton("📋 All Users",   callback_data="au:list")],
        [InlineKeyboardButton("⏰ Expired",     callback_data="au:expired"),
         InlineKeyboardButton("✅ Active",       callback_data="au:active")],
    ])

def kb_user_detail(uid):
    u = get_user(uid)
    active = u["active"] if u else 0
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Extend 7d",  callback_data=f"um:ext7:{uid}"),
         InlineKeyboardButton("➕ Extend 30d", callback_data=f"um:ext30:{uid}")],
        [InlineKeyboardButton("✏️ Set Limit",  callback_data=f"um:limit:{uid}"),
         InlineKeyboardButton("🔄 Custom Days",callback_data=f"um:extN:{uid}")],
        [InlineKeyboardButton("🚫 Block" if active else "✅ Unblock",
                              callback_data=f"um:block:{uid}"),
         InlineKeyboardButton("🗑 Delete",     callback_data=f"um:del:{uid}")],
        [InlineKeyboardButton("« Back",        callback_data="au:list")],
    ])

def kb_panels_home():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Panel",   callback_data="p:add"),
         InlineKeyboardButton("📋 List Panels", callback_data="p:list")],
        [InlineKeyboardButton("✅ Start All",   callback_data="p:allon"),
         InlineKeyboardButton("⏹ Stop All",    callback_data="p:alloff")],
        [InlineKeyboardButton("🔄 Restart All", callback_data="p:restartall")],
    ])

def kb_accounts_home():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Account", callback_data="a:add"),
         InlineKeyboardButton("📋 List",        callback_data="a:list")],
    ])

def kb_settings_home():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Chat ID", callback_data="s:addchat"),
         InlineKeyboardButton("🗑 Del Chat ID", callback_data="s:delchat")],
        [InlineKeyboardButton("📋 Chat IDs",    callback_data="s:listchat"),
         InlineKeyboardButton("📊 Status",      callback_data="s:status")],
        [InlineKeyboardButton("🎨 OTP Format",  callback_data="s:tmpl")],
    ])

def kb_panel_list(user_id):
    panels = get_panels(user_id)
    rows = []
    for p in panels:
        icon = "🟢" if is_running(user_id, p["id"]) else (
               "⚫" if not p["enabled"] else "🔴")
        rows.append([InlineKeyboardButton(
            f"{icon} {p['name']}", callback_data=f"pv:{p['id']}")])
    rows.append([InlineKeyboardButton("« Back", callback_data="p:back")])
    return InlineKeyboardMarkup(rows)

def kb_panel_detail(user_id, panel_id):
    running = is_running(user_id, panel_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏹ Stop" if running else "▶ Start",
                              callback_data=f"pd:toggle:{panel_id}")],
        [InlineKeyboardButton("➕ Add Account", callback_data=f"pd:addacc:{panel_id}"),
         InlineKeyboardButton("👤 Accounts",    callback_data=f"pd:accs:{panel_id}")],
        [InlineKeyboardButton("🗑 Delete Panel", callback_data=f"pd:del:{panel_id}")],
        [InlineKeyboardButton("« Back",          callback_data="p:list")],
    ])

def kb_panel_accounts(user_id, panel_id):
    accs = get_all_accounts(panel_id)
    rows = []
    for a in accs:
        icon = "✅" if a["active"] else "❌"
        rows.append([
            InlineKeyboardButton(f"{icon} {a['username']}", callback_data="ac:noop"),
            InlineKeyboardButton("🗑 Del", callback_data=f"ac:del:{a['id']}:{panel_id}"),
        ])
    rows.append([InlineKeyboardButton("➕ Add Account",
                                      callback_data=f"pd:addacc:{panel_id}")])
    rows.append([InlineKeyboardButton("« Back", callback_data=f"pv:{panel_id}")])
    return InlineKeyboardMarkup(rows)

def kb_builtin_select():
    rows = []
    row  = []
    for name in sorted(BUILTIN_PANELS.keys()):
        row.append(InlineKeyboardButton(name, callback_data=f"bi:{name}"))
        if len(row) == 3: rows.append(row); row = []
    if row: rows.append(row)
    rows.append([InlineKeyboardButton("✏️ Custom Panel", callback_data="bi:__custom__")])
    rows.append([InlineKeyboardButton("« Cancel",        callback_data="p:back")])
    return InlineKeyboardMarkup(rows)

def kb_ptype():
    rows = []
    row  = []
    for pt in PANEL_TYPES:
        row.append(InlineKeyboardButton(pt, callback_data=f"pt:{pt}"))
        if len(row) == 3: rows.append(row); row = []
    if row: rows.append(row)
    return InlineKeyboardMarkup(rows)

def kb_panel_select_for_account(user_id):
    panels = get_panels(user_id)
    rows = [[InlineKeyboardButton(p["name"], callback_data=f"pd:addacc:{p['id']}")]
            for p in panels]
    rows.append([InlineKeyboardButton("« Cancel", callback_data="a:back")])
    return InlineKeyboardMarkup(rows)

def kb_plan_select():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Weekly (7 days)",   callback_data="ap:weekly"),
         InlineKeyboardButton("📆 Monthly (30 days)", callback_data="ap:monthly")],
        [InlineKeyboardButton("✏️ Custom Days",        callback_data="ap:custom")],
        [InlineKeyboardButton("« Cancel",              callback_data="au:list")],
    ])

_ustate: dict = {}

def is_admin(uid):
    return uid in ADMIN_IDS

def get_main_kb(uid):
    return ADMIN_KB if is_admin(uid) else MAIN_KB

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_admin(uid):
        await update.message.reply_text(
            "🤖 <b>OTP Bot Manager</b> — Admin Panel\n\nChoose an option:",
            parse_mode="HTML", reply_markup=ADMIN_KB)
        return
    if not is_user_active(uid):
        u = get_user(uid)
        if u and not u["active"]:
            exp = u["expire_date"]
            await update.message.reply_text(
                f"❌ <b>Access Denied!</b>\n\n"
                f"আপনার subscription expire হয়েছে ({exp}).\n"
                f"Renew করতে admin এর সাথে যোগাযোগ করুন।",
                parse_mode="HTML")
        else:
            await update.message.reply_text(
                "⛔ <b>Access Denied!</b>\n\n"
                "আপনার কাছে এই bot ব্যবহারের permission নেই।\n"
                "Admin এর সাথে যোগাযোগ করুন।",
                parse_mode="HTML")
        return
    u = get_user(uid)
    dr = days_remaining(uid)
    await update.message.reply_text(
        f"🤖 <b>OTP Bot</b>\n\n"
        f"✅ Subscription active — <b>{dr} দিন</b> বাকি\n"
        f"📋 Plan: {u['plan']}\n\n"
        f"Choose an option:",
        parse_mode="HTML", reply_markup=MAIN_KB)

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = update.message.text.strip()

    if text == "👥 Users" and is_admin(uid):
        _ustate.pop(uid, None)
        await update.message.reply_text(
            "👥 <b>User Management</b>",
            parse_mode="HTML", reply_markup=kb_admin_users_home())
        return

    if text == "📊 Admin Status" and is_admin(uid):
        _ustate.pop(uid, None)
        users = get_all_users()
        total   = len(users)
        active  = sum(1 for u in users if is_user_active(u["user_id"]))
        expired = total - active
        lines = []
        for u in users:
            dr   = days_remaining(u["user_id"])
            icon = "🟢" if is_user_active(u["user_id"]) else "🔴"
            uname = u["username"] or u["user_id"]
            lines.append(f"{icon} <code>{u['user_id']}</code> @{uname} — {dr}d")
        await update.message.reply_text(
            f"📊 <b>Admin Status</b>\n\n"
            f"Total: {total} | Active: {active} | Expired: {expired}\n\n"
            + ("\n".join(lines) if lines else "No users yet."),
            parse_mode="HTML", reply_markup=ADMIN_KB)
        return

    if not is_admin(uid) and not is_user_active(uid):
        await update.message.reply_text("⛔ Access denied. Subscription expired.")
        return

    if text == "📋 Panels":
        _ustate.pop(uid, None)
        await update.message.reply_text(
            "📋 <b>Panels</b>", parse_mode="HTML",
            reply_markup=kb_panels_home())
        return
    if text == "👤 Accounts":
        _ustate.pop(uid, None)
        await update.message.reply_text(
            "👤 <b>Accounts</b>", parse_mode="HTML",
            reply_markup=kb_accounts_home())
        return
    if text == "⚙️ Settings":
        _ustate.pop(uid, None)
        await update.message.reply_text(
            "⚙️ <b>Settings</b>", parse_mode="HTML",
            reply_markup=kb_settings_home())
        return

    st = _ustate.get(uid)
    if not st:
        await update.message.reply_text(
            "Use the buttons below. 👇",
            reply_markup=get_main_kb(uid))
        return

    action = st.get("action")

    if action == "admin_add_user":
        if "target_uid" not in st:
            try:
                st["target_uid"] = int(text.strip())
                await update.message.reply_text(
                    "👤 Username দাও (optional, শুধু নাম লেখো):")
            except ValueError:
                await update.message.reply_text("❌ Valid Telegram ID দাও (number).")
        elif "target_uname" not in st:
            st["target_uname"] = text.strip().lstrip("@")
            await update.message.reply_text(
                "📦 Panel limit দাও (যত panel add করতে পারবে, e.g. 3):")
        elif "panel_limit" not in st:
            try:
                st["panel_limit"] = int(text.strip())
                await update.message.reply_text(
                    "📅 Plan select করো:",
                    reply_markup=kb_plan_select())
            except ValueError:
                await update.message.reply_text("❌ Number দাও (e.g. 3).")
        return

    if action == "admin_add_user_custom_days":
        try:
            days = int(text.strip())
            uid2 = st["target_uid"]
            uname = st.get("target_uname","")
            limit = st.get("panel_limit", 3)
            add_user(uid2, uname, "custom", days, limit)
            _ustate.pop(uid, None)
            await update.message.reply_text(
                f"✅ User <code>{uid2}</code> added!\n"
                f"Plan: Custom {days} days\n"
                f"Panel limit: {limit}",
                parse_mode="HTML", reply_markup=ADMIN_KB)
        except ValueError:
            await update.message.reply_text("❌ Number দাও (e.g. 14).")
        return

    if action == "admin_extend_custom":
        try:
            days = int(text.strip())
            uid2 = st["target_uid"]
            extend_user(uid2, days)
            _ustate.pop(uid, None)
            u2 = get_user(uid2)
            await update.message.reply_text(
                f"✅ <code>{uid2}</code> এর subscription {days} দিন বাড়ানো হয়েছে.\n"
                f"Expire: {u2['expire_date']}",
                parse_mode="HTML", reply_markup=ADMIN_KB)
        except ValueError:
            await update.message.reply_text("❌ Number দাও (e.g. 7).")
        return

    if action == "admin_set_limit":
        try:
            limit = int(text.strip())
            uid2  = st["target_uid"]
            with db_conn() as c:
                c.execute("UPDATE users SET panel_limit=? WHERE user_id=?",
                          (limit, str(uid2)))
            _ustate.pop(uid, None)
            await update.message.reply_text(
                f"✅ <code>{uid2}</code> এর panel limit → <b>{limit}</b>",
                parse_mode="HTML", reply_markup=ADMIN_KB)
        except ValueError:
            await update.message.reply_text("❌ Number দাও.")
        return

    # ── Chat ID — DUPLICATE CHECK ──────────────────────────
    if action == "add_chat":
        cid = text.strip()
        with db_conn() as c:
            existing = c.execute(
                "SELECT user_id FROM chat_ids WHERE chat_id=?",
                (cid,)
            ).fetchone()
        if existing:
            await update.message.reply_text(
                f"❌ এই Chat ID <code>{cid}</code> already অন্য কেউ use করছে!\n\n"
                f"অন্য একটি Chat ID ব্যবহার করুন।",
                parse_mode="HTML",
                reply_markup=get_main_kb(uid))
        else:
            with db_conn() as c:
                c.execute(
                    "INSERT OR IGNORE INTO chat_ids(user_id,chat_id) VALUES(?,?)",
                    (str(uid), cid)
                )
            _ustate.pop(uid, None)
            await update.message.reply_text(
                f"✅ Chat ID <code>{cid}</code> added!",
                parse_mode="HTML",
                reply_markup=get_main_kb(uid))
        return

    if action == "add_builtin":
        if "username" not in st:
            st["username"] = text
            await update.message.reply_text("🔑 Enter <b>password</b>:", parse_mode="HTML")
        else:
            bn    = st["panel_name"]
            uname = st["username"]
            pwd   = text

            u2 = get_user(uid) if not is_admin(uid) else None
            if u2:
                current_panels = len(get_panels(uid))
                if current_panels >= u2["panel_limit"]:
                    _ustate.pop(uid, None)
                    await update.message.reply_text(
                        f"❌ Panel limit পূর্ণ হয়েছে! (max {u2['panel_limit']})\n"
                        f"Admin এর সাথে যোগাযোগ করুন।",
                        reply_markup=MAIN_KB)
                    return

            with db_conn() as c:
                c.execute("""
                    INSERT OR REPLACE INTO panels(user_id,name,url,ptype,fp)
                    VALUES(?,?,?,?,?)
                """, (str(uid), bn, st["url"], st["ptype"], st.get("fp")))
                panel = c.execute(
                    "SELECT id FROM panels WHERE user_id=? AND name=?",
                    (str(uid), bn)
                ).fetchone()
                panel_id = panel["id"]
                c.execute("""
                    INSERT INTO accounts(panel_id,username,password)
                    VALUES(?,?,?)
                """, (panel_id, uname, pwd))

            _ustate.pop(uid, None)
            p_row = get_panel_by_id(panel_id)
            started = start_panel(uid, p_row)
            await update.message.reply_text(
                f"✅ <b>{bn}</b> added"
                f"{'& started' if started else ' (no start — check accounts)'}!\n"
                f"Account: <code>{uname}</code>",
                parse_mode="HTML", reply_markup=get_main_kb(uid))
        return

    if action == "add_custom":
        if "panel_name" not in st:
            st["panel_name"] = text
            await update.message.reply_text(
                "🌐 Enter panel <b>URL</b>:", parse_mode="HTML")
        elif "url" not in st:
            st["url"] = text
            await update.message.reply_text(
                "🔧 Select panel <b>type</b>:",
                parse_mode="HTML", reply_markup=kb_ptype())
        return

    if action == "save_template":
        raw = text.strip()
        raw = re.sub(r"^```[a-z]*\n?","",raw)
        raw = re.sub(r"\n?```$","",raw).strip()
        try:
            tmpl = json.loads(raw)
            if "text" not in tmpl:
                raise ValueError("'text' field missing")
            set_user_setting(uid, "otp_template", json.dumps(tmpl))
            _ustate.pop(uid, None)
            prev_text, prev_kb = _render_template(
                tmpl, "TestPanel", "+8801712345678", "847293",
                "WhatsApp", "Your WhatsApp code is 847293")
            await update.message.reply_text(
                "✅ <b>Template saved!</b>\n\nPreview:",
                parse_mode="HTML", reply_markup=get_main_kb(uid))
            payload = {"chat_id": uid, "text": prev_text, "parse_mode": "HTML"}
            if prev_kb:
                payload["reply_markup"] = json.dumps(prev_kb)
            data = json.dumps(payload).encode()
            try:
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    data=data, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    r.read()
            except Exception as e:
                log.error(f"Preview send error: {e}")
        except Exception as e:
            await update.message.reply_text(
                f"❌ <b>Invalid JSON!</b>\n\n<code>{str(e)[:120]}</code>",
                parse_mode="HTML")
        return

    if action == "add_account":
        panel_id = st["panel_id"]
        if "username" not in st:
            st["username"] = text
            await update.message.reply_text(
                "🔑 Enter <b>password</b>:", parse_mode="HTML")
        else:
            uname = st["username"]
            pwd   = text
            with db_conn() as c:
                c.execute("""
                    INSERT INTO accounts(panel_id,username,password)
                    VALUES(?,?,?)
                """, (panel_id, uname, pwd))
            _ustate.pop(uid, None)
            p_row = get_panel_by_id(panel_id)
            if p_row and p_row["enabled"]:
                stop_panel(uid, panel_id)
                time.sleep(0.3)
                start_panel(uid, p_row)
            await update.message.reply_text(
                f"✅ Account <code>{uname}</code> added.",
                parse_mode="HTML", reply_markup=get_main_kb(uid))
        return

    await update.message.reply_text(
        "Use the buttons below. 👇", reply_markup=get_main_kb(uid))

async def _safe_edit(q, text, parse_mode=None, reply_markup=None):
    try:
        await q.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        err = str(e).lower()
        if "not found" in err or "message to edit" in err or "message_id_invalid" in err:
            try:
                await q.message.reply_text(
                    text, parse_mode=parse_mode, reply_markup=reply_markup)
            except Exception:
                pass
        elif "message is not modified" in err:
            pass
        else:
            log.warning(f"edit_message_text error: {e}")

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    try:
        await q.answer()
    except Exception:
        pass
    d = q.data

    if d == "au:add":
        if not is_admin(uid):
            await q.answer("⛔ Admin only", show_alert=True); return
        _ustate[uid] = {"action": "admin_add_user"}
        await _safe_edit(q,
            "➕ <b>Add User</b>\n\nUser এর <b>Telegram ID</b> দাও:",
            parse_mode="HTML")

    elif d.startswith("ap:"):
        if not is_admin(uid):
            await q.answer("⛔ Admin only", show_alert=True); return
        plan = d[3:]
        st   = _ustate.get(uid, {})
        uid2  = st.get("target_uid")
        uname = st.get("target_uname","")
        limit = st.get("panel_limit", 3)
        if not uid2:
            await _safe_edit(q,"❌ State lost. Start again.", reply_markup=kb_admin_users_home())
            return
        if plan == "custom":
            _ustate[uid] = {"action":"admin_add_user_custom_days",
                            "target_uid": uid2, "target_uname": uname,
                            "panel_limit": limit}
            await _safe_edit(q,"✏️ কত দিনের subscription দেবে? (number লেখো):")
        else:
            days = 7 if plan == "weekly" else 30
            add_user(uid2, uname, plan, days, limit)
            _ustate.pop(uid, None)
            _tg_notify_user(uid2,
                f"✅ <b>OTP Bot Access Granted!</b>\n\n"
                f"Plan: <b>{plan.title()}</b>\n"
                f"Duration: <b>{days} days</b>\n"
                f"Panel Limit: <b>{limit}</b>\n\n"
                f"/start দিয়ে শুরু করো।")
            await _safe_edit(q,
                f"✅ User <code>{uid2}</code> added!\n"
                f"Plan: {plan} ({days}d)\nPanel limit: {limit}",
                parse_mode="HTML", reply_markup=kb_admin_users_home())

    elif d == "au:list":
        if not is_admin(uid): return
        users = get_all_users()
        if not users:
            await _safe_edit(q,"No users yet.", reply_markup=kb_admin_users_home()); return
        rows = []
        for u in users:
            dr   = days_remaining(u["user_id"])
            icon = "🟢" if is_user_active(u["user_id"]) else "🔴"
            uname = u["username"] or "—"
            rows.append([InlineKeyboardButton(
                f"{icon} {uname} | {dr}d",
                callback_data=f"uv:{u['user_id']}")])
        rows.append([InlineKeyboardButton("« Back", callback_data="au:back")])
        await _safe_edit(q,"👥 <b>All Users:</b>",
                         parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup(rows))

    elif d == "au:active":
        if not is_admin(uid): return
        users = [u for u in get_all_users() if is_user_active(u["user_id"])]
        rows = []
        for u in users:
            dr    = days_remaining(u["user_id"])
            uname = u["username"] or "—"
            rows.append([InlineKeyboardButton(
                f"🟢 {uname} | {dr}d remaining",
                callback_data=f"uv:{u['user_id']}")])
        rows.append([InlineKeyboardButton("« Back", callback_data="au:back")])
        txt = f"✅ Active Users: {len(users)}" if users else "No active users."
        await _safe_edit(q, txt, reply_markup=InlineKeyboardMarkup(rows))

    elif d == "au:expired":
        if not is_admin(uid): return
        users = [u for u in get_all_users() if not is_user_active(u["user_id"])]
        rows = []
        for u in users:
            uname = u["username"] or "—"
            rows.append([InlineKeyboardButton(
                f"🔴 {uname} | {u['expire_date'][:10]}",
                callback_data=f"uv:{u['user_id']}")])
        rows.append([InlineKeyboardButton("« Back", callback_data="au:back")])
        txt = f"🔴 Expired Users: {len(users)}" if users else "No expired users."
        await _safe_edit(q, txt, reply_markup=InlineKeyboardMarkup(rows))

    elif d == "au:back":
        if not is_admin(uid): return
        await _safe_edit(q,"👥 <b>User Management</b>",
                         parse_mode="HTML", reply_markup=kb_admin_users_home())

    elif d.startswith("uv:"):
        if not is_admin(uid): return
        uid2 = d[3:]
        u2   = get_user(uid2)
        if not u2:
            await _safe_edit(q,"User not found.", reply_markup=kb_admin_users_home()); return
        dr     = days_remaining(uid2)
        panels = get_panels(uid2)
        run_p  = sum(1 for p in panels if is_running(uid2, p["id"]))
        status = "🟢 Active" if is_user_active(uid2) else "🔴 Inactive"
        await _safe_edit(q,
            f"👤 <b>User Detail</b>\n\n"
            f"ID    : <code>{uid2}</code>\n"
            f"Name  : @{u2['username'] or '—'}\n"
            f"Status: {status}\n"
            f"Plan  : {u2['plan']}\n"
            f"Expire: {u2['expire_date'][:10]}\n"
            f"Remain: <b>{dr} days</b>\n"
            f"Panels: {len(panels)} total / {run_p} running\n"
            f"Limit : {u2['panel_limit']}",
            parse_mode="HTML", reply_markup=kb_user_detail(uid2))

    elif d.startswith("um:"):
        if not is_admin(uid): return
        _, action, uid2 = d.split(":", 2)
        u2 = get_user(uid2)
        if not u2:
            await _safe_edit(q,"User not found.", reply_markup=kb_admin_users_home()); return

        if action == "ext7":
            extend_user(uid2, 7)
            _tg_notify_user(uid2,
                "✅ <b>Subscription Extended!</b>\n7 দিন বাড়ানো হয়েছে।")
            await _safe_edit(q,f"✅ {uid2} এর subscription 7 দিন বাড়ানো হয়েছে.",
                             reply_markup=kb_user_detail(uid2))

        elif action == "ext30":
            extend_user(uid2, 30)
            _tg_notify_user(uid2,
                "✅ <b>Subscription Extended!</b>\n30 দিন বাড়ানো হয়েছে।")
            await _safe_edit(q,f"✅ {uid2} এর subscription 30 দিন বাড়ানো হয়েছে.",
                             reply_markup=kb_user_detail(uid2))

        elif action == "extN":
            _ustate[uid] = {"action":"admin_extend_custom","target_uid": uid2}
            await _safe_edit(q,"✏️ কত দিন extend করবে? (number লেখো):")

        elif action == "limit":
            _ustate[uid] = {"action":"admin_set_limit","target_uid": uid2}
            await _safe_edit(q,
                f"📦 <code>{uid2}</code> এর নতুন panel limit দাও:",
                parse_mode="HTML")

        elif action == "block":
            if u2["active"]:
                block_user(uid2)
                stop_all_user_panels(uid2)
                _tg_notify_user(uid2,"❌ আপনার access block করা হয়েছে।")
                msg = f"🚫 {uid2} blocked."
            else:
                with db_conn() as c:
                    c.execute("UPDATE users SET active=1 WHERE user_id=?", (uid2,))
                _tg_notify_user(uid2,"✅ আপনার access restore করা হয়েছে।")
                msg = f"✅ {uid2} unblocked."
            await _safe_edit(q, msg, reply_markup=kb_user_detail(uid2))

        elif action == "del":
            stop_all_user_panels(uid2)
            delete_user(uid2)
            await _safe_edit(q,f"🗑 {uid2} deleted.",
                             reply_markup=kb_admin_users_home())

    elif d == "p:back":
        await _safe_edit(q,"📋 <b>Panels</b>",
                         parse_mode="HTML", reply_markup=kb_panels_home())

    elif d == "p:list":
        panels = get_panels(uid)
        txt = "📋 Select a panel:" if panels else "No panels yet.\nAdd one with ➕"
        kb  = kb_panel_list(uid) if panels else kb_panels_home()
        await _safe_edit(q, txt, reply_markup=kb)

    elif d == "p:add":
        if not is_admin(uid):
            u2 = get_user(uid)
            if u2 and len(get_panels(uid)) >= u2["panel_limit"]:
                await _safe_edit(q,
                    f"❌ Panel limit পূর্ণ! (max {u2['panel_limit']})\n"
                    f"Admin এর সাথে যোগাযোগ করুন।"); return
        await _safe_edit(q,"➕ Select panel to add:",
                         reply_markup=kb_builtin_select())

    elif d == "p:allon":
        count = 0
        for p in get_panels(uid):
            with db_conn() as c:
                c.execute("UPDATE panels SET enabled=1 WHERE id=?", (p["id"],))
            p2 = get_panel_by_id(p["id"])
            if start_panel(uid, p2): count += 1
        await _safe_edit(q,f"✅ Started <b>{count}</b> panels.",
                         parse_mode="HTML", reply_markup=kb_panels_home())

    elif d == "p:alloff":
        for p in get_panels(uid):
            stop_panel(uid, p["id"])
            with db_conn() as c:
                c.execute("UPDATE panels SET enabled=0 WHERE id=?", (p["id"],))
        await _safe_edit(q,"⏹ All panels stopped.",
                         reply_markup=kb_panels_home())

    elif d == "p:restartall":
        count = 0
        for p in get_panels(uid):
            if p["enabled"]:
                stop_panel(uid, p["id"])
                time.sleep(0.2)
                p2 = get_panel_by_id(p["id"])
                if start_panel(uid, p2): count += 1
        await _safe_edit(q,f"🔄 Restarted <b>{count}</b> panels.",
                         parse_mode="HTML", reply_markup=kb_panels_home())

    elif d.startswith("bi:"):
        name = d[3:]
        if name == "__custom__":
            _ustate[uid] = {"action":"add_custom"}
            await _safe_edit(q,"✏️ Enter panel <b>name</b>:", parse_mode="HTML")
        else:
            bp = BUILTIN_PANELS[name]
            _ustate[uid] = {
                "action":     "add_builtin",
                "panel_name": name,
                "url":        bp["url"],
                "ptype":      bp["ptype"],
                "fp":         bp.get("fp"),
            }
            await _safe_edit(q,
                f"📌 <b>{name}</b>\n\nEnter <b>username</b>:",
                parse_mode="HTML")

    elif d.startswith("pt:"):
        ptype = d[3:]
        st    = _ustate.get(uid, {})
        st.update({"ptype": ptype, "fp": None, "action": "add_builtin"})
        _ustate[uid] = st
        await _safe_edit(q,
            f"Type: <b>{ptype}</b> ✅\n\nEnter <b>username</b>:",
            parse_mode="HTML")

    elif d.startswith("pv:"):
        panel_id = int(d[3:])
        p = get_panel_by_id(panel_id)
        if not p or str(p["user_id"]) != str(uid):
            await _safe_edit(q,"Panel not found."); return
        accs    = get_accounts(panel_id)
        running = is_running(uid, panel_id)
        status  = ("🟢 Running" if running else
                   ("⚫ Stopped" if p["enabled"] else "🔴 Disabled"))
        await _safe_edit(q,
            f"📌 <b>{p['name']}</b>\n"
            f"Status : {status}\n"
            f"Type   : <code>{p['ptype']}</code>\n"
            f"URL    : <code>{p['url']}</code>\n"
            f"Accounts: {len(accs)} active",
            parse_mode="HTML",
            reply_markup=kb_panel_detail(uid, panel_id))

    elif d.startswith("pd:"):
        parts  = d.split(":", 2)
        action = parts[1]
        panel_id = int(parts[2])
        p = get_panel_by_id(panel_id)
        if not p or (str(p["user_id"]) != str(uid) and not is_admin(uid)):
            await _safe_edit(q,"Access denied."); return

        if action == "toggle":
            if is_running(uid, panel_id):
                stop_panel(uid, panel_id)
                with db_conn() as c:
                    c.execute("UPDATE panels SET enabled=0 WHERE id=?", (panel_id,))
                msg = f"⏹ <b>{p['name']}</b> stopped."
            else:
                with db_conn() as c:
                    c.execute("UPDATE panels SET enabled=1 WHERE id=?", (panel_id,))
                p2  = get_panel_by_id(panel_id)
                ok  = start_panel(uid, p2)
                msg = (f"✅ <b>{p['name']}</b> started." if ok
                       else f"⚠️ <b>{p['name']}</b>: no accounts — add one first!")
            await _safe_edit(q, msg, parse_mode="HTML",
                             reply_markup=kb_panel_detail(uid, panel_id))

        elif action == "del":
            stop_panel(uid, panel_id)
            with db_conn() as c:
                c.execute("DELETE FROM accounts WHERE panel_id=?", (panel_id,))
                c.execute("DELETE FROM panels WHERE id=?", (panel_id,))
            panels = get_panels(uid)
            await _safe_edit(q,f"🗑 <b>{p['name']}</b> deleted.",
                             parse_mode="HTML",
                             reply_markup=kb_panel_list(uid) if panels else kb_panels_home())

        elif action == "addacc":
            _ustate[uid] = {"action":"add_account","panel_id": panel_id}
            await _safe_edit(q,
                f"➕ Add account to <b>{p['name']}</b>\n\nEnter <b>username</b>:",
                parse_mode="HTML")

        elif action == "accs":
            await _safe_edit(q,
                f"👤 Accounts — <b>{p['name']}</b>:",
                parse_mode="HTML",
                reply_markup=kb_panel_accounts(uid, panel_id))

    elif d.startswith("ac:"):
        parts  = d.split(":")
        action = parts[1]
        if action == "noop": return
        if action == "del":
            acc_id   = int(parts[2])
            panel_id = int(parts[3])
            with db_conn() as c:
                c.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
            p = get_panel_by_id(panel_id)
            await _safe_edit(q,
                f"🗑 Account removed from <b>{p['name'] if p else panel_id}</b>.",
                parse_mode="HTML",
                reply_markup=kb_panel_accounts(uid, panel_id))

    elif d == "a:list":
        panels = get_panels(uid)
        if not panels:
            await _safe_edit(q,"No panels yet.",
                             reply_markup=kb_accounts_home()); return
        rows = []
        for p in panels:
            n = len(get_all_accounts(p["id"]))
            rows.append([InlineKeyboardButton(
                f"📌 {p['name']} ({n} accs)",
                callback_data=f"pd:accs:{p['id']}")])
        rows.append([InlineKeyboardButton("« Back", callback_data="a:back")])
        await _safe_edit(q,"👤 Select panel:",
                         reply_markup=InlineKeyboardMarkup(rows))

    elif d == "a:add":
        panels = get_panels(uid)
        if not panels:
            await _safe_edit(q,"No panels yet. Add a panel first.",
                             reply_markup=kb_accounts_home()); return
        await _safe_edit(q,"Select panel to add account to:",
                         reply_markup=kb_panel_select_for_account(uid))

    elif d == "a:back":
        await _safe_edit(q,"👤 <b>Accounts</b>",
                         parse_mode="HTML", reply_markup=kb_accounts_home())

    elif d == "s:addchat":
        _ustate[uid] = {"action":"add_chat"}
        await _safe_edit(q,
            "📢 Send the <b>Chat ID</b>:\n"
            "Group/Channel: <code>-1001234567890</code>\n"
            "Personal: <code>987654321</code>",
            parse_mode="HTML")

    elif d == "s:listchat":
        chats = get_chat_ids(uid)
        txt   = ("📢 <b>Chat IDs:</b>\n"
                 + "\n".join(f"• <code>{c}</code>" for c in chats)
                 if chats else "No chat IDs added yet.")
        await _safe_edit(q, txt, parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup([
                             [InlineKeyboardButton("« Back",
                                                   callback_data="s:back")]]))

    elif d == "s:delchat":
        chats = get_chat_ids(uid)
        if not chats:
            await _safe_edit(q,"No chats to remove.",
                             reply_markup=kb_settings_home()); return
        rows  = [[InlineKeyboardButton(f"🗑 {c}", callback_data=f"sc:{c}")]
                 for c in chats]
        rows.append([InlineKeyboardButton("« Back", callback_data="s:back")])
        await _safe_edit(q,"Select chat to remove:",
                         reply_markup=InlineKeyboardMarkup(rows))

    elif d.startswith("sc:"):
        cid = d[3:]
        with db_conn() as c:
            c.execute("DELETE FROM chat_ids WHERE user_id=? AND chat_id=?",
                      (str(uid), cid))
        await _safe_edit(q,f"🗑 <code>{cid}</code> removed.",
                         parse_mode="HTML",
                         reply_markup=kb_settings_home())

    elif d == "s:status":
        panels  = get_panels(uid)
        total   = len(panels)
        running = sum(1 for p in panels if is_running(uid, p["id"]))
        chats   = get_chat_ids(uid)
        u2      = get_user(uid) if not is_admin(uid) else None
        dr      = days_remaining(uid) if u2 else 0
        lines   = [f"{'🟢' if is_running(uid, p['id']) else '🔴'} {p['name']}"
                   for p in panels]
        sub_line = (f"\n📅 Subscription: <b>{dr} days</b> remaining"
                    if u2 else "")
        await _safe_edit(q,
            f"📊 <b>Status</b>\n\n"
            f"🟢 Running : {running}/{total}\n"
            f"📢 Chats  : {len(chats)}"
            f"{sub_line}\n\n"
            + ("\n".join(lines) if lines else "No panels"),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="s:status")],
                [InlineKeyboardButton("« Back",     callback_data="s:back")],
            ]))

    elif d == "s:back":
        await _safe_edit(q,"⚙️ <b>Settings</b>",
                         parse_mode="HTML", reply_markup=kb_settings_home())

    elif d == "s:tmpl":
        raw = get_user_setting(uid, "otp_template")
        has = "✅ Template saved" if raw else "⚠️ Using default template"
        await _safe_edit(q,
            f"🎨 <b>OTP Format</b>\n\nStatus: {has}\n\n"
            f"📌 <b>Step 1:</b> Template বানাও\n"
            f"📌 <b>Step 2:</b> JSON copy করো\n"
            f"📌 <b>Step 3:</b> এখানে paste করো\n\n"
            f"🔗 <a href='{TEMPLATE_EDITOR_URL}'>Template Editor</a>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📝 Paste JSON",
                                      callback_data="s:tmpl_paste")],
                [InlineKeyboardButton("🧪 Test Format",
                                      callback_data="s:tmpl_test")],
                [InlineKeyboardButton("♻️ Reset Default",
                                      callback_data="s:tmpl_reset")],
                [InlineKeyboardButton("« Back",
                                      callback_data="s:back")],
            ]))

    elif d == "s:tmpl_paste":
        _ustate[uid] = {"action":"save_template"}
        await _safe_edit(q,
            "📋 JSON paste করো:\n\n"
            "<i>({ দিয়ে শুরু হবে)</i>",
            parse_mode="HTML")

    elif d == "s:tmpl_test":
        raw = get_user_setting(uid, "otp_template")
        try:
            tmpl = json.loads(raw) if raw else DEFAULT_TEMPLATE
        except Exception:
            tmpl = DEFAULT_TEMPLATE
        text, kb = _render_template(
            tmpl, "TestPanel", "+8801712345678", "847293",
            "WhatsApp", "Your WhatsApp code is 847293")
        await _safe_edit(q,"🧪 <b>Test পাঠানো হচ্ছে...</b>",
                         parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup([
                             [InlineKeyboardButton("« Back",
                                                   callback_data="s:tmpl")]]))
        threading.Thread(
            target=_tg_raw_send_to,
            args=([str(uid)], text, kb),
            daemon=True).start()

    elif d == "s:tmpl_reset":
        set_user_setting(uid, "otp_template", json.dumps(DEFAULT_TEMPLATE))
        await _safe_edit(q,"♻️ Default template restore হয়েছে.",
                         reply_markup=InlineKeyboardMarkup([
                             [InlineKeyboardButton("« Back",
                                                   callback_data="s:tmpl")]]))

def main():
    db_init()
    count = start_all_on_boot()
    log.info(f"✅ Auto-started panels for {count} account(s) on boot")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("🤖 Bot polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()