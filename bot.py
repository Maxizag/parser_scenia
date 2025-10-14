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
    """Синхронная функция для инициализации таблиц при старте."""
    log.info("Initializing database tables...")
    conn = sqlite3.connect(DB_FILE)
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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS forward_reasons (
            target_msg_id INTEGER PRIMARY KEY,
            reason TEXT
        )
    """)

    conn.commit()
    conn.close()
    log.info("Database tables initialized.")


# ====== Клиент ======
client = TelegramClient("parser_session", API_ID, API_HASH)


# ==============================================================================
# АСИНХРОННЫЕ ОБЕРТКИ ДЛЯ СИНХРОННЫХ ОПЕРАЦИЙ SQLITE
# КАЖДАЯ ФУНКЦИЯ ТЕПЕРЬ ОТКРЫВАЕТ/ЗАКРЫВАЕТ СВОЕ СОЕДИНЕНИЕ
# ==============================================================================

def run_in_executor(func):
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)
    return wrapper

# ----------------- ФУНКЦИИ ДЛЯ КЛИЕНТОВ -----------------
@run_in_executor
def add_client(control_id, target_id, name=""):
    """Регистрирует нового клиента."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO clients (control_chat_id, target_chat_id, name) VALUES (?, ?, ?)", 
                      (control_id, target_id, name))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

@run_in_executor
def get_client_by_control(control_id):
    """Находит данные клиента по его чату команд."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT control_chat_id, target_chat_id, name FROM clients WHERE control_chat_id = ?", 
                      (control_id,))
        row = cursor.fetchone()
        if row:
            return {'control_id': row[0], 'target_id': row[1], 'name': row[2]}
        return None
    finally:
        conn.close()

@run_in_executor
def get_clients_monitoring_source(source_id):
    """Находит всех клиентов, которые мониторят данный источник."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT 
                c.control_chat_id, 
                c.target_chat_id 
            FROM clients c
            JOIN sources s ON c.control_chat_id = s.control_chat_id
            WHERE s.source_chat_id = ? AND c.is_active = 1
        """, (source_id,))
        
        return [{'control_id': row[0], 'target_id': row[1]} for row in cursor.fetchall()]
    finally:
        conn.close()


# ----------------- ФУНКЦИИ КЛЮЧЕВЫХ СЛОВ -----------------
@run_in_executor
def get_keywords(control_chat_id, source_chat_id=None):
    """Получает ключевые слова для конкретного клиента."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        query = "SELECT keyword FROM keywords WHERE control_chat_id = ? AND source_chat_id = 0"
        params = [control_chat_id]
        
        if source_chat_id is not None and source_chat_id != 0:
            query += " UNION SELECT keyword FROM keywords WHERE control_chat_id = ? AND source_chat_id = ?"
            params.extend([control_chat_id, source_chat_id])
            
        cursor.execute(query, params)
        return [row[0].lower() for row in cursor.fetchall()]
    finally:
        conn.close()

@run_in_executor
def add_keyword(kw, control_chat_id, source_chat_id=0):
    """Добавляет слово для конкретного клиента."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO keywords (control_chat_id, source_chat_id, keyword) VALUES (?, ?, ?)", 
                      (control_chat_id, source_chat_id, kw.lower()))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

@run_in_executor
def delete_keyword(kw, control_chat_id, source_chat_id=0):
    """Удаляет слово для конкретного клиента."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM keywords WHERE keyword = ? AND control_chat_id = ? AND source_chat_id = ?", 
                      (kw.lower(), control_chat_id, source_chat_id))
        conn.commit()
        return cursor.rowcount > 0 
    finally:
        conn.close()

# ----------------- ФУНКЦИИ НЕГАТИВНЫХ СЛОВ -----------------
@run_in_executor
def get_negwords(control_chat_id):
    """Получает негативные слова для конкретного клиента."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT negword FROM negwords WHERE control_chat_id = ?", (control_chat_id,))
        return [row[0].lower() for row in cursor.fetchall()]
    finally:
        conn.close()

@run_in_executor
def add_negword(nw, control_chat_id):
    """Добавляет негативное слово для конкретного клиента."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO negwords (control_chat_id, negword) VALUES (?, ?)", (control_chat_id, nw.lower()))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

@run_in_executor
def delete_negword(nw, control_chat_id):
    """Удаляет негативное слово для конкретного клиента."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM negwords WHERE negword = ? AND control_chat_id = ?", (nw.lower(), control_chat_id))
        conn.commit()
        return cursor.rowcount > 0 
    finally:
        conn.close()

# ----------------- ФУНКЦИИ ИСТОЧНИКОВ -----------------
@run_in_executor
def list_sources(control_chat_id):
    """Получает список источников, которые мониторит конкретный клиент."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT source_chat_id, chat_title FROM sources WHERE control_chat_id = ?", (control_chat_id,))
        return cursor.fetchall()
    finally:
        conn.close()

@run_in_executor
def add_source(source_chat_id, control_chat_id, chat_title):
    """Добавляет источник для конкретного клиента."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT OR REPLACE INTO sources (source_chat_id, control_chat_id, chat_title) VALUES (?, ?, ?)", 
                      (source_chat_id, control_chat_id, chat_title))
        conn.commit()
        return True
    except Exception as e:
        log.error(f"Error adding source: {e}")
        return False
    finally:
        conn.close()

@run_in_executor
def delete_source(source_chat_id, control_chat_id):
    """Удаляет источник для конкретного клиента."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM sources WHERE source_chat_id = ? AND control_chat_id = ?", (source_chat_id, control_chat_id))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()

# ----------------- ФУНКЦИИ AI ПРАВИЛ -----------------
@run_in_executor
def get_ai_rule(source_chat_id, control_chat_id):
    """Получает AI правило для конкретного источника и клиента."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT rule FROM ai_rules WHERE source_chat_id = ? AND control_chat_id = ?", 
                      (source_chat_id, control_chat_id))
        row = cursor.fetchone()
        return row[0] if row else None
    finally:
        conn.close()

@run_in_executor
def set_ai_rule(source_chat_id, control_chat_id, rule):
    """Устанавливает AI правило для конкретного источника и клиента."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT OR REPLACE INTO ai_rules (source_chat_id, control_chat_id, rule) VALUES (?, ?, ?)", 
                      (source_chat_id, control_chat_id, rule))
        conn.commit()
    finally:
        conn.close()

@run_in_executor
def clear_ai_rule(source_chat_id, control_chat_id):
    """Удаляет AI правило для конкретного источника и клиента."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM ai_rules WHERE source_chat_id = ? AND control_chat_id = ?", 
                      (source_chat_id, control_chat_id))
        conn.commit()
    finally:
        conn.close()

# ----------------- ГЛОБАЛЬНЫЕ ФУНКЦИИ БЕЗ ПРИВЯЗКИ К КЛИЕНТУ -----------------
@run_in_executor
def is_seen(msg_key):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM seen_messages WHERE msg_key = ?", (msg_key,))
        return cursor.fetchone() is not None
    finally:
        conn.close()

@run_in_executor
def mark_seen(msg_key):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT OR IGNORE INTO seen_messages (msg_key) VALUES (?)", (msg_key,))
        conn.commit()
    finally:
        conn.close()

@run_in_executor
def is_admin(user_id):
    """Проверяет, является ли пользователь администратором."""
    if user_id == ADMIN_USER_ID:
        return True 
        
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
        return cursor.fetchone() is not None
    finally:
        conn.close()

@run_in_executor
def add_admin(user_id, username):
    """Добавляет пользователя в список администраторов."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT OR IGNORE INTO admins (user_id, username) VALUES (?, ?)", 
                      (user_id, username))
        conn.commit()
        return cursor.rowcount > 0
    except Exception:
        return False
    finally:
        conn.close()

@run_in_executor
def remove_admin(user_id):
    """Удаляет пользователя из списка администраторов."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()

@run_in_executor
def list_admins():
    """Возвращает список всех администраторов."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT user_id, username FROM admins")
        return cursor.fetchall()
    finally:
        conn.close()

@run_in_executor
def is_banned(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,))
        return cursor.fetchone() is not None
    finally:
        conn.close()

@run_in_executor
def ban_user(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT OR IGNORE INTO banned_users (user_id) VALUES (?)", (user_id,))
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()

@run_in_executor
def unban_user(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()

@run_in_executor
def list_banned_users():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT user_id FROM banned_users")
        return [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()

@run_in_executor
def store_forward_reason(target_msg_id, reason):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT OR REPLACE INTO forward_reasons (target_msg_id, reason) VALUES (?, ?)", 
                      (target_msg_id, reason))
        conn.commit()
    finally:
        conn.close()

@run_in_executor
def get_forward_reason(target_msg_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT reason FROM forward_reasons WHERE target_msg_id = ?", (target_msg_id,))
        row = cursor.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ----------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (БЕЗ ИЗМЕНЕНИЙ) -----------------
# Эти функции не работают с БД, поэтому не нуждаются в декораторе
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


# ====== AI фильтр (УНИВЕРСАЛЬНЫЙ МЕТОД ДЛЯ СТАРЫХ ВЕРСИЙ) ======
async def ai_filter(text, source_chat_id, control_chat_id):
    """
    Отправляет текст сообщения и правило в OpenAI для проверки.
    """
    if not OPENAI_API_KEY:
        return True, "SKIPPED (OPENAI_API_KEY not set)"

    rule = await get_ai_rule(source_chat_id, control_chat_id) 
    if not rule:
        return True, "SKIPPED (No AI rule set for this chat by client)"
    
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
            return True, f"AI VERDICT: Passed (ДА). Rule: {rule[:30]}..."
        else:
            return False, f"AI VERDICT: Failed (НЕТ). Rule: {rule[:30]}..."

    except Exception as e:
        log.error(f"OpenAI API Error (acreate): {e}")
        return True, f"ERROR (AI API FAILED): {e}"


# ---


# ====== Обработка сообщений (КРИТИЧЕСКОЕ ИЗМЕНЕНИЕ: ОБРАБОТКА МНОГИХ КЛИЕНТОВ) ======
@client.on(events.NewMessage())
async def on_message(evt: events.NewMessage.Event):
    source_chat_id = evt.chat_id

    # 1. Проверяем, есть ли вообще клиенты, которые мониторят этот источник (await!)
    monitoring_clients = await get_clients_monitoring_source(source_chat_id)
    
    if not monitoring_clients:
        return
    
    # 2. Проверяем базовые условия (для всех клиентов) (await!)
    msg_key = f"{source_chat_id}:{evt.id}"
    if await is_seen(msg_key):
        return

    text = (evt.message.message or "").strip()
    if not text:
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
            continue
            
        match_keywords = False
        forward_reason = "Ключевое слово не найдено." 
        
        for kw in keywords:
            if kw in text_lower:
                match_keywords = True
                forward_reason = f"Сообщение содержит ключевое слово: **{kw}**"
                break
        
        if not match_keywords:
            continue
        
        match_negwords = any(nw in text_lower for nw in negwords)

        if match_negwords:
            log.info(f"✗ Filtered out for client {control_chat_id} (Negword match): {text[:50]}")
            continue

        # 3.2. Проверка ИИ (персонализированная)
        ai_passed, ai_verdict = await ai_filter(text, source_chat_id, control_chat_id)

        # 3.3. Логика пересылки
        if ai_passed:
            try:
                # Получаем данные (один раз)
                chat_title = await get_chat_title(source_chat_id)
                sender_info = await get_sender_info(evt.message.sender_id)
                
                # Ссылка на оригинальное сообщение
                channel_id_for_link = str(source_chat_id).replace('-100', '')
                original_link = f"https://t.me/c/{channel_id_for_link}/{evt.id}"

                # Формируем сообщение
                header = f"**Монитор Клиента: {client_data['name']}**" 
                chat_line = f"Чат: [{chat_title}]({original_link})"
                
                sender_display = f"@{sender_info['username']}" if sender_info['username'] else f"ID {sender_info['id']}"
                sender_line = f"Отправитель: {sender_display}\nUID: {sender_info['id']}" 
                
                separator = "—" * 20
                
                if "AI VERDICT" in ai_verdict:
                    forward_reason += f"\nAI Filter: {ai_verdict.replace('AI VERDICT: ', '')}"
                    
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
                
                # Сохраняем причину (await!)
                await store_forward_reason(sent_msg.id, forward_reason)

                log.info(f"✓ Sent to client {control_chat_id} from {source_chat_id}: {text[:50]}... | AI: {ai_verdict}")
                
            except Exception as e:
                log.error(f"Failed to process message for client {control_chat_id}: {e}")
        else:
            log.info(f"✗ Filtered out for client {control_chat_id} (AI Failed): {text[:50]}... | AI: {ai_verdict}")

    # 4. Отмечаем сообщение как увиденное (Глобально) (await!)
    await mark_seen(msg_key)

    await asyncio.sleep(0.6)

# ---

# ====== БЫСТРАЯ КОМАНДА 'бан' В ЦЕЛЕВОМ ЧАТЕ (TARGET_CHAT_ID) ======
@client.on(events.NewMessage(chats=TARGET_CHAT_ID)) 
async def on_quick_ban(evt: events.NewMessage.Event):
    # Эта команда пока работает только в старом TARGET_CHAT_ID (главного клиента)
    
    if evt.chat_id != TARGET_CHAT_ID:
         return

    if (evt.message.message or "").strip().lower() != 'бан':
        return
        
    # Важно: тут проверяем админа ГЛОБАЛЬНО (await!)
    if not await is_admin(evt.sender_id):
        return
    
    if not evt.reply_to_msg_id:
        await evt.reply("Используйте 'бан', ответив на пересланное ботом сообщение.", parse_mode='md')
        return

    try:
        replied_msg = await client.get_messages(evt.chat_id, ids=evt.reply_to_msg_id)
        text_to_search = replied_msg.message or ""
        match = re.search(r'UID: (\d+)', text_to_search)
        
        if not match:
            await evt.reply("⚠️ Не удалось найти ID пользователя (`UID: <ID>`) в тексте этого сообщения. Проверьте форматирование.", parse_mode='md')
            return

        user_id_to_ban = int(match.group(1))

        # await!)
        if await ban_user(user_id_to_ban): 
            try:
                entity = await client.get_entity(user_id_to_ban)
                ban_name = get_display_name(entity)
                await evt.reply(f"✅ **Быстрый БАН!** Сообщения от **{ban_name}** (ID: `{user_id_to_ban}`) будут игнорироваться.", parse_mode='md')
            except Exception:
                await evt.reply(f"✅ **Быстрый БАН!** Сообщения от ID `{user_id_to_ban}` будут игнорироваться.", parse_mode='md')
        else:
            await evt.reply(f"⚠️ Пользователь ID `{user_id_to_ban}` уже был заблокирован.", parse_mode='md')

    except Exception as e:
        log.error(f"Error during quick ban: {e}")
        await evt.reply("❌ Произошла ошибка при попытке бана.", parse_mode='md')

# ---

# ====== ОТДЕЛЬНЫЙ ОБРАБОТЧИК ДЛЯ КОМАНДЫ 'Почему' ======
@client.on(events.NewMessage(chats=[CONTROL_CHAT_ID, TARGET_CHAT_ID], pattern=r'^/Почему'))
async def on_command_why(evt: events.NewMessage.Event):
    
    # Проверяем админа ГЛОБАЛЬНО (await!)
    if not await is_admin(evt.sender_id):
        return
    
    if evt.reply_to_msg_id:
        target_msg_id = evt.reply_to_msg_id
        # await!)
        reason = await get_forward_reason(target_msg_id)
        
        if reason:
            await evt.reply(f"🔍 **Причина пересылки**:\n{reason}", parse_mode='md')
        else:
            await evt.reply("⚠️ Не удалось найти причину пересылки для этого сообщения. Убедитесь, что вы отвечаете на сообщение, которое **только что** переслал бот.", parse_mode='md')
    else:
        await evt.reply("Используйте /Почему, ответив на пересланное сообщение в этом чате.", parse_mode='md')

# ---

# ====== ОСНОВНОЙ ОБРАБОТЧИК КОМАНД ======
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

    # Если команда от клиента, то он должен быть админом в списке администраторов (await!)
    if is_client_command and not await is_admin(evt.sender_id):
         await evt.reply("❌ У вас нет прав для управления ботом в этом чате. Обратитесь к главному администратору.")
         return
         
    # --- Парсинг команды ---
    text = evt.message.message.strip()
    parts = text.split(maxsplit=2) 
    
    if len(parts) < 1:
        return
    
    cmd = parts[0].lower()
    
    # ====================================================================
    # /register (await!)
    # ====================================================================
    if cmd == "/register":
        if not is_main_admin:
            await evt.reply("❌ Доступ запрещен. Эту команду может использовать только главный администратор.")
            return

        if len(parts) < 3:
            await evt.reply("⚠️ Неверный формат. Используйте: `/register <control_chat_id> <target_chat_id> [Имя Клиента]`")
            return
        
        try:
            control_id = int(parts[1])
            target_id = int(parts[2].split(maxsplit=1)[0])
            name = parts[2].split(maxsplit=1)[1].strip() if len(parts[2].split(maxsplit=1)) > 1 else f"Клиент {control_id}"
            
            # await!)
            if await add_client(control_id, target_id, name):
                await evt.reply(f"✅ **Новый клиент зарегистрирован!**\n"
                                f"Имя: **{name}**\n"
                                f"Чат команд: `{control_id}`\n"
                                f"Чат пересылки: `{target_id}`\n"
                                f"Не забудьте добавить его ID в **администраторы** (`/owner add <ID>`) в его чате команд!", parse_mode='md')
            else:
                await evt.reply(f"⚠️ Клиент с чатом команд `{control_id}` или чатом пересылки `{target_id}` уже существует.", parse_mode='md')

        except ValueError:
            await evt.reply("⚠️ ID чатов должны быть числами.")
        except Exception as e:
            await evt.reply(f"❌ Ошибка регистрации: {e}")
            
    # /+слово (await!)
    elif cmd == "/+слово":
        
        subcmd = "add"
        command_prefix = cmd
        source_chat_id = 0 
        keyword = None
        remaining_text = text[len(command_prefix):].strip()

        if not remaining_text:
            await evt.reply(f"⚠️ Неверный формат команды. Используйте: `/{cmd.lstrip('/')} [ID] <слово>`", parse_mode='md')
            return
        
        try:
            id_part, keyword_part = remaining_text.split(maxsplit=1)
            source_chat_id = int(id_part)
            keyword = keyword_part.strip()
            
        except ValueError:
            source_chat_id = 0
            keyword = remaining_text.strip()
            
            try:
                _ = int(keyword)
                await evt.reply("⚠️ Неверный формат. Если вы указываете только число, оно должно быть ID, за которым следует ключевое слово.", parse_mode='md')
                return
            except ValueError:
                pass

        if not keyword:
            await evt.reply("⚠️ Ключевое слово не может быть пустым.", parse_mode='md')
            return

        if subcmd == "add" and keyword:
            
            # await!)
            if await add_keyword(keyword, control_chat_id, source_chat_id): 
                chat_name = await get_chat_title(source_chat_id)
                await evt.reply(f"✓ Добавлено слово **'{keyword}'** для: **{chat_name}** (ID: `{source_chat_id}`) [Клиент: `{control_chat_id}`]", parse_mode='md')
            else:
                chat_name = await get_chat_title(source_chat_id)
                await evt.reply(f"⚠️ Уже существует: **{keyword}** для {chat_name} [Клиент: `{control_chat_id}`]", parse_mode='md')

    # /удалить +слово (await!)
    elif cmd == "/удалить" and len(parts) >= 2 and parts[1].lower() == "+слово":
        
        subcmd = "del"
        command_prefix = f"{cmd} {parts[1]}"
        
        source_chat_id = 0 
        keyword = None
        
        remaining_text = text[len(command_prefix):].strip()

        if not remaining_text:
            await evt.reply(f"⚠️ Неверный формат команды. Используйте: `/{command_prefix.lstrip('/')} [ID] <слово>`", parse_mode='md')
            return
        
        try:
            id_part, keyword_part = remaining_text.split(maxsplit=1)
            source_chat_id = int(id_part)
            keyword = keyword_part.strip()
        except ValueError:
            source_chat_id = 0
            keyword = remaining_text.strip()
            
        if not keyword:
            await evt.reply("⚠️ Ключевое слово не может быть пустым.", parse_mode='md')
            return
            
        # await!)
        if await delete_keyword(keyword, control_chat_id, source_chat_id): 
            chat_name = await get_chat_title(source_chat_id)
            await evt.reply(f"✓ Удалено слово **'{keyword}'** для: **{chat_name}** (ID: `{source_chat_id}`) [Клиент: `{control_chat_id}`]", parse_mode='md')
        else:
            chat_name = await get_chat_title(source_chat_id)
            await evt.reply(f"⚠️ Слово **'{keyword}'** не найдено для: {chat_name} [Клиент: `{control_chat_id}`]", parse_mode='md')


    # /список слов (await!)
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

            
    # /минус слово (await!)
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
        else:
            await evt.reply(f"⚠️ Уже существует: {nw} [Клиент: `{control_chat_id}`]")

    # /удалить минус слово (await!)
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
        else:
            await evt.reply(f"⚠️ Слово не найдено: {nw} [Клиент: `{control_chat_id}`]")


    # /список минус слов (await!)
    elif cmd == "/список" and len(parts) >= 3 and parts[1].lower() == "минус" and parts[2].lower() == "слов":
        # await!)
        nws = await get_negwords(control_chat_id) 
        await evt.reply(f"🚫 Негативные слова [Клиент: `{control_chat_id}`] ({len(nws)}):\n" + "\n".join(f"• {nw}" for nw in nws) if nws else "Список пуст", parse_mode='md')


    # /добавить чат (await!)
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
            else:
                await evt.reply(f"⚠️ Ошибка добавления источника")
        
        except ValueError:
            await evt.reply("⚠️ Неверный формат ID/ссылки.")
        except Exception as e:
            await evt.reply(f"⚠️ Ошибка: Не удалось найти чат по ссылке или ID. Возможно, бот не состоит в этом чате. Ошибка: {e}")
    
    # /удалить чат (await!)
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
            else:
                await evt.reply(f"⚠️ Источник `{source_chat_id}` не найден в списке [Клиент: `{control_chat_id}`].", parse_mode='md')
        except ValueError:
            await evt.reply("⚠️ Неверный формат ID/ссылки.")
        except Exception:
            try:
                source_chat_id = int(chat_input)
                # await!)
                if await delete_source(source_chat_id, control_chat_id): 
                    await evt.reply(f"✓ Источник `{source_chat_id}` удален [Клиент: `{control_chat_id}`].", parse_mode='md')
                else:
                    await evt.reply(f"⚠️ Источник `{source_chat_id}` не найден в списке [Клиент: `{control_chat_id}`].", parse_mode='md')
            except ValueError:
                await evt.reply("⚠️ Не удалось определить ID источника. Используйте ID, @username или ссылку.")

    # /список чатов (await!)
    elif cmd == "/список" and len(parts) >= 2 and parts[1].lower() == "чатов":
        # await!)
        sources = await list_sources(control_chat_id) 
        await evt.reply(f"📢 Источники [Клиент: `{control_chat_id}`] ({len(sources)}):\n" + 
                      "\n".join(f"• {title} (ID: `{cid}`)" for cid, title in sources), parse_mode='md')
    
    # /ai (await!)
    elif cmd == "/ai":
        if len(parts) < 2:
            await evt.reply("Используйте: /ai set <source_chat_id> <правило> | /ai show <source_chat_id> | /ai clear <source_chat_id>")
            return
        
        subcmd = parts[1].lower()
        if subcmd == "set" and len(parts) == 3:
            
            if not OPENAI_API_KEY:
                await evt.reply("⚠️ OPENAI_API_KEY не установлен. Правило AI не будет работать.", parse_mode='md')
                return
            
            try:
                chat_id_and_rule = parts[2].split(maxsplit=1)
                if len(chat_id_and_rule) < 2:
                    await evt.reply("Формат: /ai set <source_chat_id> <правило>")
                    return
                source_chat_id = int(chat_id_and_rule[0])
                rule = chat_id_and_rule[1]
                
                # await!)
                await set_ai_rule(source_chat_id, control_chat_id, rule) 
                
                await evt.reply(f"✓ AI правило установлено для источника `{source_chat_id}` [Клиент: `{control_chat_id}`]")
            except ValueError:
                await evt.reply("⚠️ Неверный формат ID чата")
                
        elif subcmd == "show" and len(parts) == 3:
            try:
                source_chat_id = int(parts[2])
                # await!)
                rule = await get_ai_rule(source_chat_id, control_chat_id) 
                if rule:
                    await evt.reply(f"AI правило для `{source_chat_id}` [Клиент: `{control_chat_id}`]:\n{rule}")
                else:
                    await evt.reply(f"Нет правила для чата `{source_chat_id}` [Клиент: `{control_chat_id}`]")
            except ValueError:
                await evt.reply("⚠️ Неверный формат ID чата")
                
        elif subcmd == "clear" and len(parts) == 3:
            try:
                source_chat_id = int(parts[2])
                # await!)
                await clear_ai_rule(source_chat_id, control_chat_id) 
                await evt.reply(f"✓ AI правило удалено для чата `{source_chat_id}` [Клиент: `{control_chat_id}`]")
            except ValueError:
                await evt.reply("⚠️ Неверный формат ID чата")
                
    # /owner (await!)
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
                else:
                    await evt.reply(f"⚠️ Пользователь **{username}** (ID: `{user_id}`) уже был администратором.", parse_mode='md')

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
                else:
                    await evt.reply(f"⚠️ Пользователь с ID `{user_id}` не был в списке администраторов.")
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

    # /бан (await!)
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
                except Exception:
                    await evt.reply(f"✅ Пользователь с ID `{user_id}` добавлен в список заблокированных.", parse_mode='md')
            else:
                await evt.reply(f"⚠️ Пользователь с ID `{user_id}` уже заблокирован.")
        except ValueError:
            await evt.reply("⚠️ Неверный формат ID пользователя. ID должен быть числом.")

    # /ban remove (await!)
    elif cmd == "/ban" and len(parts) >= 2 and parts[1].lower() == "remove":
        if len(parts) < 3:
            await evt.reply("Используйте: `/ban remove <ID>`", parse_mode='md')
            return
        
        try:
            user_id = int(parts[2])
            # await!)
            if await unban_user(user_id):
                await evt.reply(f"✅ Пользователь с ID `{user_id}` удален из списка заблокированных.")
            else:
                await evt.reply(f"⚠️ Пользователь с ID `{user_id}` не был в списке заблокированных.")
        except ValueError:
            await evt.reply("⚠️ Неверный формат ID пользователя.")
        
    # /список бан (await!)
    elif cmd == "/список" and len(parts) >= 2 and parts[1].lower() == "бан":
        # await!)
        banned_ids = await list_banned_users()
        response = f"🚫 **Заблокированные пользователи** ({len(banned_ids)}):\n\n"
        
        if not banned_ids:
            response += "*(Список пуст)*"
        else:
            for uid in banned_ids:
                try:
                    entity = await client.get_entity(uid)
                    response += f"• **{get_display_name(entity)}** (ID: `{uid}`)\n"
                except Exception:
                    response += f"• *Неизвестный пользователь* (ID: `{uid}`)\n"

        await evt.reply(response, parse_mode='md')
    
    # /help
    elif cmd == "/help":
        response = (
            "**🤖 Управление Мониторингом (Клиентский режим)**\n\n"
            "**1. Ключевые слова (привязаны к этому чату):**\n"
            "• `/+слово <слово>`: Добавить глобальное слово (сработает везде).\n"
            "• `/+слово <ID> <слово>`: Добавить слово только для конкретного источника.\n"
            "• `/удалить +слово <слово>`: Удалить глобальное слово.\n"
            "• `/удалить +слово <ID> <слово>`: Удалить слово для конкретного источника.\n"
            "• `/список слов [ID]`\n\n"
            "**2. Исключения (привязаны к этому чату):**\n"
            "• `/минус слово <слово>`: Добавить негативное слово.\n"
            "• `/удалить минус слово <слово>`: Удалить негативное слово.\n"
            "• `/список минус слов`\n\n"
            "**3. Источники (привязаны к этому чату):**\n"
            "• `/добавить чат <ID|ссылка>`: Начать мониторинг канала.\n"
            "• `/удалить чат <ID|ссылка>`: Остановить мониторинг.\n"
            "• `/список чатов`\n\n"
            "**4. AI Фильтрация (привязана к этому чату):**\n"
            "• `/ai set <ID> <правило>`: Установить правило AI для источника.\n"
            "• `/ai show <ID>` / `/ai clear <ID>`\n\n"
            "**5. Действия в чате пересылки:**\n"
            "• Ответьте словом `бан` на сообщение, чтобы заблокировать отправителя.\n"
            "• Ответьте `/Почему` на пересланное сообщение, чтобы увидеть причину.\n\n"
            "**6. Глобальное администрирование (Только Главный Админ):**\n"
            "• `/owner add <ID>` / `/owner remove <ID>` / `/owner list`\n"
            "• `/бан <ID>` / `/ban remove <ID>` / `/список бан`\n"
            "• `/register <control_id> <target_id> <Имя>` (Регистрация нового клиента)"
        )
        await evt.reply(response, parse_mode='md')


# ====== Запуск ======
async def main():
    
    # 0. Инициализация БД (таблиц)
    init_db()
    
    if API_ID == 0 or not API_HASH:
        log.error("CRITICAL: API_ID or API_HASH is empty. Check your .env file!")
        return
        
    await client.start(phone=PHONE)
    me = await client.get_me()
    log.info(f"✓ Started as {me.id} @ {get_display_name(me)}")
    log.info(f"📤 Target chat (Legacy): {TARGET_CHAT_ID}")
    log.info(f"🎛 Control chat (Legacy): {CONTROL_CHAT_ID}")
    log.info(f"👤 Admin user: {ADMIN_USER_ID}")
    
    # 1. Зарегистрируем главного клиента (себя) при первом запуске (await!)
    if CONTROL_CHAT_ID != 0 and TARGET_CHAT_ID != 0:
         if await add_client(CONTROL_CHAT_ID, TARGET_CHAT_ID, "Главный Админ"):
             log.info("✓ Legacy client (Main Admin) registered.")

    await client.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down...")
    except Exception as e:
        log.error(f"FATAL ERROR during bot execution: {e}")