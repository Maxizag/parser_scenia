import asyncio
import logging
import sqlite3
import os
import re 
from dotenv import load_dotenv
from telethon import TelegramClient, events
import openai 
from telethon.utils import get_peer_id
from telethon.tl.types import User, Channel, Chat 
import json

# Загрузка переменных окружения из .env файла
load_dotenv()

# ====== Настройки: Сначала получаем все переменные окружения! ======
API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
PHONE = os.getenv("TG_PHONE", "")
# Используйте CONTROL_CHAT_ID и TARGET_CHAT_ID только как запасные или для главного админа
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "0"))
CONTROL_CHAT_ID = int(os.getenv("CONTROL_CHAT_ID", "0"))
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

# OpenAI API
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# ==============================================================================
# НАСТРОЙКА ЛОГИРОВАНИЯ
# ==============================================================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ==============================================================================
# ИНИЦИАЛИЗАЦИЯ OpenAI
# ==============================================================================
if OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY
    log.info("✓ OpenAI API Key set. AI filtering is active.")
else:
    log.warning("⚠️ OPENAI_API_KEY not found. AI filtering will be skipped.")

# ====== База данных: ОСТАВЛЯЕМ ТОЛЬКО ПУТЬ К ФАЙЛУ ======
DB_FILE = "bot_data.db"


def init_db():
    """Синхронная функция для инициализации таблиц при старте. Добавлен timeout."""
    log.info("Initializing database tables...")
    # !!! КРИТИЧНОЕ ИЗМЕНЕНИЕ: Добавлен timeout !!!
    conn = sqlite3.connect(DB_FILE, timeout=10) 
    cursor = conn.cursor()
    
    # КОД СОЗДАНИЯ ВСЕХ ВАШИХ ТАБЛИЦ ПЕРЕНЕСЕН СЮДА:
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                client_id INTEGER PRIMARY KEY AUTOINCREMENT,
                control_chat_id INTEGER UNIQUE NOT NULL,  
                target_chat_id INTEGER UNIQUE NOT NULL,  
                name TEXT,
                is_active INTEGER DEFAULT 1
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                control_chat_id INTEGER NOT NULL,  
                source_chat_id INTEGER NOT NULL,   
                keyword TEXT NOT NULL,
                UNIQUE(control_chat_id, source_chat_id, keyword) 
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS negwords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                control_chat_id INTEGER NOT NULL, 
                negword TEXT NOT NULL,
                UNIQUE(control_chat_id, negword)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sources (
                source_chat_id INTEGER NOT NULL,      
                control_chat_id INTEGER NOT NULL,     
                chat_title TEXT,
                PRIMARY KEY (source_chat_id, control_chat_id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS seen_messages (
                msg_key TEXT PRIMARY KEY,
                timestamp INTEGER DEFAULT (strftime('%s', 'now'))
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ai_rules (
                source_chat_id INTEGER NOT NULL,      
                control_chat_id INTEGER NOT NULL,     
                rule TEXT NOT NULL,
                PRIMARY KEY (source_chat_id, control_chat_id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id INTEGER PRIMARY KEY
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                username TEXT
            )
        """)
        # !!! ИЗМЕНЕННАЯ ТАБЛИЦА: Добавлен log_entry !!!
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS forward_reasons (
                target_msg_id INTEGER PRIMARY KEY,
                source_chat_id INTEGER NOT NULL,
                log_entry TEXT,  -- Будет хранить полный лог AI-проверки и причину
                reason TEXT
            )
        """)

        conn.commit()
        log.info("Database tables initialized.")
    except Exception as e:
        log.error(f"FATAL DB ERROR during initialization: {e}")
    finally:
        conn.close()


# ====== Клиент ======
client = TelegramClient("parser_session", API_ID, API_HASH)


# ==============================================================================
# АСИНХРОННЫЕ ОБЕРТКИ ДЛЯ СИНХРОННЫХ ОПЕРАЦИЙ SQLITE
# ==============================================================================

def run_in_executor(func):
    async def wrapper(*args, **kwargs):
        # Используем asyncio.to_thread для запуска синхронных функций в отдельном потоке
        return await asyncio.to_thread(func, *args, **kwargs)
    return wrapper

# ----------------- ФУНКЦИИ ДЛЯ КЛИЕНТОВ -----------------
@run_in_executor
def add_client(control_id, target_id, name=""):
    """Регистрирует нового клиента. Добавлен timeout."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO clients (control_chat_id, target_chat_id, name) VALUES (?, ?, ?)", 
                      (control_id, target_id, name))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    except Exception as e:
        log.error(f"DB ERROR (add_client): {e}")
        return False
    finally:
        conn.close()

@run_in_executor
def get_client_by_control(control_id):
    """Находит данные клиента по его чату команд. Добавлен timeout."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT client_id, control_chat_id, target_chat_id, name FROM clients WHERE control_chat_id = ?", 
                      (control_id,))
        row = cursor.fetchone()
        if row:
            return {'id': row[0], 'control_id': row[1], 'target_id': row[2], 'name': row[3]}
        return None
    except Exception as e:
        log.error(f"DB ERROR (get_client_by_control): {e}")
        return None
    finally:
        conn.close()

@run_in_executor
def is_control_chat(chat_id):
    """Проверяет, является ли чат чатом управления клиента."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM clients WHERE control_chat_id = ?", (chat_id,))
        return cursor.fetchone() is not None
    except Exception as e:
        log.error(f"DB ERROR (is_control_chat): {e}")
        return False
    finally:
        conn.close()

@run_in_executor
def get_clients_monitoring_source(source_id):
    """Находит всех клиентов, которые мониторят данный источник. Добавлен timeout."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT 
                c.client_id, 
                c.control_chat_id, 
                c.target_chat_id,
                c.name 
            FROM clients c
            JOIN sources s ON c.control_chat_id = s.control_chat_id
            WHERE s.source_chat_id = ? AND c.is_active = 1
        """, (source_id,))
        
        return [{'id': row[0], 'control_id': row[1], 'target_id': row[2], 'name': row[3]} for row in cursor.fetchall()]
    except Exception as e:
        log.error(f"DB ERROR (get_clients_monitoring_source): {e}")
        return []
    finally:
        conn.close()


# ----------------- ФУНКЦИИ КЛЮЧЕВЫХ СЛОВ -----------------
@run_in_executor
def get_keywords(control_chat_id, source_chat_id=None):
    """Получает ключевые слова для конкретного клиента. Добавлен timeout."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        # 1. Запрос глобальных слов (source_chat_id = 0)
        query = "SELECT keyword FROM keywords WHERE control_chat_id = ? AND source_chat_id = 0"
        params = [control_chat_id]
        
        # 2. Добавление специфических слов, если source_chat_id указан и не равен 0
        if source_chat_id is not None and source_chat_id != 0:
            query += " UNION SELECT keyword FROM keywords WHERE control_chat_id = ? AND source_chat_id = ?"
            params.extend([control_chat_id, source_chat_id])
            
        cursor.execute(query, params)
        return [row[0].lower() for row in cursor.fetchall()]
    except Exception as e:
        log.error(f"DB ERROR (get_keywords): {e}")
        return []
    finally:
        conn.close()

@run_in_executor
def add_keyword(kw, control_chat_id, source_chat_id=0):
    """Добавляет слово для конкретного клиента. Добавлен timeout."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO keywords (control_chat_id, source_chat_id, keyword) VALUES (?, ?, ?)", 
                      (control_chat_id, source_chat_id, kw.lower()))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    except Exception as e:
        log.error(f"DB ERROR (add_keyword): {e}")
        return False
    finally:
        conn.close()

@run_in_executor
def delete_keyword(kw, control_chat_id, source_chat_id=0):
    """Удаляет слово для конкретного клиента. Добавлен timeout."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM keywords WHERE keyword = ? AND control_chat_id = ? AND source_chat_id = ?", 
                      (kw.lower(), control_chat_id, source_chat_id))
        conn.commit()
        return cursor.rowcount > 0 
    except Exception as e:
        log.error(f"DB ERROR (delete_keyword): {e}")
        return False
    finally:
        conn.close()

# ----------------- ФУНКЦИИ НЕГАТИВНЫХ СЛОВ -----------------
@run_in_executor
def get_negwords(control_chat_id):
    """Получает негативные слова для конкретного клиента. Добавлен timeout."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT negword FROM negwords WHERE control_chat_id = ?", (control_chat_id,))
        return [row[0].lower() for row in cursor.fetchall()]
    except Exception as e:
        log.error(f"DB ERROR (get_negwords): {e}")
        return []
    finally:
        conn.close()

@run_in_executor
def add_negword(nw, control_chat_id):
    """Добавляет негативное слово для конкретного клиента. Добавлен timeout."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO negwords (control_chat_id, negword) VALUES (?, ?)", (control_chat_id, nw.lower()))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    except Exception as e:
        log.error(f"DB ERROR (add_negword): {e}")
        return False
    finally:
        conn.close()

@run_in_executor
def delete_negword(nw, control_chat_id):
    """Удаляет негативное слово для конкретного клиента. Добавлен timeout."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM negwords WHERE negword = ? AND control_chat_id = ?", (nw.lower(), control_chat_id))
        conn.commit()
        return cursor.rowcount > 0 
    except Exception as e:
        log.error(f"DB ERROR (delete_negword): {e}")
        return False
    finally:
        conn.close()

# ----------------- ФУНКЦИИ ИСТОЧНИКОВ -----------------
@run_in_executor
def list_sources(control_chat_id):
    """Получает список источников, которые мониторит конкретный клиент. Добавлен timeout."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT source_chat_id, chat_title FROM sources WHERE control_chat_id = ?", (control_chat_id,))
        return cursor.fetchall()
    except Exception as e:
        log.error(f"DB ERROR (list_sources): {e}")
        return []
    finally:
        conn.close()

@run_in_executor
def add_source(source_chat_id, control_chat_id, chat_title):
    """Добавляет источник для конкретного клиента. Добавлен timeout."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT OR REPLACE INTO sources (source_chat_id, control_chat_id, chat_title) VALUES (?, ?, ?)", 
                      (source_chat_id, control_chat_id, chat_title))
        conn.commit()
        return True
    except Exception as e:
        log.error(f"DB ERROR (add_source): {e}")
        return False
    finally:
        conn.close()

@run_in_executor
def delete_source(source_chat_id, control_chat_id):
    """Удаляет источник для конкретного клиента. Добавлен timeout."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM sources WHERE source_chat_id = ? AND control_chat_id = ?", (source_chat_id, control_chat_id))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        log.error(f"DB ERROR (delete_source): {e}")
        return False
    finally:
        conn.close()

# ----------------- ФУНКЦИИ AI ПРАВИЛ -----------------
@run_in_executor
def get_ai_rule(source_chat_id, control_chat_id):
    """Получает AI правило: сначала специфическое, потом глобальное (source_id = 0). Добавлен timeout."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        # 1. Поиск специфического правила
        cursor.execute("SELECT rule FROM ai_rules WHERE source_chat_id = ? AND control_chat_id = ?", 
                      (source_chat_id, control_chat_id))
        row = cursor.fetchone()
        if row:
            return row[0]
            
        # 2. Поиск ГЛОБАЛЬНОГО правила
        cursor.execute("SELECT rule FROM ai_rules WHERE source_chat_id = 0 AND control_chat_id = ?", 
                      (control_chat_id,))
        row = cursor.fetchone()
        return row[0] if row else None
        
    except Exception as e:
        log.error(f"DB ERROR (get_ai_rule): {e}")
        return None
    finally:
        conn.close()

@run_in_executor
def set_ai_rule(source_chat_id, control_chat_id, rule):
    """Устанавливает AI правило для конкретного источника или глобально (0). Добавлен timeout."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT OR REPLACE INTO ai_rules (source_chat_id, control_chat_id, rule) VALUES (?, ?, ?)", 
                      (source_chat_id, control_chat_id, rule))
        conn.commit()
    except Exception as e:
        log.error(f"DB ERROR (set_ai_rule): {e}")
    finally:
        conn.close()

@run_in_executor
def clear_ai_rule(source_chat_id, control_chat_id):
    """Удаляет AI правило для конкретного источника или глобально (0). Добавлен timeout."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM ai_rules WHERE source_chat_id = ? AND control_chat_id = ?", 
                      (source_chat_id, control_chat_id))
        conn.commit()
    except Exception as e:
        log.error(f"DB ERROR (clear_ai_rule): {e}")
    finally:
        conn.close()

# ----------------- ГЛОБАЛЬНЫЕ ФУНКЦИИ БЕЗ ПРИВЯЗКИ К КЛИЕНТУ -----------------
@run_in_executor
def is_seen(msg_key):
    """Проверяет, было ли сообщение увидено. Добавлен timeout."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM seen_messages WHERE msg_key = ?", (msg_key,))
        return cursor.fetchone() is not None
    except Exception as e:
        log.error(f"DB ERROR (is_seen): {e}")
        return False
    finally:
        conn.close()

@run_in_executor
def mark_seen(msg_key):
    """Помечает сообщение как увиденное. Добавлен timeout."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT OR IGNORE INTO seen_messages (msg_key) VALUES (?)", (msg_key,))
        conn.commit()
    except Exception as e:
        log.error(f"DB ERROR (mark_seen): {e}")
    finally:
        conn.close()

@run_in_executor
def is_admin(user_id):
    """Проверяет, является ли пользователь администратором. Добавлен timeout."""
    if user_id == ADMIN_USER_ID and ADMIN_USER_ID != 0:
        return True 
        
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
        return cursor.fetchone() is not None
    except Exception as e:
        log.error(f"DB ERROR (is_admin): {e}")
        return False
    finally:
        conn.close()

@run_in_executor
def add_admin(user_id, username):
    """Добавляет пользователя в список администраторов. Добавлен timeout."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT OR IGNORE INTO admins (user_id, username) VALUES (?, ?)", 
                      (user_id, username))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        log.error(f"DB ERROR (add_admin): {e}")
        return False
    finally:
        conn.close()

@run_in_executor
def remove_admin(user_id):
    """Удаляет пользователя из списка администраторов. Добавлен timeout."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        log.error(f"DB ERROR (remove_admin): {e}")
        return False
    finally:
        conn.close()

@run_in_executor
def list_admins():
    """Возвращает список всех администраторов. Добавлен timeout."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT user_id, username FROM admins")
        return cursor.fetchall()
    except Exception as e:
        log.error(f"DB ERROR (list_admins): {e}")
        return []
    finally:
        conn.close()

@run_in_executor
def is_banned(user_id):
    """Проверяет, забанен ли пользователь. Добавлен timeout."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,))
        return cursor.fetchone() is not None
    except Exception as e:
        log.error(f"DB ERROR (is_banned): {e}")
        return False
    finally:
        conn.close()

@run_in_executor
def ban_user(user_id):
    """Банит пользователя. Добавлен timeout."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT OR IGNORE INTO banned_users (user_id) VALUES (?)", (user_id,))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        log.error(f"DB ERROR (ban_user): {e}")
        return False
    finally:
        conn.close()

@run_in_executor
def unban_user(user_id):
    """Разбанивает пользователя. Добавлен timeout."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        log.error(f"DB ERROR (unban_user): {e}")
        return False
    finally:
        conn.close()

@run_in_executor
def list_banned_users():
    """Возвращает список забаненных. Добавлен timeout."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT user_id FROM banned_users")
        return [row[0] for row in cursor.fetchall()]
    except Exception as e:
        log.error(f"DB ERROR (list_banned_users): {e}")
        return []
    finally:
        conn.close()

# ----------------- ФУНКЦИИ ЛОГИРОВАНИЯ ДЛЯ /почему и /бан -----------------
@run_in_executor
def store_forward_log(target_msg_id, source_chat_id, ai_log, reason):
    """Сохраняет ID пересланного сообщения, ID источника, лог AI и причину."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT OR REPLACE INTO forward_reasons (target_msg_id, source_chat_id, log_entry, reason) 
            VALUES (?, ?, ?, ?)""", 
            (target_msg_id, source_chat_id, ai_log, reason)
        )
        conn.commit()
    except Exception as e:
        log.error(f"DB ERROR (store_forward_log): {e}")
    finally:
        conn.close()

@run_in_executor
def get_source_data_by_forwarded_id(target_msg_id):
    """Получает source_chat_id и лог AI по ID сообщения в control_chat."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT source_chat_id, log_entry FROM forward_reasons WHERE target_msg_id = ?", (target_msg_id,))
        row = cursor.fetchone()
        if row:
            # log_entry - это весь лог, который мы записали (включая причину KW)
            return {'source_id': row[0], 'log_entry': row[1]} 
        return None
    except Exception as e:
        log.error(f"DB ERROR (get_source_data_by_forwarded_id): {e}")
        return None
    finally:
        conn.close()


# ----------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ -----------------
def get_display_name(entity):
    if hasattr(entity, 'title'):
        return entity.title
    name = getattr(entity, 'first_name', '') or ''
    last = getattr(entity, 'last_name', '') or ''
    return f"{name} {last}".strip() or str(entity.id)

async def get_chat_title(chat_id):
    """Получает название чата по ID."""
    try:
        if chat_id == 0:
            return "Глобально"
            
        entity = await client.get_entity(chat_id)
        return get_display_name(entity)
    except Exception:
        return str(chat_id)

async def get_sender_info(sender_id):
    """Получает username и ID отправителя."""
    try:
        sender = await client.get_entity(sender_id)
        return {
            'username': getattr(sender, 'username', None),
            'id': sender.id
        }
    except Exception:
        return {
            'username': None,
            'id': sender_id
        }
# ------------------------------------------


# ====== AI фильтр ======
async def ai_filter(text, source_chat_id, control_chat_id):
    """
    Отправляет текст сообщения и правило в OpenAI для проверки.
    """
    if not OPENAI_API_KEY:
        return True, "SKIPPED (OPENAI_API_KEY not set)"

    # Используем обновленную функцию, которая ищет глобальное правило, если нет специфического
    rule = await get_ai_rule(source_chat_id, control_chat_id) 
    if not rule:
        return True, "SKIPPED (No AI rule set for this source or globally by client)"
    
    try:
        
        system_prompt = (
            f"Ты - строгий фильтр контента. Твоя задача - определить, соответствует ли сообщение следующим правилам: "
            f"**Правило:** '{rule}' "
            "Ты должен ответить только одним словом: 'ДА' или 'НЕТ'. "
            "'ДА' означает, что сообщение СОВЕРШЕННО соответствует правилу и его нужно переслать. "
            "'НЕТ' означает, что сообщение НЕ соответствует правилу."
        )
        
        response = await openai.ChatCompletion.acreate( 
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Проверь сообщение: '{text}'"},
            ],
            temperature=0.0,
            max_tokens=3 
        )
        
        verdict = response.choices[0].message.content.strip().upper()
        
        if "ДА" in verdict:
            return True, f"AI VERDICT: Passed (ДА). Rule: {rule}"
        else:
            return False, f"AI VERDICT: Failed (НЕТ). Rule: {rule}"

    except Exception as e:
        log.error(f"OpenAI API Error (acreate) for client {control_chat_id}: {e}")
        return True, f"ERROR (AI API FAILED): {e}"


# ---

# ====== БЫСТРЫЕ КОМАНДЫ 'бан' и 'почему' ПО ОТВЕТУ (НОВЫЙ ОБРАБОТЧИК) ======
# Этот обработчик проверяет все чаты, которые являются 'control_chat'
@client.on(events.NewMessage()) 
async def on_quick_action(evt: events.NewMessage.Event):
    # 1. Проверяем, что это чат управления клиента
    is_control = await is_control_chat(evt.chat_id)
    if not is_control:
         return
    
    # 2. Проверяем, что это ответ на сообщение
    if not evt.reply_to_msg_id:
        return

    # 3. Проверяем, что сообщение - это просто 'бан' или 'почему'
    text_lower = (evt.message.message or "").strip().lower()
    
    if text_lower not in ['бан', 'почему']:
        return

    # 4. Проверяем, является ли отправитель администратором (Ваше требование)
    if not await is_admin(evt.sender_id):
        log.warning(f"Quick command attempt by non-admin: {evt.sender_id} in {evt.chat_id}")
        return
        
    # Получаем данные клиента
    client_data = await get_client_by_control(evt.chat_id)
    if not client_data: return 

    control_chat_id = client_data['control_id']
    
    # Получаем пересланное сообщение, на которое был дан ответ
    try:
        replied_msg = await client.get_messages(evt.chat_id, ids=evt.reply_to_msg_id)
        if not replied_msg: return
    except Exception as e:
        log.error(f"Error getting replied message for quick action: {e}")
        return

    # 5. Обрабатываем команду 'почему'
    if text_lower == 'почему':
        log_data = await get_source_data_by_forwarded_id(replied_msg.id) 
            
        if log_data:
            await evt.reply(f"**🤖 АНАЛИЗ AI (Лог):**\n`{log_data['log_entry']}`", parse_mode='md')
            log.info(f"CMD SUCCESS: Quick 'почему' for client {control_chat_id}")
        else:
            await evt.reply("⚠️ Не удалось найти запись AI-анализа для этого сообщения. Убедитесь, что это сообщение, пересланное ботом.")
            log.warning(f"CMD FAILED: Quick 'почему' failed to find log for msg {replied_msg.id}")

    # 6. Обрабатываем команду 'бан'
    elif text_lower == 'бан':
        ban_data = await get_source_data_by_forwarded_id(replied_msg.id)
        source_chat_id_to_ban = ban_data['source_id'] if ban_data else None
            
        if source_chat_id_to_ban and source_chat_id_to_ban != 0:
            if await delete_source(source_chat_id_to_ban, control_chat_id):
                chat_name = await get_chat_title(source_chat_id_to_ban)
                await evt.reply(f"✅ Чат **{chat_name}** (ID: `{source_chat_id_to_ban}`) успешно удален из вашего мониторинга (бан).", parse_mode='md')
                log.info(f"CMD SUCCESS: Quick 'бан' deleted source {source_chat_id_to_ban} for client {control_chat_id}")
            else:
                 await evt.reply(f"⚠️ Чат ID `{source_chat_id_to_ban}` уже был удален или не найден в списке источников.")
                 log.warning(f"CMD FAILED: Quick 'бан' source {source_chat_id_to_ban} not found/already deleted.")
        elif source_chat_id_to_ban == 0:
            await evt.reply("⚠️ Нельзя забанить источник 'Глобально' через ответ на сообщение.", parse_mode='md')
        else:
            await evt.reply("⚠️ Не удалось найти исходный чат для бана по этому сообщению. Возможно, запись устарела.")

    await asyncio.sleep(0.5)

# ---


# ====== ОБРАБОТЧИК ДЛЯ КОМАНД (ТОЛЬКО КОМАНДЫ СО СЛЕШЕМ /) ======
@client.on(events.NewMessage(pattern=r'^/'))
async def on_command(evt: events.NewMessage.Event):
    
    # 1. Проверяем, главный ли это администратор (await!)
    is_main_admin = await is_admin(evt.sender_id)
    
    # 2. Идентифицируем клиента по чату команд (await!)
    client_data = await get_client_by_control(evt.chat_id)
    
    # Если это не главный админ, и это не зарегистрированный чат клиента, то игнорируем
    if not is_main_admin and not client_data:
        return
        
    # Определяем, какой control_chat_id будем использовать
    if client_data:
        # Если команда пришла от зарегистрированного клиента
        control_chat_id = client_data['control_id']
        target_chat_id = client_data['target_id']
        is_client_command = True
    elif is_main_admin:
        # Если команда от главного админа, но не из зарегистрированного чата, используем его CONTROL_CHAT_ID
        control_chat_id = CONTROL_CHAT_ID
        target_chat_id = TARGET_CHAT_ID
        is_client_command = False
    else:
        return 

    import asyncio
import logging
import sqlite3
import os
import re 
from dotenv import load_dotenv
from telethon import TelegramClient, events
import openai 
from telethon.utils import get_peer_id
from telethon.tl.types import User, Channel, Chat 
import json

# Загрузка переменных окружения из .env файла
load_dotenv()

# ====== Настройки: Сначала получаем все переменные окружения! ======
API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
PHONE = os.getenv("TG_PHONE", "")
# Используйте CONTROL_CHAT_ID и TARGET_CHAT_ID только как запасные или для главного админа
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "0"))
CONTROL_CHAT_ID = int(os.getenv("CONTROL_CHAT_ID", "0"))
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

# OpenAI API
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# ==============================================================================
# НАСТРОЙКА ЛОГИРОВАНИЯ
# ==============================================================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ==============================================================================
# ИНИЦИАЛИЗАЦИЯ OpenAI
# ==============================================================================
if OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY
    log.info("✓ OpenAI API Key set. AI filtering is active.")
else:
    log.warning("⚠️ OPENAI_API_KEY not found. AI filtering will be skipped.")

# ====== База данных: ОСТАВЛЯЕМ ТОЛЬКО ПУТЬ К ФАЙЛУ ======
DB_FILE = "bot_data.db"


def init_db():
    """Синхронная функция для инициализации таблиц при старте. Добавлен timeout."""
    log.info("Initializing database tables...")
    # !!! КРИТИЧНОЕ ИЗМЕНЕНИЕ: Добавлен timeout и контекстный менеджер !!!
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn: 
            cursor = conn.cursor()
            
            # КОД СОЗДАНИЯ ВСЕХ ВАШИХ ТАБЛИЦ ПЕРЕНЕСЕН СЮДА:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS clients (
                    client_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    control_chat_id INTEGER UNIQUE NOT NULL,  
                    target_chat_id INTEGER UNIQUE NOT NULL,  
                    name TEXT,
                    is_active INTEGER DEFAULT 1
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS keywords (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    control_chat_id INTEGER NOT NULL,  
                    source_chat_id INTEGER NOT NULL,   
                    keyword TEXT NOT NULL,
                    UNIQUE(control_chat_id, source_chat_id, keyword) 
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS negwords (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    control_chat_id INTEGER NOT NULL, 
                    negword TEXT NOT NULL,
                    UNIQUE(control_chat_id, negword)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sources (
                    source_chat_id INTEGER NOT NULL,      
                    control_chat_id INTEGER NOT NULL,     
                    chat_title TEXT,
                    PRIMARY KEY (source_chat_id, control_chat_id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS seen_messages (
                    msg_key TEXT PRIMARY KEY,
                    timestamp INTEGER DEFAULT (strftime('%s', 'now'))
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ai_rules (
                    source_chat_id INTEGER NOT NULL,      
                    control_chat_id INTEGER NOT NULL,     
                    rule TEXT NOT NULL,
                    PRIMARY KEY (source_chat_id, control_chat_id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS banned_users (
                    user_id INTEGER PRIMARY KEY
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS admins (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT
                )
            """)
            # !!! ИЗМЕНЕННАЯ ТАБЛИЦА: Добавлен log_entry !!!
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS forward_reasons (
                    target_msg_id INTEGER PRIMARY KEY,
                    source_chat_id INTEGER NOT NULL,
                    control_chat_id INTEGER NOT NULL,  
                    log_entry TEXT,  -- Будет хранить полный лог AI-проверки и причину
                    reason TEXT
                )
            """)

            conn.commit()
            log.info("Database tables initialized.")
    except Exception as e:
        log.error(f"FATAL DB ERROR during initialization: {e}")


# ====== Клиент ======
client = TelegramClient("parser_session", API_ID, API_HASH)


# ==============================================================================
# АСИНХРОННЫЕ ОБЕРТКИ ДЛЯ СИНХРОННЫХ ОПЕРАЦИЙ SQLITE
# ==============================================================================

def run_in_executor(func):
    async def wrapper(*args, **kwargs):
        # Используем asyncio.to_thread для запуска синхронных функций в отдельном потоке
        return await asyncio.to_thread(func, *args, **kwargs)
    return wrapper

# ----------------- ФУНКЦИИ ДЛЯ КЛИЕНТОВ -----------------
@run_in_executor
def add_client(control_id, target_id, name=""):
    """Регистрирует нового клиента. Добавлен timeout."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO clients (control_chat_id, target_chat_id, name) VALUES (?, ?, ?)", 
                          (control_id, target_id, name))
            conn.commit()
            return True
    except sqlite3.IntegrityError:
        return False
    except Exception as e:
        log.error(f"DB ERROR (add_client): {e}")
        return False

@run_in_executor
def get_client_by_control(control_id):
    """Находит данные клиента по его чату команд. Добавлен timeout."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT client_id, control_chat_id, target_chat_id, name FROM clients WHERE control_chat_id = ?", 
                          (control_id,))
            row = cursor.fetchone()
            if row:
                return {'id': row[0], 'control_id': row[1], 'target_id': row[2], 'name': row[3]}
            return None
    except Exception as e:
        log.error(f"DB ERROR (get_client_by_control): {e}")
        return None

# **# ИСПРАВЛЕНИЕ: Новая функция для поиска клиента по target_chat_id**
@run_in_executor
def get_client_by_target(target_id):
    """Находит данные клиента по его чату пересылки."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT client_id, control_chat_id, target_chat_id, name FROM clients WHERE target_chat_id = ?", 
                          (target_id,))
            row = cursor.fetchone()
            if row:
                return {'id': row[0], 'control_id': row[1], 'target_id': row[2], 'name': row[3]}
            return None
    except Exception as e:
        log.error(f"DB ERROR (get_client_by_target): {e}")
        return None

# **# ИСПРАВЛЕНИЕ: Функция для проверки, является ли чат контрольным или целевым**
@run_in_executor
def get_client_role_by_chat_id(chat_id):
    """
    Проверяет, является ли чат чатом управления или чатом пересылки клиента.
    Возвращает: 'control' или 'target' или None.
    """
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT control_chat_id FROM clients WHERE control_chat_id = ?", (chat_id,))
            if cursor.fetchone() is not None:
                return 'control'
            
            cursor.execute("SELECT target_chat_id FROM clients WHERE target_chat_id = ?", (chat_id,))
            if cursor.fetchone() is not None:
                return 'target'
            
            return None
    except Exception as e:
        log.error(f"DB ERROR (get_client_role_by_chat_id): {e}")
        return None

@run_in_executor
def get_clients_monitoring_source(source_id):
    """Находит всех клиентов, которые мониторят данный источник. Добавлен timeout."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 
                    c.client_id, 
                    c.control_chat_id, 
                    c.target_chat_id,
                    c.name 
                FROM clients c
                JOIN sources s ON c.control_chat_id = s.control_chat_id
                WHERE s.source_chat_id = ? AND c.is_active = 1
            """, (source_id,))
            
            return [{'id': row[0], 'control_id': row[1], 'target_id': row[2], 'name': row[3]} for row in cursor.fetchall()]
    except Exception as e:
        log.error(f"DB ERROR (get_clients_monitoring_source): {e}")
        return []


# ----------------- ФУНКЦИИ КЛЮЧЕВЫХ СЛОВ -----------------
@run_in_executor
def get_keywords(control_chat_id, source_chat_id=None):
    """Получает ключевые слова для конкретного клиента. Добавлен timeout."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            # 1. Запрос глобальных слов (source_chat_id = 0)
            query = "SELECT keyword FROM keywords WHERE control_chat_id = ? AND source_chat_id = 0"
            params = [control_chat_id]
            
            # 2. Добавление специфических слов, если source_chat_id указан и не равен 0
            if source_chat_id is not None and source_chat_id != 0:
                query += " UNION SELECT keyword FROM keywords WHERE control_chat_id = ? AND source_chat_id = ?"
                params.extend([control_chat_id, source_chat_id])
                
            cursor.execute(query, params)
            return [row[0].lower() for row in cursor.fetchall()]
    except Exception as e:
        log.error(f"DB ERROR (get_keywords): {e}")
        return []

@run_in_executor
def add_keyword(kw, control_chat_id, source_chat_id=0):
    """Добавляет слово для конкретного клиента. Добавлен timeout."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO keywords (control_chat_id, source_chat_id, keyword) VALUES (?, ?, ?)", 
                          (control_chat_id, source_chat_id, kw.lower()))
            conn.commit()
            return True
    except sqlite3.IntegrityError:
        return False
    except Exception as e:
        log.error(f"DB ERROR (add_keyword): {e}")
        return False

@run_in_executor
def delete_keyword(kw, control_chat_id, source_chat_id=0):
    """Удаляет слово для конкретного клиента. Добавлен timeout."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM keywords WHERE keyword = ? AND control_chat_id = ? AND source_chat_id = ?", 
                          (kw.lower(), control_chat_id, source_chat_id))
            conn.commit()
            return cursor.rowcount > 0 
    except Exception as e:
        log.error(f"DB ERROR (delete_keyword): {e}")
        return False

# ----------------- ФУНКЦИИ НЕГАТИВНЫХ СЛОВ -----------------
@run_in_executor
def get_negwords(control_chat_id):
    """Получает негативные слова для конкретного клиента. Добавлен timeout."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT negword FROM negwords WHERE control_chat_id = ?", (control_chat_id,))
            return [row[0].lower() for row in cursor.fetchall()]
    except Exception as e:
        log.error(f"DB ERROR (get_negwords): {e}")
        return []

@run_in_executor
def add_negword(nw, control_chat_id):
    """Добавляет негативное слово для конкретного клиента. Добавлен timeout."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO negwords (control_chat_id, negword) VALUES (?, ?)", (control_chat_id, nw.lower()))
            conn.commit()
            return True
    except sqlite3.IntegrityError:
        return False
    except Exception as e:
        log.error(f"DB ERROR (add_negword): {e}")
        return False

@run_in_executor
def delete_negword(nw, control_chat_id):
    """Удаляет негативное слово для конкретного клиента. Добавлен timeout."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM negwords WHERE negword = ? AND control_chat_id = ?", (nw.lower(), control_chat_id))
            conn.commit()
            return cursor.rowcount > 0 
    except Exception as e:
        log.error(f"DB ERROR (delete_negword): {e}")
        return False

# ----------------- ФУНКЦИИ ИСТОЧНИКОВ -----------------
@run_in_executor
def list_sources(control_chat_id):
    """Получает список источников, которые мониторит конкретный клиент. Добавлен timeout."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT source_chat_id, chat_title FROM sources WHERE control_chat_id = ?", (control_chat_id,))
            return cursor.fetchall()
    except Exception as e:
        log.error(f"DB ERROR (list_sources): {e}")
        return []

@run_in_executor
def add_source(source_chat_id, control_chat_id, chat_title):
    """Добавляет источник для конкретного клиента. Добавлен timeout."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO sources (source_chat_id, control_chat_id, chat_title) VALUES (?, ?, ?)", 
                          (source_chat_id, control_chat_id, chat_title))
            conn.commit()
            return True
    except Exception as e:
        log.error(f"DB ERROR (add_source): {e}")
        return False

@run_in_executor
def delete_source(source_chat_id, control_chat_id):
    """Удаляет источник для конкретного клиента. Добавлен timeout."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM sources WHERE source_chat_id = ? AND control_chat_id = ?", (source_chat_id, control_chat_id))
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        log.error(f"DB ERROR (delete_source): {e}")
        return False

# ----------------- ФУНКЦИИ AI ПРАВИЛ -----------------
@run_in_executor
def get_ai_rule(source_chat_id, control_chat_id):
    """Получает AI правило: сначала специфическое, потом глобальное (source_id = 0). Добавлен timeout."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            # 1. Поиск специфического правила
            cursor.execute("SELECT rule FROM ai_rules WHERE source_chat_id = ? AND control_chat_id = ?", 
                          (source_chat_id, control_chat_id))
            row = cursor.fetchone()
            if row:
                return row[0]
                
            # 2. Поиск ГЛОБАЛЬНОГО правила
            cursor.execute("SELECT rule FROM ai_rules WHERE source_chat_id = 0 AND control_chat_id = ?", 
                          (control_chat_id,))
            row = cursor.fetchone()
            return row[0] if row else None
            
    except Exception as e:
        log.error(f"DB ERROR (get_ai_rule): {e}")
        return None

@run_in_executor
def set_ai_rule(source_chat_id, control_chat_id, rule):
    """Устанавливает AI правило для конкретного источника или глобально (0). Добавлен timeout."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO ai_rules (source_chat_id, control_chat_id, rule) VALUES (?, ?, ?)", 
                          (source_chat_id, control_chat_id, rule))
            conn.commit()
    except Exception as e:
        log.error(f"DB ERROR (set_ai_rule): {e}")

@run_in_executor
def clear_ai_rule(source_chat_id, control_chat_id):
    """Удаляет AI правило для конкретного источника или глобально (0). Добавлен timeout."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM ai_rules WHERE source_chat_id = ? AND control_chat_id = ?", 
                          (source_chat_id, control_chat_id))
            conn.commit()
    except Exception as e:
        log.error(f"DB ERROR (clear_ai_rule): {e}")

# ----------------- ГЛОБАЛЬНЫЕ ФУНКЦИИ БЕЗ ПРИВЯЗКИ К КЛИЕНТУ -----------------
@run_in_executor
def is_seen(msg_key):
    """Проверяет, было ли сообщение увидено. Добавлен timeout."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM seen_messages WHERE msg_key = ?", (msg_key,))
            return cursor.fetchone() is not None
    except Exception as e:
        log.error(f"DB ERROR (is_seen): {e}")
        return False

@run_in_executor
def mark_seen(msg_key):
    """Помечает сообщение как увиденное. Добавлен timeout."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO seen_messages (msg_key) VALUES (?)", (msg_key,))
            conn.commit()
    except Exception as e:
        log.error(f"DB ERROR (mark_seen): {e}")

@run_in_executor
def is_admin(user_id):
    """Проверяет, является ли пользователь администратором. Добавлен timeout."""
    if user_id == ADMIN_USER_ID and ADMIN_USER_ID != 0:
        return True 
        
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
            return cursor.fetchone() is not None
    except Exception as e:
        log.error(f"DB ERROR (is_admin): {e}")
        return False

@run_in_executor
def add_admin(user_id, username):
    """Добавляет пользователя в список администраторов. Добавлен timeout."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO admins (user_id, username) VALUES (?, ?)", 
                          (user_id, username))
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        log.error(f"DB ERROR (add_admin): {e}")
        return False

@run_in_executor
def remove_admin(user_id):
    """Удаляет пользователя из списка администраторов. Добавлен timeout."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        log.error(f"DB ERROR (remove_admin): {e}")
        return False

@run_in_executor
def list_admins():
    """Возвращает список всех администраторов. Добавлен timeout."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, username FROM admins")
            return cursor.fetchall()
    except Exception as e:
        log.error(f"DB ERROR (list_admins): {e}")
        return []

@run_in_executor
def is_banned(user_id):
    """Проверяет, забанен ли пользователь. Добавлен timeout."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,))
            return cursor.fetchone() is not None
    except Exception as e:
        log.error(f"DB ERROR (is_banned): {e}")
        return False

@run_in_executor
def ban_user(user_id):
    """Банит пользователя. Добавлен timeout."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO banned_users (user_id) VALUES (?)", (user_id,))
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        log.error(f"DB ERROR (ban_user): {e}")
        return False

@run_in_executor
def unban_user(user_id):
    """Разбанивает пользователя. Добавлен timeout."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        log.error(f"DB ERROR (unban_user): {e}")
        return False

@run_in_executor
def list_banned_users():
    """Возвращает список забаненных. Добавлен timeout."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM banned_users")
            return [row[0] for row in cursor.fetchall()]
    except Exception as e:
        log.error(f"DB ERROR (list_banned_users): {e}")
        return []

# ----------------- ФУНКЦИИ ЛОГИРОВАНИЯ ДЛЯ /почему и /бан -----------------
# **# ИСПРАВЛЕНИЕ: Добавлен control_chat_id в store_forward_log**
@run_in_executor
def store_forward_log(target_msg_id, source_chat_id, control_chat_id, ai_log, reason):
    """Сохраняет ID пересланного сообщения, ID источника, control_chat_id, лог AI и причину."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO forward_reasons (target_msg_id, source_chat_id, control_chat_id, log_entry, reason) 
                VALUES (?, ?, ?, ?, ?)""", 
                (target_msg_id, source_chat_id, control_chat_id, ai_log, reason)
            )
            conn.commit()
    except Exception as e:
        log.error(f"DB ERROR (store_forward_log): {e}")

@run_in_executor
def get_source_data_by_forwarded_id(target_msg_id):
    """Получает source_chat_id, control_chat_id и лог AI по ID сообщения в control_chat."""
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            cursor = conn.cursor()
            # **# ИСПРАВЛЕНИЕ: Добавлен control_chat_id в выборку**
            cursor.execute("SELECT source_chat_id, control_chat_id, log_entry FROM forward_reasons WHERE target_msg_id = ?", (target_msg_id,))
            row = cursor.fetchone()
            if row:
                # log_entry - это весь лог, который мы записали (включая причину KW)
                return {'source_id': row[0], 'control_id': row[1], 'log_entry': row[2]} 
            return None
    except Exception as e:
        log.error(f"DB ERROR (get_source_data_by_forwarded_id): {e}")
        return None
# --------------------------------------------------------------------------


# ----------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ -----------------
def get_display_name(entity):
    if hasattr(entity, 'title'):
        return entity.title
    name = getattr(entity, 'first_name', '') or ''
    last = getattr(entity, 'last_name', '') or ''
    return f"{name} {last}".strip() or str(entity.id)

async def get_chat_title(chat_id):
    """Получает название чата по ID."""
    try:
        if chat_id == 0:
            return "Глобально"
            
        entity = await client.get_entity(chat_id)
        return get_display_name(entity)
    except Exception:
        return str(chat_id)

async def get_sender_info(sender_id):
    """Получает username и ID отправителя."""
    try:
        sender = await client.get_entity(sender_id)
        return {
            'username': getattr(sender, 'username', None),
            'id': sender.id
        }
    except Exception:
        return {
            'username': None,
            'id': sender_id
        }
# ------------------------------------------


# ====== AI фильтр ======
async def ai_filter(text, source_chat_id, control_chat_id):
    """
    Отправляет текст сообщения и правило в OpenAI для проверки.
    """
    if not OPENAI_API_KEY:
        return True, "SKIPPED (OPENAI_API_KEY not set)"

    # Используем обновленную функцию, которая ищет глобальное правило, если нет специфического
    rule = await get_ai_rule(source_chat_id, control_chat_id) 
    if not rule:
        return True, "SKIPPED (No AI rule set for this source or globally by client)"
    
    try:
        
        system_prompt = (
            f"Ты - строгий фильтр контента. Твоя задача - определить, соответствует ли сообщение следующим правилам: "
            f"**Правило:** '{rule}' "
            "Ты должен ответить только одним словом: 'ДА' или 'НЕТ'. "
            "'ДА' означает, что сообщение СОВЕРШЕННО соответствует правилу и его нужно переслать. "
            "'НЕТ' означает, что сообщение НЕ соответствует правилу."
        )
        
        response = await openai.ChatCompletion.acreate( 
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Проверь сообщение: '{text}'"},
            ],
            temperature=0.0,
            max_tokens=3 
        )
        
        verdict = response.choices[0].message.content.strip().upper()
        
        if "ДА" in verdict:
            return True, f"AI VERDICT: Passed (ДА). Rule: {rule}"
        else:
            return False, f"AI VERDICT: Failed (НЕТ). Rule: {rule}"

    except Exception as e:
        log.error(f"OpenAI API Error (acreate) for client {control_chat_id}: {e}")
        return True, f"ERROR (AI API FAILED): {e}"


# ---

# ====== БЫСТРЫЕ КОМАНДЫ 'бан' и 'почему' ПО ОТВЕТУ (НОВЫЙ ОБРАБОТЧИК) ======
# **# ИСПРАВЛЕНИЕ: Теперь проверяет, является ли чат контрольным ИЛИ целевым**
@client.on(events.NewMessage()) 
async def on_quick_action(evt: events.NewMessage.Event):
    chat_id = evt.chat_id
    
    # 1. Проверяем роль чата
    chat_role = await get_client_role_by_chat_id(chat_id)
    if not chat_role:
         return
    
    # 2. Проверяем, что это ответ на сообщение
    if not evt.reply_to_msg_id:
        return

    # 3. Проверяем, что сообщение - это просто 'бан' или 'почему'
    text_lower = (evt.message.message or "").strip().lower()
    
    if text_lower not in ['бан', 'почему']:
        return

    # 4. Проверяем, является ли отправитель администратором
    if not await is_admin(evt.sender_id):
        log.warning(f"Quick command attempt by non-admin: {evt.sender_id} in {chat_id}")
        return
        
    # **# ИСПРАВЛЕНИЕ: Определяем control_chat_id клиента**
    if chat_role == 'control':
        client_data = await get_client_by_control(chat_id)
    else: # chat_role == 'target'
        client_data = await get_client_by_target(chat_id)
    
    if not client_data: return 
    control_chat_id = client_data['control_id']
    
    # Получаем пересланное сообщение, на которое был дан ответ
    try:
        replied_msg = await client.get_messages(chat_id, ids=evt.reply_to_msg_id)
        if not replied_msg: return
    except Exception as e:
        log.error(f"Error getting replied message for quick action: {e}")
        return

    # Получаем данные о пересланном сообщении
    log_data = await get_source_data_by_forwarded_id(replied_msg.id) 
        
    # 5. Обрабатываем команду 'почему'
    if text_lower == 'почему':
        if log_data and log_data['control_id'] == control_chat_id: # Проверка, что лог относится к этому клиенту
            await evt.reply(f"**🤖 АНАЛИЗ AI (Лог):**\n`{log_data['log_entry']}`", parse_mode='md')
            log.info(f"CMD SUCCESS: Quick 'почему' for client {control_chat_id}")
        else:
            await evt.reply("⚠️ Не удалось найти запись AI-анализа для этого сообщения или оно переслано не для вас. Убедитесь, что это сообщение, пересланное ботом.")
            log.warning(f"CMD FAILED: Quick 'почему' failed to find log for msg {replied_msg.id}")

    # 6. Обрабатываем команду 'бан'
    elif text_lower == 'бан':
        if log_data and log_data['control_id'] == control_chat_id:
            source_chat_id_to_ban = log_data['source_id']
                
            if source_chat_id_to_ban and source_chat_id_to_ban != 0:
                if await delete_source(source_chat_id_to_ban, control_chat_id):
                    chat_name = await get_chat_title(source_chat_id_to_ban)
                    await evt.reply(f"✅ Чат **{chat_name}** (ID: `{source_chat_id_to_ban}`) успешно удален из вашего мониторинга (бан).", parse_mode='md')
                    log.info(f"CMD SUCCESS: Quick 'бан' deleted source {source_chat_id_to_ban} for client {control_chat_id}")
                else:
                    await evt.reply(f"⚠️ Чат ID `{source_chat_id_to_ban}` уже был удален или не найден в списке источников.")
                    log.warning(f"CMD FAILED: Quick 'бан' source {source_chat_id_to_ban} not found/already deleted.")
            elif source_chat_id_to_ban == 0:
                await evt.reply("⚠️ Нельзя забанить источник 'Глобально' через ответ на сообщение.", parse_mode='md')
            else:
                await evt.reply("⚠️ Не удалось найти исходный чат для бана по этому сообщению. Возможно, запись устарела.")
        else:
            await evt.reply("⚠️ Не удалось найти исходный чат для бана по этому сообщению или оно переслано не для вас.")


    await asyncio.sleep(0.5)

# ---


# ====== ОБРАБОТЧИК ДЛЯ КОМАНД (ТОЛЬКО КОМАНДЫ СО СЛЕШЕМ /) ======
@client.on(events.NewMessage(pattern=r'^/'))
async def on_command(evt: events.NewMessage.Event):
    
    # 1. Проверяем, главный ли это администратор
    is_main_admin = await is_admin(evt.sender_id)
    
    # 2. Идентифицируем клиента по чату команд
    client_data = await get_client_by_control(evt.chat_id)
    
    # Если это не главный админ, и это не зарегистрированный чат клиента, то игнорируем
    if not is_main_admin and not client_data:
        return
        
    # Определяем, какой control_chat_id будем использовать
    if client_data:
        # Если команда пришла от зарегистрированного клиента
        control_chat_id = client_data['control_id']
        target_chat_id = client_data['target_id']
        is_client_command = True
    elif is_main_admin:
        # Если команда от главного админа, но не из зарегистрированного чата, используем его CONTROL_CHAT_ID
        control_chat_id = CONTROL_CHAT_ID
        target_chat_id = TARGET_CHAT_ID
        is_client_command = False
    else:
        return 

    # Если команда от клиента, то он должен быть админом в списке администраторов (await!)
    if is_client_command and not await is_admin(evt.sender_id):
         await evt.reply("❌ У вас нет прав для управления ботом в этом чате. Обратитесь к главному администратору.")
         return
         
    # --- Парсинг команды ---
    text = evt.message.message.strip()
    parts = text.split(maxsplit=3) 
    
    if len(parts) < 1:
        return
    
    cmd = parts[0].lower()
    
    # ====================================================================
    # КОМАНДЫ КЛИЕНТА (СО СЛЕШЕМ)
    # ====================================================================
    
    # /register
    if cmd == "/register":
        if not is_main_admin:
            await evt.reply("❌ Доступ запрещен. Эту команду может использовать только главный администратор.")
            return

        if len(parts) < 3:
            await evt.reply("⚠️ Неверный формат. Используйте: `/register <control_chat_id> <target_chat_id> [Имя Клиента]`")
            return
        
        try:
            control_id = int(parts[1])
            target_id = int(parts[2]) # **# ИСПРАВЛЕНИЕ: Правильный парсинг ID**
            name = parts[3].strip() if len(parts) >= 4 else f"Клиент {control_id}" # **# ИСПРАВЛЕНИЕ: Правильный парсинг имени**
            
            # await!)
            if await add_client(control_id, target_id, name):
                await evt.reply(f"✅ **Новый клиент зарегистрирован!**\n"
                                f"Имя: **{name}**\n"
                                f"Чат команд: `{control_id}`\n"
                                f"Чат пересылки: `{target_id}`\n"
                                f"Не забудьте добавить его ID в **администраторы** (`/owner add <ID>`) в его чате команд!", parse_mode='md')
                log.info(f"CMD SUCCESS: Registered new client: {name} (Control: {control_id})")
            else:
                await evt.reply(f"⚠️ Клиент с чатом команд `{control_id}` или чатом пересылки `{target_id}` уже существует.", parse_mode='md')
                log.warning(f"CMD CONFLICT: Registration failed for client {control_id} (already exists).")

        except ValueError:
            await evt.reply("⚠️ ID чатов должны быть числами.")
        except Exception as e:
            await evt.reply(f"❌ Ошибка регистрации: {e}")
            log.error(f"CMD ERROR: Failed to register client: {e}")
            
    # /+слово
    elif cmd == "/+слово":
        
        command_prefix = cmd
        source_chat_id = 0 
        keyword = None
        remaining_text = text[len(command_prefix):].strip()

        if not remaining_text:
            await evt.reply(f"⚠️ Неверный формат команды. Используйте: `/{cmd.lstrip('/')} [ID чата] <слово>` или `/{cmd.lstrip('/')} <слово>` (глобально)", parse_mode='md')
            return
        
        parts_kw = remaining_text.split(maxsplit=1)

        try:
            # **# ИСПРАВЛЕНИЕ: Более чистый парсинг: если первый аргумент - это числовой ID, используем его**
            if len(parts_kw) == 2 and (parts_kw[0].startswith('-100') or (parts_kw[0].lstrip('-').isdigit() and int(parts_kw[0]) < 0)):
                source_chat_id = int(parts_kw[0])
                keyword = parts_kw[1].strip()
            else:
                source_chat_id = 0
                keyword = remaining_text.strip()
        except ValueError:
            source_chat_id = 0
            keyword = remaining_text.strip()
            
        if not keyword:
            await evt.reply("⚠️ Ключевое слово не может быть пустым.", parse_mode='md')
            return

        if await add_keyword(keyword, control_chat_id, source_chat_id): 
            chat_name = await get_chat_title(source_chat_id)
            log.info(f"CMD SUCCESS: Client {control_chat_id} added keyword '{keyword}' for source {source_chat_id}")
            await evt.reply(f"✓ Добавлено слово **'{keyword}'** для: **{chat_name}** (ID: `{source_chat_id}`) [Клиент: `{control_chat_id}`]", parse_mode='md')
        else:
            chat_name = await get_chat_title(source_chat_id)
            log.warning(f"CMD CONFLICT: Client {control_chat_id} failed to add keyword '{keyword}' (already exists) for source {source_chat_id}")
            await evt.reply(f"⚠️ Уже существует: **{keyword}** для {chat_name} [Клиент: `{control_chat_id}`]", parse_mode='md')

    # /удалить +слово
    elif cmd == "/удалить" and len(parts) >= 2 and parts[1].lower() == "+слово":
        
        command_prefix = f"{cmd} {parts[1]}"
        source_chat_id = 0 
        keyword = None
        
        remaining_text = text[len(command_prefix):].strip()

        if not remaining_text:
            await evt.reply(f"⚠️ Неверный формат команды. Используйте: `/{command_prefix.lstrip('/')} [ID чата] <слово>`", parse_mode='md')
            return
        
        parts_kw = remaining_text.split(maxsplit=1)
        
        try:
            if len(parts_kw) == 2 and (parts_kw[0].startswith('-100') or (parts_kw[0].lstrip('-').isdigit() and int(parts_kw[0]) < 0)):
                source_chat_id = int(parts_kw[0])
                keyword = parts_kw[1].strip()
            else:
                source_chat_id = 0
                keyword = remaining_text.strip()
        except ValueError:
            source_chat_id = 0
            keyword = remaining_text.strip()
            
        if not keyword:
            await evt.reply("⚠️ Ключевое слово не может быть пустым.", parse_mode='md')
            return
            
        # await!)
        if await delete_keyword(keyword, control_chat_id, source_chat_id): 
            chat_name = await get_chat_title(source_chat_id)
            log.info(f"CMD SUCCESS: Client {control_chat_id} deleted keyword '{keyword}' for source {source_chat_id}")
            await evt.reply(f"✓ Удалено слово **'{keyword}'** для: **{chat_name}** (ID: `{source_chat_id}`) [Клиент: `{control_chat_id}`]", parse_mode='md')
        else:
            chat_name = await get_chat_title(source_chat_id)
            log.warning(f"CMD FAILED: Client {control_chat_id} failed to delete keyword '{keyword}' (not found) for source {source_chat_id}")
            await evt.reply(f"⚠️ Слово **'{keyword}'** не найдено для: {chat_name} [Клиент: `{control_chat_id}`]", parse_mode='md')


    # /список слов
    elif cmd == "/список" and len(parts) >= 2 and parts[1].lower() == "слов":
        
        source_chat_id = 0
        if len(parts) == 3:
            try:
                source_chat_id = int(parts[2])
            except ValueError:
                await evt.reply("⚠️ Ошибка: ID чата должен быть числом.", parse_mode='md')
                return
                
        # await!)
        kws_global = await get_keywords(control_chat_id, 0)
        
        if source_chat_id != 0:
            # await!)
            kws_local = await get_keywords(control_chat_id, source_chat_id)
            local_only = [kw for kw in kws_local if kw not in kws_global]
            unique_kws = sorted(list(set(kws_global + local_only))) 
        else:
            kws_local = []
            local_only = []
            unique_kws = sorted(kws_global)
            
        
        if source_chat_id == 0:
            title = f"📝 Глобальные ключевые слова [Клиент: `{control_chat_id}`]"
            
        else:
            chat_name = await get_chat_title(source_chat_id)
            title = f"📝 Ключевые слова для: **{chat_name}** (ID: `{source_chat_id}`) [Клиент: `{control_chat_id}`]"
            
        response = f"{title} (Всего: {len(unique_kws)}):\n\n"
        
        if source_chat_id != 0:
            
            response += "**— Локальные слова (только для этого источника):**\n"
            
            if local_only:
                 response += "\n".join(f"• {kw}" for kw in local_only) + "\n\n"
            else:
                response += "*(Локальных слов нет)*\n\n"
            
            response += "**— Глобальные слова (наследуются):**\n"
        
        if kws_global:
            response += "\n".join(f"• {kw}" for kw in kws_global)
        elif source_chat_id == 0:
            response += "*(Список пуст)*"

        await evt.reply(response, parse_mode='md')
        log.info(f"CMD SUCCESS: Client {control_chat_id} viewed keyword list for source {source_chat_id}")

            
    # /минус слово
    elif cmd == "/минус" and len(parts) >= 2 and parts[1].lower() == "слово":
        
        if len(parts) < 3:
            # await!)
            nws = await get_negwords(control_chat_id) 
            await evt.reply(f"🚫 Негативные слова [Клиент: `{control_chat_id}`] ({len(nws)}):\n" + "\n".join(f"• {nw}" for nw in nws) if nws else "Список пуст", parse_mode='md')
            await evt.reply("Используйте: `/минус слово <слово>` | `/удалить минус слово <слово>` | `/список минус слов`", parse_mode='md')
            return
            
        nw = parts[2].strip()
        # await!)
        if await add_negword(nw, control_chat_id): 
            await evt.reply(f"✓ Добавлено негативное слово: {nw} [Клиент: `{control_chat_id}`]")
            log.info(f"CMD SUCCESS: Client {control_chat_id} added negword '{nw}'")
        else:
            await evt.reply(f"⚠️ Уже существует: {nw} [Клиент: `{control_chat_id}`]")
            log.warning(f"CMD CONFLICT: Client {control_chat_id} failed to add negword '{nw}' (already exists)")

    # /удалить минус слово
    elif cmd == "/удалить" and len(parts) >= 3 and parts[1].lower() == "минус" and parts[2].lower() == "слово":
        
        command_prefix = f"{cmd} {parts[1]} {parts[2]}"
        remaining_text = text[len(command_prefix):].strip()
        
        if not remaining_text:
            await evt.reply("⚠️ Неверный формат команды. Используйте: `/удалить минус слово <слово>`", parse_mode='md')
            return
            
        nw = remaining_text.strip()
        # await!)
        if await delete_negword(nw, control_chat_id): 
            await evt.reply(f"✓ Удалено негативное слово: {nw} [Клиент: `{control_chat_id}`]")
            log.info(f"CMD SUCCESS: Client {control_chat_id} deleted negword '{nw}'")
        else:
            await evt.reply(f"⚠️ Слово не найдено: {nw} [Клиент: `{control_chat_id}`]")
            log.warning(f"CMD FAILED: Client {control_chat_id} failed to delete negword '{nw}' (not found)")


    # /список минус слов
    elif cmd == "/список" and len(parts) >= 3 and parts[1].lower() == "минус" and parts[2].lower() == "слов":
        # await!)
        nws = await get_negwords(control_chat_id) 
        await evt.reply(f"🚫 Негативные слова [Клиент: `{control_chat_id}`] ({len(nws)}):\n" + "\n".join(f"• {nw}" for nw in nws) if nws else "Список пуст", parse_mode='md')
        log.info(f"CMD SUCCESS: Client {control_chat_id} viewed negword list")


    # /добавить чат
    elif cmd == "/добавить" and len(parts) >= 2 and parts[1].lower() == "чат":
        
        if len(parts) < 3:
            # await!)
            sources = await list_sources(control_chat_id) 
            await evt.reply(f"📢 Источники [Клиент: `{control_chat_id}`] ({len(sources)}):\n" + 
                          "\n".join(f"• {title} (ID: `{cid}`)" for cid, title in sources), parse_mode='md')
            await evt.reply("Используйте: `/добавить чат <id|@user|t.me/link>` | `/удалить чат <id|@user|t.me/link>` | `/список чатов`", parse_mode='md')
            return

        chat_input = parts[2].strip()
        
        try:
            entity = await client.get_entity(chat_input) 
            
            if isinstance(entity, User):
                await evt.reply(f"⚠️ Ошибка: '{chat_input}' — это ID/username пользователя. Требуется ID чата.", parse_mode='md')
                return
            
            source_chat_id = entity.id 
            if source_chat_id > 0: 
                source_chat_id = get_peer_id(entity, add_mark=True)
            
            title = get_display_name(entity)

            # await!)
            if await add_source(source_chat_id, control_chat_id, title): 
                await evt.reply(f"✓ Добавлен источник: **{title}** (ID: `{source_chat_id}`) [Клиент: `{control_chat_id}`]", parse_mode='md')
                log.info(f"CMD SUCCESS: Client {control_chat_id} added source {source_chat_id}")
            else:
                await evt.reply(f"⚠️ Ошибка добавления источника")
                log.error(f"CMD ERROR: Client {control_chat_id} failed to add source {source_chat_id}")
        
        except ValueError:
            await evt.reply("⚠️ Неверный формат ID/ссылки.")
        except Exception as e:
            await evt.reply(f"⚠️ Ошибка: Не удалось найти чат по ссылке или ID. Возможно, бот не состоит в этом чате. Ошибка: {e}")
            log.error(f"CMD ERROR: Client {control_chat_id} failed to resolve source {chat_input}: {e}")
    
    # /удалить чат
    elif cmd == "/удалить" and len(parts) >= 2 and parts[1].lower() == "чат":
        
        if len(parts) < 3:
            await evt.reply("⚠️ Неверный формат команды. Используйте: `/удалить чат <id|@user|t.me/link>`", parse_mode='md')
            return

        chat_input = parts[2].strip()
        try:
            entity = await client.get_entity(chat_input)
            source_chat_id = entity.id 
            if source_chat_id > 0: 
                source_chat_id = get_peer_id(entity, add_mark=True)
                
            # await!)
            if await delete_source(source_chat_id, control_chat_id): 
                await evt.reply(f"✓ Источник `{source_chat_id}` (**{get_display_name(entity)}**) удален [Клиент: `{control_chat_id}`].", parse_mode='md')
                log.info(f"CMD SUCCESS: Client {control_chat_id} deleted source {source_chat_id}")
            else:
                await evt.reply(f"⚠️ Источник `{source_chat_id}` не найден в списке [Клиент: `{control_chat_id}`].", parse_mode='md')
                log.warning(f"CMD FAILED: Client {control_chat_id} failed to delete source {source_chat_id} (not found).")
        except ValueError:
            await evt.reply("⚠️ Неверный формат ID/ссылки.")
        except Exception:
            try:
                source_chat_id = int(chat_input)
                # await!)
                if await delete_source(source_chat_id, control_chat_id): 
                    await evt.reply(f"✓ Источник `{source_chat_id}` удален [Клиент: `{control_chat_id}`].", parse_mode='md')
                    log.info(f"CMD SUCCESS: Client {control_chat_id} deleted source {source_chat_id} (by raw ID)")
                else:
                    await evt.reply(f"⚠️ Источник `{source_chat_id}` не найден в списке [Клиент: `{control_chat_id}`].", parse_mode='md')
                    log.warning(f"CMD FAILED: Client {control_chat_id} failed to delete source {source_chat_id} (not found by raw ID).")
            except ValueError:
                await evt.reply("⚠️ Не удалось определить ID источника. Используйте ID, @username или ссылку.")
                log.error(f"CMD ERROR: Client {control_chat_id} failed to determine source ID for {chat_input}")

    # /список чатов
    elif cmd == "/список" and len(parts) >= 2 and parts[1].lower() == "чатов":
        # await!)
        sources = await list_sources(control_chat_id) 
        await evt.reply(f"📢 Источники [Клиент: `{control_chat_id}`] ({len(sources)}):\n" + 
                      "\n".join(f"• {title} (ID: `{cid}`)" for cid, title in sources), parse_mode='md')
        log.info(f"CMD SUCCESS: Client {control_chat_id} viewed source list")
    
    # /ai (изменена для поддержки глобальных правил)
    elif cmd == "/ai":
        if len(parts) < 2:
            await evt.reply("Используйте: /ai set <source_chat_id> <правило> ИЛИ /ai set <правило> (Глобально) | /ai show [source_chat_id] | /ai clear [source_chat_id]")
            return
        
        subcmd = parts[1].lower()
        
        if subcmd == "set":
            if not OPENAI_API_KEY:
                await evt.reply("⚠️ OPENAI_API_KEY не установлен. Правило AI не будет работать.", parse_mode='md')
                return
                
            rule_text = ""
            target_id = 0
            rule_type = "Глобальное"
            
            # **# ИСПРАВЛЕНИЕ: Улучшенная логика парсинга для AI set**
            if len(parts) == 4 and parts[2].strip().startswith('-100'): 
                try:
                    target_id = int(parts[2].strip())
                    rule_text = parts[3].strip()
                    rule_type = "Специфическое"
                except ValueError:
                    await evt.reply("⚠️ Неверный формат ID чата.")
                    return
            
            # Логика: если 3 части, то это глобальное правило, если ID чата не указан
            elif len(parts) >= 3: 
                target_id = 0
                # Если ввели /ai set "правило с пробелами"
                if len(parts) == 3:
                     rule_text = parts[2].strip()
                else:
                    # Если больше 3 частей, то это часть правила, когда ID не указан
                    rule_text = " ".join(parts[2:]).strip()
                
                rule_type = "Глобальное"
            else:
                await evt.reply("Формат: `/ai set <source_chat_id> <правило>` ИЛИ `/ai set <правило>` (Глобально)", parse_mode='md')
                return

            if not rule_text:
                 await evt.reply("⚠️ Правило не может быть пустым.")
                 return

            # await!)
            await set_ai_rule(target_id, control_chat_id, rule_text) 
            chat_name = await get_chat_title(target_id)
            await evt.reply(f"✓ **{rule_type}** AI правило установлено для **{chat_name}** (ID: `{target_id}`) [Клиент: `{control_chat_id}`]")
            log.info(f"CMD SUCCESS: Client {control_chat_id} set {rule_type} AI rule for source {target_id}")

                
        elif subcmd == "show":
            try:
                # 0 для глобального, если ID не указан
                source_chat_id = int(parts[2]) if len(parts) == 3 else 0 
                # await!)
                rule = await get_ai_rule(source_chat_id, control_chat_id) 
                chat_name = await get_chat_title(source_chat_id)
                rule_type = "Специфическое" if source_chat_id != 0 else "Глобальное"
                
                if rule:
                    await evt.reply(f"**{rule_type}** AI правило для **{chat_name}** (ID: `{source_chat_id}`) [Клиент: `{control_chat_id}`]:\n`{rule}`", parse_mode='md')
                    log.info(f"CMD SUCCESS: Client {control_chat_id} viewed AI rule for source {source_chat_id}")
                else:
                    await evt.reply(f"Нет **{rule_type}** правила для **{chat_name}** (ID: `{source_chat_id}`) [Клиент: `{control_chat_id}`]")
            except ValueError:
                await evt.reply("⚠️ Неверный формат ID чата")
                
        elif subcmd == "clear":
            try:
                source_chat_id = int(parts[2]) if len(parts) == 3 else 0
                # await!)
                await clear_ai_rule(source_chat_id, control_chat_id) 
                chat_name = await get_chat_title(source_chat_id)
                rule_type = "Специфическое" if source_chat_id != 0 else "Глобальное"
                await evt.reply(f"✓ **{rule_type}** AI правило удалено для чата **{chat_name}** (ID: `{source_chat_id}`) [Клиент: `{control_chat_id}`]")
                log.info(f"CMD SUCCESS: Client {control_chat_id} cleared AI rule for source {source_chat_id}")
            except ValueError:
                await evt.reply("⚠️ Неверный формат ID чата")
        
        else:
            await evt.reply("Используйте: /ai set <ID> <правило> | /ai show <ID> | /ai clear <ID>")

    # /owner
    elif cmd == "/owner":
        # Команда доступна только главному администратору.
        if not is_main_admin:
            await evt.reply("❌ Доступ запрещен. Эту команду может использовать только главный администратор.")
            return

        if len(parts) < 2:
            await evt.reply("Используйте: `/owner add <ID>` | `/owner remove <ID>` | `/owner list`", parse_mode='md')
            return

        subcmd = parts[1].lower()
        
        if subcmd == "add" and len(parts) == 3:
            try:
                user_id = int(parts[2])
                user_entity = await client.get_entity(user_id)
                username = get_display_name(user_entity)
                
                # await!)
                if await add_admin(user_id, username):
                    await evt.reply(f"✅ Пользователь **{username}** (ID: `{user_id}`) добавлен в список администраторов.", parse_mode='md')
                    log.info(f"CMD SUCCESS: Admin {user_id} added.")
                else:
                    await evt.reply(f"⚠️ Пользователь **{username}** (ID: `{user_id}`) уже был администратором.", parse_mode='md')
                    log.warning(f"CMD CONFLICT: Admin {user_id} already exists.")

            except ValueError:
                await evt.reply("⚠️ Неверный формат ID пользователя. ID должен быть числом.")
            except Exception as e:
                await evt.reply(f"❌ Ошибка: Не удалось найти пользователя по ID. Ошибка: {e}")

        elif subcmd == "remove" and len(parts) == 3:
            try:
                user_id = int(parts[2])
                
                if user_id == ADMIN_USER_ID:
                    await evt.reply("⚠️ Вы не можете удалить из списка главного администратора (из .env).", parse_mode='md')
                    return
                
                # await!)
                if await remove_admin(user_id):
                    await evt.reply(f"✅ Пользователь с ID `{user_id}` удален из администраторов.")
                    log.info(f"CMD SUCCESS: Admin {user_id} removed.")
                else:
                    await evt.reply(f"⚠️ Пользователь с ID `{user_id}` не был в списке администраторов.")
                    log.warning(f"CMD FAILED: Admin {user_id} not found for removal.")
            except ValueError:
                await evt.reply("⚠️ Неверный формат ID пользователя.")
                
        elif subcmd == "list":
            # await!)
            admins = await list_admins()
            response = f"**👤 Администраторы** ({len(admins)}):\n\n"
            response += f"**• Главный Администратор** (ID: `{ADMIN_USER_ID}`) *из .env*\n"
            
            for uid, username in admins:
                if uid != ADMIN_USER_ID:
                    response += f"• **{username}** (ID: `{uid}`)\n"

            await evt.reply(response, parse_mode='md')
            log.info(f"CMD SUCCESS: Admin list viewed.")

    # /бан (банит пользователя по ID)
    elif cmd == "/бан":
        if len(parts) < 2:
            await evt.reply("Используйте: `/бан <ID>` | `/ban remove <ID>` | `/список бан`", parse_mode='md')
            return
            
        try:
            user_id = int(parts[1])
            # await!)
            if await ban_user(user_id):
                try:
                    user_entity = await client.get_entity(user_id)
                    ban_name = get_display_name(user_entity)
                    await evt.reply(f"✅ Пользователь **{ban_name}** (ID: `{user_id}`) добавлен в список заблокированных.", parse_mode='md')
                    log.info(f"CMD SUCCESS: User {user_id} banned.")
                except Exception:
                    await evt.reply(f"✅ Пользователь с ID `{user_id}` добавлен в список заблокированных.", parse_mode='md')
                    log.info(f"CMD SUCCESS: User {user_id} banned (No Entity).")
            else:
                await evt.reply(f"⚠️ Пользователь с ID `{user_id}` уже заблокирован.")
                log.warning(f"CMD CONFLICT: User {user_id} already banned.")
        except ValueError:
            await evt.reply("⚠️ Неверный формат ID пользователя. ID должен быть числом.")

    # /ban remove
    elif cmd == "/ban" and len(parts) >= 2 and parts[1].lower() == "remove":
        if len(parts) < 3:
            await evt.reply("Используйте: `/ban remove <ID>`", parse_mode='md')
            return
        
        try:
            user_id = int(parts[2])
            # await!)
            if await unban_user(user_id):
                await evt.reply(f"✅ Пользователь с ID `{user_id}` удален из списка заблокированных.")
                log.info(f"CMD SUCCESS: User {user_id} unbanned.")
            else:
                await evt.reply(f"⚠️ Пользователь с ID `{user_id}` не был в списке заблокированных.")
                log.warning(f"CMD FAILED: User {user_id} not found for unban.")
        except ValueError:
            await evt.reply("⚠️ Неверный формат ID пользователя.")
            
    # /список бан
    elif cmd == "/список" and len(parts) >= 2 and parts[1].lower() == "бан":
        # await!)
        banned_users = await list_banned_users()
        response = f"**🚫 Заблокированные пользователи** ({len(banned_users)}):\n\n"
        
        for uid in banned_users:
            try:
                entity = await client.get_entity(uid)
                name = get_display_name(entity)
                response += f"• **{name}** (ID: `{uid}`)\n"
            except Exception:
                response += f"• ID: `{uid}` (Не найдено)\n"
                
        if not banned_users:
             response += "Список пуст."

        await evt.reply(response, parse_mode='md')
        log.info(f"CMD SUCCESS: Admin {evt.sender_id} viewed banned list.")


# ====== ОБРАБОТКА ВХОДЯЩИХ СООБЩЕНИЙ (ПАРСЕР) ======
@client.on(events.NewMessage())
async def on_message(evt: events.NewMessage.Event):
    source_chat_id = evt.chat_id

    # 1. Проверяем, есть ли вообще клиенты, которые мониторят этот источник (await!)
    monitoring_clients = await get_clients_monitoring_source(source_chat_id)
    
    # **# ИСПРАВЛЕНИЕ: Добавлен ранний выход для команд. Основной обработчик команд (on_command)
    # обрабатывает команды со слешем, но on_quick_action (который тоже NewMessage) 
    # должен быть вызван для команд без слеша, таких как 'бан' и 'почему'.
    # Здесь мы проверяем, не является ли это командой, чтобы не запускать парсинг
    # для сообщений, которые мы уже обработали или обработаем позже.**
    
    text = (evt.message.message or "").strip()
    if not text:
        return
        
    # Если это сообщение в контрольном или целевом чате - пропускаем, т.к. оно либо 
    # команда для on_command, либо 'бан'/'почему' для on_quick_action
    if await get_client_role_by_chat_id(source_chat_id) in ['control', 'target']:
        return


    if not monitoring_clients:
        return
    
    # 2. Проверяем базовые условия (для всех клиентов) (await!)
    msg_key = f"{source_chat_id}:{evt.id}"
    if await is_seen(msg_key):
        return

    text_lower = text.lower()
    
    # ПРОВЕРКА: Блокировка пользователя (Глобально) (await!)
    if evt.sender_id and await is_banned(evt.sender_id):
        log.info(f"✗ Skipped message from banned user: {evt.sender_id} (Global Ban)")
        return

    # 3. Цикл по каждому клиенту
    for client_data in monitoring_clients:
        control_chat_id = client_data['control_id']
        target_chat_id = client_data['target_id']
        
        # 3.1. Фильтрация ключевыми и негативными словами (персонализированная) (await!)
        keywords = await get_keywords(control_chat_id, source_chat_id) 
        negwords = await get_negwords(control_chat_id) 
        
        if not keywords:
            log.debug(f"Skipping client {control_chat_id}: No keywords set.")
            continue
            
        match_keywords = False
        kw_match_reason = "Ключевое слово не найдено." 
        
        for kw in keywords:
            if kw in text_lower:
                match_keywords = True
                kw_match_reason = f"Сообщение содержит ключевое слово: **{kw}**"
                break
        
        if not match_keywords:
            continue
        
        # Проверка негативных слов
        match_negwords = any(nw in text_lower for nw in negwords)

        if match_negwords:
            log.info(f"✗ Filtered out for client {control_chat_id} (Negword match): {text[:50]}")
            continue

        # 3.2. Проверка ИИ (персонализированная)
        ai_passed, ai_verdict = await ai_filter(text, source_chat_id, control_chat_id)
        
        # Логирование результата AI (для сохранения в БД)
        ai_log_entry = ai_verdict.replace('\n', ' ')
        
        # **# ИСПРАВЛЕНИЕ: Формируем полный лог для записи в DB и окончательную причину**
        full_log_entry = f"KW Reason: {kw_match_reason} | AI Verdict: {ai_log_entry}"
        forward_reason = f"{kw_match_reason}\nAI Filter Result: {ai_log_entry.replace('AI VERDICT: ', '').replace('SKIPPED ', '(Skipped) ')}"


        log.info(
            f"AI CHECK for client {control_chat_id} (Source: {source_chat_id}, KW Match): "
            f"Verdict: {ai_verdict} | Msg: {text[:50]}..."
        )
        
        # 3.3. Логика пересылки
        if ai_passed:
            try:
                # Получаем данные (один раз)
                chat_title = await get_chat_title(source_chat_id)
                sender_info = await get_sender_info(evt.message.sender_id)
                
                # Безопасно получаем имя клиента 
                client_name = client_data.get('name', f"Клиент {control_chat_id}")
                
                # Ссылка на оригинальное сообщение
                channel_id_for_link = str(source_chat_id).replace('-100', '')
                original_link = f"https://t.me/c/{channel_id_for_link}/{evt.id}"

                # Формируем сообщение
                header = f"**Монитор Клиента: {client_name}**" 
                chat_line = f"Чат: [{chat_title}]({original_link})"
                
                sender_display = f"@{sender_info['username']}" if sender_info['username'] else f"ID {sender_info['id']}"
                sender_line = f"Отправитель: {sender_display}\nUID: {sender_info['id']}" 
                
                separator = "—" * 20
                
                
                final_text = (
                    f"{header}\n"
                    f"{chat_line}\n"
                    f"{sender_line}\n"
                    f"{separator}\n\n"
                    f"{text}" 
                )
                
                sent_msg = await client.send_message(
                    target_chat_id, 
                    final_text, 
                    link_preview=False, 
                    parse_mode='md' 
                )
                
                # Сохраняем log AI, source_chat_id и control_chat_id (для команд 'бан'/'почему')
                # **# ИСПРАВЛЕНИЕ: Используем full_log_entry и сохраняем control_chat_id**
                await store_forward_log(sent_msg.id, source_chat_id, control_chat_id, full_log_entry, forward_reason)

                log.info(f"✓ FORWARDED to client {control_chat_id} (Source {source_chat_id})") 
                
            except Exception as e:
                log.error(f"Failed to format or send message for client {control_chat_id} to target {target_chat_id}: {e}")
                continue 
        else:
            log.info(f"✗ FILTER STOPPED: AI check failed for client {control_chat_id}")


    # 4. Отмечаем сообщение как увиденное (Глобально) (await!)
    await mark_seen(msg_key)

    await asyncio.sleep(0.6)

# ---
async def main():
    """Основная функция, запускающая бота."""
    init_db() # Инициализация БД перед стартом клиента
    await client.start(phone=PHONE)
    log.info("Bot started and running!")
    
    # ----------------------------------------------------
    # ИНИЦИАЛИЗАЦИЯ АДМИНА
    if ADMIN_USER_ID != 0:
        admin_info = await get_sender_info(ADMIN_USER_ID)
        # Добавляем или обновляем главного админа
        await add_admin(ADMIN_USER_ID, admin_info.get('username', "admin")) 
        log.info(f"Default admin {ADMIN_USER_ID} ensured.")
    # ----------------------------------------------------

    await client.run_until_disconnected()

if __name__ == '__main__':
    try:
        # Установка политик для asyncio
        if os.name == 'nt':  # Windows
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped manually by user.")
    except Exception as e:
        log.error(f"Main execution error: {e}")