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
create table if not exists receipts(id integer primary key autoincrement, uid integer, file_id text, ftype text, kind text, amount integer, status text default 'pending', t integer, dt integer, admin integer, note text);
""")
for _tb, _col in (("users", "last integer"), ("users", "until integer"), ("users", "phone text"), ("users", "warned integer"),
                  ("receipts", "tariff text"), ("receipts", "months integer"), ("receipts", "phone text"), ("receipts", "path text")):
    try: db.execute(f"alter table {_tb} add column {_col}")
    except Exception: pass
RCPT_DIR = DB_PATH.parent / "receipts"; RCPT_DIR.mkdir(parents=True, exist_ok=True)
def kv(k, d=None):
    r = db.execute("select v from kv where k=?", (k,)).fetchone(); return r[0] if r else d
def set_kv(k, v): db.execute("insert or replace into kv values(?,?)", (k, str(v))); db.commit()
if kv("price") is None: set_kv("price", 500000)
if kv("auto_open") is None: set_kv("auto_open", 0)
if kv("paytext") is None or kv("paytext").startswith("To‘lov uchun admin bilan bog‘laning. To‘lov chekini (skrinshot) shu botga"):
    set_kv("paytext", "To‘lovni admin ko‘rsatgan karta raqamiga o‘tkazing va to‘lov chekini (skrinshot yoki PDF) shu yerga biriktiring. Admin tasdiqlagach to‘liq kirish ochiladi.")
if kv("tariffs") is None:
    set_kv("tariffs", json.dumps([{"id": "m1", "name": "1 oylik obuna", "months": 1, "price": 50000},
                                  {"id": "y1", "name": "1 yillik obuna", "months": 12, "price": int(kv("price"))}], ensure_ascii=False))
def tariffs():
    try: return [t_ for t_ in json.loads(kv("tariffs")) if t_.get("id")]
    except Exception: return []
def tariff(tid): return next((t_ for t_ in tariffs() if t_["id"] == tid), None)
def add_months(ts, m):
    import datetime as _d
    d = _d.datetime.fromtimestamp(ts, _d.timezone(_d.timedelta(hours=5)))
    y, mo = divmod(d.month - 1 + int(m), 12); y += d.year; mo += 1
    import calendar; day = min(d.day, calendar.monthrange(y, mo)[1])
    return int(d.replace(year=y, month=mo, day=day).timestamp())
def user(uid, name=""):
    r = db.execute("select id,name,paid,cyr,until,phone from users where id=?", (uid,)).fetchone()
    now = int(time.time())
    if not r:
        db.execute("insert into users(id,name,created,last) values(?,?,?,?)", (uid, name, now, now)); db.commit()
        r = (uid, name, 0, 0, None, None)
    else:
        db.execute("update users set last=?" + (", name=?" if name else "") + " where id=?", ((now, name, uid) if name else (now, uid))); db.commit()
    return {"id": r[0], "name": r[1], "paid": r[2], "cyr": r[3], "until": r[4], "phone": r[5]}
def has_full(u): return bool(u["paid"]) or (u.get("until") or 0) > time.time() or u["id"] in ADMINS

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
           "• lotin va kirill yozuvi.\n\n"
           "🆓 <b>Demo:</b> har bir mavzu va hujjatdan 5 tadan savol.\n"
           "🔓 <b>To‘liq kirish:</b> kanalga obuna + admin tasdig‘i (oylik yoki yillik obuna).\n\n"
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

@r.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, bot: Bot):
    u = user(m.from_user.id, m.from_user.full_name)
    if (command.args or "") == "buy" and not has_full(u):
        return await offer_buy(m, u, bot)
    await send_welcome(bot, m.chat.id, u)

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
async def offer_buy(msg: Message, u, bot: Bot):
    if has_full(u): return await msg.answer(T(u, "Sizda " + until_text(u) + " ✅"))
    t = ("🔓 <b>To‘liq kirish — barcha savol-javoblar</b>\n\n" + tariffs_text() +
         "\n\n1) kanalga obuna bo‘ling;\n2) ilovada muddatni tanlang, telefon raqamingizni va (pullik bo‘lsa) to‘lov chekini yuboring;\n3) admin tasdiqlagach to‘liq kirish ochiladi.")
    kb = [[B(text=T(u, "🔓 Ilovada so‘rov yuborish"), web_app=WebAppInfo(url=WEBAPP_URL + "/?buy=1"))]] if WEBAPP_URL else []
    kb.append([B(text=T(u, "📢 Kanalga o‘tish"), url=CHANNEL_URL)])
    await msg.answer(T(u, t), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
@r.callback_query(F.data == "buy")
async def cb_buy(c: CallbackQuery, bot: Bot):
    u = user(c.from_user.id); await offer_buy(c.message, u, bot); await c.answer()
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
async def got_contact(m: Message):
    if m.contact and m.contact.user_id == m.from_user.id:
        user(m.from_user.id); db.execute("update users set phone=? where id=?", (m.contact.phone_number, m.from_user.id)); db.commit()
        await m.answer("✓ Telefon raqamingiz saqlandi.")
# Chat orqali chek (rasm/fayl) → so'rov
@r.message(F.photo | F.document)
async def receipt(m: Message, bot: Bot):
    if m.from_user.id in ADMINS: return
    u = user(m.from_user.id, m.from_user.full_name)
    fid, ft = (m.photo[-1].file_id, "photo") if m.photo else (m.document.file_id, m.document.mime_type or "doc")
    t0 = next((x for x in tariffs() if x["price"] > 0), None) or (tariffs() or [{"id": "", "months": 1, "price": 0}])[0]
    cur = db.execute("insert into receipts(uid,file_id,ftype,kind,amount,status,t,tariff,months,phone) values(?,?,?,?,?,?,?,?,?,?)",
                     (u["id"], fid, ft, "chek", t0["price"], "pending", int(time.time()), t0["id"], t0["months"], u.get("phone")))
    db.commit(); rid = cur.lastrowid
    await send_request_to_admins(bot, rid, m=m)
    await m.answer(T(u, "Chek adminga yuborildi. Admin tasdiqlagach to‘liq kirish ochiladi va sizga xabar keladi.\n\nMuddatni tanlash va telefon raqamini yuborish uchun ilovadagi “To‘liq kirish” tugmasidan foydalaning."))

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
    r_ = db.execute("select r.uid,u.name,r.amount,r.tariff,r.months,r.phone,r.kind from receipts r left join users u on u.id=r.uid where r.id=?", (rid,)).fetchone()
    t_ = tariff(r_[3]) or {}
    return (f"🧾 <b>So‘rov #{rid}</b>: {html.escape(r_[1] or '—')} (<code>{r_[0]}</code>)\n📞 {html.escape(r_[5] or '—')}\n"
            f"🗓 {html.escape(t_.get('name') or (str(r_[4]) + ' oy'))} — {fmt_sum(r_[2])} so‘m" + ("" if r_[2] else " (bepul, chek talab qilinmaydi)"))
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
async def grant(bot, uid, months, admin, amount=None, kind="manual"):
    """months > 0 — shuncha oyga (mavjud muddat ustiga), 0/None — muddatsiz"""
    u = user(uid); now = int(time.time())
    if months and int(months) > 0:
        base = max(now, u.get("until") or 0); until = add_months(base, int(months))
        db.execute("update users set until=?, warned=null where id=?", (until, uid)); msg = f"✅ To‘liq kirish ochildi: {dstr(until)} gacha.\nIlovani qayta oching — barcha savol-javoblar ochiq."
    else:
        db.execute("update users set paid=1 where id=?", (uid,)); msg = "✅ To‘liq kirish ochildi (muddatsiz).\nIlovani qayta oching — barcha savol-javoblar ochiq."
    if amount: db.execute("insert into pays(uid,amount,kind,t,info) values(?,?,?,?,?)", (uid, int(amount), kind, now, f"admin {admin}; {months or 'muddatsiz'} oy"))
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
    if not a or not a[0].isdigit(): return await m.answer("Masalan: /ochish 123456789 12  (12 oyga) yoki /ochish 123456789 (muddatsiz)")
    await grant(bot, int(a[0]), int(a[1]) if len(a) > 1 and a[1].isdigit() else 0, m.from_user.id); await m.answer("✓ Ochildi")
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
async def receipt_act(bot, rid, act, admin, amount=None, note="", months=None):
    r_ = db.execute("select uid,amount,status,kind,months from receipts where id=?", (rid,)).fetchone()
    if not r_: return "chek topilmadi"
    if r_[2] != "pending": return "allaqachon ko‘rib chiqilgan"
    uid = r_[0]; amt = int(r_[1] or 0) if amount in (None, "") else int(amount)
    mon = int(r_[4] or 1) if months in (None, "") else int(months)
    db.execute("update receipts set status=?, dt=?, admin=?, amount=?, months=?, note=? where id=?", ("ok" if act == "ok" else "no", int(time.time()), admin, amt, mon, note, rid)); db.commit()
    if act == "ok": await grant(bot, uid, mon, admin, amt, r_[3])
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

# ---------------- Mini App (Telegram ichidagi platforma) ----------------
PB = json.loads((BASE / "data" / "platform_bank.json").read_text(encoding="utf-8"))
APP_TPL = (BASE / "web" / "app.html").read_text(encoding="utf-8")
LOADER = (BASE / "web" / "loader.html").read_text(encoding="utf-8")
ADMIN_HTML = (BASE / "web" / "admin.html").read_text(encoding="utf-8")
# Demo: darsliklarning har bir mavzusidan (dars) va har bir qonunchilik hujjatidan DEMO_N tadan savol (matn tartibida)
_SIDX = {s_["id"]: i for i, s_ in enumerate(PB["sentences"])}
_SL = {s_["id"]: s_["l"] for s_ in PB["sentences"]}
_SSEC = {s_["id"]: s_.get("sec") for s_ in PB["sentences"]}
_CHPB = {c_["id"]: c_ for c_ in PB["chapters"]}
_L2SRC = {l_["id"]: s_ for s_ in PB["sources"] for cid in s_["chapters"] if cid in _CHPB for l_ in _CHPB[cid]["lessons"]}
_grp = {}
for q_ in sorted(PB["questions"], key=lambda q_: (_SIDX.get(q_["src"][0], 10 ** 9), q_["id"])):
    l_ = _SL.get(q_["src"][0]); s_ = _L2SRC.get(l_)
    _grp.setdefault(l_ if (s_ and s_["type"] == "darslik") else (s_["id"] if s_ else l_), []).append(q_["id"])
DEMO.update(qid for g in _grp.values() for qid in g[:DEMO_N])
FULLC = {}   # bo'lim kaliti (dars:bo'lim) → [oddiy, murakkab] — demo'da haqiqiy sonlarni ko'rsatish uchun
for q_ in PB["questions"]:
    sid = q_["src"][0]; k_ = f"{_SL.get(sid)}:{_SSEC.get(sid)}"
    FULLC.setdefault(k_, [0, 0])[0 if q_.get("lvl") == "o" else 1] += 1
_APP_CACHE = {}
def app_html(level):
    if level not in _APP_CACHE:
        full = level >= 2
        qs = PB["questions"] if full else [q for q in PB["questions"] if q["id"] in DEMO]
        used_l = {_SL.get(q["src"][0]) for q in qs}
        sents = PB["sentences"] if full else [s_ for s_ in PB["sentences"] if s_["l"] in used_l]
        b = dict(PB, questions=qs, sentences=sents)
        if not full: b["full"] = FULLC
        js = json.dumps(b, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        _APP_CACHE[level] = (APP_TPL.replace("__BANK__", js), len(PB["questions"]) - len(qs))
    return _APP_CACHE[level]
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
def pub_tariffs(): return [{"id": t_["id"], "name": t_["name"], "months": int(t_["months"]), "price": int(t_["price"])} for t_ in tariffs()]
def make_web(bot):
    async def index(req): return web.Response(text=LOADER, content_type="text/html")
    async def app(req):
        u = tg_user(await req.text())
        if not u: return web.Response(status=403, text="Telegram orqali oching")
        lv = await level_of(bot, u); html_, locked = app_html(lv)
        pending = bool(db.execute("select 1 from receipts where uid=? and status='pending' limit 1", (u["id"],)).fetchone())
        mem = None if lv >= 2 else await is_member(bot, u["id"])
        mini = json.dumps({"level": lv, "locked": locked, "until": (u["until"] if not u["paid"] and u["id"] not in ADMINS and (u.get("until") or 0) > time.time() else None),
                           "pending": pending, "member": mem is not False, "tariffs": pub_tariffs(), "paytext": kv("paytext"), "phone": u.get("phone") or "",
                           "channel_url": CHANNEL_URL, "bot_url": "https://t.me/huquqsavoljavob_bot", "total": len(PB["questions"]), "demo_n": DEMO_N}, ensure_ascii=False)
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
        phone = re.sub(r"[^\d+]", "", str(form.get("phone") or ""))[:20]
        if len(re.sub(r"\D", "", phone)) < 9: return web.json_response({"error": "Telefon raqamini to‘liq kiriting."})
        f = form.get("file"); data = None; ctype = ""; fname = "chek"
        if f is not None and hasattr(f, "file"):
            data = f.file.read(); ctype = f.content_type or ""; fname = os.path.basename(f.filename or "chek")[:60]
            if len(data) > 10 * 1024 * 1024: return web.json_response({"error": "Fayl hajmi 10 MB dan oshmasin."})
            if not (ctype.startswith("image/") or ctype == "application/pdf"): return web.json_response({"error": "Faqat rasm yoki PDF yuboring."})
        if int(t_["price"]) > 0 and not data: return web.json_response({"error": "To‘lov chekini biriktiring."})
        db.execute("update users set phone=? where id=?", (phone, u["id"]))
        cur = db.execute("insert into receipts(uid,file_id,ftype,kind,amount,status,t,tariff,months,phone) values(?,?,?,?,?,?,?,?,?,?)",
                         (u["id"], "", ("photo" if ctype.startswith("image/") else ctype), "mini", int(t_["price"]), "pending", int(time.time()), t_["id"], int(t_["months"]), phone))
        rid = cur.lastrowid
        if data:
            ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/heic": ".heic", "application/pdf": ".pdf"}.get(ctype, "")
            path = RCPT_DIR / f"{rid}{ext}"; path.write_bytes(data); db.execute("update receipts set path=?, note=? where id=?", (str(path), ctype, rid))
        db.commit()
        if kv("auto_open") == "1":
            await receipt_act(bot, rid, "ok", 0); return web.json_response({"ok": True, "auto": True})
        await send_request_to_admins(bot, rid, data=data, fname=fname, ctype=ctype)
        return web.json_response({"ok": True})
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
                questions=len(PB["questions"]), demo=len(DEMO),
                daily=[[r_[0], r_[1]] for r_ in db.execute("select date(t,'unixepoch','+5 hours') d, sum(amount) from pays where t>=? group by d order by d", (now - 30 * 86400,))],
                signups=[[r_[0], r_[1]] for r_ in db.execute("select date(created,'unixepoch','+5 hours') d, count(*) from users where created>=? group by d order by d", (now - 30 * 86400,))]))
        if act == "receipts":
            st = body.get("status", "pending")
            rows = db.execute("select r.id,r.uid,u.name,r.kind,r.ftype,r.amount,r.status,r.t,r.dt,r.note,r.tariff,r.months,coalesce(r.phone,u.phone),(coalesce(r.path,'')<>'' or coalesce(r.file_id,'')<>''),u.until "
                              "from receipts r left join users u on u.id=r.uid where (?='all' or r.status=?) order by r.id desc limit 200", (st, st)).fetchall()
            return web.json_response([dict(zip(["id", "uid", "name", "kind", "ftype", "amount", "status", "t", "dt", "note", "tariff", "months", "phone", "has_file", "until"], r_)) for r_ in rows])
        if act == "receipt_act":
            res = await receipt_act(bot, int(body["id"]), body["act"], aid, body.get("amount"), body.get("note", ""), body.get("months"))
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
            if body.get("paid"): await grant(bot, int(body["id"]), int(body.get("months") or 0), aid, body.get("amount") or None)
            else: await revoke(bot, int(body["id"]))
            return web.json_response({"ok": True})
        if act == "pays":
            rows = db.execute("select p.id,p.uid,u.name,p.amount,p.kind,p.t,p.info from pays p left join users u on u.id=p.uid order by p.id desc limit 300").fetchall()
            return web.json_response([dict(zip(["id", "uid", "name", "amount", "kind", "t", "info"], r_)) for r_ in rows])
        if act == "settings":
            if body.get("tariffs") is not None:
                out = []
                for i_, x in enumerate(body["tariffs"]):
                    try: mo = max(1, min(120, int(x.get("months") or 1))); pr = max(0, int(re.sub(r"\D", "", str(x.get("price") or 0)) or 0))
                    except Exception: continue
                    nm = str(x.get("name") or "").strip()[:60] or (f"{mo // 12} yillik obuna" if mo % 12 == 0 else f"{mo} oylik obuna")
                    out.append({"id": str(x.get("id") or f"t{int(time.time())}{i_}")[:20], "name": nm, "months": mo, "price": pr})
                if not out: return web.json_response({"ok": False, "msg": "Kamida bitta tarif kerak"})
                set_kv("tariffs", json.dumps(out, ensure_ascii=False))
            if body.get("paytext") is not None: set_kv("paytext", body["paytext"])
            if body.get("auto_open") is not None: set_kv("auto_open", 1 if body["auto_open"] else 0)
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
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
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
    for lv in (0, 2): app_html(lv)   # oldindan tayyorlab qo'yish
    asyncio.create_task(remind_loop(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
