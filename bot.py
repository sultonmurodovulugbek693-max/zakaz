"""
Render uchun ishga tushirgich: "python bot.py" repo'ning bosh papkasidan
chaqirilsa ham, Ona tili botini (onatili_bot/bot.py) ishga tushiradi.
"""

import os
import runpy
import sys

BOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "onatili_bot")
sys.path.insert(0, BOT_DIR)
runpy.run_path(os.path.join(BOT_DIR, "bot.py"), run_name="__main__")
