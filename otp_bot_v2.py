"""
otp_bot_v2.py — Full Bot-Managed OTP Forwarder
================================================
File এ শুধু BOT_TOKEN আর ADMIN_IDS দাও।
বাকি সব Telegram bot থেকে manage করো।

MAIN KEYBOARD (3 Reply buttons):
  📋 Panels | 👤 Accounts | ⚙️ Settings

REQUIREMENTS:
  pip install python-telegram-bot==20.* requests
  panel_fetchers.py একই folder এ থাকতে হবে
"""

import re, time, threading, logging, json, sqlite3, asyncio
import urllib.request
from datetime import datetime as _dt, timezone as _UTC
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)

# ══════════════════════════════════════════════════════════════
#  ★ শুধু এই দুইটা change করো ★
# ══════════════════════════════════════════════════════════════
BOT_TOKEN = "8609593081:AAFmYML-bbCyEcYaBLKEnn_dtiV4_N2b6lU"
ADMIN_IDS = [7095358778]       # তোমার Telegram user ID
# ══════════════════════════════════════════════════════════════

DB_FILE = "otp_bot.db"

# ══════════════════════════════════════════════════════════════
#  ★ SMART TIME FILTER ★
#  GRACE_PERIOD_SEC  : Bot start এর পর এতক্ষণ পুরানো OTP ignore করবে (5 মিনিট)
#  OTP_MAX_AGE_SEC   : Grace period শেষে শুধু এতক্ষণের নতুন OTP নেবে (50 সেকেন্ড)
#
#  Logic:
#    • Bot চালু হওয়ার পর প্রথম 5 মিনিট → _BOT_START_TIME এর আগের সব OTP block
#    • 5 মিনিট পর → শুধু last 50 সেকেন্ডের OTP accept
# ══════════════════════════════════════════════════════════════
_BOT_START_TIME   = _dt.now(_UTC.utc)  # Bot start এর সময় record (UTC, timezone-aware)
_GRACE_PERIOD_SEC = 300                # 5 মিনিট grace period
_OTP_MAX_AGE_SEC  = 50                 # Grace শেষে last 50 সেকেন্ডের OTP নেবে
# ══════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════
#  TEMPLATE EDITOR URL — তোমার Railway template_editor.html URL দাও
# ══════════════════════════════════════════════════════════════
TEMPLATE_EDITOR_URL = "https://your-railway-app.up.railway.app/template_editor.html"

DEFAULT_TEMPLATE = {
    "text": "    {flag} {svc_icon} <b><code>+{number}</code></b>          ",
    "buttons": [{"type": "otp", "label": "{sender}", "value": "{otp}"}]
}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("OTPBot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# ══════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════

_db_lock = threading.Lock()

def db_conn():
    c = sqlite3.connect(DB_FILE, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c

def db_init():
    with db_conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS panels (
            name       TEXT PRIMARY KEY,
            url        TEXT NOT NULL,
            ptype      TEXT NOT NULL,
            fp         TEXT DEFAULT NULL,
            enabled    INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS accounts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            panel_name TEXT NOT NULL,
            username   TEXT NOT NULL,
            password   TEXT NOT NULL,
            active     INTEGER DEFAULT 1,
            FOREIGN KEY(panel_name) REFERENCES panels(name) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS chat_ids (
            chat_id  TEXT PRIMARY KEY,
            added_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)

def get_chat_ids():
    with db_conn() as c:
        return [r["chat_id"] for r in c.execute("SELECT chat_id FROM chat_ids")]

def get_panels():
    with db_conn() as c:
        return c.execute("SELECT * FROM panels ORDER BY name").fetchall()

def get_accounts(panel_name):
    with db_conn() as c:
        return c.execute(
            "SELECT * FROM accounts WHERE panel_name=? AND active=1", (panel_name,)
        ).fetchall()

def get_all_accounts(panel_name):
    with db_conn() as c:
        return c.execute("SELECT * FROM accounts WHERE panel_name=?", (panel_name,)).fetchall()

def get_setting(key, default=None):
    with db_conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

def set_setting(key, value):
    with db_conn() as c:
        c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))

# ══════════════════════════════════════════════════════════════
#  BUILT-IN PANEL TEMPLATES
# ══════════════════════════════════════════════════════════════

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

# ══════════════════════════════════════════════════════════════
#  COUNTRY / SERVICE DATA
# ══════════════════════════════════════════════════════════════

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
    "🚩":"5294236848103643477","🇦🇫":"5291937511591925566","🇦🇽":"5294077418917616055","🇦🇱":"5294202819077756005",
    "🇩🇿":"5294048127240655242","🇦🇸":"5291994273879709721","🇦🇩":"5294215205763434181","🇦🇴":"5294516785482062829",
    "🇦🇮":"5292186323342350940","🇦🇬":"5294005972136647964","🇦🇷":"5292208210495689627","🇦🇲":"5291978717508164018",
    "🇦🇼":"5294007002928798927","🇦🇺":"5294444247779399477","🇦🇹":"5291975174160145850","🇦🇿":"5294323533428579078",
    "🇧🇸":"5294031587321600012","🇧🇭":"5294108398516720753","🇧🇩":"5291824687096027834","🇧🇧":"5294526187165471742",
    "🇧🇾":"5294134426018536120","🇧🇪":"5291774466043435275","🇧🇿":"5294171848068584842","🇧🇯":"5293984969746566866",
    "🇧🇹":"5294121983498277263","🇧🇴":"5294201479047957700","🇧🇼":"5294026179957772585","🇧🇷":"5291892229751723900",
    "🇧🇳":"5292098293692650297","🇧🇬":"5294308947719640437","🇧🇫":"5294153164960848949","🇧🇮":"5294051631933967760",
    "🇰🇭":"5294225191562400452","🇨🇲":"5291997306126626950","🇨🇦":"5292290347450259214","🇨🇻":"5292203503211535593",
    "🇨🇫":"5294210571493724819","🇹🇩":"5291780728105753403","🇨🇱":"5294231037012888049","🇨🇳":"5294068833277990704",
    "🇨🇴":"5294010206974397371","🇰🇲":"5294351381996521508","🇨🇬":"5294035229453865597","🇨🇰":"5292098684534675100",
    "🇨🇷":"5292063805105263554","🇨🇮":"5293991322003200135","🇭🇷":"5291999676948569127","🇨🇺":"5291963947115631526",
    "🇨🇾":"5294062721539526918","🇨🇿":"5294242852467923382","🇩🇰":"5294531860817268837","🇩🇯":"5294127214768468283",
    "🇨🇩":"5294159262587600271","🇮🇩":"5291915686100012878",
    "🇩🇲":"5294485513825178032","🇩🇴":"5294522197140857947","🇪🇨":"5292083733753517221","🇪🇬":"5293992082212409502",
    "🇸🇻":"5294337307388695687","🏴󠁧󠁢󠁥󠁮󠁧󠁿":"5294410107084365278","🇬🇶":"5292170045416297012","🇪🇷":"5291922054004625949",
    "🇪🇪":"5291951143818123103","🇪🇹":"5292245976143124155","🇪🇺":"5291992809295861098","🇬🇮":"5292055799286224027",
    "🇬🇲":"5294399820637688352","🇬🇱":"5292014752283774878","🇫🇮":"5294049961191690629","🇫🇷":"5291817660529533837",
    "🇬🇦":"5294321325815389139","🇬🇪":"5294349389131697267","🇩🇪":"5292013274815028523","🇬🇭":"5294347396266873249",
    "🇬🇷":"5291948395039054764","🇬🇼":"5294409819321550432","🇬🇹":"5294336633078831209","🇬🇳":"5291892096607739008",
    "🇬🇾":"5292062692708736193","🇭🇹":"5292045130587462814","🇭🇳":"5291901034434682297","🇭🇰":"5292166459118606932",
    "🇭🇺":"5294229581018975260","🇮🇸":"5294354358408859664","🇮🇳":"5291933173674957761","🇮🇷":"5294220170745630736",
    "🇮🇶":"5294325010897327367","🇮🇪":"5294471971793293647","🇮🇲":"5294318478252070646","🇮🇱":"5294069056616289553",
    "🇮🇹":"5291826830284709120","🇯🇲":"5294505107465982830","🇯🇵":"5291799063321139445","🇯🇪":"5291950280529697493",
    "🇯🇴":"5291988613112814801","🇰🇿":"5294227175837290463","🇰🇪":"5292111852904416801","🇰🇮":"5294538934628405146",
    "🇰🇵":"5294193812531333564","🇰🇷":"5294408281723262763","🇰🇼":"5292066437920218075","🇰🇬":"5292091954320922577",
    "🇱🇦":"5291981530711746037","🇱🇻":"5292236016113966127","🇱🇧":"5294193108156699621","🇱🇸":"5292040693886247604",
    "🇱🇷":"5291793810576137439","🇱🇾":"5291858711826946840","🇱🇮":"5292048742654957785","🇱🇹":"5294343084119708700",
    "🇱🇺":"5294423709245787718","🇲🇰":"5294023611567332075","🇲🇬":"5291991568050312348","🇲🇼":"5294241881805312589",
    "🇲🇾":"5291858351049696702","🇲🇻":"5292004203844097218","🇲🇱":"5292086972158858331","🇲🇹":"5294532213004588353",
    "🇲🇭":"5294180730060954484","🇲🇷":"5294429743674840973","🇲🇺":"5294127824653797277","🇲🇽":"5294535073452809778",
    "🇫🇲":"5291838156113470124","🇲🇩":"5294158486425325375","🇲🇨":"5294378161117614233","🇲🇳":"5294316532631883496",
    "🇲🇦":"5292108962391414885","🇲🇿":"5294086708931874940","🇲🇲":"5294254478944393569","🇳🇦":"5292021761670404922",
    "🇳🇷":"5294463274484521342","🇳🇵":"5294458756178924088","🇳🇱":"5291917797692042265","🇳🇿":"5294189019347833274",
    "🇳🇮":"5294240825243358100","🇳🇪":"5291809418487290691","🇳🇬":"5294456308047563965","🇳🇺":"5294471336138134209",
    "🇳🇴":"5291761718580502030","🇴🇲":"5291813666209946812","🇵🇰":"5291825606219029010","🇵🇸":"5294289826525238172",
    "🇵🇦":"5291959935616178405","🇵🇬":"5291917995260533077","🇵🇾":"5294525611639852679","🇵🇭":"5291798075478661634",
    "🇵🇪":"5292099427564018941","🇵🇱":"5292190970496963836","🇵🇹":"5294436555492973610","🇵🇷":"5292121516580820347",
    "🇶🇦":"5292166360334357676","🇷🇴":"5294107724206856227","🇷🇺":"5294335323113807278","🇷🇼":"5294191265615729158",
    "🇸🇲":"5292147350809106831","🇸🇹":"5292183188016222701","🇸🇦":"5294163983983463099","🏴󠁧󠁢󠁳󠁣󠁴󠁿":"5294434665707368018",
    "🇸🇳":"5292087023698466689","🇷🇸":"5294458584380230360","🇸🇨":"5291891186074672309","🇸🇱":"5294494314213167952",
    "🇸🇬":"5294451304410663668","🇸🇰":"5294538440707166931","🇸🇮":"5294279359689938006","🇸🇧":"5294283890880433237",
    "🇸🇴":"5294058817414255960","🇿🇦":"5294325281480266304","🇪🇸":"5294513087515216901","🇱🇰":"5292102670264328257",
    "🇸🇩":"5294177148058228060","🇸🇷":"5294396668131692138","🇸🇿":"5294312482477724867","🇸🇪":"5291737091238026321",
    "🇨🇭":"5291791748991835084","🇸🇾":"5294013428199869487","🇹🇼":"5294095745543069603","🇹🇯":"5294120269806328883",
    "🇹🇿":"5292146096678658977","🇹🇭":"5293994384314882755","🇹🇬":"5294097669688415562","🇹🇴":"5294283689016973348",
    "🇹🇹":"5294362935458548705","🇹🇳":"5294484680601521871","🇹🇷":"5293993400767367408","🇹🇲":"5294098958178603764",
    "🇹🇨":"5294320866253884749","🇺🇸":"5294244076533600593","🇺🇬":"5294192317882716626","🇦🇪":"5294314831824835370",
    "🇬🇧":"5293993521026453119","🇺🇦":"5294263837678131580","🇻🇺":"5294448585696368047","🇺🇿":"5294217645304864345",
    "🇺🇾":"5291928449210932974","🇻🇪":"5294476442854247878","🇻🇳":"5294235963340379688","🇻🇮":"5294228039125718124",
    "🏴󠁧󠁢󠁷󠁬󠁳󠁿":"5294139949346476093","🇾🇪":"5294058972033076492","🇿🇲":"5294100109229838880","🇿🇼":"5294422158762592930",
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

# ══════════════════════════════════════════════════════════════
#  OTP HELPERS
# ══════════════════════════════════════════════════════════════

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
        "WhatsApp": ["whatsapp","wapp","wa "],  "Facebook": ["facebook","fb "],
        "Telegram": ["telegram","tg "],          "Instagram": ["instagram","ig "],
        "TikTok":   ["tiktok","tik tok"],        "Google": ["google","gmail"],
        "Discord":  ["discord"],                 "Twitter": ["twitter","x.com"],
        "Apple":    ["apple","icloud"],           "Binance": ["binance"],
        "PayPal":   ["paypal"],                  "Microsoft": ["microsoft","outlook"],
        "1xBet":    ["1xbet","1x bet"],
    }
    t = (sms+" "+cli).lower()
    for svc, keys in SVCS.items():
        if any(k in t for k in keys): return svc
    return cli.strip() if cli.strip() else "SMS"

# ══════════════════════════════════════════════════════════════
#  TELEGRAM SEND (OTP forward — raw urllib, no bot object needed)
# ══════════════════════════════════════════════════════════════

def _tg_raw_send(text, keyboard=None):
    for cid in get_chat_ids():
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
                    log.warning(f"TG 429 → {cid}: retry in {wait}s (attempt {attempt+1})")
                    time.sleep(wait)
                else:
                    log.error(f"TG forward → {cid}: {e}")
                    break
            except Exception as e:
                log.error(f"TG forward → {cid}: {e}")
                break
        else:
            log.error(f"TG forward → {cid}: 429 max retries exceeded, message dropped")
        time.sleep(0.05)

def _mask_number(num_clean):
    """Middle digits replace with CDY. e.g. 8801712345678 → 8801712CDY678"""
    if len(num_clean) < 7:
        return num_clean
    keep_start = len(num_clean) - 3
    if keep_start <= 4:
        return num_clean
    return num_clean[:4] + "SPYX" + num_clean[keep_start:]

# Language: ISO code → (short, full_name)
LANG_DATA = {
    "EN": ("EN", "English"),
    "AR": ("AR", "Arabic"),
    "BN": ("BN", "Bengali"),
    "CN": ("CN", "Chinese"),
    "RU": ("RU", "Russian"),
    "TR": ("TR", "Turkish"),
    "FA": ("FA", "Persian"),
    "HI": ("HI", "Hindi"),
    "UR": ("UR", "Urdu"),
    "ID": ("ID", "Indonesian"),
    "MS": ("MS", "Malay"),
    "TH": ("TH", "Thai"),
    "VI": ("VI", "Vietnamese"),
    "FR": ("FR", "French"),
    "DE": ("DE", "German"),
    "ES": ("ES", "Spanish"),
    "PT": ("PT", "Portuguese"),
    "IT": ("IT", "Italian"),
    "PL": ("PL", "Polish"),
    "UK": ("UK", "Ukrainian"),
    "KO": ("KO", "Korean"),
    "JA": ("JA", "Japanese"),
}

def _render_template(tmpl, bn, number, otp, service, sms=""):
    """Template JSON দিয়ে (text, inline_keyboard_dict) বানাও।"""
    num_clean = re.sub(r"\D", "", str(number))
    num_masked = _mask_number(num_clean)

    flag, iso, country  = _get_country(number)
    fid      = FLAG_STICKER.get(flag)
    flag_tag = f'<tg-emoji emoji-id="{fid}">{flag}</tg-emoji>' if fid else flag
    svc_id   = SVC_BTN_STICKER.get(service)
    svc_icon = f'<tg-emoji emoji-id="{svc_id}">📩</tg-emoji>' if svc_id else "📩"

    lang_code = "EN"
    if re.search(r"[\u0600-\u06FF]", sms):
        if iso in ("PK", "AF"):          lang_code = "UR"
        elif iso == "IR":                lang_code = "FA"
        else:                            lang_code = "AR"
    elif re.search(r"[\u0980-\u09FF]", sms):  lang_code = "BN"
    elif re.search(r"[\u4e00-\u9fff]", sms):  lang_code = "CN"
    elif re.search(r"[\u0400-\u04FF]", sms):
        if iso in ("UA",):               lang_code = "UK"
        else:                            lang_code = "RU"
    elif re.search(r"[\u0900-\u097F]", sms):  lang_code = "HI"
    elif re.search(r"[\u0e00-\u0e7f]", sms):  lang_code = "TH"
    elif re.search(r"[\uac00-\ud7af]", sms):  lang_code = "KO"
    elif re.search(r"[\u3040-\u30ff]", sms):  lang_code = "JA"
    elif re.search(r"\b(le|la|les|est|bonjour)\b", sms, re.I):  lang_code = "FR"
    elif re.search(r"\b(der|die|das|ist|bitte)\b", sms, re.I):  lang_code = "DE"

    lang_short, lang_full = LANG_DATA.get(lang_code, (lang_code, lang_code))

    country_tag = f"{flag} {iso} {country}"

    vars_ = {
        "{flag}":         flag_tag,
        "{flag_plain}":   flag,
        "{iso}":          iso,
        "{sender}":       service,
        "{number}":       num_clean,
        "{number_masked}": num_masked,
        "{otp}":          str(otp),
        "{language}":     lang_short,
        "{lang_full}":    lang_full,
        "{country}":      country,
        "{country_iso}":  iso,
        "{country_tag}":  country_tag,
        "{panel}":        bn,
        "{message}":      sms[:80] if sms else "",
        "{time}":         time.strftime("%H:%M"),
        "{svc_icon}":     svc_icon,
    }

    def subst(s):
        for k, v in vars_.items():
            s = s.replace(k, v)
        return s

    raw_text = tmpl.get("text", DEFAULT_TEMPLATE["text"])
    raw_text = raw_text.replace("\\n", "\n")
    import re as _re2
    raw_text = _re2.sub(r"\n(\n+)", lambda m: "\n" + "\u200b\n" * len(m.group(1)), raw_text)
    text = subst(raw_text)
    btns = tmpl.get("buttons", DEFAULT_TEMPLATE["buttons"])

    STYLE_MAP = {
        "primary": "primary",
        "success": "success",
        "danger":  "danger",
        "warn":    "primary",
        "accent":  "primary",
        "default": None,
    }

    kb_rows = []
    cur_row = []
    for b in btns:
        if b.get("type") == "sep":
            if cur_row: kb_rows.append(cur_row); cur_row = []
            continue
        label = subst(b.get("label", service))
        if b.get("type") == "link":
            btn = {"text": label, "url": subst(b.get("value", ""))}
        elif b.get("type") == "copy":
            btn = {"text": label, "copy_text": {"text": subst(b.get("value", str(otp)))}}
        else:
            btn = {"text": label, "copy_text": {"text": str(otp)}}

        raw_style = b.get("style", "default")
        tg_style = STYLE_MAP.get(raw_style)
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


def _build_otp_msg(bn, number, otp, service, sms=""):
    """Load saved template and render. Falls back to default."""
    raw = get_setting("otp_template")
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
    """Single thread — queue থেকে message নিয়ে একতাবার send করে।"""
    while True:
        try:
            text, kb = _send_queue.get(timeout=5)
        except _queue.Empty:
            continue
        try:
            _tg_raw_send(text, kb)
        except Exception as e:
            log.error(f"sender_worker error: {e}")
        finally:
            _send_queue.task_done()
        time.sleep(0.8)

threading.Thread(target=_sender_worker, daemon=True, name="TGSender").start()

def _forward(bn, number, otp, service, sms):
    num_clean = re.sub(r"\D","",str(number))
    key = f"{bn}:+{num_clean}:{otp}"
    now = time.time()
    with _fwd_lock:
        if now - _fwd_seen.get(key, 0) < 90: return
        _fwd_seen[key] = now
        if len(_fwd_seen) > 5000:
            cutoff = now - 60
            for k in [k for k,v in list(_fwd_seen.items()) if v < cutoff]:
                _fwd_seen.pop(k, None)
    qsize = _send_queue.qsize()
    if qsize > 50:
        log.warning(f"Send queue backed up: {qsize} pending")
    text, kb = _build_otp_msg(bn, number, otp, service, sms)
    _send_queue.put((text, kb))
    log.info(f"✅ [{bn}] +{num_clean} → {otp} ({service})")

# ══════════════════════════════════════════════════════════════
#  PANEL RUNNER ENGINE
# ══════════════════════════════════════════════════════════════

_panel_stop:     dict = {}
_running_threads:dict = {}

def _sleep_panel(bn, ptype):
    if ptype == "timesms":    time.sleep(30)
    elif bn in _KEEPALIVE:    time.sleep(5)
    elif bn in _SLOW:         time.sleep(20)
    elif bn in _MEDIUM:       time.sleep(12)
    else:                     time.sleep(8)

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
            return (lambda u,p: timesms_login(bn,u,p,url), lambda s: timesms_fetch(bn,s,url))
        if ptype == "ims":
            return (lambda u,p: ims_login(u,p,url), lambda s: ims_fetch(s,url))
        if ptype == "konekta":
            return (lambda u,p: konekta_login(u,p), lambda s: konekta_fetch(s))
        if ptype in ("roxy","voicegate","numberpanel") or bn == "SniperPanel":
            return (lambda u,p: new_panel_login(bn,u,p,url), lambda s: new_panel_fetch(bn,s,url))
        if ptype == "standard":
            return (lambda u,p: panel_login(bn,u,p,url), lambda s: panel_fetch(s,url))
        if ptype == "proofsms":
            return (lambda u,p: ints_login(bn,u,p,url,fp), lambda s: proofsms_fetch(s,url))
        return (lambda u,p: ints_login(bn,u,p,url,fp), lambda s: ints_fetch(bn,s,url))
    except Exception as e:
        log.error(f"[{bn}] _get_fns error: {e}")
        return None, None

def _account_loop(bn, url, ptype, fp, username, password, stop_evt):
    import html as _html
    login_fn, fetch_fn = _get_fns(bn, url, ptype, fp)
    if not login_fn:
        log.error(f"[{bn}:{username}] cannot get fetch functions"); return

    log.info(f"▶ [{bn}:{username}] starting")
    seen=set(); session=None; fails=0; empty_s=0

    while not stop_evt.is_set():
        try:
            if session is None:
                try:
                    session = login_fn(username, password)
                    log.info(f"[{bn}:{username}] login OK"); fails=0
                except Exception as e:
                    fails += 1
                    log.error(f"[{bn}:{username}] login fail #{fails}: {e}")
                    stop_evt.wait(min(20*(2**min(fails-1,4)), 300))
                    continue

            try:
                rows = fetch_fn(session)
            except Exception as fe:
                log.warning(f"[{bn}:{username}] fetch error: {fe}")
                session=None; empty_s=0; stop_evt.wait(15); continue

            if rows is None:
                log.info(f"[{bn}:{username}] session expired → re-login")
                session=None; empty_s=0; stop_evt.wait(5 if bn in _KEEPALIVE else 10); continue

            if not rows:
                empty_s += 1
                if empty_s >= 180:
                    log.info(f"[{bn}:{username}] 180 empty → re-login")
                    session=None; empty_s=0
                _sleep_panel(bn, ptype); continue
            empty_s = 0

            for row in rows:
                if isinstance(row, list):
                    num=str(row[2]) if len(row)>2 else ""
                    cli=str(row[3]) if len(row)>3 else ""
                    sms=str(row[5] if len(row)>5 else (row[4] if len(row)>4 else ""))
                    dt =str(row[0]) if row else ""
                elif isinstance(row, dict):
                    num=str(row.get("number",row.get("num","")))
                    cli=str(row.get("cli",   row.get("service","")))
                    sms=str(row.get("sms",   row.get("message","")))
                    dt =str(row.get("date",  row.get("dt","")))
                else: continue

                if not num: continue
                num_clean = re.sub(r"\D","",num)
                otp = _detect_otp(sms)
                if not otp: continue

                if dt and dt not in ("None", "0", ""):
                    _row_dt = None
                    try:
                        _ts_val = float(dt.strip())
                        if 1577836800 < _ts_val < 2051222400:
                            _row_dt = _dt.fromtimestamp(_ts_val, tz=_UTC.utc)
                    except (ValueError, OSError):
                        pass
                    if _row_dt is None:
                        for _fmt in (
                            "%Y-%m-%d %H:%M:%S",
                            "%Y-%m-%dT%H:%M:%S",
                            "%Y-%m-%d %H:%M:%S.%f",
                            "%d/%m/%Y %H:%M:%S",
                            "%Y-%m-%dT%H:%M:%S.%f",
                            "%d-%m-%Y %H:%M:%S",
                            "%m/%d/%Y %H:%M:%S",
                        ):
                            try:
                                _row_dt = _dt.strptime(dt.strip(), _fmt).replace(tzinfo=_UTC.utc)
                                break
                            except ValueError:
                                pass
                    if _row_dt is None:
                        log.debug(f"[{bn}] unrecognized ts format '{dt}' → pass to seen-set")
                    else:
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
                if len(seen) > 8000: seen = set(list(seen)[-3000:])

                svc = _detect_svc(sms, cli)
                _forward(bn, f"+{num_clean}", otp, svc, _html.unescape(sms[:120]))

        except Exception as e:
            log.error(f"[{bn}:{username}] loop error: {e}"); session=None

        _sleep_panel(bn, ptype)
    log.info(f"⏹ [{bn}:{username}] stopped")

def start_panel(panel_row):
    bn    = panel_row["name"]
    url   = panel_row["url"]
    ptype = panel_row["ptype"]
    fp    = panel_row["fp"]

    stop_panel(bn)

    accounts = get_accounts(bn)
    if not accounts:
        log.warning(f"[{bn}] no active accounts — skipped"); return False

    stop_evt = threading.Event()
    _panel_stop[bn] = stop_evt
    _running_threads[bn] = []

    for acc in accounts:
        t = threading.Thread(
            target=_account_loop,
            args=(bn, url, ptype, fp, acc["username"], acc["password"], stop_evt),
            daemon=True, name=f"{bn}:{acc['username']}"
        )
        t.start()
        _running_threads[bn].append(t)
        time.sleep(0.2)

    log.info(f"✅ [{bn}] started {len(accounts)} account(s)")
    return True

def stop_panel(bn):
    if bn in _panel_stop:
        _panel_stop[bn].set()
        del _panel_stop[bn]
    if bn in _running_threads:
        del _running_threads[bn]

def is_running(bn):
    if bn not in _panel_stop or _panel_stop[bn].is_set(): return False
    return any(t.is_alive() for t in _running_threads.get(bn, []))

def start_all():
    count = 0
    for p in get_panels():
        if p["enabled"] and start_panel(p): count += 1
    return count

# ══════════════════════════════════════════════════════════════
#  KEYBOARD BUILDERS
# ══════════════════════════════════════════════════════════════

MAIN_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("📋 Panels"),
      KeyboardButton("👤 Accounts"),
      KeyboardButton("⚙️ Settings")]],
    resize_keyboard=True
)

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

def kb_panel_list():
    panels = get_panels()
    rows = []
    for p in panels:
        icon = "🟢" if is_running(p["name"]) else ("⚫" if not p["enabled"] else "🔴")
        rows.append([InlineKeyboardButton(
            f"{icon} {p['name']}", callback_data=f"pv:{p['name']}")])
    rows.append([InlineKeyboardButton("« Back", callback_data="p:back")])
    return InlineKeyboardMarkup(rows)

def kb_panel_detail(bn):
    running = is_running(bn)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏹ Stop" if running else "▶ Start",
                              callback_data=f"pd:toggle:{bn}")],
        [InlineKeyboardButton("➕ Add Account", callback_data=f"pd:addacc:{bn}"),
         InlineKeyboardButton("👤 Accounts",    callback_data=f"pd:accs:{bn}")],
        [InlineKeyboardButton("🗑 Delete Panel", callback_data=f"pd:del:{bn}")],
        [InlineKeyboardButton("« Back",          callback_data="p:list")],
    ])

def kb_panel_accounts(bn):
    accs = get_all_accounts(bn)
    rows = []
    for a in accs:
        icon = "✅" if a["active"] else "❌"
        rows.append([
            InlineKeyboardButton(f"{icon} {a['username']}", callback_data=f"ac:noop"),
            InlineKeyboardButton("🗑 Del", callback_data=f"ac:del:{a['id']}:{bn}"),
        ])
    rows.append([InlineKeyboardButton("➕ Add Account", callback_data=f"pd:addacc:{bn}")])
    rows.append([InlineKeyboardButton("« Back",         callback_data=f"pv:{bn}")])
    return InlineKeyboardMarkup(rows)

def kb_builtin_select():
    rows = []
    row  = []
    for name in sorted(BUILTIN_PANELS.keys()):
        row.append(InlineKeyboardButton(name, callback_data=f"bi:{name}"))
        if len(row) == 3: rows.append(row); row=[]
    if row: rows.append(row)
    rows.append([InlineKeyboardButton("✏️ Custom Panel", callback_data="bi:__custom__")])
    rows.append([InlineKeyboardButton("« Cancel",        callback_data="p:back")])
    return InlineKeyboardMarkup(rows)

def kb_ptype():
    rows = []
    row  = []
    for pt in PANEL_TYPES:
        row.append(InlineKeyboardButton(pt, callback_data=f"pt:{pt}"))
        if len(row) == 3: rows.append(row); row=[]
    if row: rows.append(row)
    return InlineKeyboardMarkup(rows)

def kb_panel_select_for_account():
    panels = get_panels()
    rows = [[InlineKeyboardButton(p["name"], callback_data=f"pd:addacc:{p['name']}")]
            for p in panels]
    rows.append([InlineKeyboardButton("« Cancel", callback_data="a:back")])
    return InlineKeyboardMarkup(rows)

# ══════════════════════════════════════════════════════════════
#  USER CONVERSATION STATE
# ══════════════════════════════════════════════════════════════

_ustate: dict = {}

def is_admin(uid):
    return uid in ADMIN_IDS

# ══════════════════════════════════════════════════════════════
#  HANDLERS
# ══════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Access denied."); return
    await update.message.reply_text(
        "🤖 <b>OTP Bot Manager</b>\n\nChoose an option:",
        parse_mode="HTML", reply_markup=MAIN_KB
    )

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔ Access denied."); return

    text = update.message.text.strip()

    if text == "📋 Panels":
        _ustate.pop(uid, None)
        await update.message.reply_text("📋 <b>Panels</b>",
                                        parse_mode="HTML", reply_markup=kb_panels_home())
        return
    if text == "👤 Accounts":
        _ustate.pop(uid, None)
        await update.message.reply_text("👤 <b>Accounts</b>",
                                        parse_mode="HTML", reply_markup=kb_accounts_home())
        return
    if text == "⚙️ Settings":
        _ustate.pop(uid, None)
        await update.message.reply_text("⚙️ <b>Settings</b>",
                                        parse_mode="HTML", reply_markup=kb_settings_home())
        return

    st = _ustate.get(uid)
    if not st:
        await update.message.reply_text("Use the buttons below. 👇", reply_markup=MAIN_KB)
        return

    action = st.get("action")

    if action == "add_chat":
        cid = text.strip()
        with db_conn() as c:
            c.execute("INSERT OR IGNORE INTO chat_ids(chat_id) VALUES(?)", (cid,))
        _ustate.pop(uid, None)
        await update.message.reply_text(
            f"✅ Chat ID <code>{cid}</code> added!", parse_mode="HTML", reply_markup=MAIN_KB)

    elif action == "add_builtin":
        if "username" not in st:
            st["username"] = text
            await update.message.reply_text("🔑 Enter <b>password</b>:", parse_mode="HTML")
        else:
            bn    = st["panel_name"]
            uname = st["username"]
            pwd   = text
            with db_conn() as c:
                c.execute("INSERT OR REPLACE INTO panels(name,url,ptype,fp) VALUES(?,?,?,?)",
                          (bn, st["url"], st["ptype"], st.get("fp")))
                c.execute("INSERT INTO accounts(panel_name,username,password) VALUES(?,?,?)",
                          (bn, uname, pwd))
            _ustate.pop(uid, None)
            with db_conn() as c:
                p = c.execute("SELECT * FROM panels WHERE name=?", (bn,)).fetchone()
            started = start_panel(p)
            await update.message.reply_text(
                f"✅ <b>{bn}</b> added{'& started' if started else ' (no start — check accounts)'}!\n"
                f"Account: <code>{uname}</code>",
                parse_mode="HTML", reply_markup=MAIN_KB)

    elif action == "add_custom":
        if "panel_name" not in st:
            st["panel_name"] = text
            await update.message.reply_text("🌐 Enter panel <b>URL</b>:", parse_mode="HTML")
        elif "url" not in st:
            st["url"] = text
            await update.message.reply_text(
                "🔧 Select panel <b>type</b>:", parse_mode="HTML", reply_markup=kb_ptype())

    elif action == "save_template":
        raw = text.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        raw = raw.strip()
        try:
            tmpl = json.loads(raw)
            if "text" not in tmpl:
                raise ValueError("'text' field missing")
            set_setting("otp_template", json.dumps(tmpl))
            _ustate.pop(uid, None)
            prev_text, prev_kb = _render_template(
                tmpl, "TestPanel", "+8801712345678", "847293", "WhatsApp",
                "Your WhatsApp code is 847293"
            )
            await update.message.reply_text(
                "✅ <b>Template saved!</b>\n\n"
                "নিচে preview দেখো — group এ এভাবে যাবে:\n"
                "─────────────────",
                parse_mode="HTML", reply_markup=MAIN_KB)
            payload = {
                "chat_id": uid,
                "text": prev_text,
                "parse_mode": "HTML"
            }
            if prev_kb:
                payload["reply_markup"] = json.dumps(prev_kb)
            data = json.dumps(payload).encode()
            try:
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    data=data, headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    r.read()
            except Exception as e:
                log.error(f"Preview send error: {e}")
        except Exception as e:
            await update.message.reply_text(
                f"❌ <b>Invalid JSON!</b>\n\n<code>{str(e)[:120]}</code>\n\n"
                "Template Editor থেকে <b>📋 Copy Code</b> button দিয়ে copy করো। "
                "তারপর এখানে paste করো।",
                parse_mode="HTML")
        return

    elif action == "add_account":
        bn = st["panel_name"]
        if "username" not in st:
            st["username"] = text
            await update.message.reply_text("🔑 Enter <b>password</b>:", parse_mode="HTML")
        else:
            uname = st["username"]
            pwd   = text
            with db_conn() as c:
                c.execute("INSERT INTO accounts(panel_name,username,password) VALUES(?,?,?)",
                          (bn, uname, pwd))
            _ustate.pop(uid, None)
            with db_conn() as c:
                p = c.execute("SELECT * FROM panels WHERE name=?", (bn,)).fetchone()
            if p and p["enabled"]:
                stop_panel(bn); time.sleep(0.3); start_panel(p)
            await update.message.reply_text(
                f"✅ Account <code>{uname}</code> added to <b>{bn}</b>.",
                parse_mode="HTML", reply_markup=MAIN_KB)

async def _safe_edit(q, text, parse_mode=None, reply_markup=None):
    """edit_message_text wrapper — message delete হলে নতুন message পাঠায়।"""
    try:
        await q.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        err = str(e).lower()
        if "not found" in err or "message to edit" in err or "message_id_invalid" in err:
            try:
                await q.message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
            except Exception:
                pass
        elif "message is not modified" in err:
            pass
        else:
            log.warning(f"edit_message_text error: {e}")


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    if not is_admin(uid):
        await q.answer("⛔ Access denied", show_alert=True); return
    try:
        await q.answer()
    except Exception:
        pass
    d = q.data

    if d == "p:back":
        await _safe_edit(q, "📋 <b>Panels</b>", parse_mode="HTML",
                                  reply_markup=kb_panels_home())

    elif d == "p:list":
        panels = get_panels()
        txt = "📋 Select a panel:" if panels else "No panels yet.\nAdd one with ➕"
        kb  = kb_panel_list() if panels else kb_panels_home()
        await _safe_edit(q, txt, reply_markup=kb)

    elif d == "p:add":
        await _safe_edit(q, "➕ Select panel to add:",
                                  reply_markup=kb_builtin_select())

    elif d == "p:allon":
        count = 0
        for p in get_panels():
            with db_conn() as c:
                c.execute("UPDATE panels SET enabled=1 WHERE name=?", (p["name"],))
            with db_conn() as c:
                p2 = c.execute("SELECT * FROM panels WHERE name=?", (p["name"],)).fetchone()
            if start_panel(p2): count += 1
        await _safe_edit(q, f"✅ Started <b>{count}</b> panels.", parse_mode="HTML",
                                  reply_markup=kb_panels_home())

    elif d == "p:alloff":
        for p in get_panels():
            stop_panel(p["name"])
            with db_conn() as c:
                c.execute("UPDATE panels SET enabled=0 WHERE name=?", (p["name"],))
        await _safe_edit(q, "⏹ All panels stopped.", reply_markup=kb_panels_home())

    elif d == "p:restartall":
        count = 0
        for p in get_panels():
            if p["enabled"]:
                stop_panel(p["name"]); time.sleep(0.2)
                with db_conn() as c:
                    p2 = c.execute("SELECT * FROM panels WHERE name=?", (p["name"],)).fetchone()
                if start_panel(p2): count += 1
        await _safe_edit(q, f"🔄 Restarted <b>{count}</b> panels.", parse_mode="HTML",
                                  reply_markup=kb_panels_home())

    elif d.startswith("bi:"):
        name = d[3:]
        if name == "__custom__":
            _ustate[uid] = {"action": "add_custom"}
            await _safe_edit(q, "✏️ Enter panel <b>name</b>:", parse_mode="HTML")
        else:
            bp = BUILTIN_PANELS[name]
            _ustate[uid] = {
                "action": "add_builtin",
                "panel_name": name,
                "url": bp["url"],
                "ptype": bp["ptype"],
                "fp": bp.get("fp"),
            }
            await _safe_edit(q,
                f"📌 <b>{name}</b>\n\nEnter <b>username</b>:", parse_mode="HTML")

    elif d.startswith("pt:"):
        ptype = d[3:]
        st = _ustate.get(uid, {})
        st["ptype"] = ptype
        st["fp"]    = None
        _ustate[uid] = st
        st["action"] = "add_builtin"
        await _safe_edit(q,
            f"Type: <b>{ptype}</b> ✅\n\nEnter <b>username</b>:", parse_mode="HTML")

    elif d.startswith("pv:"):
        bn = d[3:]
        with db_conn() as c:
            p = c.execute("SELECT * FROM panels WHERE name=?", (bn,)).fetchone()
        if not p:
            await _safe_edit(q, "Panel not found."); return
        accs    = get_accounts(bn)
        running = is_running(bn)
        status  = "🟢 Running" if running else ("⚫ Stopped" if p["enabled"] else "🔴 Disabled")
        await _safe_edit(q,
            f"📌 <b>{bn}</b>\n"
            f"Status : {status}\n"
            f"Type   : <code>{p['ptype']}</code>\n"
            f"URL    : <code>{p['url']}</code>\n"
            f"Accounts: {len(accs)} active",
            parse_mode="HTML", reply_markup=kb_panel_detail(bn))

    elif d.startswith("pd:"):
        _, action, bn = d.split(":", 2)

        if action == "toggle":
            with db_conn() as c:
                p = c.execute("SELECT * FROM panels WHERE name=?", (bn,)).fetchone()
            if is_running(bn):
                stop_panel(bn)
                with db_conn() as c:
                    c.execute("UPDATE panels SET enabled=0 WHERE name=?", (bn,))
                msg = f"⏹ <b>{bn}</b> stopped."
            else:
                with db_conn() as c:
                    c.execute("UPDATE panels SET enabled=1 WHERE name=?", (bn,))
                    p = c.execute("SELECT * FROM panels WHERE name=?", (bn,)).fetchone()
                ok  = start_panel(p)
                msg = f"✅ <b>{bn}</b> started." if ok else f"⚠️ <b>{bn}</b>: no accounts — add one first!"
            await _safe_edit(q, msg, parse_mode="HTML",
                                      reply_markup=kb_panel_detail(bn))

        elif action == "del":
            stop_panel(bn)
            with db_conn() as c:
                c.execute("DELETE FROM accounts WHERE panel_name=?", (bn,))
                c.execute("DELETE FROM panels WHERE name=?", (bn,))
            panels = get_panels()
            txt = f"🗑 <b>{bn}</b> deleted."
            kb  = kb_panel_list() if panels else kb_panels_home()
            await _safe_edit(q, txt, parse_mode="HTML", reply_markup=kb)

        elif action == "addacc":
            _ustate[uid] = {"action": "add_account", "panel_name": bn}
            await _safe_edit(q,
                f"➕ Add account to <b>{bn}</b>\n\nEnter <b>username</b>:",
                parse_mode="HTML")

        elif action == "accs":
            await _safe_edit(q,
                f"👤 Accounts — <b>{bn}</b>:",
                parse_mode="HTML", reply_markup=kb_panel_accounts(bn))

    elif d.startswith("ac:"):
        parts = d.split(":")
        action = parts[1]
        if action == "noop":
            return
        if action == "del":
            acc_id = int(parts[2])
            bn     = parts[3]
            with db_conn() as c:
                c.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
            await _safe_edit(q,
                f"🗑 Account removed from <b>{bn}</b>.",
                parse_mode="HTML", reply_markup=kb_panel_accounts(bn))

    elif d == "a:list":
        panels = get_panels()
        if not panels:
            await _safe_edit(q, "No panels yet.", reply_markup=kb_accounts_home()); return
        rows = []
        for p in panels:
            n = len(get_all_accounts(p["name"]))
            rows.append([InlineKeyboardButton(f"📌 {p['name']} ({n} accs)",
                                              callback_data=f"pd:accs:{p['name']}")])
        rows.append([InlineKeyboardButton("« Back", callback_data="a:back")])
        await _safe_edit(q, "👤 Select panel:", reply_markup=InlineKeyboardMarkup(rows))

    elif d == "a:add":
        panels = get_panels()
        if not panels:
            await _safe_edit(q, "No panels yet. Add a panel first.",
                                      reply_markup=kb_accounts_home()); return
        await _safe_edit(q, "Select panel to add account to:",
                                  reply_markup=kb_panel_select_for_account())

    elif d == "a:back":
        await _safe_edit(q, "👤 <b>Accounts</b>", parse_mode="HTML",
                                  reply_markup=kb_accounts_home())

    elif d == "s:addchat":
        _ustate[uid] = {"action": "add_chat"}
        await _safe_edit(q,
            "📢 Send the <b>Chat ID</b>:\n"
            "Group/Channel: <code>-1001234567890</code>\n"
            "Personal: <code>987654321</code>",
            parse_mode="HTML")

    elif d == "s:listchat":
        chats = get_chat_ids()
        txt = ("📢 <b>Chat IDs:</b>\n" + "\n".join(f"• <code>{c}</code>" for c in chats)
               if chats else "No chat IDs added yet.")
        await _safe_edit(q, txt, parse_mode="HTML",
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("« Back", callback_data="s:back")]]))

    elif d == "s:delchat":
        chats = get_chat_ids()
        if not chats:
            await _safe_edit(q, "No chats to remove.", reply_markup=kb_settings_home()); return
        rows  = [[InlineKeyboardButton(f"🗑 {c}", callback_data=f"sc:{c}")] for c in chats]
        rows.append([InlineKeyboardButton("« Back", callback_data="s:back")])
        await _safe_edit(q, "Select chat to remove:", reply_markup=InlineKeyboardMarkup(rows))

    elif d.startswith("sc:"):
        cid = d[3:]
        with db_conn() as c:
            c.execute("DELETE FROM chat_ids WHERE chat_id=?", (cid,))
        await _safe_edit(q, f"🗑 <code>{cid}</code> removed.", parse_mode="HTML",
                                  reply_markup=kb_settings_home())

    elif d == "s:status":
        panels  = get_panels()
        total   = len(panels)
        running = sum(1 for p in panels if is_running(p["name"]))
        chats   = get_chat_ids()
        lines   = [f"{'🟢' if is_running(p['name']) else '🔴'} {p['name']}" for p in panels]
        await _safe_edit(q,
            f"📊 <b>Status</b>\n\n"
            f"🟢 Running : {running}/{total}\n"
            f"📢 Chats  : {len(chats)}\n\n"
            + ("\n".join(lines) if lines else "No panels"),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="s:status")],
                [InlineKeyboardButton("« Back",     callback_data="s:back")],
            ]))

    elif d == "s:back":
        await _safe_edit(q, "⚙️ <b>Settings</b>", parse_mode="HTML",
                                  reply_markup=kb_settings_home())

    elif d == "s:tmpl":
        raw = get_setting("otp_template")
        has = "✅ Template saved" if raw else "⚠️ Using default template"
        await _safe_edit(q,
            f"🎨 <b>OTP Format</b>\n\n"
            f"Status: {has}\n\n"
            f"📌 <b>Step 1:</b> নিচের link এ যাও, template বানাও\n"
            f"📌 <b>Step 2:</b> নিচের <b>📋 Copy Code</b> button click করো\n"
            f"📌 <b>Step 3:</b> copied JSON এখানে paste করো\n\n"
            f"🔗 <a href='{TEMPLATE_EDITOR_URL}'>Template Editor খোলো</a>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📝 Paste JSON নিচে লিখো", callback_data="s:tmpl_paste")],
                [InlineKeyboardButton("🧪 Test Format", callback_data="s:tmpl_test")],
                [InlineKeyboardButton("♻️ Reset Default", callback_data="s:tmpl_reset")],
                [InlineKeyboardButton("« Back", callback_data="s:back")],
            ]))

    elif d == "s:tmpl_paste":
        _ustate[uid] = {"action": "save_template"}
        await _safe_edit(q,
            "📋 Template Editor থেকে copy করা JSON paste করো:\n\n"
            "<i>(শুধু JSON পাঠাও — { দিয়ে শুরু হবে)</i>",
            parse_mode="HTML")

    elif d == "s:tmpl_test":
        raw = get_setting("otp_template")
        try:
            tmpl = json.loads(raw) if raw else DEFAULT_TEMPLATE
        except Exception:
            tmpl = DEFAULT_TEMPLATE
        text, kb = _render_template(
            tmpl, "TestPanel", "+8801712345678", "847293", "WhatsApp",
            "Your WhatsApp code is 847293"
        )
        await _safe_edit(q,
            "🧪 <b>Test পাঠানো হচ্ছে...</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("« Back", callback_data="s:tmpl")]
            ]))
        threading.Thread(target=_tg_raw_send, args=(text, kb), daemon=True).start()

    elif d == "s:tmpl_reset":
        set_setting("otp_template", json.dumps(DEFAULT_TEMPLATE))
        await _safe_edit(q,
            "♻️ Default template restore হয়েছে।",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("« Back", callback_data="s:tmpl")]
            ]))

# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main():
    db_init()
    count = start_all()
    log.info(f"✅ Auto-started {count} panel(s) on boot")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("🤖 Bot polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()