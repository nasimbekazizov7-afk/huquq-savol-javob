"""
Huquq savol-javob — Telegram bot (@huquqsavoljavob_bot)

Kirish darajalari:
  • demo (hamma uchun) — darsliklarning har bir mavzusidan va har bir qonunchilik hujjatidan 5 tadan savol
  • to'liq kirish      — barcha savol-javoblar: kanalga obuna + (narx > 0 bo'lsa) to'lov cheki va telefon raqami
                         adminga yuboriladi; admin tasdiqlagach tanlangan muddatga (oylik/yillik tarif) ochiladi
Admin buyruqlari: /admin
"""
import asyncio, json, os, re, sqlite3, time, html, logging
from pathlib import Path
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton as B,
                           FSInputFile, LabeledPrice, PreCheckoutQuery, WebAppInfo, MenuButtonWebApp)
import hmac, hashlib
from urllib.parse import parse_qsl
from aiohttp import web

load_dotenv()
BASE = Path(__file__).parent
TOKEN = os.environ["BOT_TOKEN"]
CHANNEL = os.getenv("CHANNEL", "@huquqsavoljavob")            # kanal (bot kanalda admin bo'lishi shart)
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/huquqsavoljavob")
BOT_USERNAME = os.getenv("BOT_USERNAME", "huquqsavoljavob_bot").lstrip("@")   # boshqa botga ko'chirilganda shu o'zgaruvchini yozing
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "")       # Payme/Click (BotFather → Payments). Bo'sh bo'lsa — qo'lda to'lov
DEMO_N = 5                                                      # demo: har bir mavzu / hujjatdan ochiq savollar
logging.basicConfig(level=logging.INFO)

# ---------------- Ma'lumotlar ----------------
BANK = json.loads((BASE / "data" / "bank.json").read_text(encoding="utf-8"))
SRC = {s["id"]: s for s in BANK["sources"]}
SENT = BANK["sentences"]
Q = {q["id"]: q for q in BANK["questions"]}
Q_BY_SRC, Q_BY_CH, Q_BY_L = {}, {}, {}
CH_SRC, CH_IDX, L_TITLE, CH_TITLE = {}, {}, {}, {}
for s in BANK["sources"]:
    for i, c in enumerate(s["chapters"]):
        CH_SRC[c["id"]], CH_IDX[c["id"]], CH_TITLE[c["id"]] = s["id"], i, c["title"]
        for l in c["lessons"]:
            L_TITLE[l["id"]] = l["title"]
for q in BANK["questions"]:
    sid = CH_SRC[q["c"]]
    Q_BY_SRC.setdefault(sid, []).append(q["id"])
    Q_BY_CH.setdefault(q["c"], []).append(q["id"])
    Q_BY_L.setdefault(q["l"], []).append(q["id"])
POS_IN_SRC = {qid: i for lst in Q_BY_SRC.values() for i, qid in enumerate(lst)}
BOOKS = [s for s in BANK["sources"] if s["type"] == "darslik"]
LAWS = [s for s in BANK["sources"] if s["type"] == "qonun"]

# ---------------- Baza ----------------
DB_PATH = Path(os.getenv("DB_PATH", str(BASE / "bot.sqlite3"))); DB_PATH.parent.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DB_PATH)
db.executescript("""
create table if not exists users(id integer primary key, name text, paid integer default 0, cyr integer default 0, created integer);
create table if not exists prog(uid integer, qid text, g text, n integer default 0, t integer, primary key(uid,qid));
create table if not exists kv(k text primary key, v text);
create table if not exists pays(id integer primary key autoincrement, uid integer, amount integer, kind text, t integer, info text);
create table if not exists feedback(id integer primary key autoincrement, uid integer, text text, t integer, src text, reply text, rt integer, seen integer default 0);
create table if not exists receipts(id integer primary key autoincrement, uid integer, file_id text, ftype text, kind text, amount integer, status text default 'pending', t integer, dt integer, admin integer, note text);
""")
for _tb, _col in (("users", "last integer"), ("users", "until integer"), ("users", "phone text"), ("users", "warned integer"), ("users", "agreed integer"), ("users", "lang text"), ("receipts", "days integer"),
                  ("receipts", "tariff text"), ("receipts", "months integer"), ("receipts", "phone text"), ("receipts", "path text")):
    try: db.execute(f"alter table {_tb} add column {_col}")
    except Exception: pass
RCPT_DIR = DB_PATH.parent / "receipts"; RCPT_DIR.mkdir(parents=True, exist_ok=True)
def kv(k, d=None):
    r = db.execute("select v from kv where k=?", (k,)).fetchone(); return r[0] if r else d
def set_kv(k, v): db.execute("insert or replace into kv values(?,?)", (k, str(v))); db.commit()
if kv("price") is None: set_kv("price", 500000)
if kv("auto_open") is None: set_kv("auto_open", 0)
if kv("paytext") is None or kv("paytext").startswith("To‘lov uchun admin bilan bog‘laning. To‘lov chekini (skrinshot) shu botga") or kv("paytext").startswith("To‘lovni admin ko‘rsatgan karta raqamiga"):
    set_kv("paytext", "Tanlangan summani yuqoridagi karta raqamiga (yoki QR kod orqali) o‘tkazing va to‘lov chekini biriktiring. Admin tasdiqlagach to‘liq kirish ochiladi.")
if kv("tariffs") is None:
    set_kv("tariffs", json.dumps([{"id": "m1", "name": "1 oylik obuna", "months": 1, "price": 50000},
                                  {"id": "y1", "name": "1 yillik obuna", "months": 12, "price": int(kv("price"))}], ensure_ascii=False))
def tariffs():
    try: out = [t_ for t_ in json.loads(kv("tariffs")) if t_.get("id")]
    except Exception: return []
    for t_ in out: t_["months"] = int(t_.get("months") or 0); t_["days"] = int(t_.get("days") or 0); t_["price"] = int(t_.get("price") or 0)
    return out
def period_text(m, d):
    m, d = int(m or 0), int(d or 0); p = []
    if m: p.append(f"{m // 12} yil" if m % 12 == 0 else f"{m} oy")
    if d: p.append(f"{d} kun")
    return " ".join(p) or "muddatsiz"
# Hamma uchun bepul davr va texnik to'xtatish (admin belgilaydi)
def free_until(): return int(kv("free_until") or 0)
def free_active(): return free_until() > time.time()
def maint_until(): return int(kv("maint_until") or 0)
def maint_active(): return maint_until() > time.time()
def maint_message():
    import datetime as _d
    u_ = _d.datetime.fromtimestamp(maint_until(), _d.timezone(_d.timedelta(hours=5))).strftime("%d.%m.%Y %H:%M")
    return (kv("maint_text") or "Bot va ilovada texnik ishlar olib borilmoqda.") + f"\n\n⏳ Xizmat {u_} gacha vaqtincha to‘xtatilgan. Noqulaylik uchun uzr so‘raymiz."
TERMS_VER = "2026-10-15"
def pay_info():
    return {"card": kv("card_number") or "", "owner": kv("card_owner") or "", "phone": kv("admin_phone") or "", "qr": bool(kv("card_qr"))}
def tariff(tid): return next((t_ for t_ in tariffs() if t_["id"] == tid), None)
def add_months(ts, m):
    import datetime as _d
    d = _d.datetime.fromtimestamp(ts, _d.timezone(_d.timedelta(hours=5)))
    y, mo = divmod(d.month - 1 + int(m), 12); y += d.year; mo += 1
    import calendar; day = min(d.day, calendar.monthrange(y, mo)[1])
    return int(d.replace(year=y, month=mo, day=day).timestamp())
def user(uid, name=""):
    r = db.execute("select id,name,paid,cyr,until,phone,agreed,lang from users where id=?", (uid,)).fetchone()
    now = int(time.time())
    if not r:
        db.execute("insert into users(id,name,created,last) values(?,?,?,?)", (uid, name, now, now)); db.commit()
        r = (uid, name, 0, 0, None, None, None, None)
    else:
        db.execute("update users set last=?" + (", name=?" if name else "") + " where id=?", ((now, name, uid) if name else (now, uid))); db.commit()
    return {"id": r[0], "name": r[1], "paid": r[2], "cyr": r[3], "until": r[4], "phone": r[5], "agreed": r[6], "lang": r[7]}
def has_full(u): return bool(u["paid"]) or (u.get("until") or 0) > time.time() or u["id"] in ADMINS or free_active()

# ---------------- Lotin → Kirill ----------------
_M = dict(a="а", b="б", d="д", e="е", f="ф", g="г", h="ҳ", i="и", j="ж", k="к", l="л", m="м", n="н", o="о", p="п", q="қ", r="р", s="с", t="т", u="у", v="в", x="х", y="й", z="з", c="с", w="в")
_RX = re.compile(r"s[’ʼ'`]h|o[ʻ‘'`ʼ]|g[ʻ‘'`ʼ]|sh|ch|yo(?![ʻ‘'`ʼ])|yu|ya|ye|ts|’|ʼ|[a-z]", re.I)
_MON = {"январ": "январь", "феврал": "февраль", "апрел": "апрель", "июн": "июнь", "июл": "июль", "сентябр": "сентябрь", "октябр": "октябрь", "ноябр": "ноябрь", "декабр": "декабрь"}
# rus tilidan o'zlashgan so'zlar (o'zak → o'zak), shablondagi ro'yxat bilan bir xil
_EXS = sorted([("prinsip","принцип"),("funksiya","функция"),("funksional","функционал"),("aksiyador","акциядор"),("aksioner","акционер"),("aksiz","акциз"),("aksiya","акция"),("aksent","акцент"),("aksept","акцепт"),("sanksiya","санкция"),("reaksiya","реакция"),("fraksiya","фракция"),("inspeksiya","инспекция"),("instansiya","инстанция"),("kompetensiya","компетенция"),("konsepsiya","концепция"),("konsert","концерт"),("konsern","концерн"),("konvensiya","конвенция"),("kolleksiya","коллекция"),("korreksiya","коррекция"),("distansiya","дистанция"),("tendensiya","тенденция"),("potensial","потенциал"),("sement","цемент"),("senz","ценз"),("sentr","центр"),("sivil","цивил"),("sikl","цикл"),("sirk","цирк"),("sitata","цитата"),("kvitansiya","квитанция"),("provinsiya","провинция"),("rezidensiya","резиденция"),("direksiya","дирекция"),("seksiya","секция"),("konstruksiya","конструкция"),("instruksiya","инструкция"),("produksiya","продукция"),("redaksiya","редакция"),("tranzaksiya","транзакция"),("abstraksiya","абстракция"),("fransiya","франция"),("fransuz","француз"),("kansler","канцлер"),("kanselyariya","канцелярия"),("konkurensiya","конкуренция"),("intervensiya","интервенция"),("preferensiya","преференция"),("konferensiya","конференция"),("ssenariy","сценарий"),("kompyuter","компьютер"),("pyesa","пьеса"),("obyekt","объект"),("subyekt","субъект"),("inyeksiya","инъекция"),("konyunktura","конъюнктура"),("podyezd","подъезд"),("rayon","район"),("mayor","майор"),("batalyon","батальон"),("pavilyon","павильон"),("sentabr","сентябр"),("oktabr","октябр"),("korrupsiya","коррупция"),("prezumpsiya","презумпция"),("auksion","аукцион"),("seleksiya","селекция"),("stansiya","станция"),("ensiklopediya","энциклопедия"),("proporsional","пропорционал"),("yurisdiksiya","юрисдикция"),("yurisprudensiya","юриспруденция"),("subvensiya","субвенция"),("sitrus","цитрус"),("reproduksiya","репродукция"),("rekonstruksiya","реконструкция"),("sentyabr","сентябр"),("konseptual","концептуал"),("ierarx","иерарх"),("lisey","лицей"),("aksionerlik","акционерлик")], key=lambda x: -len(x[0]))
_EXW = {"rol": "роль", "model": "модель", "parol": "пароль", "medal": "медаль", "avtomobil": "автомобиль", "mebel": "мебель", "spektakl": "спектакль", "portfel": "портфель"}
def _cap(w, r): return r.upper() if len(w) > 1 and w.isupper() else (r[0].upper() + r[1:] if w[0].isupper() else r)
def _exw(m):
    w = m.group(0); lo = w.lower()
    if lo in _EXW: return _cap(w, _EXW[lo])
    for l, c in _EXS:
        if lo.startswith(l): return _cap(w, c) + w[len(l):]
    return w
def _c0(s):
    def rep(m):
        t = m.group(0); lo = t.lower(); st = m.start(); prev = s[st - 1] if st else " "
        if len(lo) == 3 and lo[0] == "s" and lo[2] == "h": r = "сҳ"
        elif lo == "ts" and re.match(r"(a|in(?![a-z])|inlar|iz)", s[m.end():m.end() + 5], re.I): r = "тс"
        elif lo == "e" and prev.lower() in "aiouаиоуўэ": r = "э"
        elif len(lo) == 2 and lo[0] == "o" and not lo[1].isalpha(): r = "ў"
        elif len(lo) == 2 and lo[0] == "g" and not lo[1].isalpha(): r = "ғ"
        elif t in "’ʼ": return "ъ"
        else:
            r = {"sh": "ш", "ch": "ч", "yo": "ё", "yu": "ю", "ya": "я", "ye": "е", "ts": "ц"}.get(lo) or ("э" if lo == "e" and not prev.isalpha() else _M.get(lo))
        if r is None: return t
        if t[0].isupper(): return r.upper() if (len(t) == 1 or t[1:].isupper()) else r[0].upper() + r[1:]
        return r
    return _RX.sub(rep, s)
def cyr(s):
    parts = re.split(r"(<[^>]+>|\b(?:https?://)?(?:www\.)?[a-z]+\.(?:uz|com|org|ru|me)(?:/\S*)?|@\w+|\b(?![IVX]\. [A-ZʻO‘]{3})[IVXLC]+\b(?![ʻ‘'`ʼ’]))", s)
    out = []
    for i, p in enumerate(parts):
        if i % 2: out.append(p); continue
        p = re.sub(r"(\d)-(yil|yanvar|fevral|mart|aprel|may|iyun|iyul|avgust|sentabr|oktabr|noyabr|dekabr)", r"\1 \2", p, flags=re.I)
        p = _c0(re.sub(r"[A-Za-z]+", _exw, p))
        p = re.sub(r"(^|[^а-яёўқғҳъьА-ЯЁЎҚҒҲ])(январ|феврал|апрел|июн|июл|сентябр|октябр|ноябр|декабр)(?![а-яёўқғҳъь])", lambda m: m.group(1) + _cap(m.group(2), _MON[m.group(2).lower()]), p, flags=re.I)
        out.append(p)
    return "".join(out)
def T(u, s): return cyr(s) if u["cyr"] else s

# ---------------- Kirish huquqi ----------------
async def is_member(bot, uid):
    """True/False; tekshirib bo'lmasa (bot kanalda admin emas) — None"""
    try:
        m = await bot.get_chat_member(CHANNEL, uid)
        return m.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except Exception as e:
        logging.warning("a'zolikni tekshirib bo'lmadi: %s", e); return None
DEMO = set()   # demo savollar (pastda, platforma bazasi yuklangach to'ldiriladi)
def need_level(qid):
    """0 — demo, 2 — to'liq kirish"""
    return 0 if qid in DEMO else 2
def chapter_level(cid): return 0
async def level_of(bot, u): return 2 if has_full(u) else 0
LOCK = {0: "", 1: "🔒", 2: "🔒"}
def until_text(u):
    if u["id"] in ADMINS or u["paid"]: return "to‘liq kirish ochiq"
    if (u.get("until") or 0) > time.time():
        import datetime as _d
        return "to‘liq kirish " + _d.datetime.fromtimestamp(u["until"], _d.timezone(_d.timedelta(hours=5))).strftime("%d.%m.%Y") + " gacha"
    if free_active(): return "hamma uchun bepul davr: " + dstr(free_until()) + " gacha"
    return "demo"

# ---------------- Sessiya (xotirada) ----------------
SES = {}   # uid -> {"list":[qid], "i":int, "rev":bool, "title":str, "res":{k,p,u}}
def start_session(uid, qids, title):
    SES[uid] = {"list": qids, "i": 0, "rev": False, "title": title, "res": {"k": 0, "p": 0, "u": 0}}

# ---------------- Matn yordamchilari ----------------
STOP = set("va bilan uchun ham yoki bu shu qaysi qanday nima necha qancha kim kimlar qayerda qachon nechta qilib boʻlib boʻladi hisoblanadi etib deb esa emas mumkin ushbu mazkur oʻzbekiston respublikasi respublikasining respublikasida davlat qonun qonunga koʻra boʻyicha".split())
def _norm(w): return re.sub(r"[ʻ‘'`ʼ’]", "ʻ", w.lower())
def _stem(w):
    w = _norm(w); return re.sub(r"[^\d,./]", "", w) if w[:1].isdigit() else w[:max(4, min(len(w), 6))]
def _toks(s): return {_stem(w) for w in re.findall(r"[^\W\d_][\wʻ‘'`ʼ’-]*|\d+(?:[.,/]\d+)*", s) if (len(w) > 2 or w[:1].isdigit()) and _norm(w) not in STOP}
def highlight(text, ans, qq, is_src):
    A, Qs = _toks(ans), _toks(qq); out = []
    for part in re.split(r"([^\W\d_][\wʻ‘'`ʼ’-]*|\d+(?:[.,/]\d+)*)", text):
        if not part or not (part[0].isalnum()): out.append(html.escape(part)); continue
        w = _norm(part)
        if w in STOP or (len(w) < 3 and not w[:1].isdigit()): out.append(html.escape(part)); continue
        st = _stem(part)
        if is_src and st in A: out.append(f"<b>{html.escape(part)}</b>")
        elif st in Qs: out.append(f"<u>{html.escape(part)}</u>")
        else: out.append(html.escape(part))
    return "".join(out)
def kora(s):
    if s["type"] != "qonun": return "Darslikka ko‘ra"
    return {"Kodeks": "Kodeksga ko‘ra", "Nizom": "Nizomga ko‘ra", "Farmon": "Farmonga ko‘ra", "Konstitutsiya": "Konstitutsiyaga ko‘ra"}.get(s.get("kind"), "Qonunga ko‘ra")
def src_name(s): return s["title"] if s["type"] == "qonun" else f"“{s['title']}” · {s['grade']} darsligi"
def explain(q):
    s = SRC[CH_SRC[q["c"]]]; law = s["type"] == "qonun"
    ids = set(q["src"]); L = [k for k, v in SENT.items() if v["l"] == q["l"]] if law else [k for k in q["src"]]
    if law:
        pars = {SENT[k]["p"] for k in q["src"] if k in SENT}
        L = [k for k in L if SENT[k]["p"] in pars]
    body = "\n".join(highlight(SENT[k]["t"], q["a"], q["q"], k in ids) for k in L if k in SENT)
    loc = L_TITLE.get(q["l"], "").split(". ")[0]
    pg = "" if law else ", " + ", ".join(sorted({str(SENT[k]["pg"]) for k in q["src"] if k in SENT and SENT[k].get("pg")})) + "-bet"
    return f"📖 <i>{html.escape(kora(s))} · {html.escape(src_name(s))}, {html.escape(loc)}{pg}</i>\n{body}"

def card_text(u, uid):
    ss = SES[uid]; q = Q[ss["list"][ss["i"]]]; n = len(ss["list"])
    r = ss["res"]; head = f"<b>{ss['i']+1}/{n}</b> · {'Murakkab' if q['lvl']=='m' else 'Oddiy'}   ✅{r['k']} ≈{r['p']} ❌{r['u']}"
    t = f"{head}\n<i>{html.escape(ss['title'])}</i>\n\n❓ <b>{html.escape(q['q'])}</b>"
    if ss["rev"]:
        t += f"\n\n💡 <b>Javob:</b> {html.escape(q['a'])}"
        if q.get("w"): t += f"\n<i>{html.escape(q['w'])}</i>"
        t += "\n\n" + explain(q)
    t = T(u, t)
    return t[:4000] + ("…" if len(t) > 4000 else "")
def card_kb(u, uid):
    ss = SES[uid]; rows = []
    if not ss["rev"]: rows.append([B(text=T(u, "👁 Javobni ko‘rish"), callback_data="rev")])
    else: rows.append([B(text=T(u, "❌ Bilmadim"), callback_data="g:u"), B(text=T(u, "≈ Qisman"), callback_data="g:p"), B(text=T(u, "✅ Bildim"), callback_data="g:k")])
    rows.append([B(text="◀", callback_data="nav:-1"), B(text=T(u, "🏁 Yakunlash"), callback_data="fin"), B(text="▶", callback_data="nav:1")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def show_card(bot, chat_id, u, msg: Message | None = None):
    uid = u["id"]; ss = SES[uid]; q = Q[ss["list"][ss["i"]]]
    text, kb = card_text(u, uid), card_kb(u, uid)
    img = BASE / "data" / "img" / f"{q['img']}.png" if q.get("img") else None
    if img and img.exists() and not ss["rev"]:
        if msg:
            try: await msg.delete()
            except Exception: pass
        await bot.send_photo(chat_id, FSInputFile(img), caption=text[:1024], reply_markup=kb); return
    if msg and not msg.photo:
        try: await msg.edit_text(text, reply_markup=kb); return
        except Exception: pass
    if msg and msg.photo and not ss["rev"]:
        try: await msg.edit_caption(caption=text[:1024], reply_markup=kb); return
        except Exception: pass
    if msg and msg.photo:
        try: await msg.edit_reply_markup(reply_markup=None)
        except Exception: pass
    await bot.send_message(chat_id, text, reply_markup=kb)

# ---------------- Menyular ----------------
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")
APP_NAME = "huquqsavoljavob"
def main_kb(u):
    rows = []
    if WEBAPP_URL:
        rows.append([B(text=T(u, "📱 § ilovasini ochish").replace("§", APP_NAME), web_app=WebAppInfo(url=WEBAPP_URL + "/"))])
        if not has_full(u): rows.append([B(text=T(u, "🔓 To‘liq kirish — barcha savol-javoblar"), web_app=WebAppInfo(url=WEBAPP_URL + "/?buy=1"))])
    rows.append([B(text=T(u, "💬 Fikr bildirish"), callback_data="fb"), B(text=T(u, "📄 Foydalanish shartlari"), callback_data="terms")])
    rows.append([B(text=T(u, "📢 Kanal"), url=CHANNEL_URL), B(text=("Lotin" if u["cyr"] else "Кирилл"), callback_data="script")])
    if WEBAPP_URL and u["id"] in ADMINS: rows.append([B(text="🛠 Admin panel", web_app=WebAppInfo(url=WEBAPP_URL + "/admin"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)
def total_text():
    n = len(PB["questions"]); return f"{n // 1000 * 1000:,}".replace(",", " ")
WELCOME = ("<b>{app}</b> — huquqshunoslik fanlari bo‘yicha onlayn savol-javob platformasi\n\n"
           "📚 <b>{n} dan ortiq savol-javob</b>:\n"
           "• “Davlat va huquq asoslari” darsliklari (8–11-sinf) asosida;\n"
           "• 35 ta qonunchilik hujjati (Konstitutsiya, kodekslar, qonunlar) asosida.\n\n"
           "✨ <b>Imkoniyatlar:</b>\n"
           "• savol-javob kartochkalari yoki oddiy matn ko‘rinishi;\n"
           "• har bir javobga darslik yoki modda matnidan izoh;\n"
           "• darslik, bob, mavzu, modda bo‘yicha tanlash; oddiy/murakkab daraja; o‘ylash vaqti;\n"
           "• o‘zlashtirish statistikasi va bilmagan savollarni qayta ishlash;\n"
           "• lotin va kirill yozuvi; rus tilidagi versiya — darsliklarning asl rus nashrlari asosida (ilovada «Русский» tugmasi).\n\n"
           "🆓 <b>Demo:</b> har bir mavzu va hujjatdan 5 tadan savol.\n"
           "🔓 <b>To‘liq kirish:</b> kanalga obuna + admin tasdig‘i (obuna muddati tarif bo‘yicha).\n"
           "⚠️ Savol-javoblar sun’iy intellekt yordamida tuzilgan — xatolar bo‘lishi mumkin.\n\n"
           "Sizda: <b>{st}</b>\n👇 Ilovani oching")
def welcome_text(u): return T(u, WELCOME.format(app="§", n=total_text(), st=until_text(u))).replace("§", APP_NAME)
def fmt_price(): return f"{int(kv('price')):,}".replace(",", " ")
def fmt_sum(v): return f"{int(v or 0):,}".replace(",", " ")
WELCOME_IMG = BASE / "data" / "welcome.png"

r = Router()

async def send_welcome(bot, chat_id, u):
    if WELCOME_IMG.exists():
        try: return await bot.send_photo(chat_id, FSInputFile(WELCOME_IMG), caption=welcome_text(u), reply_markup=main_kb(u))
        except Exception as e: logging.warning("welcome photo: %s", e)
    await bot.send_message(chat_id, welcome_text(u), reply_markup=main_kb(u))

# ---- Kirish darvozalari: telefon raqami → foydalanish shartlariga rozilik → ilova ----
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
TERMS_TXT = (BASE / "web" / "oferta.txt").read_text(encoding="utf-8").replace("@huquqsavoljavob_bot", "@" + BOT_USERNAME)
TERMS_SHORT = ("📄 <b>Foydalanish shartlari va ommaviy oferta</b>\n\n"
               "Asosiy qoidalar:\n"
               "• savol-javoblar sun’iy intellekt yordamida darsliklar va qonunchilik hujjatlari asosida tuzilgan; sun’iy intellekt xato qilishi mumkin — muhim ma’lumotni rasmiy manbalardan (lex.uz) tekshiring;\n"
               "• xizmat o‘quv-yordamchi vosita, rasmiy huquqiy maslahat emas; tuzuvchi savol-javoblar mazmuni bo‘yicha hech qanday pulli majburiyat olmaydi;\n"
               "• materiallarni nusxalash, skrinshot olish va tarqatish taqiqlanadi;\n"
               "• to‘lov faqat bot va mini ilova ishlamagan holatda, to‘lovdan keyin 1 sutka ichidagi talabingizga asosan qaytarilishi mumkin; ortiqcha va shubhali to‘lovlar qonunchilikda belgilangan tartibda qaytariladi;\n"
               "• nizolar avvalo sudgacha hal etish tartibida hal etiladi.\n\n"
               "To‘liq matn — “📄 To‘liq matn” tugmasida. Davom etish uchun shartlarga rozilik bering.")
def terms_kb(u):
    rows = [[B(text=T(u, "✅ Shartlarga roziman"), callback_data="agree")]]
    rows.append([B(text=T(u, "📄 To‘liq matn"), url=WEBAPP_URL + "/oferta" + ("?s=cyr" if u["cyr"] else ""))] if WEBAPP_URL else [B(text=T(u, "📄 To‘liq matn"), callback_data="terms_full")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
def agreed(u): return (u.get("agreed") or 0) > 0
async def ask_phone(bot, chat_id, u):
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=T(u, "📱 Telefon raqamni yuborish"), request_contact=True)]], resize_keyboard=True, one_time_keyboard=True)
    await bot.send_message(chat_id, T(u, "Assalomu alaykum! § ilovasidan foydalanish uchun telefon raqamingizni yuboring — pastdagi “📱 Telefon raqamni yuborish” tugmasini bosing.").replace("§", APP_NAME), reply_markup=kb)
async def proceed(bot, chat_id, u, args=""):
    """Navbatdagi qadam: telefon → shartlar → bosh menyu"""
    if not u.get("phone") and u["id"] not in ADMINS: return await ask_phone(bot, chat_id, u)
    if not agreed(u) and u["id"] not in ADMINS: return await bot.send_message(chat_id, T(u, TERMS_SHORT), reply_markup=terms_kb(u))
    if args == "buy" and not has_full(u):
        return await offer_buy_chat(bot, chat_id, u)
    await send_welcome(bot, chat_id, u)

@r.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, bot: Bot):
    u = user(m.from_user.id, m.from_user.full_name)
    await proceed(bot, m.chat.id, u, command.args or "")
@r.callback_query(F.data == "agree")
async def cb_agree(c: CallbackQuery, bot: Bot):
    db.execute("update users set agreed=? where id=?", (int(time.time()), c.from_user.id)); db.commit()
    u = user(c.from_user.id); await c.answer("✓")
    try: await c.message.edit_reply_markup(reply_markup=None)
    except Exception: pass
    await proceed(bot, c.message.chat.id, u)
@r.callback_query(F.data == "terms")
async def cb_terms(c: CallbackQuery, bot: Bot):
    u = user(c.from_user.id); await c.answer()
    await bot.send_message(c.message.chat.id, T(u, TERMS_SHORT.replace("Davom etish uchun shartlarga rozilik bering.", "")), reply_markup=terms_kb(u) if not agreed(u) else
                           (InlineKeyboardMarkup(inline_keyboard=[[B(text=T(u, "📄 To‘liq matn"), url=WEBAPP_URL + "/oferta" + ("?s=cyr" if u["cyr"] else ""))]]) if WEBAPP_URL else None))

# ---- Fikr va takliflar ----
FB_WAIT = set()
@r.callback_query(F.data == "fb")
async def cb_fb(c: CallbackQuery):
    u = user(c.from_user.id); FB_WAIT.add(u["id"]); await c.answer()
    await c.message.answer(T(u, "💬 Fikr, taklif yoki savoldagi xato haqida yozing — keyingi xabaringiz adminga yuboriladi.\nBekor qilish: /bekor"))
@r.message(Command("fikr"))
async def cmd_fb(m: Message):
    u = user(m.from_user.id); FB_WAIT.add(u["id"])
    await m.answer(T(u, "💬 Fikr, taklif yoki savoldagi xato haqida yozing — keyingi xabaringiz adminga yuboriladi.\nBekor qilish: /bekor"))
@r.message(Command("bekor"))
async def cmd_cancel(m: Message): FB_WAIT.discard(m.from_user.id); await m.answer("✓")
async def save_feedback(bot, u, text, src):
    text = (text or "").strip()[:3000]
    if not text: return False
    cur = db.execute("insert into feedback(uid,text,t,src) values(?,?,?,?)", (u["id"], text, int(time.time()), src)); db.commit()
    await notify_admins(bot, f"💬 <b>Fikr #{cur.lastrowid}</b> ({'ilova' if src == 'app' else 'bot'}): {html.escape(u.get('name') or '—')} (<code>{u['id']}</code>)\n\n{html.escape(text)}")
    return True

@r.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery, bot: Bot):
    u = user(c.from_user.id); await c.answer(); await send_welcome(bot, c.message.chat.id, u)

@r.callback_query(F.data == "script")
async def cb_script(c: CallbackQuery, bot: Bot):
    u = user(c.from_user.id); db.execute("update users set cyr=? where id=?", (0 if u["cyr"] else 1, u["id"])); db.commit()
    await cb_home(c, bot)

@r.callback_query(F.data == "books")
async def cb_books(c: CallbackQuery):
    u = user(c.from_user.id)
    kb = [[B(text=T(u, f"{s['grade']} · {s['title']} ({len(Q_BY_SRC.get(s['id'], []))})"), callback_data=f"src:{s['id']}")] for s in BOOKS]
    kb.append([B(text=T(u, "⬅ Bosh menyu"), callback_data="home")])
    await c.message.edit_text(T(u, "📘 <b>Darsliklar</b>\nDarslikni tanlang:"), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)); await c.answer()

@r.callback_query(F.data.startswith("laws:"))
async def cb_laws(c: CallbackQuery):
    u = user(c.from_user.id); pg = int(c.data.split(":")[1]); per = 9
    part = LAWS[pg * per:(pg + 1) * per]
    kb = [[B(text=T(u, f"{LAWS.index(s)+1}. {s['title'][:52]}"), callback_data=f"src:{s['id']}")] for s in part]
    nav = []
    if pg: nav.append(B(text="◀", callback_data=f"laws:{pg-1}"))
    nav.append(B(text=f"{pg+1}/{(len(LAWS)+per-1)//per}", callback_data="noop"))
    if (pg + 1) * per < len(LAWS): nav.append(B(text="▶", callback_data=f"laws:{pg+1}"))
    kb += [nav, [B(text=T(u, "⬅ Bosh menyu"), callback_data="home")]]
    await c.message.edit_text(T(u, "⚖️ <b>Qonunchilik hujjatlari (35 ta)</b>\nHujjatni tanlang:"), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)); await c.answer()

@r.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery): await c.answer()

@r.callback_query(F.data.startswith("src:"))
async def cb_src(c: CallbackQuery, bot: Bot):
    u = user(c.from_user.id); s = SRC[c.data[4:]]; lv = await level_of(bot, u)
    if s["type"] == "darslik":
        kb = []
        for c0 in s["chapters"]:
            n = len(Q_BY_CH.get(c0["id"], [])); need = chapter_level(c0["id"])
            kb.append([B(text=T(u, f"{LOCK[need] if need > lv else ''} {c0['title'][:50]} ({n})").strip(), callback_data=f"ch:{c0['id']}")])
        kb.append([B(text=T(u, "⬅ Darsliklar"), callback_data="books")])
        head = f"📘 <b>{html.escape(src_name(s))}</b>\n" + (f"Mualliflar: {html.escape(s['authors'])}\n" if s.get("authors") else "") + "\nBobni tanlang."
        await c.message.edit_text(T(u, head), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    else:
        qs = Q_BY_SRC.get(s["id"], []); n = len(qs)
        avail = n if lv == 2 else len([q for q in qs if q in DEMO])
        kb = [[B(text=T(u, f"▶ Boshlash ({avail} ta savol)"), callback_data=f"go:{s['id']}")]]
        if lv == 2:
            kb += [[B(text=T(u, f"{c0['title'][:50]} ({len(Q_BY_CH.get(c0['id'], []))})"), callback_data=f"ch:{c0['id']}")] for c0 in s["chapters"] if Q_BY_CH.get(c0["id"])][:40]
        if lv < 2: kb.append([B(text=T(u, f"🔒 Barcha {n} ta savol — to‘liq kirish"), callback_data="buy")])
        kb.append([B(text=T(u, "⬅ Qonunchilik hujjatlari"), callback_data=f"laws:{LAWS.index(s)//9}")])
        head = f"⚖️ <b>{html.escape(s['title'])}</b>\n{html.escape(s.get('kind',''))} · {html.escape(s.get('pub',''))}\n\nJami {n} ta savol. Sizga ochiq: {avail} ta."
        await c.message.edit_text(T(u, head), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), disable_web_page_preview=True)
    await c.answer()

async def gate(c: CallbackQuery, bot: Bot, u, need):
    lv = await level_of(bot, u)
    if lv >= need: return True
    if need == 1: await ask_join(c.message, u)
    else: await offer_buy(c.message, u, bot)
    await c.answer(); return False

@r.callback_query(F.data.startswith("ch:"))
async def cb_ch(c: CallbackQuery, bot: Bot):
    u = user(c.from_user.id); cid = c.data[3:]
    if not await gate(c, bot, u, chapter_level(cid)): return
    s = SRC[CH_SRC[cid]]; ch = s["chapters"][CH_IDX[cid]]
    kb = [[B(text=T(u, f"▶ Butun bobdan ({len(Q_BY_CH.get(cid, []))})"), callback_data=f"gc:{cid}")]]
    kb += [[B(text=T(u, f"{l['title'][:55]} ({len(Q_BY_L.get(l['id'], []))})"), callback_data=f"gl:{l['id']}")] for l in ch["lessons"] if Q_BY_L.get(l["id"])][:45]
    kb.append([B(text=T(u, "⬅ Orqaga"), callback_data=f"src:{s['id']}")])
    await c.message.edit_text(T(u, f"<b>{html.escape(ch['title'])}</b>\n{html.escape(src_name(s))}\n\nMavzuni tanlang yoki butun bobdan boshlang:"), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)); await c.answer()

async def begin(c: CallbackQuery, bot: Bot, u, qids, title):
    lv = await level_of(bot, u)
    qids = [q for q in qids if need_level(q) <= lv]
    if not qids: return await gate(c, bot, u, 1 if lv == 0 else 2)
    start_session(u["id"], qids, title); await c.answer()
    await show_card(bot, c.message.chat.id, u)

@r.callback_query(F.data.startswith("go:"))
async def cb_go(c: CallbackQuery, bot: Bot):
    u = user(c.from_user.id); s = SRC[c.data[3:]]; await begin(c, bot, u, Q_BY_SRC.get(s["id"], []), s["title"])
@r.callback_query(F.data.startswith("gc:"))
async def cb_gc(c: CallbackQuery, bot: Bot):
    u = user(c.from_user.id); cid = c.data[3:]
    if not await gate(c, bot, u, chapter_level(cid)): return
    await begin(c, bot, u, Q_BY_CH.get(cid, []), CH_TITLE[cid])
@r.callback_query(F.data.startswith("gl:"))
async def cb_gl(c: CallbackQuery, bot: Bot):
    u = user(c.from_user.id); lid = c.data[3:]; qs = Q_BY_L.get(lid, [])
    if qs and not await gate(c, bot, u, need_level(qs[0])): return
    await begin(c, bot, u, qs, L_TITLE.get(lid, ""))

@r.callback_query(F.data == "rev")
async def cb_rev(c: CallbackQuery, bot: Bot):
    u = user(c.from_user.id)
    if c.from_user.id not in SES: return await c.answer(T(u, "Sessiya tugagan. /start"), show_alert=True)
    SES[u["id"]]["rev"] = True; await show_card(bot, c.message.chat.id, u, c.message); await c.answer()

@r.callback_query(F.data.startswith("g:"))
async def cb_grade(c: CallbackQuery, bot: Bot):
    u = user(c.from_user.id); ss = SES.get(u["id"])
    if not ss: return await c.answer("/start", show_alert=True)
    g = c.data[2:]; qid = ss["list"][ss["i"]]
    db.execute("insert into prog(uid,qid,g,n,t) values(?,?,?,1,?) on conflict(uid,qid) do update set g=excluded.g, n=n+1, t=excluded.t", (u["id"], qid, g, int(time.time()))); db.commit()
    ss["res"][g] += 1
    if ss["i"] + 1 >= len(ss["list"]): return await finish(c, bot, u)
    ss["i"] += 1; ss["rev"] = False
    await show_card(bot, c.message.chat.id, u, c.message); await c.answer()

@r.callback_query(F.data.startswith("nav:"))
async def cb_nav(c: CallbackQuery, bot: Bot):
    u = user(c.from_user.id); ss = SES.get(u["id"])
    if not ss: return await c.answer("/start", show_alert=True)
    j = ss["i"] + int(c.data[4:])
    if not 0 <= j < len(ss["list"]): return await c.answer(T(u, "Boshqa savol yo‘q"))
    ss["i"] = j; ss["rev"] = False; await show_card(bot, c.message.chat.id, u, c.message); await c.answer()

@r.callback_query(F.data == "fin")
async def cb_fin(c: CallbackQuery, bot: Bot):
    u = user(c.from_user.id)
    if u["id"] not in SES: return await c.answer()
    await finish(c, bot, u)
async def finish(c, bot, u):
    ss = SES.pop(u["id"]); r_ = ss["res"]; n = sum(r_.values()); pct = round(r_["k"] / n * 100) if n else 0
    t = f"🏁 <b>Natija: {pct}%</b>\nJavob berildi: {n}\n✅ Bildim: {r_['k']}\n≈ Qisman: {r_['p']}\n❌ Bilmadim: {r_['u']}\n\nBilmagan savollaringizni “🔁 Bilmaganlarim” orqali qayta ishlang."
    await bot.send_message(c.message.chat.id, T(u, t), reply_markup=main_kb(u)); await c.answer()

@r.callback_query(F.data == "redo")
async def cb_redo(c: CallbackQuery, bot: Bot):
    u = user(c.from_user.id)
    rows = db.execute("select qid from prog where uid=? and g in ('u','p') order by t", (u["id"],)).fetchall()
    qids = [x[0] for x in rows if x[0] in Q]
    if not qids: return await c.answer(T(u, "Hozircha “bilmadim” deb belgilangan savol yo‘q."), show_alert=True)
    await begin(c, bot, u, qids, "Bilmagan savollarim")

@r.callback_query(F.data == "stat")
async def cb_stat(c: CallbackQuery, bot: Bot):
    u = user(c.from_user.id); lv = await level_of(bot, u)
    g = dict(db.execute("select g,count(*) from prog where uid=? group by g", (u["id"],)).fetchall())
    seen = sum(g.values()); lvl = ["🆓 Demo", "📢 Kanal a’zosi", "🔓 To‘liq kirish"][lv]
    t = f"📊 <b>O‘zlashtirish statistikasi</b>\nKirish darajasi: {lvl}\nKo‘rilgan savollar: {seen} / {len(Q)}\n✅ Bildim: {g.get('k',0)}\n≈ Qisman: {g.get('p',0)}\n❌ Bilmadim: {g.get('u',0)}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[B(text=T(u, "🗑 Statistikani tozalash"), callback_data="clr")], [B(text=T(u, "⬅ Bosh menyu"), callback_data="home")]])
    await c.message.edit_text(T(u, t), reply_markup=kb); await c.answer()
@r.callback_query(F.data == "clr")
async def cb_clr(c: CallbackQuery, bot: Bot):
    db.execute("delete from prog where uid=?", (c.from_user.id,)); db.commit(); await c.answer("✓"); await cb_stat(c, bot)

# ---------------- Kanal ----------------
async def ask_join(msg: Message, u):
    kb = InlineKeyboardMarkup(inline_keyboard=[[B(text=T(u, "📢 Kanalga o‘tish"), url=CHANNEL_URL)], [B(text=T(u, "✅ A’zo bo‘ldim — tekshirish"), callback_data="chk")]])
    await msg.answer(T(u, "To‘liq kirish uchun avval kanalimizga obuna bo‘ling va “A’zo bo‘ldim” tugmasini bosing."), reply_markup=kb)
@r.callback_query(F.data == "join")
async def cb_join(c: CallbackQuery): await ask_join(c.message, user(c.from_user.id)); await c.answer()
@r.callback_query(F.data == "chk")
async def cb_chk(c: CallbackQuery, bot: Bot):
    u = user(c.from_user.id)
    if await is_member(bot, u["id"]) is not False: await c.answer(); await offer_buy(c.message, u, bot)
    else: await c.answer(T(u, "Hali kanalga a’zo bo‘lmagansiz."), show_alert=True)

# ---------------- To'liq kirish so'rovi ----------------
def tariffs_text():
    return "\n".join(f"• {t_['name']} — " + (f"{fmt_sum(t_['price'])} so‘m" if t_["price"] > 0 else "bepul") for t_ in tariffs())
BUY_T = {}   # uid -> chat orqali tanlangan tarif
def pay_text():
    p = pay_info(); out = []
    if p["card"]: out.append(f"💳 Karta: <code>{html.escape(p['card'])}</code>")
    if p["owner"]: out.append(f"👤 Karta egasi: {html.escape(p['owner'])}")
    if p["phone"]: out.append(f"📞 Admin: {html.escape(p['phone'])}")
    return "\n".join(out)
async def offer_buy(msg: Message, u, bot: Bot): await offer_buy_chat(bot, msg.chat.id, u)
async def offer_buy_chat(bot, chat_id, u):
    if has_full(u): return await bot.send_message(chat_id, T(u, "Sizda " + until_text(u) + " ✅"))
    if await is_member(bot, u["id"]) is False:
        kb = InlineKeyboardMarkup(inline_keyboard=[[B(text=T(u, "📢 Kanalga o‘tish"), url=CHANNEL_URL)], [B(text=T(u, "✅ A’zo bo‘ldim — tekshirish"), callback_data="chk")]])
        return await bot.send_message(chat_id, T(u, "🔓 To‘liq kirish uchun avval kanalimizga obuna bo‘ling, so‘ng “A’zo bo‘ldim” tugmasini bosing."), reply_markup=kb)
    kb = [[B(text=T(u, f"{x['name']} — " + (fmt_sum(x['price']) + " so‘m" if x["price"] > 0 else "bepul")), callback_data=f"bt:{x['id']}")] for x in tariffs()]
    if WEBAPP_URL: kb.append([B(text=T(u, "📱 Ilovada so‘rov yuborish"), web_app=WebAppInfo(url=WEBAPP_URL + "/?buy=1"))])
    await bot.send_message(chat_id, T(u, "🔓 <b>To‘liq kirish — barcha savol-javoblar</b>\n\nObuna muddatini tanlang:"), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
@r.callback_query(F.data.startswith("bt:"))
async def cb_bt(c: CallbackQuery, bot: Bot):
    u = user(c.from_user.id); t_ = tariff(c.data[3:]); await c.answer()
    if not t_: return
    if has_full(u): return await c.message.answer(T(u, "Sizda " + until_text(u) + " ✅"))
    if t_["price"] <= 0:
        cur = db.execute("insert into receipts(uid,file_id,ftype,kind,amount,status,t,tariff,months,days,phone) values(?,?,?,?,?,?,?,?,?,?,?)",
                         (u["id"], "", "", "bot", 0, "pending", int(time.time()), t_["id"], t_["months"], t_["days"], u.get("phone"))); db.commit()
        # 0 so'mlik tarif — admin tasdig'isiz darhol ochiladi
        await receipt_act(bot, cur.lastrowid, "ok", 0)
        await notify_admins(bot, f"🆓 Bepul tarif avtomatik ochildi: {html.escape(u.get('name') or '—')} (<code>{u['id']}</code>) — {html.escape(t_['name'])}")
        return
    BUY_T[u["id"]] = t_["id"]
    txt = f"🗓 <b>{html.escape(t_['name'])}</b> — {fmt_sum(t_['price'])} so‘m\n\n" + (pay_text() + "\n\n" if pay_text() else "") + (html.escape(kv("paytext") or "") + "\n\n") + \
          "📎 To‘lov chekini shu yerga yuboring: rasm, skrinshot, PDF yoki istalgan fayl ko‘rinishida."
    qr = kv("card_qr")
    if qr and Path(qr).exists():
        try: return await bot.send_photo(c.message.chat.id, FSInputFile(qr), caption=T(u, txt)[:1024])
        except Exception: pass
    await c.message.answer(T(u, txt))
@r.callback_query(F.data == "buy")
async def cb_buy(c: CallbackQuery, bot: Bot):
    u = user(c.from_user.id); await c.answer(); await offer_buy_chat(bot, c.message.chat.id, u)
@r.pre_checkout_query()
async def pre_checkout(p: PreCheckoutQuery): await p.answer(ok=True)
@r.message(F.successful_payment)
async def paid(m: Message, bot: Bot):
    sp = m.successful_payment; amt = sp.total_amount // 100; now = int(time.time()); t0 = (tariffs() or [{"id": "", "months": 1}])[0]
    db.execute("insert into receipts(uid,file_id,ftype,kind,amount,status,t,note,tariff,months) values(?,?,?,?,?,?,?,?,?,?)",
               (m.from_user.id, "", "", "invoice", amt, "pending", now, sp.provider_payment_charge_id, t0["id"], t0["months"])); db.commit()
    await m.answer("✅ To‘lov qabul qilindi. Admin tasdiqlagach to‘liq kirish ochiladi va sizga xabar keladi.")
    await notify_admins(bot, f"💰 To‘lov: {html.escape(m.from_user.full_name)} (<code>{m.from_user.id}</code>) — {amt} so‘m\nTasdiqlash: admin paneldagi “Cheklar” bo‘limi.")
@r.message(F.contact)
async def got_contact(m: Message, bot: Bot):
    if m.contact and m.contact.user_id == m.from_user.id:
        u = user(m.from_user.id, m.from_user.full_name); ph = m.contact.phone_number
        db.execute("update users set phone=? where id=?", (ph if ph.startswith("+") else "+" + ph, m.from_user.id)); db.commit()
        await m.answer(T(u, "✓ Telefon raqamingiz saqlandi."), reply_markup=ReplyKeyboardRemove())
        await proceed(bot, m.chat.id, user(m.from_user.id))
    else: await m.answer("Iltimos, o‘zingizning raqamingizni “📱 Telefon raqamni yuborish” tugmasi orqali yuboring.")
# Chat orqali chek (rasm/fayl) → so'rov
@r.message(F.photo | F.document | F.video | F.animation)
async def receipt(m: Message, bot: Bot):
    if m.from_user.id in ADMINS: return
    u = user(m.from_user.id, m.from_user.full_name)
    if has_full(u) and not free_active(): return await m.answer(T(u, "Sizda " + until_text(u) + " ✅"))
    if m.photo: fid, ft = m.photo[-1].file_id, "photo"
    elif m.document: fid, ft = m.document.file_id, m.document.mime_type or "doc"
    elif m.video: fid, ft = m.video.file_id, "video/mp4"
    else: fid, ft = m.animation.file_id, "video/mp4"
    t0 = tariff(BUY_T.pop(u["id"], "")) or next((x for x in tariffs() if x["price"] > 0), None) or (tariffs() or [{"id": "", "months": 1, "days": 0, "price": 0}])[0]
    cur = db.execute("insert into receipts(uid,file_id,ftype,kind,amount,status,t,tariff,months,days,phone) values(?,?,?,?,?,?,?,?,?,?,?)",
                     (u["id"], fid, ft, "chek", t0["price"], "pending", int(time.time()), t0["id"], t0["months"], t0.get("days", 0), u.get("phone")))
    db.commit(); rid = cur.lastrowid
    await send_request_to_admins(bot, rid, m=m)
    await m.answer(T(u, f"✅ Chek adminga yuborildi ({t0.get('name', '')}). Admin tasdiqlagach to‘liq kirish ochiladi va sizga xabar keladi."))

# ---------------- Admin ----------------
def is_admin(uid): return uid in ADMINS
def dstr(ts):
    import datetime as _d
    return _d.datetime.fromtimestamp(ts, _d.timezone(_d.timedelta(hours=5))).strftime("%d.%m.%Y")
@r.message(Command("admin"))
async def adm(m: Message):
    if not is_admin(m.from_user.id): return
    nu = db.execute("select count(*) from users").fetchone()[0]
    act = db.execute("select count(*) from users where paid=1 or until>?", (int(time.time()),)).fetchone()[0]
    pend = db.execute("select count(*) from receipts where status='pending'").fetchone()[0]
    kb = InlineKeyboardMarkup(inline_keyboard=[[B(text="🛠 Admin panelni ochish", web_app=WebAppInfo(url=WEBAPP_URL + "/admin"))]]) if WEBAPP_URL else None
    await m.answer(reply_markup=kb, text=f"<b>Admin</b>\nFoydalanuvchilar: {nu}, to‘liq kirishli: {act}, tasdiq kutayotgan: {pend}\n\nTariflar:\n{tariffs_text()}\n\n"
                   "Tariflar, cheklar, foydalanuvchilar — admin panelda.\n/ochish ID [oy] — to‘liq kirishni ochish (oy ko‘rsatilmasa — muddatsiz)\n/yopish ID — yopish\n/xabar matn — barcha foydalanuvchilarga xabar")
async def notify_admins(bot, text):
    kb = InlineKeyboardMarkup(inline_keyboard=[[B(text="🛠 Admin panel", web_app=WebAppInfo(url=WEBAPP_URL + "/admin"))]]) if WEBAPP_URL else None
    for a in ADMINS:
        try: await bot.send_message(a, text, reply_markup=kb)
        except Exception: pass
def rcpt_caption(rid):
    r_ = db.execute("select r.uid,u.name,r.amount,r.tariff,r.months,coalesce(r.phone,u.phone),r.kind,r.days from receipts r left join users u on u.id=r.uid where r.id=?", (rid,)).fetchone()
    t_ = tariff(r_[3]) or {"name": period_text(r_[4], r_[7])}
    return (f"🧾 <b>So‘rov #{rid}</b>: {html.escape(r_[1] or '—')} (<code>{r_[0]}</code>)\n📞 {html.escape(r_[5] or '—')}\n"
            f"🗓 {html.escape(t_.get('name') or '')} ({period_text(r_[4], r_[7])}) — {fmt_sum(r_[2])} so‘m" + ("" if r_[2] else " (bepul, chek talab qilinmaydi)"))
async def send_request_to_admins(bot, rid, m: Message | None = None, data: bytes | None = None, fname="chek", ctype=""):
    kb = InlineKeyboardMarkup(inline_keyboard=[[B(text="✅ Tasdiqlash", callback_data=f"adm_ok:{rid}"), B(text="❌ Rad etish", callback_data=f"adm_no:{rid}")]] +
                              ([[B(text="🛠 Admin panelda ko‘rish (muddat/summani tanlash)", web_app=WebAppInfo(url=WEBAPP_URL + "/admin"))]] if WEBAPP_URL else []))
    cap = rcpt_caption(rid); fid = None
    for a in ADMINS:
        try:
            if m is not None:
                await m.forward(a); await bot.send_message(a, cap, reply_markup=kb)
            elif data:
                from aiogram.types import BufferedInputFile
                f = BufferedInputFile(data, filename=fname)
                msg = await (bot.send_photo(a, f, caption=cap, reply_markup=kb) if ctype.startswith("image/") else bot.send_document(a, f, caption=cap, reply_markup=kb))
                if not fid: fid = msg.photo[-1].file_id if msg.photo else (msg.document.file_id if msg.document else None)
            else: await bot.send_message(a, cap, reply_markup=kb)
        except Exception as e: logging.warning("adminga yuborib bo'lmadi: %s", e)
    if fid: db.execute("update receipts set file_id=? where id=? and (file_id is null or file_id='')", (fid, rid)); db.commit()
async def grant(bot, uid, months, admin, amount=None, kind="manual", days=0):
    """months/days > 0 — shuncha muddatga (mavjud muddat ustiga), ikkalasi 0 — muddatsiz"""
    u = user(uid); now = int(time.time()); months = int(months or 0); days = int(days or 0)
    if months > 0 or days > 0:
        base = max(now, u.get("until") or 0); until = add_months(base, months) + days * 86400
        db.execute("update users set until=?, warned=null where id=?", (until, uid)); msg = f"✅ To‘liq kirish ochildi: {dstr(until)} gacha.\nIlovani qayta oching — barcha savol-javoblar ochiq."
    else:
        db.execute("update users set paid=1 where id=?", (uid,)); msg = "✅ To‘liq kirish ochildi (muddatsiz).\nIlovani qayta oching — barcha savol-javoblar ochiq."
    if amount: db.execute("insert into pays(uid,amount,kind,t,info) values(?,?,?,?,?)", (uid, int(amount), kind, now, f"admin {admin}; {period_text(months, days)}"))
    db.commit(); u = user(uid)
    try: await bot.send_message(uid, T(u, msg), reply_markup=main_kb(u))
    except Exception: pass
async def revoke(bot, uid):
    user(uid); db.execute("update users set paid=0, until=null where id=?", (uid,)); db.commit()
    try: await bot.send_message(uid, "To‘liq kirish yopildi.")
    except Exception: pass
@r.message(Command("ochish"))
async def adm_open(m: Message, command: CommandObject, bot: Bot):
    if not is_admin(m.from_user.id): return
    a = (command.args or "").split()
    if not a or not a[0].isdigit(): return await m.answer("Masalan: /ochish 123456789 12  (12 oyga), /ochish 123456789 0 10  (10 kunga) yoki /ochish 123456789 (muddatsiz)")
    mo = int(a[1]) if len(a) > 1 and a[1].isdigit() else 0; dy = int(a[2]) if len(a) > 2 and a[2].isdigit() else 0
    await grant(bot, int(a[0]), mo, m.from_user.id, days=dy); await m.answer("✓ Ochildi")
@r.message(Command("yopish"))
async def adm_close(m: Message, command: CommandObject, bot: Bot):
    if not is_admin(m.from_user.id): return
    if not (command.args or "").strip().isdigit(): return await m.answer("Masalan: /yopish 123456789")
    await revoke(bot, int(command.args)); await m.answer("✓ Yopildi")
@r.callback_query(F.data.startswith("adm_"))
async def adm_cb(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id): return await c.answer()
    act, rid = c.data[4:].split(":"); res = await receipt_act(bot, int(rid), act, c.from_user.id)
    tail = ("\n✅ Tasdiqlandi" if act == "ok" else "\n❌ Rad etildi") + (f" ({res})" if res else "")
    try:
        if c.message.caption is not None: await c.message.edit_caption(caption=c.message.html_text + tail)
        else: await c.message.edit_text(c.message.html_text + tail)
    except Exception: pass
    await c.answer(res or "✓")
async def receipt_act(bot, rid, act, admin, amount=None, note="", months=None, days=None):
    r_ = db.execute("select uid,amount,status,kind,months,days from receipts where id=?", (rid,)).fetchone()
    if not r_: return "chek topilmadi"
    if r_[2] != "pending": return "allaqachon ko‘rib chiqilgan"
    uid = r_[0]; amt = int(r_[1] or 0) if amount in (None, "") else int(amount)
    if months in (None, "") and days in (None, ""): mon, dy = int(r_[4] or 0), int(r_[5] or 0)
    else: mon, dy = int(months or 0), int(days or 0)
    if not mon and not dy: mon = 1
    db.execute("update receipts set status=?, dt=?, admin=?, amount=?, months=?, days=?, note=? where id=?", ("ok" if act == "ok" else "no", int(time.time()), admin, amt, mon, dy, note, rid)); db.commit()
    if act == "ok": await grant(bot, uid, mon, admin, amt, r_[3], days=dy)
    else:
        try: await bot.send_message(uid, "So‘rovingiz tasdiqlanmadi." + (f" Sabab: {html.escape(note)}" if note else "") + " Savollar bo‘lsa admin bilan bog‘laning.")
        except Exception: pass
    return ""
@r.message(Command("xabar"))
async def adm_bc(m: Message, command: CommandObject, bot: Bot):
    if not is_admin(m.from_user.id) or not command.args: return
    ok = 0
    for (uid,) in db.execute("select id from users").fetchall():
        try: await bot.send_message(uid, command.args); ok += 1; await asyncio.sleep(0.05)
        except Exception: pass
    await m.answer(f"✓ Yuborildi: {ok}")

@r.message(F.text)
async def any_text(m: Message, bot: Bot):
    u = user(m.from_user.id, m.from_user.full_name)
    if u["id"] in FB_WAIT:
        FB_WAIT.discard(u["id"])
        if await save_feedback(bot, u, m.text, "bot"): return await m.answer(T(u, "✅ Rahmat! Fikringiz adminga yuborildi."))
    if not u.get("phone") or not agreed(u): return await proceed(bot, m.chat.id, u)
    await m.answer(T(u, "Menyu: /start · Fikr bildirish: /fikr"))

# Texnik to'xtatish: admin bo'lmagan foydalanuvchilar uchun bot javob bermaydi
async def maint_mw(handler, event, data):
    fu = getattr(event, "from_user", None)
    if maint_active() and fu and fu.id not in ADMINS:
        try:
            if isinstance(event, CallbackQuery): await event.answer(maint_message()[:190], show_alert=True)
            else: await event.answer(maint_message())
        except Exception: pass
        return
    return await handler(event, data)
r.message.outer_middleware(maint_mw); r.callback_query.outer_middleware(maint_mw)

# ---------------- Mini App (Telegram ichidagi platforma) ----------------
PB = json.loads((BASE / "data" / "platform_bank.json").read_text(encoding="utf-8"))
APP_TPL = (BASE / "web" / "app.html").read_text(encoding="utf-8")
LOADER = (BASE / "web" / "loader.html").read_text(encoding="utf-8").replace("@huquqsavoljavob_bot", "@" + BOT_USERNAME)
ADMIN_HTML = (BASE / "web" / "admin.html").read_text(encoding="utf-8").replace("@huquqsavoljavob_bot", "@" + BOT_USERNAME)
# Demo: darsliklarning har bir mavzusidan (dars) va har bir qonunchilik hujjatidan DEMO_N tadan savol (matn tartibida)
def bank_meta(pb):
    sidx = {s_["id"]: i for i, s_ in enumerate(pb["sentences"])}
    sl = {s_["id"]: s_["l"] for s_ in pb["sentences"]}; ssec = {s_["id"]: s_.get("sec") for s_ in pb["sentences"]}
    chpb = {c_["id"]: c_ for c_ in pb["chapters"]}
    l2src = {l_["id"]: s_ for s_ in pb["sources"] for cid in s_["chapters"] if cid in chpb for l_ in chpb[cid]["lessons"]}
    grp = {}
    for q_ in sorted(pb["questions"], key=lambda q_: (sidx.get(q_["src"][0], 10 ** 9), q_["id"])):
        l_ = sl.get(q_["src"][0]); s_ = l2src.get(l_)
        grp.setdefault(l_ if (s_ and s_["type"] == "darslik") else (s_["id"] if s_ else l_), []).append(q_["id"])
    demo = {qid for g in grp.values() for qid in g[:DEMO_N]}
    fullc = {}   # bo'lim kaliti (dars:bo'lim) → [oddiy, murakkab] — demo'da haqiqiy sonlarni ko'rsatish uchun
    for q_ in pb["questions"]:
        sid = q_["src"][0]; k_ = f"{sl.get(sid)}:{ssec.get(sid)}"
        fullc.setdefault(k_, [0, 0])[0 if q_.get("lvl") == "o" else 1] += 1
    return dict(pb=pb, demo=demo, full=fullc, sl=sl)
BANKS = {"uz": bank_meta(PB)}
DEMO.update(BANKS["uz"]["demo"])
_RU = BASE / "data" / "platform_bank_ru.json"     # rus tilidagi darsliklar (asl rus nashrlari asosida)
if _RU.exists(): BANKS["ru"] = bank_meta(json.loads(_RU.read_text(encoding="utf-8")))
def lang_of(u): return u.get("lang") if u.get("lang") in BANKS else "uz"
_APP_CACHE = {}
def app_html(level, lang="uz"):
    key = (level, lang)
    if key not in _APP_CACHE:
        M = BANKS[lang]; pb = M["pb"]; full = level >= 2
        qs = pb["questions"] if full else [q for q in pb["questions"] if q["id"] in M["demo"]]
        used_l = {M["sl"].get(q["src"][0]) for q in qs}
        sents = pb["sentences"] if full else [s_ for s_ in pb["sentences"] if s_["l"] in used_l]
        b = dict(pb, questions=qs, sentences=sents)
        if not full: b["full"] = M["full"]
        js = json.dumps(b, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        _APP_CACHE[key] = (APP_TPL.replace("__BANK__", js), len(pb["questions"]) - len(qs))
    return _APP_CACHE[key]
def check_init_data(init_data: str):
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True)); h = data.pop("hash", "")
        dcs = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest(), h): return None
        if time.time() - int(data.get("auth_date", "0")) > 86400 * 2: return None
        return json.loads(data.get("user", "{}"))
    except Exception: return None
def tg_user(init):
    tu = check_init_data(init or "")
    if not tu or "id" not in tu: return None
    return user(int(tu["id"]), (tu.get("first_name", "") + " " + tu.get("last_name", "")).strip())
def pub_tariffs(): return [{"id": t_["id"], "name": t_["name"], "months": t_["months"], "days": t_["days"], "price": t_["price"]} for t_ in tariffs()]
def terms_html(cy=False):
    out = []
    for ln in TERMS_TXT.splitlines():
        ln = ln.strip()
        if not ln: continue
        s = html.escape(cyr(ln) if cy else ln)
        if ln.startswith("## "): out.append(f"<h2>{s[3:]}</h2>")
        elif ln.startswith("# "): out.append(f"<h1>{s[2:]}</h1>")
        else: out.append(f"<p>{s}</p>")
    return "\n".join(out)
GATE_CSS = ("*{box-sizing:border-box}body{margin:0;background:#e7f2fc;color:#13243a;font:14px/1.5 system-ui,-apple-system,'Segoe UI',sans-serif;padding:16px;"
            "-webkit-user-select:none;user-select:none}.box{max-width:640px;margin:0 auto;background:#fff;border:1px solid #c9dbec;border-radius:16px;padding:18px}"
            "h1{font-size:18px;margin:0 0 6px}h2{font-size:15px;margin:16px 0 4px}p{margin:4px 0}.btn{display:block;width:100%;border:0;border-radius:12px;padding:13px;"
            "font:600 15px inherit;background:#0d5ea8;color:#fff;margin-top:12px;cursor:pointer}.btn:disabled{opacity:.5}.mut{color:#5b6b7c;font-size:13px}"
            ".terms{max-height:58vh;overflow:auto;border:1px solid #c9dbec;border-radius:12px;padding:10px 12px;margin:10px 0;font-size:13px}.chk{display:flex;gap:8px;align-items:flex-start;margin-top:8px}"
            ".chk input{width:20px;height:20px;flex:none}.err{color:#b4402f;margin-top:8px}")
def gate_page(kind, u=None):
    cy = bool(u and u.get("cyr")); Tg = (lambda s: cyr(s)) if cy else (lambda s: s)
    if kind == "maint":
        body = f"<h1>⏳ {html.escape(Tg('Texnik ishlar'))}</h1><p>{html.escape(Tg(maint_message())).replace(chr(10), '<br>')}</p>"
        js = ""
    elif kind == "phone":
        body = (f"<h1>📱 {html.escape(Tg('Telefon raqamingizni yuboring'))}</h1><p>{html.escape(Tg('Ilovadan foydalanish uchun telefon raqamingizni botga yuboring. Raqam faqat xizmat ko‘rsatish va to‘lovni tasdiqlash uchun ishlatiladi.'))}</p>"
                f"<button class='btn' id='b'>{html.escape(Tg('📱 Telefon raqamni yuborish'))}</button><p class='mut' style='margin-top:10px'>{html.escape(Tg('Tugma ishlamasa, botga /start yozib, “Telefon raqamni yuborish” tugmasini bosing.'))}</p><div class='err' id='e'></div>")
        js = ("const W=Telegram.WebApp;W.ready();W.expand();async function poll(n){const f=new FormData();f.append('init',W.initData);"
              "const j=await(await fetch('api/state',{method:'POST',body:f})).json();if(j.phone){location.reload();return;}if(n>0)setTimeout(()=>poll(n-1),1000);}"
              "document.getElementById('b').onclick=()=>{try{W.requestContact(ok=>{if(ok)poll(15);else document.getElementById('e').textContent='"
              + Tg("Raqam yuborilmadi. Qayta urinib ko‘ring.") + "';});}catch(e){W.openTelegramLink('https://t.me/" + BOT_USERNAME + "?start=phone');}};")
    else:
        body = (f"<div class='terms'>{terms_html(cy)}</div><label class='chk'><input type='checkbox' id='c'><span>{html.escape(Tg('Foydalanish shartlari va ommaviy oferta bilan tanishdim va ularga roziman.'))}</span></label>"
                f"<button class='btn' id='b' disabled>{html.escape(Tg('✅ Shartlarga roziman — davom etish'))}</button>")
        js = ("const W=Telegram.WebApp;W.ready();W.expand();const c=document.getElementById('c'),b=document.getElementById('b');c.onchange=()=>b.disabled=!c.checked;"
              "b.onclick=async()=>{b.disabled=true;const f=new FormData();f.append('init',W.initData);await fetch('api/agree',{method:'POST',body:f});location.reload();};")
    return (f"<!doctype html><html lang='uz'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{APP_NAME}</title>"
            f"<script src='https://telegram.org/js/telegram-web-app.js'></script><style>{GATE_CSS}</style></head><body><div class='box'>{body}</div><script>(function(){{{js}}})();</script></body></html>")
def make_web(bot):
    async def index(req): return web.Response(text=LOADER, content_type="text/html")
    async def app(req):
        u = tg_user(await req.text())
        if not u: return web.Response(status=403, text="Telegram orqali oching")
        adm = u["id"] in ADMINS
        if maint_active() and not adm: return web.Response(text=gate_page("maint", u), content_type="text/html")
        if not u.get("phone") and not adm: return web.Response(text=gate_page("phone", u), content_type="text/html")
        if not agreed(u) and not adm: return web.Response(text=gate_page("terms", u), content_type="text/html")
        lv = await level_of(bot, u); lang = lang_of(u); html_, locked = app_html(lv, lang)
        pending = bool(db.execute("select 1 from receipts where uid=? and status='pending' limit 1", (u["id"],)).fetchone())
        mem = None if lv >= 2 else await is_member(bot, u["id"])
        mini = json.dumps({"level": lv, "locked": locked, "until": (u["until"] if not u["paid"] and u["id"] not in ADMINS and (u.get("until") or 0) > time.time() else None),
                           "pending": pending, "member": mem is not False, "tariffs": pub_tariffs(), "paytext": kv("paytext"), "pay": pay_info(), "uid": u["id"],
                           "free_until": free_until() if free_active() else None, "cyr": bool(u.get("cyr")),
                           "channel_url": CHANNEL_URL, "bot_url": "https://t.me/" + BOT_USERNAME, "total": len(BANKS[lang]["pb"]["questions"]), "demo_n": DEMO_N, "lang": lang, "langs": list(BANKS)}, ensure_ascii=False)
        return web.Response(text=html_.replace("__MINI__", mini.replace("</", "<\\/"), 1), content_type="text/html")
    async def api_check(req):
        form = await req.post(); u = tg_user(form.get("init"))
        if not u: return web.json_response({"error": "Telegram orqali oching"}, status=403)
        return web.json_response({"member": (await is_member(bot, u["id"])) is not False})
    async def api_request(req):
        form = await req.post(); u = tg_user(form.get("init"))
        if not u: return web.json_response({"error": "Telegram orqali oching"}, status=403)
        if has_full(u): return web.json_response({"error": "Sizda to‘liq kirish allaqachon ochiq. Ilovani qayta oching."})
        if await is_member(bot, u["id"]) is False: return web.json_response({"need_channel": True, "error": "Avval kanalga obuna bo‘ling."})
        if db.execute("select count(*) from receipts where uid=? and status='pending'", (u["id"],)).fetchone()[0] >= 3:
            return web.json_response({"error": "Sizda ko‘rib chiqilayotgan so‘rovlar bor. Admin javobini kuting."})
        t_ = tariff(form.get("tariff") or "") or (tariffs() or [None])[0]
        if not t_: return web.json_response({"error": "Tarif topilmadi"})
        phone = u.get("phone") or ""
        f = form.get("file"); data = None; ctype = ""; fname = "chek"
        if f is not None and hasattr(f, "file"):
            data = f.file.read(); ctype = f.content_type or ""; fname = os.path.basename(f.filename or "chek")[:60]
            if len(data) > 10 * 1024 * 1024: return web.json_response({"error": "Fayl hajmi 10 MB dan oshmasin."})
        if int(t_["price"]) > 0 and not data: return web.json_response({"error": "To‘lov chekini biriktiring."})
        cur = db.execute("insert into receipts(uid,file_id,ftype,kind,amount,status,t,tariff,months,days,phone) values(?,?,?,?,?,?,?,?,?,?,?)",
                         (u["id"], "", ("photo" if ctype.startswith("image/") else (ctype or "doc")), "mini", int(t_["price"]), "pending", int(time.time()), t_["id"], t_["months"], t_["days"], phone))
        rid = cur.lastrowid
        if data:
            ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/heic": ".heic", "application/pdf": ".pdf"}.get(ctype) or (os.path.splitext(fname)[1][:8] if "." in fname else ".bin")
            path = RCPT_DIR / f"{rid}{ext}"; path.write_bytes(data); db.execute("update receipts set path=?, note=? where id=?", (str(path), ctype, rid))
        db.commit()
        if kv("auto_open") == "1" or int(t_["price"]) <= 0:   # 0 so'm — admin tasdig'isiz
            await receipt_act(bot, rid, "ok", 0)
            if int(t_["price"]) <= 0: await notify_admins(bot, f"🆓 Bepul tarif avtomatik ochildi: {html.escape(u.get('name') or '—')} (<code>{u['id']}</code>) — {html.escape(t_['name'])}")
            return web.json_response({"ok": True, "auto": True})
        await send_request_to_admins(bot, rid, data=data, fname=fname, ctype=ctype)
        return web.json_response({"ok": True})
    async def api_state(req):
        form = await req.post(); u = tg_user(form.get("init"))
        if not u: return web.json_response({}, status=403)
        return web.json_response({"phone": bool(u.get("phone")), "agreed": agreed(u)})
    async def api_lang(req):
        form = await req.post(); u = tg_user(form.get("init"))
        if not u: return web.json_response({}, status=403)
        lg = form.get("lang") if form.get("lang") in ("uz", "ru") else "uz"
        db.execute("update users set lang=? where id=?", (lg, u["id"])); db.commit(); return web.json_response({"ok": True, "lang": lg})
    async def api_agree(req):
        form = await req.post(); u = tg_user(form.get("init"))
        if not u: return web.json_response({}, status=403)
        db.execute("update users set agreed=? where id=?", (int(time.time()), u["id"])); db.commit(); return web.json_response({"ok": True})
    async def api_feedback(req):
        form = await req.post(); u = tg_user(form.get("init"))
        if not u: return web.json_response({"error": "Telegram orqali oching"}, status=403)
        if db.execute("select count(*) from feedback where uid=? and t>?", (u["id"], int(time.time()) - 3600)).fetchone()[0] >= 10:
            return web.json_response({"error": "Juda ko‘p xabar. Birozdan keyin yozing."})
        ok = await save_feedback(bot, u, str(form.get("text") or ""), "app")
        return web.json_response({"ok": ok} if ok else {"error": "Matn bo‘sh"})
    async def oferta(req):
        cy = req.query.get("s") == "cyr"
        return web.Response(text=(f"<!doctype html><html lang='uz'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{APP_NAME}</title>"
                                  f"<style>{GATE_CSS}body{{-webkit-user-select:text;user-select:text}}</style></head><body><div class='box'>{terms_html(cy)}"
                                  f"<p class='mut' style='margin-top:14px'><a href='?s={'lat' if cy else 'cyr'}'>{'Lotin' if cy else 'Кирилл'}</a></p></div></body></html>"), content_type="text/html")
    async def pay_qr(req):
        q = kv("card_qr")
        if not q or not Path(q).exists(): return web.Response(status=404)
        p_ = Path(q); return web.Response(body=p_.read_bytes(), content_type={".png": "image/png", ".webp": "image/webp"}.get(p_.suffix, "image/jpeg"), headers={"Cache-Control": "no-cache"})
    # ---- Admin panel ----
    async def admin_page(req): return web.Response(text=ADMIN_HTML, content_type="text/html")
    PANEL_PW = os.getenv("ADMIN_PANEL_PASSWORD", "")
    def panel_key(): return hmac.new(TOKEN.encode(), ("panel:" + PANEL_PW).encode(), hashlib.sha256).hexdigest()
    async def login(req):
        try: body = await req.json()
        except Exception: body = {}
        await asyncio.sleep(1)
        if PANEL_PW and hmac.compare_digest(str(body.get("pw", "")), PANEL_PW):
            return web.json_response({"key": panel_key()})
        return web.json_response({"error": "Parol noto‘g‘ri"}, status=403)
    def adm_user(init):
        if PANEL_PW and init and hmac.compare_digest(str(init), panel_key()): return next(iter(ADMINS), 0) or -1
        tu = check_init_data(init or "")
        return int(tu["id"]) if tu and tu.get("id") and int(tu["id"]) in ADMINS else None
    async def api(req):
        try: body = await req.json()
        except Exception: body = {}
        aid = adm_user(body.get("init"))
        if not aid: return web.json_response({"error": "ruxsat yo‘q"}, status=403)
        act = req.match_info["act"]; now = int(time.time())
        import datetime as _dt
        t0 = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=5)))
        month = int(t0.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
        today = int(t0.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        q1 = lambda sql, *a: db.execute(sql, a).fetchone()[0] or 0
        if act == "summary":
            return web.json_response(dict(
                users=q1("select count(*) from users"), new_today=q1("select count(*) from users where created>=?", today),
                new_week=q1("select count(*) from users where created>=?", now - 7 * 86400), active_week=q1("select count(*) from users where last>=?", now - 7 * 86400),
                paid=q1("select count(*) from users where paid=1 or until>?", now), expiring=q1("select count(*) from users where until>? and until<?", now, now + 7 * 86400),
                revenue=q1("select sum(amount) from pays"),
                revenue_month=q1("select sum(amount) from pays where t>=?", month), revenue_today=q1("select sum(amount) from pays where t>=?", today),
                pays=q1("select count(*) from pays"), pending=q1("select count(*) from receipts where status='pending'"),
                answers=q1("select sum(n) from prog"), tariffs=pub_tariffs(), paytext=kv("paytext"), auto_open=kv("auto_open") == "1",
                questions=len(PB["questions"]), demo=len(DEMO), pay=pay_info(), free_until=free_until(), maint_until=maint_until(), maint_text=kv("maint_text") or "",
                fb_new=q1("select count(*) from feedback where seen=0"), now=now,
                daily=[[r_[0], r_[1]] for r_ in db.execute("select date(t,'unixepoch','+5 hours') d, sum(amount) from pays where t>=? group by d order by d", (now - 30 * 86400,))],
                signups=[[r_[0], r_[1]] for r_ in db.execute("select date(created,'unixepoch','+5 hours') d, count(*) from users where created>=? group by d order by d", (now - 30 * 86400,))]))
        if act == "receipts":
            st = body.get("status", "pending")
            rows = db.execute("select r.id,r.uid,u.name,r.kind,r.ftype,r.amount,r.status,r.t,r.dt,r.note,r.tariff,r.months,coalesce(r.phone,u.phone),(coalesce(r.path,'')<>'' or coalesce(r.file_id,'')<>''),u.until,r.days "
                              "from receipts r left join users u on u.id=r.uid where (?='all' or r.status=?) order by r.id desc limit 200", (st, st)).fetchall()
            return web.json_response([dict(zip(["id", "uid", "name", "kind", "ftype", "amount", "status", "t", "dt", "note", "tariff", "months", "phone", "has_file", "until", "days"], r_)) for r_ in rows])
        if act == "receipt_act":
            res = await receipt_act(bot, int(body["id"]), body["act"], aid, body.get("amount"), body.get("note", ""), body.get("months"), body.get("days"))
            return web.json_response({"ok": not res, "msg": res})
        if act == "users":
            qq = (body.get("q") or "").strip(); off = int(body.get("offset", 0)); flt = body.get("filter", "all")
            where, args = ["1=1"], []
            if qq: where.append("(cast(id as text) like ? or name like ? or phone like ?)"); args += [f"%{qq}%"] * 3
            if flt == "paid": where.append("(paid=1 or until>?)"); args.append(now)
            if flt == "free": where.append("not (paid=1 or coalesce(until,0)>?)"); args.append(now)
            if flt == "exp": where.append("until>? and until<?"); args += [now, now + 7 * 86400]
            w = " and ".join(where)
            rows = db.execute(f"select id,name,paid,created,last,(select sum(n) from prog where uid=users.id),until,phone from users where {w} order by created desc limit 50 offset ?", (*args, off)).fetchall()
            tot = db.execute(f"select count(*) from users where {w}", args).fetchone()[0]
            return web.json_response({"total": tot, "now": now, "rows": [dict(zip(["id", "name", "paid", "created", "last", "answers", "until", "phone"], r_)) for r_ in rows]})
        if act == "user_set":
            if body.get("paid"): await grant(bot, int(body["id"]), int(body.get("months") or 0), aid, body.get("amount") or None, days=int(body.get("days") or 0))
            else: await revoke(bot, int(body["id"]))
            return web.json_response({"ok": True})
        if act == "pays":
            rows = db.execute("select p.id,p.uid,u.name,p.amount,p.kind,p.t,p.info from pays p left join users u on u.id=p.uid order by p.id desc limit 300").fetchall()
            return web.json_response([dict(zip(["id", "uid", "name", "amount", "kind", "t", "info"], r_)) for r_ in rows])
        if act == "settings":
            if body.get("tariffs") is not None:
                out = []
                for i_, x in enumerate(body["tariffs"]):
                    try: mo = max(0, min(120, int(x.get("months") or 0))); dy = max(0, min(3650, int(x.get("days") or 0))); pr = max(0, int(re.sub(r"\D", "", str(x.get("price") or 0)) or 0))
                    except Exception: continue
                    if not mo and not dy: continue
                    nm = str(x.get("name") or "").strip()[:60] or (period_text(mo, dy) + " obuna")
                    out.append({"id": str(x.get("id") or f"t{int(time.time())}{i_}")[:20], "name": nm, "months": mo, "days": dy, "price": pr})
                if not out: return web.json_response({"ok": False, "msg": "Kamida bitta tarif kerak"})
                set_kv("tariffs", json.dumps(out, ensure_ascii=False))
            if body.get("paytext") is not None: set_kv("paytext", body["paytext"])
            if body.get("auto_open") is not None: set_kv("auto_open", 1 if body["auto_open"] else 0)
            for k_ in ("card_number", "card_owner", "admin_phone", "maint_text"):
                if body.get(k_) is not None: set_kv(k_, str(body[k_]).strip()[:500])
            if body.get("free") is not None:   # {"months":m,"days":d} yoki {"off":true}
                f_ = body["free"]
                if f_.get("off"): set_kv("free_until", 0)
                else:
                    mo, dy = int(f_.get("months") or 0), int(f_.get("days") or 0)
                    if mo or dy: set_kv("free_until", add_months(now, mo) + dy * 86400)
            if body.get("maint") is not None:  # {"days":d,"hours":h} yoki {"off":true}
                m_ = body["maint"]
                if m_.get("off"): set_kv("maint_until", 0)
                else:
                    sec = int(m_.get("days") or 0) * 86400 + int(m_.get("hours") or 0) * 3600
                    if sec > 0: set_kv("maint_until", now + sec)
            if body.get("qr_off"):
                q_ = kv("card_qr"); set_kv("card_qr", "")
                try: Path(q_).unlink()
                except Exception: pass
            return web.json_response({"ok": True})
        if act == "feedback":
            rows = db.execute("select f.id,f.uid,u.name,u.phone,f.text,f.t,f.src,f.reply,f.rt,f.seen from feedback f left join users u on u.id=f.uid order by f.id desc limit 300").fetchall()
            db.execute("update feedback set seen=1 where seen=0"); db.commit()
            return web.json_response([dict(zip(["id", "uid", "name", "phone", "text", "t", "src", "reply", "rt", "seen"], r_)) for r_ in rows])
        if act == "fb_reply":
            fid, txt = int(body["id"]), (body.get("text") or "").strip()
            row = db.execute("select uid from feedback where id=?", (fid,)).fetchone()
            if not row or not txt: return web.json_response({"ok": False, "msg": "Matn bo‘sh"})
            try: await bot.send_message(row[0], "💬 <b>Admin javobi:</b>\n" + html.escape(txt))
            except Exception: return web.json_response({"ok": False, "msg": "Yuborib bo‘lmadi (foydalanuvchi botni bloklagan bo‘lishi mumkin)"})
            db.execute("update feedback set reply=?, rt=? where id=?", (txt, now, fid)); db.commit()
            return web.json_response({"ok": True})
        if act == "broadcast":
            txt = (body.get("text") or "").strip()
            if not txt: return web.json_response({"ok": False})
            async def _bc():
                for (uid,) in db.execute("select id from users").fetchall():
                    try: await bot.send_message(uid, txt); await asyncio.sleep(0.05)
                    except Exception: pass
            asyncio.create_task(_bc())
            return web.json_response({"ok": True, "n": q1("select count(*) from users")})
        return web.json_response({"error": "noma’lum"}, status=404)
    async def upload_qr(req):
        form = await req.post()
        if not adm_user(form.get("init")): return web.json_response({"error": "ruxsat yo‘q"}, status=403)
        f = form.get("file")
        if f is None or not hasattr(f, "file"): return web.json_response({"error": "Fayl yo‘q"})
        data = f.file.read(); ct = f.content_type or ""
        if not ct.startswith("image/") or len(data) > 5 * 1024 * 1024: return web.json_response({"error": "Rasm (5 MB gacha) yuboring"})
        path = DB_PATH.parent / ("card_qr" + {"image/png": ".png", "image/webp": ".webp"}.get(ct, ".jpg")); path.write_bytes(data); set_kv("card_qr", str(path))
        return web.json_response({"ok": True})
    async def file_(req):
        if not adm_user(req.query.get("init")): return web.Response(status=403)
        r_ = db.execute("select file_id,ftype,path,note from receipts where id=?", (int(req.match_info["rid"]),)).fetchone()
        if not r_: return web.Response(status=404)
        if r_[2] and Path(r_[2]).exists():
            p_ = Path(r_[2]); ct = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".heic": "image/heic", ".pdf": "application/pdf"}.get(p_.suffix, "application/octet-stream")
            return web.Response(body=p_.read_bytes(), content_type=ct)
        if not r_[0]: return web.Response(status=404)
        f = await bot.get_file(r_[0]); buf = await bot.download_file(f.file_path)
        ct = "image/jpeg" if r_[1] == "photo" else (r_[1] if "/" in (r_[1] or "") else "application/octet-stream")
        return web.Response(body=buf.read(), content_type=ct)
    a = web.Application(client_max_size=12 << 20)
    a.router.add_get("/", index); a.router.add_post("/app", app)
    a.router.add_post("/api/check", api_check); a.router.add_post("/api/request", api_request)
    a.router.add_post("/api/state", api_state); a.router.add_post("/api/agree", api_agree); a.router.add_post("/api/lang", api_lang); a.router.add_post("/api/feedback", api_feedback)
    a.router.add_get("/oferta", oferta); a.router.add_get("/pay/qr", pay_qr); a.router.add_post("/admin/upload_qr", upload_qr)
    a.router.add_get("/admin", admin_page); a.router.add_post("/admin/login", login); a.router.add_post("/admin/api/{act}", api); a.router.add_get("/admin/file/{rid}", file_)
    return a

async def remind_loop(bot):
    """Obuna tugashiga 3 kun qolganda bir marta eslatish"""
    while True:
        try:
            now = int(time.time())
            for uid, until in db.execute("select id,until from users where until>? and until<? and coalesce(warned,0)<>until", (now, now + 3 * 86400)).fetchall():
                try:
                    u = user(uid)
                    await bot.send_message(uid, T(u, f"⏳ To‘liq kirish muddati {dstr(until)} da tugaydi. Uzaytirish uchun ilovada “To‘liq kirish” tugmasini bosing."), reply_markup=main_kb(u))
                except Exception: pass
                db.execute("update users set warned=? where id=?", (until, uid)); db.commit()
        except Exception as e: logging.warning("remind: %s", e)
        await asyncio.sleep(3600)

async def main():
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, protect_content=True))   # bot xabarlarini forward/saqlab bo‘lmaydi
    dp = Dispatcher(); dp.include_router(r)
    await bot.delete_webhook(drop_pending_updates=True)
    runner = web.AppRunner(make_web(bot)); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080"))).start()
    if WEBAPP_URL:
        try: await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text=APP_NAME, web_app=WebAppInfo(url=WEBAPP_URL + "/")))
        except Exception as e: logging.warning("menu button: %s", e)
    try:
        await bot.set_my_description(f"{APP_NAME} — huquqshunoslik fanlari bo‘yicha onlayn savol-javob platformasi.\n\n📚 {total_text()} dan ortiq savol-javob: “Davlat va huquq asoslari” darsliklari (8–11-sinf) va 35 ta qonunchilik hujjati asosida.\n\n✨ Kartochkalar, darslik/modda matnidan izohlar, mavzu va modda bo‘yicha tanlash, o‘zlashtirish statistikasi, lotin va kirill yozuvi.\n\n🆓 Demo bepul. «Start» ni bosing.")
        await bot.set_my_short_description(f"{total_text()} dan ortiq huquqiy savol-javob: 4 ta darslik va 35 ta qonunchilik hujjati asosida.")
    except Exception as e: logging.warning("description: %s", e)
    for lv in (0, 2):
        for lg in BANKS: app_html(lv, lg)   # oldindan tayyorlab qo'yish
    asyncio.create_task(remind_loop(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
