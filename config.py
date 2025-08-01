import os
from dotenv import load_dotenv

# Загружаем переменные из .env файла
load_dotenv()

# Теперь переменные можно использовать через os.getenv
BOT_TOKEN = os.getenv("BOT_TOKEN")
TOKEN = os.getenv("TOKEN")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(BASE_DIR, "форма.xlsx")
