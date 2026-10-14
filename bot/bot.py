"""
Huquq savol-javob — Telegram bot (@huquqsavoljavob_bot)

Kirish darajalari:
  • demo (hamma uchun)      — har bir darslikning 1-bobi, 35 ta hujjatning har biridan dastlabki 10 ta savol
  • kanal a'zolari           — + darsliklarning 2-bobi, hujjatlarning 11–20-savollari
  • to'liq (to'lov)          — barcha savol-javoblar
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
                           FSInputFile, LabeledPrice, PreCheckoutQuery)

load_dotenv()
BASE = Path(__file__).parent
TOKEN = os.environ["BOT_TOKEN"]
CHANNEL = os.getenv("CHANNEL", "@huquqsavoljavob")            # kanal (bot kanalda admin bo'lishi shart)
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/huquqsavoljavob")
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "")       # Payme/Click (BotFather → Payments). Bo'sh bo'lsa — qo'lda to'lov
FREE_N, CHANNEL_N = 10, 20                                      # hujjatlar: demo / kanal chegarasi
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
db = sqlite3.connect(BASE / "bot.sqlite3")
db.executescript("""
create table if not exists users(id integer primary key, name text, paid integer default 0, cyr integer default 0, created integer);
create table if not exists prog(uid integer, qid text, g text, n integer default 0, t integer, primary key(uid,qid));
create table if not exists kv(k text primary key, v text);
create table if not exists pays(id integer primary key autoincrement, uid integer, amount integer, kind text, t integer, info text);
""")
def kv(k, d=None):
    r = db.execute("select v from kv where k=?", (k,)).fetchone(); return r[0] if r else d
def set_kv(k, v): db.execute("insert or replace into kv values(?,?)", (k, str(v))); db.commit()
if kv("price") is None: set_kv("price", 500000)
if kv("paytext") is None: set_kv("paytext", "To‘lov uchun admin bilan bog‘laning. To‘lov chekini (skrinshot) shu botga yuboring — admin tasdiqlagach to‘liq kirish ochiladi.")
def user(uid, name=""):
    r = db.execute("select id,name,paid,cyr from users where id=?", (uid,)).fetchone()
    if not r:
        db.execute("insert into users(id,name,created) values(?,?,?)", (uid, name, int(time.time()))); db.commit()
        r = (uid, name, 0, 0)
    return {"id": r[0], "name": r[1], "paid": r[2], "cyr": r[3]}

# ---------------- Lotin → Kirill ----------------
_M = dict(a="а", b="б", d="д", e="е", f="ф", g="г", h="ҳ", i="и", j="ж", k="к", l="л", m="м", n="н", o="о", p="п", q="қ", r="р", s="с", t="т", u="у", v="в", x="х", y="й", z="з", c="с", w="в")
_RX = re.compile(r"o[ʻ‘'`ʼ]|g[ʻ‘'`ʼ]|sh|ch|yo|yu|ya|ye|ts|’|ʼ|[a-z]", re.I)
_MON = {"январ": "январь", "феврал": "февраль", "апрел": "апрель", "июн": "июнь", "июл": "июль", "сентабр": "сентябрь", "октабр": "октябрь", "ноябр": "ноябрь", "декабр": "декабрь"}
def _c0(s):
    def rep(m):
        t = m.group(0); lo = t.lower(); st = m.start(); prev = s[st - 1] if st else " "
        if len(lo) == 2 and lo[0] == "o" and not lo[1].isalpha(): r = "ў"
        elif len(lo) == 2 and lo[0] == "g" and not lo[1].isalpha(): r = "ғ"
        elif t in "’ʼ": return "ъ"
        else:
            r = {"sh": "ш", "ch": "ч", "yo": "ё", "yu": "ю", "ya": "я", "ye": "е", "ts": "ц"}.get(lo) or ("э" if lo == "e" and not prev.isalpha() else _M.get(lo))
        if r is None: return t
        if t[0].isupper(): return r.upper() if (len(t) == 1 or t[1:].isupper()) else r[0].upper() + r[1:]
        return r
    return _RX.sub(rep, s)
def cyr(s):
    parts = re.split(r"(<[^>]+>|\b(?:https?://)?[a-z]+\.(?:uz|com|org|ru|me)(?:/\S*)?|@\w+|\b(?![IVX]\. [A-ZʻO‘]{3})[IVXLC]+\b(?![ʻ‘'`ʼ’]))", s)
    out = []
    for i, p in enumerate(parts):
        if i % 2: out.append(p); continue
        p = re.sub(r"(\d)-(yil|yanvar|fevral|mart|aprel|may|iyun|iyul|avgust|sentabr|oktabr|noyabr|dekabr)", r"\1 \2", p, flags=re.I)
        p = _c0(p)
        p = re.sub(r"(\d )(январ|феврал|апрел|июн|июл|сентабр|октабр|ноябр|декабр)(?![а-яёўқғҳъь])", lambda m: m.group(1) + _MON[m.group(2)], p)
        out.append(p)
    return "".join(out)
def T(u, s): return cyr(s) if u["cyr"] else s

# ---------------- Kirish huquqi ----------------
async def is_member(bot, uid):
    try:
        m = await bot.get_chat_member(CHANNEL, uid)
        return m.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except Exception as e:
        logging.warning("a'zolikni tekshirib bo'lmadi: %s", e); return False
def need_level(qid):
    """0 — demo, 1 — kanal, 2 — to'lov"""
    q = Q[qid]; s = SRC[CH_SRC[q["c"]]]
    if s["type"] == "darslik":
        i = CH_IDX[q["c"]]; return 0 if i == 0 else 1 if i == 1 else 2
    p = POS_IN_SRC[qid]; return 0 if p < FREE_N else 1 if p < CHANNEL_N else 2
def chapter_level(cid):
    s = SRC[CH_SRC[cid]]
    if s["type"] == "darslik":
        i = CH_IDX[cid]; return 0 if i == 0 else 1 if i == 1 else 2
    return 0
async def level_of(bot, u):
    if u["paid"] or u["id"] in ADMINS: return 2
    return 1 if await is_member(bot, u["id"]) else 0
LOCK = {0: "", 1: "📢", 2: "🔒"}

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
def src_name(s): return s["title"] if s["type"] == "qonun" else f"“{s['title']}” · {s['grade']}"
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
def main_kb(u):
    return InlineKeyboardMarkup(inline_keyboard=[
        [B(text=T(u, "📘 Darsliklar (4)"), callback_data="books"), B(text=T(u, "⚖️ Qonunchilik hujjatlari (35)"), callback_data="laws:0")],
        [B(text=T(u, "📊 O‘zlashtirish statistikasi"), callback_data="stat"), B(text=T(u, "🔁 Bilmaganlarim"), callback_data="redo")],
        [B(text=T(u, "🔓 To‘liq kirish"), callback_data="buy"), B(text=("Lotin" if u["cyr"] else "Кирилл"), callback_data="script")]])
WELCOME = ("<b>Onlayn savol-javob platformasi</b>\n(Huquqshunoslik fanlari bo‘yicha)\n\n"
           "“Davlat va huquq asoslari” darsliklari (8–11-sinf) va 35 ta normativ-huquqiy hujjat bo‘yicha savol-javob kartochkalari.\n\n"
           "🆓 <b>Demo:</b> har bir darslikning 1-bobi va har bir hujjatdan dastlabki 10 ta savol.\n"
           "📢 <b>Kanalga a’zo bo‘lsangiz:</b> 2-boblar va hujjatlarning 11–20-savollari.\n"
           "🔒 <b>To‘liq kirish:</b> barcha {n} ta savol-javob — {price} so‘m.")
def fmt_price(): return f"{int(kv('price')):,}".replace(",", " ")

r = Router()

@r.message(CommandStart())
async def cmd_start(m: Message):
    u = user(m.from_user.id, m.from_user.full_name)
    await m.answer(T(u, WELCOME.format(n=len(Q), price=fmt_price())), reply_markup=main_kb(u))

@r.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery):
    u = user(c.from_user.id); await c.message.edit_text(T(u, WELCOME.format(n=len(Q), price=fmt_price())), reply_markup=main_kb(u)); await c.answer()

@r.callback_query(F.data == "script")
async def cb_script(c: CallbackQuery):
    u = user(c.from_user.id); db.execute("update users set cyr=? where id=?", (0 if u["cyr"] else 1, u["id"])); db.commit()
    await cb_home(c)

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
        head = f"📘 <b>{html.escape(src_name(s))}</b>\n" + (f"Mualliflar: {html.escape(s['authors'])}\n" if s.get("authors") else "") + "\nBobni tanlang. 📢 — kanal a’zolari uchun, 🔒 — to‘liq kirish."
        await c.message.edit_text(T(u, head), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    else:
        qs = Q_BY_SRC.get(s["id"], []); n = len(qs)
        avail = n if lv == 2 else min(n, CHANNEL_N if lv == 1 else FREE_N)
        kb = [[B(text=T(u, f"▶ Boshlash ({avail} ta savol)"), callback_data=f"go:{s['id']}")]]
        if lv == 2:
            kb += [[B(text=T(u, f"{c0['title'][:50]} ({len(Q_BY_CH.get(c0['id'], []))})"), callback_data=f"ch:{c0['id']}")] for c0 in s["chapters"] if Q_BY_CH.get(c0["id"])][:40]
        elif lv == 0: kb.append([B(text=T(u, "📢 11–20-savollar: kanalga a’zo bo‘lish"), callback_data="join")])
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
    await msg.answer(T(u, "Bu bo‘lim kanal a’zolari uchun ochiq: darsliklarning 2-bobi va har bir hujjatning 11–20-savollari.\n\nKanalga a’zo bo‘ling va “A’zo bo‘ldim” tugmasini bosing."), reply_markup=kb)
@r.callback_query(F.data == "join")
async def cb_join(c: CallbackQuery): await ask_join(c.message, user(c.from_user.id)); await c.answer()
@r.callback_query(F.data == "chk")
async def cb_chk(c: CallbackQuery, bot: Bot):
    u = user(c.from_user.id)
    if await is_member(bot, u["id"]):
        await c.message.edit_text(T(u, "✅ Rahmat! Kanal a’zolari uchun bo‘limlar ochildi."), reply_markup=main_kb(u))
    else: await c.answer(T(u, "Hali kanalga a’zo bo‘lmagansiz."), show_alert=True)

# ---------------- To'lov ----------------
async def offer_buy(msg: Message, u, bot: Bot):
    price = int(kv("price"))
    t = f"🔒 <b>To‘liq kirish</b>\n\nBarcha {len(Q)} ta savol-javob: 4 ta darslikning barcha boblari va 35 ta hujjatning barcha savollari.\n\nNarxi: <b>{fmt_price()} so‘m</b>."
    if PROVIDER_TOKEN:
        await msg.answer(T(u, t))
        await bot.send_invoice(msg.chat.id, title="To‘liq kirish", description=f"Barcha {len(Q)} ta savol-javobga doimiy kirish", payload=f"full:{u['id']}",
                               provider_token=PROVIDER_TOKEN, currency="UZS", prices=[LabeledPrice(label="To‘liq kirish", amount=price * 100)])
    else:
        await msg.answer(T(u, t + "\n\n" + kv("paytext") + f"\n\nSizning ID: <code>{u['id']}</code>"))
@r.callback_query(F.data == "buy")
async def cb_buy(c: CallbackQuery, bot: Bot):
    u = user(c.from_user.id)
    if u["paid"]: return await c.answer(T(u, "Sizda to‘liq kirish allaqachon ochiq ✅"), show_alert=True)
    await offer_buy(c.message, u, bot); await c.answer()
@r.pre_checkout_query()
async def pre_checkout(p: PreCheckoutQuery): await p.answer(ok=True)
@r.message(F.successful_payment)
async def paid(m: Message, bot: Bot):
    sp = m.successful_payment
    db.execute("update users set paid=1 where id=?", (m.from_user.id,))
    db.execute("insert into pays(uid,amount,kind,t,info) values(?,?,?,?,?)", (m.from_user.id, sp.total_amount // 100, "invoice", int(time.time()), sp.provider_payment_charge_id)); db.commit()
    u = user(m.from_user.id); await m.answer(T(u, "✅ To‘lov qabul qilindi. Barcha savol-javoblar ochildi!"), reply_markup=main_kb(u))
    for a in ADMINS:
        try: await bot.send_message(a, f"💰 To‘lov: {m.from_user.full_name} (<code>{m.from_user.id}</code>) — {sp.total_amount//100} so‘m")
        except Exception: pass
# Qo'lda to'lov: foydalanuvchi chek (rasm) yuboradi → admin tasdiqlaydi
@r.message(F.photo | F.document)
async def receipt(m: Message, bot: Bot):
    if m.from_user.id in ADMINS: return
    u = user(m.from_user.id, m.from_user.full_name)
    for a in ADMINS:
        try:
            await m.forward(a)
            await bot.send_message(a, f"🧾 Chek: {html.escape(m.from_user.full_name)} (<code>{m.from_user.id}</code>)",
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[[B(text="✅ Ochish", callback_data=f"adm_ok:{m.from_user.id}"), B(text="❌ Rad etish", callback_data=f"adm_no:{m.from_user.id}")]]))
        except Exception: pass
    await m.answer(T(u, "Chek adminga yuborildi. Tasdiqlangach xabar beramiz."))

# ---------------- Admin ----------------
def is_admin(uid): return uid in ADMINS
@r.message(Command("admin"))
async def adm(m: Message):
    if not is_admin(m.from_user.id): return
    nu = db.execute("select count(*), sum(paid) from users").fetchone()
    await m.answer(f"<b>Admin</b>\nFoydalanuvchilar: {nu[0]}, to‘lov qilganlar: {nu[1] or 0}\nNarx: {fmt_price()} so‘m\n\n"
                   "/narx 500000 — narxni o‘zgartirish\n/ochish ID — to‘liq kirishni ochish\n/yopish ID — yopish\n"
                   "/tolov_matni matn — qo‘lda to‘lov yo‘riqnomasi (karta raqami va h.k.)\n/xabar matn — barcha foydalanuvchilarga xabar")
@r.message(Command("narx"))
async def adm_price(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id): return
    v = re.sub(r"\D", "", command.args or "")
    if not v: return await m.answer("Masalan: /narx 500000")
    set_kv("price", int(v)); await m.answer(f"✓ Yangi narx: {fmt_price()} so‘m")
@r.message(Command("tolov_matni"))
async def adm_paytext(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id): return
    if not command.args: return await m.answer("Joriy matn:\n" + kv("paytext"))
    set_kv("paytext", command.args); await m.answer("✓ Saqlandi")
async def set_paid(bot, uid, v, admin):
    user(uid); db.execute("update users set paid=? where id=?", (v, uid))
    if v: db.execute("insert into pays(uid,amount,kind,t,info) values(?,?,?,?,?)", (uid, int(kv("price")), "manual", int(time.time()), f"admin {admin}"))
    db.commit()
    try: await bot.send_message(uid, "✅ To‘liq kirish ochildi! /start" if v else "To‘liq kirish yopildi.")
    except Exception: pass
@r.message(Command("ochish"))
async def adm_open(m: Message, command: CommandObject, bot: Bot):
    if not is_admin(m.from_user.id): return
    if not (command.args or "").strip().isdigit(): return await m.answer("Masalan: /ochish 123456789")
    await set_paid(bot, int(command.args), 1, m.from_user.id); await m.answer("✓ Ochildi")
@r.message(Command("yopish"))
async def adm_close(m: Message, command: CommandObject, bot: Bot):
    if not is_admin(m.from_user.id): return
    if not (command.args or "").strip().isdigit(): return await m.answer("Masalan: /yopish 123456789")
    await set_paid(bot, int(command.args), 0, m.from_user.id); await m.answer("✓ Yopildi")
@r.callback_query(F.data.startswith("adm_"))
async def adm_cb(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id): return await c.answer()
    act, uid = c.data[4:].split(":"); uid = int(uid)
    if act == "ok": await set_paid(bot, uid, 1, c.from_user.id); await c.message.edit_text(c.message.html_text + "\n✅ Ochildi")
    else:
        try: await bot.send_message(uid, "To‘lov tasdiqlanmadi. Savollar bo‘lsa admin bilan bog‘laning.")
        except Exception: pass
        await c.message.edit_text(c.message.html_text + "\n❌ Rad etildi")
    await c.answer()
@r.message(Command("xabar"))
async def adm_bc(m: Message, command: CommandObject, bot: Bot):
    if not is_admin(m.from_user.id) or not command.args: return
    ok = 0
    for (uid,) in db.execute("select id from users").fetchall():
        try: await bot.send_message(uid, command.args); ok += 1; await asyncio.sleep(0.05)
        except Exception: pass
    await m.answer(f"✓ Yuborildi: {ok}")

async def main():
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(); dp.include_router(r)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
