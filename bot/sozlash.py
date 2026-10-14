"""Birinchi marta ishga tushirish: tokenni so'raydi, .env yozadi va botni ishga tushiradi."""
from pathlib import Path
import subprocess, sys
env = Path(__file__).with_name(".env")
if not env.exists():
    print("=== @huquqsavoljavob_bot sozlash ===")
    tok = input("1) BotFather bergan tokenni qo'ying (1234567:AA...): ").strip()
    adm = input("2) Sizning Telegram ID raqamingiz (@userinfobot dan): ").strip()
    pay = input("3) Payme/Click to'lov tokeni (bo'lmasa Enter): ").strip()
    usr = input("4) Bot nomi (@ belgisisiz, masalan huquqsavoljavob_bot): ").strip().lstrip("@") or "huquqsavoljavob_bot"
    url = input("5) Server https manzili (Mini App uchun; bo'lmasa Enter): ").strip().rstrip("/")
    ch = input("6) Kanal (@nomi; Enter — @huquqsavoljavob): ").strip() or "@huquqsavoljavob"
    pw = input("7) Admin panel paroli (brauzer uchun; bo'lmasa Enter): ").strip()
    env.write_text(f"BOT_TOKEN={tok}\nBOT_USERNAME={usr}\nADMIN_IDS={adm}\nWEBAPP_URL={url}\nCHANNEL={ch}\nCHANNEL_URL=https://t.me/{ch.lstrip('@')}\nADMIN_PANEL_PASSWORD={pw}\nPAYMENT_PROVIDER_TOKEN={pay}\n", encoding="utf-8")
    print("Saqlandi (.env). Eslatma: botni @huquqsavoljavob kanaliga ADMIN qilib qo'shing.\n")
subprocess.call([sys.executable, str(Path(__file__).with_name("bot.py"))])
