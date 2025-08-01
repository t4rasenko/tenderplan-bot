from database import get_connection

def init_db():
    with get_connection() as conn:
        cursor = conn.cursor()

        # Таблица user_keys
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_keys (
                tg_user_id   INTEGER NOT NULL,
                tender_key   TEXT    NOT NULL,
                tender_name  TEXT    NOT NULL DEFAULT '',
                PRIMARY KEY (tg_user_id, tender_key)
            )
        """)
        
        # Проверка на наличие столбца tender_name (если запускаешь повторно)
        cursor.execute("PRAGMA table_info(user_keys)")
        columns = [row[1] for row in cursor.fetchall()]
        if "tender_name" not in columns:
            cursor.execute("ALTER TABLE user_keys ADD COLUMN tender_name TEXT NOT NULL DEFAULT ''")

        # Таблица subscriptions
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                tg_user_id  INTEGER NOT NULL,
                tender_key  TEXT    NOT NULL,
                PRIMARY KEY (tg_user_id, tender_key)
            )
        """)

        # Таблица active_keys
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS active_keys (
                tg_user_id INTEGER PRIMARY KEY,
                tender_key TEXT NOT NULL
            )
        """)

        # Таблица subscription_state
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS subscription_state (
                tg_user_id   INTEGER NOT NULL,
                tender_key   TEXT    NOT NULL,
                last_ts      INTEGER NOT NULL,
                PRIMARY KEY (tg_user_id, tender_key)
            )
        """)
        # Таблица sent_tenders
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sent_tenders (
                tg_user_id INTEGER NOT NULL,
                tender_id TEXT NOT NULL,
                PRIMARY KEY (tg_user_id, tender_id)
            )
        """)
        # Таблица для вложений
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tender_id TEXT NOT NULL,
                file_name TEXT,
                url TEXT,
                UNIQUE(tender_id, url)
            )
        """)
        conn.commit()

if __name__ == "__main__":
    init_db()
    print("✅ База данных инициализирована.")