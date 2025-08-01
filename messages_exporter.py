import requests
import time
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import TOKEN
# Основные константы и сессия
BASE_URL = 'https://tenderplan.ru/api'
HEADERS = {
    'Authorization': f'Bearer {TOKEN}',
    'Accept': 'application/json',
    'Content-Type': 'application/json',
}

session = requests.Session()
session.headers.update(HEADERS)
# ─── Справочник статусов внутри кода ───────────────────────────────────
STATUS_LOOKUP = {
    1: "Прием заявок",
    4: "Отменено",
    5: "Не состоялось",
    2: "Работа комиссии",
    3: "Завершено",
    7: "Исполняется",
    6: "Исполнение завершено",
    8: "Расторжение",
    0: "Неизвестно"
}
# ─── Справочник ФЗ ───────────────────────────────────────────────
FZ_LOOKUP = {
    0: "223-ФЗ",
    1: "44-ФЗ",
}

# ─── Справочник способов проведения ────────────────────────────────────
PLACINGWAY_LOOKUP = {
    0: "ИС",  1: "ОК",  2: "ОА",  3: "ЭФ",  4: "ЗК",  5: "ПО",
    6: "ЕП",  7: "ОКУ", 8: "ОКД", 9: "ЗКК",10: "ЗККУ",11: "ЗККД",
   12: "ЗА", 13: "ЗКБ",14: "ЗП",15: "ЭА",16: "ИСМ",17: "СЗ",18: "ИОС",
   19: "РЕД",20: "ПЕР",21: "КП",22: "ЗКЭФ",23: "ОКЭФ",24: "ЗПЭФ",
   25: "ОКУЭФ",26: "ОКДЭФ",27: "ЗЦ",28: "ГА",29: "ПП"
}


def fetch_all_tenders(key_id: str) -> str:
    """
    Загружает все превью тендеров по ключу, выполняя постраничный запрос.
    """
    all_tenders = []
    page = 0
    size = 50  # сколько тендеров за 1 запрос
    while True:
        resp = session.get(
           f"{BASE_URL}/tenders/v2/getlist",
           params={
            'type': 0,
            'id': key_id,
            'statuses': [1],
            'page': page,
            'size': size
        },
            verify=False
        )
        resp.raise_for_status()
        raw_batch = resp.json().get('tenders', [])
        # вручную фильтруем, чтобы точно остались только "Подача заявок"
        now_ts = int(datetime.now().timestamp() * 1000)
        batch = [
            t for t in raw_batch
            if t.get("status") == 1 and
               (t.get("submissionCloseDateTime") or t.get("submissionCloseDate") or 0) > now_ts
        ]
        if not batch:
            break
        all_tenders.extend(batch)
        page += 1
        # если пришло меньше, чем запрошено — значит это последняя страница
        if len(raw_batch) < size:
            break

    print(f"Всего тендеров после пагинации: {len(all_tenders)}")

    # Удаляем дубликаты по _id
    seen = set()
    unique_tenders = []
    for t in all_tenders:
        tid = t.get('_id')
        if tid in seen:
            continue
        seen.add(tid)
        unique_tenders.append(t)
    all_tenders = unique_tenders
    print(f"После удаления дубликатов: {len(all_tenders)} тендеров")
    return all_tenders

def fetch_tender_detail(preview: dict) -> dict:
    """
    Запрашивает полные детали тендера по его ID.
    Даже если не удаётся получить полную информацию, возвращает минимальные данные,
    чтобы не нарушать поток логики и сохранить last_ts.
    """
    tid = preview.get('_id')
    try:
        resp = session.get(
            f"{BASE_URL}/tenders/get",
            params={'id': tid},
            verify=False
        )
        resp.raise_for_status()
        detail = resp.json() or {}
        # Встраиваем доп. поле со статусом из превью
        detail['_preview_status'] = preview.get('status', 0)
        # Если нет важных данных — подстрахуемся, но не возвращаем None
        if not detail.get("publicationDate"):
            detail["publicationDate"] = preview.get("publicationDateTime", 0)
        return detail
    except Exception as e:
        logging.warning(f"Ошибка при получении тендера {tid}: {e}")
        return {
            "_id": tid,
            "documents": [],
            "publicationDate": preview.get("publicationDateTime", 0),
            "noticeNumber": preview.get("noticeNumber", "—"),
            "_preview_status": preview.get("status", 0)
        }


def format_tender_message(detail: dict) -> str:
    """
    Форматирует детальную информацию о тендере в текст для Telegram.
    """

    num = detail.get('number', detail.get('_id', ''))
    name = detail.get('orderName', '')
    header = f"№ {num}  {name}" 
    close_ts = detail.get("submissionCloseDateTime") or detail.get("submissionCloseDate", 0)
    close_dt = datetime.fromtimestamp(close_ts/1000) if close_ts else ""
    # —— ОБРАБОТКА ЦЕНЫ С ВАЛЮТОЙ ——
    price = detail.get('maxPrice')
    currency_code = (detail.get('currency') or '').upper()
    # словарь символов валют (дописать по необходимости)
    CURRENCY_SYMBOLS = {
        'RUB': '₽',
        'USD': '$',
        'EUR': '€',
    }
    curr_sym = CURRENCY_SYMBOLS.get(currency_code, currency_code)
    if price is None:
        price_text = "не указана"
    else:
        # формат с разделителем тысяч
        price_text = f"{int(price):,}".replace(",", " ")  # неразрывный пробел как разделитель
        price_text += f" {curr_sym}"

    # Ссылки
    app_url = f"https://tenderplan.ru/app?key={detail.get('key')}\&tender={detail.get('_id')}"
    eis_link = detail.get('href') or ''
    
    fz_id = detail.get("type")  
    fz = FZ_LOOKUP.get(fz_id, "")
    placing_code = detail.get("placingWay")
    placing = ""
    if isinstance(placing_code, int):
        placing = PLACINGWAY_LOOKUP.get(placing_code, "")
    elif str(placing_code).isdigit():
        placing = PLACINGWAY_LOOKUP.get(int(placing_code), "")
    torg_type=" ".join(part for part in (fz, placing) if part)
    


    lines = [
        header,
        f"📅 <b>Приём заявок до:</b> {close_dt}",
        f"💰 <b>Цена:</b> {price_text}",
    ]
    if torg_type:
        lines.append(f"📂 <b>Тип торгов:</b> {torg_type}")
   

    # скрытые ссылки за текстом
    if eis_link:
        lines.append(f'🔗 <a href="{eis_link}">Ссылка на тендер</a>')
    #lines.append(f'🔗 <a href="{app_url}">TenderPlan</a>')

    return "\n".join(lines)

def export_messages(key_id: str) -> list[tuple[str,str,list[dict]]]:
    """
    Собирает все тендеры только со статусом 'Подача заявок' и возвращает список кортежей:
    (tender_id, formatted_text, attachments_list).
    """
    previews = fetch_all_tenders(key_id)
    messages: list[tuple[str,str,list[dict]]] = []

    # Параллельная загрузка деталей
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(fetch_tender_detail, p) for p in previews]
        for fut in as_completed(futures):
            try:
                detail = fut.result()
                tid  = detail.get("_id", "")
                text = format_tender_message(detail)
                atts = detail.get("attachments", [])
                messages.append((tid, text, atts))
            except Exception:
                # можно логировать ошибку
                continue
    return messages
