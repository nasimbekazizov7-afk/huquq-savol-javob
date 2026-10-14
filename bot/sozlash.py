"""Birinchi marta ishga tushirish: tokenni so'raydi, .env yozadi va botni ishga tushiradi."""
from pathlib import Path
import subprocess, sys
env = Path(__file__).with_name(".env")
if not env.exists():
    print("=== @huquqsavoljavob_bot sozlash ===")
    tok = input("1) BotFather bergan tokenni qo'ying (1234567:AA...): ").strip()
    adm = input("2) Sizning Telegram ID raqamingiz (@userinfobot dan): ").strip()
    pay = input("3) Payme/Click to'lov tokeni (bo'lmasa Enter): ").strip()
    env.write_text(f"BOT_TOKEN={tok}\nADMIN_IDS={adm}\nCHANNEL=@huquqsavoljavob\nCHANNEL_URL=https://t.me/huquqsavoljavob\nPAYMENT_PROVIDER_TOKEN={pay}\n", encoding="utf-8")
    print("Saqlandi (.env). Eslatma: botni @huquqsavoljavob kanaliga ADMIN qilib qo'shing.\n")
subprocess.call([sys.executable, str(Path(__file__).with_name("bot.py"))])
