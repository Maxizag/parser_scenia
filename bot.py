import asyncio
import logging
import sqlite3
import os
import re 
from dotenv import load_dotenv
from telethon import TelegramClient, events
import openai # Используем только основной импорт
from telethon.utils import get_peer_id
from telethon.tl.types import User, Channel, Chat 
import json

# Загрузка переменных окружения из .env файла
load_dotenv()

# ====== Настройки: Сначала получаем все переменные окружения! ======
API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
PHONE = os.getenv("TG_PHONE", "")
# ВНИМАНИЕ: TARGET_CHAT_ID, CONTROL_CHAT_ID ниже больше не используются в логике, но остаются для старта.
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "0"))
CONTROL_CHAT_ID = int(os.getenv("CONTROL_CHAT_ID", "0"))
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

# OpenAI API
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# ====== Логирование ======
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

# ====== Инициализация OpenAI (УНИВЕРСАЛЬНОЕ РЕШЕНИЕ) ======
if OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY
    log.info("✓ OpenAI API Key set. AI filtering is active.")
else:
    log.warning("⚠️ OPENAI_API_KEY not found. AI filtering will be skipped.") 

# ====== База данных (НОВАЯ МУЛЬТИКЛИЕНТСКАЯ СТРУКТУРА) ======
DB_FILE = "bot_data.db"
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cursor = conn.cursor()

# 1. Справочник клиентов (НОВАЯ ТАБЛИЦА)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS clients (
        client_id INTEGER PRIMARY KEY AUTOINCREMENT,
        control_chat_id INTEGER UNIQUE NOT NULL,  -- Чат, где клиент вводит команды
        target_chat_id INTEGER UNIQUE NOT NULL,  -- Чат, куда ему приходят пересылки
        name TEXT,
        is_active INTEGER DEFAULT 1
    )
""")

# 2. Ключевые слова (привязаны к control_chat_id и source_chat_id)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS keywords (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        control_chat_id INTEGER NOT NULL,  
        source_chat_id INTEGER NOT NULL,   
        keyword TEXT NOT NULL,
        UNIQUE(control_chat_id, source_chat_id, keyword) 
    )
""")

# 3. Негативные слова (привязаны к control_chat_id)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS negwords (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        control_chat_id INTEGER NOT NULL, 
        negword TEXT NOT NULL,
        UNIQUE(control_chat_id, negword)
    )
""")

# 4. Источники (привязаны к control_chat_id)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS sources (
        source_chat_id INTEGER NOT NULL,      
        control_chat_id INTEGER NOT NULL,     
        chat_title TEXT,
        PRIMARY KEY (source_chat_id, control_chat_id)
    )
""")

# 5. Увиденные сообщения (Глобальная)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS seen_messages (
        msg_key TEXT PRIMARY KEY,
        timestamp INTEGER DEFAULT (strftime('%s', 'now'))
    )
""")

# 6. AI Правила (привязаны к control_chat_id и source_chat_id)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS ai_rules (
        source_chat_id INTEGER NOT NULL,      
        control_chat_id INTEGER NOT NULL,     
        rule TEXT NOT NULL,
        PRIMARY KEY (source_chat_id, control_chat_id)
    )
""")

# 7. Заблокированные пользователи (Глобальная)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS banned_users (
        user_id INTEGER PRIMARY KEY
    )
""")

# 8. Администраторы (Глобальная)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY,
        username TEXT
    )
""")

# 9. Причины пересылки (Глобальная)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS forward_reasons (
        target_msg_id INTEGER PRIMARY KEY,
        reason TEXT
    )
""")

conn.commit()

# ====== Клиент ======
client = TelegramClient("parser_session", API_ID, API_HASH)

# ====== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (СИНХРОННЫЕ) ======

# ----------------- ФУНКЦИИ ДЛЯ КЛИЕНТОВ (НОВЫЕ) -----------------
def add_client(control_id, target_id, name=""):
    """Регистрирует нового клиента."""
    try:
        cursor.execute("INSERT INTO clients (control_chat_id, target_chat_id, name) VALUES (?, ?, ?)", 
                      (control_id, target_id, name))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False # Уже существует

def get_client_by_control(control_id):
    """Находит данные клиента по его чату команд."""
    cursor.execute("SELECT control_chat_id, target_chat_id, name FROM clients WHERE control_chat_id = ?", 
                  (control_id,))
    row = cursor.fetchone()
    if row:
        return {'control_id': row[0], 'target_id': row[1], 'name': row[2]}
    return None

def get_clients_monitoring_source(source_id):
    """Находит всех клиентов, которые мониторят данный источник."""
    # Используем JOIN для поиска всех клиентов, у которых есть запись в sources
    # для данного source_id.
    cursor.execute("""
        SELECT 
            c.control_chat_id, 
            c.target_chat_id 
        FROM clients c
        JOIN sources s ON c.control_chat_id = s.control_chat_id
        WHERE s.source_chat_id = ? AND c.is_active = 1
    """, (source_id,))
    
    return [{'control_id': row[0], 'target_id': row[1]} for row in cursor.fetchall()]
# ----------------- КОНЕЦ ФУНКЦИЙ ДЛЯ КЛИЕНТОВ -----------------


# --- ОБНОВЛЕННЫЕ ФУНКЦИИ КЛЮЧЕВЫХ СЛОВ (Добавлен control_chat_id) ---
def get_keywords(control_chat_id, source_chat_id=None):
    """
    Получает ключевые слова для конкретного клиента (control_chat_id).
    Возвращает ГЛОБАЛЬНЫЕ (source_chat_id=0) + слова для конкретного источника.
    """
    query = "SELECT keyword FROM keywords WHERE control_chat_id = ? AND source_chat_id = 0"
    params = [control_chat_id]
    
    if source_chat_id is not None and source_chat_id != 0:
        query += " OR (control_chat_id = ? AND source_chat_id = ?)"
        params.extend([control_chat_id, source_chat_id])
        
    cursor.execute(query, params)
    return [row[0].lower() for row in cursor.fetchall()]

def add_keyword(kw, control_chat_id, source_chat_id=0):
    """Добавляет слово для конкретного клиента. По умолчанию (source_chat_id=0) - глобально."""
    try:
        cursor.execute("INSERT INTO keywords (control_chat_id, source_chat_id, keyword) VALUES (?, ?, ?)", 
                      (control_chat_id, source_chat_id, kw.lower()))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def delete_keyword(kw, control_chat_id, source_chat_id=0):
    """
    Удаляет слово для конкретного клиента. По умолчанию (source_chat_id=0) - глобально.
    """
    cursor.execute("DELETE FROM keywords WHERE keyword = ? AND control_chat_id = ? AND source_chat_id = ?", 
                  (kw.lower(), control_chat_id, source_chat_id))
    conn.commit()
    return cursor.rowcount > 0 
# --- КОНЕЦ ОБНОВЛЕННЫХ ФУНКЦИЙ КЛЮЧЕВЫХ СЛОВ ---

# --- ОБНОВЛЕННЫЕ ФУНКЦИИ НЕГАТИВНЫХ СЛОВ (Добавлен control_chat_id) ---
def get_negwords(control_chat_id):
    """Получает негативные слова для конкретного клиента."""
    cursor.execute("SELECT negword FROM negwords WHERE control_chat_id = ?", (control_chat_id,))
    return [row[0].lower() for row in cursor.fetchall()]

def add_negword(nw, control_chat_id):
    """Добавляет негативное слово для конкретного клиента."""
    try:
        cursor.execute("INSERT INTO negwords (control_chat_id, negword) VALUES (?, ?)", (control_chat_id, nw.lower()))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def delete_negword(nw, control_chat_id):
    """Удаляет негативное слово для конкретного клиента."""
    cursor.execute("DELETE FROM negwords WHERE negword = ? AND control_chat_id = ?", (nw.lower(), control_chat_id))
    conn.commit()
    return cursor.rowcount > 0 
# --- КОНЕЦ ОБНОВЛЕННЫХ ФУНКЦИЙ НЕГАТИВНЫХ СЛОВ ---

# --- ОБНОВЛЕННЫЕ ФУНКЦИИ ИСТОЧНИКОВ (Добавлен control_chat_id) ---
def list_sources(control_chat_id):
    """Получает список источников, которые мониторит конкретный клиент."""
    cursor.execute("SELECT source_chat_id, chat_title FROM sources WHERE control_chat_id = ?", (control_chat_id,))
    return cursor.fetchall()

def add_source(source_chat_id, control_chat_id, chat_title):
    """Добавляет источник для конкретного клиента."""
    try:
        cursor.execute("INSERT OR REPLACE INTO sources (source_chat_id, control_chat_id, chat_title) VALUES (?, ?, ?)", 
                      (source_chat_id, control_chat_id, chat_title))
        conn.commit()
        return True
    except Exception as e:
        log.error(f"Error adding source: {e}")
        return False

def delete_source(source_chat_id, control_chat_id):
    """Удаляет источник для конкретного клиента."""
    cursor.execute("DELETE FROM sources WHERE source_chat_id = ? AND control_chat_id = ?", (source_chat_id, control_chat_id))
    conn.commit()
    return cursor.rowcount > 0
# --- КОНЕЦ ОБНОВЛЕННЫХ ФУНКЦИЙ ИСТОЧНИКОВ ---

# --- ОБНОВЛЕННЫЕ ФУНКЦИИ AI ПРАВИЛ (Добавлен control_chat_id) ---
def get_ai_rule(source_chat_id, control_chat_id):
    """Получает AI правило для конкретного источника и клиента."""
    cursor.execute("SELECT rule FROM ai_rules WHERE source_chat_id = ? AND control_chat_id = ?", 
                  (source_chat_id, control_chat_id))
    row = cursor.fetchone()
    return row[0] if row else None

def set_ai_rule(source_chat_id, control_chat_id, rule):
    """Устанавливает AI правило для конкретного источника и клиента."""
    cursor.execute("INSERT OR REPLACE INTO ai_rules (source_chat_id, control_chat_id, rule) VALUES (?, ?, ?)", 
                  (source_chat_id, control_chat_id, rule))
    conn.commit()

def clear_ai_rule(source_chat_id, control_chat_id):
    """Удаляет AI правило для конкретного источника и клиента."""
    cursor.execute("DELETE FROM ai_rules WHERE source_chat_id = ? AND control_chat_id = ?", 
                  (source_chat_id, control_chat_id))
    conn.commit()
# --- КОНЕЦ ОБНОВЛЕННЫХ ФУНКЦИЙ AI ПРАВИЛ ---

# ====== ОСТАЛЬНЫЕ СТАНДАРТНЫЕ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (БЕЗ ИЗМЕНЕНИЙ) ======
def is_seen(msg_key):
    cursor.execute("SELECT 1 FROM seen_messages WHERE msg_key = ?", (msg_key,))
    return cursor.fetchone() is not None

def mark_seen(msg_key):
    cursor.execute("INSERT OR IGNORE INTO seen_messages (msg_key) VALUES (?)", (msg_key,))
    conn.commit()

def get_display_name(entity):
    if hasattr(entity, 'title'):
        return entity.title
    name = getattr(entity, 'first_name', '') or ''
    last = getattr(entity, 'last_name', '') or ''
    return f"{name} {last}".strip() or str(entity.id)

def is_admin(user_id):
    """Проверяет, является ли пользователь администратором."""
    if user_id == ADMIN_USER_ID:
        return True 
    cursor.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
    return cursor.fetchone() is not None

def add_admin(user_id, username):
    """Добавляет пользователя в список администраторов."""
    try:
        cursor.execute("INSERT OR IGNORE INTO admins (user_id, username) VALUES (?, ?)", 
                      (user_id, username))
        conn.commit()
        return cursor.rowcount > 0
    except Exception:
        return False

def remove_admin(user_id):
    """Удаляет пользователя из списка администраторов."""
    cursor.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
    conn.commit()
    return cursor.rowcount > 0

def list_admins():
    """Возвращает список всех администраторов."""
    cursor.execute("SELECT user_id, username FROM admins")
    return cursor.fetchall()

def is_banned(user_id):
    cursor.execute("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,))
    return cursor.fetchone() is not None

def ban_user(user_id):
    try:
        cursor.execute("INSERT OR IGNORE INTO banned_users (user_id) VALUES (?)", (user_id,))
        conn.commit()
        return True
    except Exception:
        return False

def unban_user(user_id):
    cursor.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
    conn.commit()
    return cursor.rowcount > 0

def list_banned_users():
    cursor.execute("SELECT user_id FROM banned_users")
    return [row[0] for row in cursor.fetchall()]

def store_forward_reason(target_msg_id, reason):
    cursor.execute("INSERT OR REPLACE INTO forward_reasons (target_msg_id, reason) VALUES (?, ?)", 
                  (target_msg_id, reason))
    conn.commit()

def get_forward_reason(target_msg_id):
    cursor.execute("SELECT reason FROM forward_reasons WHERE target_msg_id = ?", (target_msg_id,))
    row = cursor.fetchone()
    return row[0] if row else None


# --- АСИНХРОННЫЕ ФУНКЦИИ ДЛЯ ПЕРЕСЫЛКИ ---
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
# Теперь функция принимает control_chat_id для получения правильного правила
async def ai_filter(text, source_chat_id, control_chat_id):
    """
    Отправляет текст сообщения и правило в OpenAI для проверки, используя 
    универсальный метод ChatCompletion.acreate для совместимости с сервером.
    Возвращает (bool: прошел ли фильтр, str: вердикт ИИ).
    """
    # 1. Проверка ключа
    if not OPENAI_API_KEY:
        return True, "SKIPPED (OPENAI_API_KEY not set)"

    # 2. Получение правила (с привязкой к клиенту)
    rule = get_ai_rule(source_chat_id, control_chat_id)
    if not rule:
        return True, "SKIPPED (No AI rule set for this chat by client)"
    
    # 3. Запрос к OpenAI
    try:
        
        system_prompt = (
            f"Ты - строгий фильтр контента. Твоя задача - определить, соответствует ли сообщение следующим правилам: "
            f"**Правило:** '{rule}' "
            "Ты должен ответить только одним словом: 'ДА' или 'НЕТ'. "
            "'ДА' означает, что сообщение СОВЕРШЕННО соответствует правилу и его нужно переслать. "
            "'НЕТ' означает, что сообщение НЕ соответствует правилу."
        )
        
        # --- ИСПОЛЬЗУЕМ СТАРЫЙ, УНИВЕРСАЛЬНЫЙ МЕТОД ACREATE ---
        response = await openai.ChatCompletion.acreate( 
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Проверь сообщение: '{text}'"},
            ],
            temperature=0.0,
            max_tokens=3 
        )
        # --- КОНЕЦ УНИВЕРСАЛЬНОГО МЕТОДА ---
        
        verdict = response.choices[0].message.content.strip().upper()
        
        if "ДА" in verdict:
            return True, f"AI VERDICT: Passed (ДА). Rule: {rule[:30]}..."
        else:
            return False, f"AI VERDICT: Failed (НЕТ). Rule: {rule[:30]}..."

    except Exception as e:
        log.error(f"OpenAI API Error (using acreate): {e}")
        # В случае ошибки API пропускаем сообщение
        return True, f"ERROR (AI API FAILED): {e}"


# ---


# ====== Обработка сообщений (КРИТИЧЕСКОЕ ИЗМЕНЕНИЕ: ОБРАБОТКА МНОГИХ КЛИЕНТОВ) ======
@client.on(events.NewMessage())
async def on_message(evt: events.NewMessage.Event):
    source_chat_id = evt.chat_id

    # 1. Проверяем, есть ли вообще клиенты, которые мониторят этот источник
    monitoring_clients = get_clients_monitoring_source(source_chat_id)
    
    if not monitoring_clients:
        # log.debug(f"Source chat {source_chat_id} is not monitored by any active client.")
        return
    
    # 2. Проверяем базовые условия (для всех клиентов)
    msg_key = f"{source_chat_id}:{evt.id}"
    if is_seen(msg_key):
        return

    text = (evt.message.message or "").strip()
    if not text:
        return
    
    text_lower = text.lower()
    
    # ПРОВЕРКА: Блокировка пользователя (Глобально)
    if evt.sender_id and is_banned(evt.sender_id):
        log.info(f"✗ Skipped message from banned user: {evt.sender_id} (Global Ban)")
        return

    # 3. Цикл по каждому клиенту, который слушает этот источник
    for client_data in monitoring_clients:
        control_chat_id = client_data['control_id']
        target_chat_id = client_data['target_id']
        
        # 3.1. Фильтрация ключевыми и негативными словами
        keywords = get_keywords(control_chat_id, source_chat_id)
        negwords = get_negwords(control_chat_id) # Теперь привязаны к клиенту
        
        # Если клиент не установил никаких слов, пропускаем его (не пересылаем ему)
        if not keywords:
            # log.debug(f"Client {control_chat_id} has no keywords for source {source_chat_id}. Skipping.")
            continue
            
        match_keywords = False
        forward_reason = "Ключевое слово не найдено." # Причина по умолчанию
        
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

        # 3.2. Проверка ИИ (с привязкой к клиенту)
        ai_passed, ai_verdict = await ai_filter(text, source_chat_id, control_chat_id)

        # 3.3. Логика пересылки
        if ai_passed:
            try:
                # --- ЛОГИКА ПЕРЕСЫЛКИ: ФОРМАТИРОВАНИЕ ТЕКСТА ---
                
                # Получаем данные (один раз)
                chat_title = await get_chat_title(source_chat_id)
                sender_info = await get_sender_info(evt.message.sender_id)
                
                channel_id_for_link = str(source_chat_id).replace('-100', '')
                original_link = f"https://t.me/c/{channel_id_for_link}/{evt.id}"

                # Формируем сообщение
                header = f"**Монитор Клиента: {control_chat_id}**" # Можно добавить имя клиента
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
                    f"{text}" # Оригинальный текст сообщения
                )
                
                # Отправляем новое сообщение В ЧАТ ПЕРЕСЫЛКИ ДАННОГО КЛИЕНТА
                sent_msg = await client.send_message(
                    target_chat_id, # <--- ТАРГЕТ ИЗ БАЗЫ ДАННЫХ
                    final_text, 
                    link_preview=False, 
                    parse_mode='md' 
                )
                
                # Сохраняем причину для команды /why (target_msg_id привязан к сообщению в целевом чате)
                store_forward_reason(sent_msg.id, forward_reason)

                log.info(f"✓ Sent to client {control_chat_id} from {source_chat_id}: {text[:50]}... | AI: {ai_verdict}")
                
            except Exception as e:
                log.error(f"Failed to process message for client {control_chat_id}: {e}")
        else:
            log.info(f"✗ Filtered out for client {control_chat_id} (AI Failed): {text[:50]}... | AI: {ai_verdict}")

    # 4. Отмечаем сообщение как увиденное (Глобально)
    mark_seen(msg_key)

    await asyncio.sleep(0.6)

# ---

# ====== БЫСТРАЯ КОМАНДА 'бан' В ЦЕЛЕВОМ ЧАТЕ (TARGET_CHAT_ID) ======
@client.on(events.NewMessage(chats=TARGET_CHAT_ID)) 
async def on_quick_ban(evt: events.NewMessage.Event):
    # Эта команда работает только в старом TARGET_CHAT_ID (главного клиента), 
    # или нужно будет переделать обработчик для всех target_chat_id
    
    # 1. Определяем, в каком чате пришла команда
    client_data = get_client_by_control(evt.chat_id)
    
    # Если команда пришла в целевой чат, но мы не знаем, чей он, игнорируем.
    # TODO: Здесь нужно будет проверить, является ли evt.chat_id одним из target_chat_id
    # Пока оставим только для старого TARGET_CHAT_ID
    if evt.chat_id != TARGET_CHAT_ID:
         # Игнорируем, пока не решим, как правильно определять TargetChat
         return

    # ... (Остальной код quick_ban остается прежним, так как бан глобальный) ...
    if (evt.message.message or "").strip().lower() != 'бан':
        return
        
    if not is_admin(evt.sender_id):
        return
    
    if not evt.reply_to_msg_id:
        return

    try:
        replied_msg = await client.get_messages(evt.chat_id, ids=evt.reply_to_msg_id)
        text_to_search = replied_msg.message or ""
        match = re.search(r'UID: (\d+)', text_to_search) 
        
        if not match:
            await evt.reply("⚠️ Не удалось найти ID пользователя (`UID: <ID>`) в тексте этого сообщения. Проверьте форматирование.", parse_mode='md')
            return

        user_id_to_ban = int(match.group(1))

        if ban_user(user_id_to_ban):
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

# ====== ОТДЕЛЬНЫЙ ОБРАБОТЧИК ДЛЯ КОМАНДЫ 'Почему' (Пока только в старых чатах) ======
@client.on(events.NewMessage(chats=[CONTROL_CHAT_ID, TARGET_CHAT_ID], pattern=r'^/Почему'))
async def on_command_why(evt: events.NewMessage.Event):
    # TODO: Здесь также нужно будет переделать логику для работы во всех target_chat_id клиентов
    if not is_admin(evt.sender_id):
        return
    
    if evt.reply_to_msg_id:
        target_msg_id = evt.reply_to_msg_id
        reason = get_forward_reason(target_msg_id)
        
        if reason:
            await evt.reply(f"🔍 **Причина пересылки**:\n{reason}", parse_mode='md')
        else:
            await evt.reply("⚠️ Не удалось найти причину пересылки для этого сообщения. Убедитесь, что вы отвечаете на сообщение, которое **только что** переслал бот.", parse_mode='md')
    else:
        await evt.reply("Используйте /Почему, ответив на пересланное сообщение в этом чате.", parse_mode='md')

# ---

# ====== ОСНОВНОЙ ОБРАБОТЧИК КОМАНД (КРИТИЧЕСКОЕ ИЗМЕНЕНИЕ: ИДЕНТИФИКАЦИЯ КЛИЕНТА) ======
@client.on(events.NewMessage(pattern=r'^/'))
async def on_command(evt: events.NewMessage.Event):
    
    # 1. Проверяем, главный ли это администратор (только он может регистрировать клиентов)
    is_main_admin = is_admin(evt.sender_id)
    
    # 2. Идентифицируем клиента по чату команд
    client_data = get_client_by_control(evt.chat_id)
    
    # Если это не главный админ, и это не зарегистрированный чат клиента, то игнорируем
    if not is_main_admin and not client_data:
        # log.debug(f"Command ignored: not main admin and not a client chat: {evt.chat_id}")
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
        # Должно быть отловлено выше, но на всякий случай
        return 

    # Если команда от клиента, то он должен быть админом в своем чате
    if is_client_command and not is_admin(evt.sender_id):
         await evt.reply("❌ У вас нет прав для управления ботом в этом чате. Обратитесь к главному администратору.")
         return
         
    # --- Парсинг команды ---
    text = evt.message.message.strip()
    parts = text.split(maxsplit=2) 
    
    if len(parts) < 1:
        return
    
    cmd = parts[0].lower()
    
    # ====================================================================
    # НОВАЯ ГЛАВНАЯ КОМАНДА: /register (Только для главного админа)
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
            
            if add_client(control_id, target_id, name):
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
            
    # /+слово - управление ключевыми словами (ДОБАВЛЕН control_chat_id)
    elif cmd == "/+слово":
        
        subcmd = "add"
        command_prefix = cmd
        
        # source_chat_id для глобальных слов
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
            
            if add_keyword(keyword, control_chat_id, source_chat_id): # <--- ИСПОЛЬЗУЕМ control_chat_id
                chat_name = await get_chat_title(source_chat_id)
                await evt.reply(f"✓ Добавлено слово **'{keyword}'** для: **{chat_name}** (ID: `{source_chat_id}`) [Клиент: `{control_chat_id}`]", parse_mode='md')
            else:
                chat_name = await get_chat_title(source_chat_id)
                await evt.reply(f"⚠️ Уже существует: **{keyword}** для {chat_name} [Клиент: `{control_chat_id}`]", parse_mode='md')

    # /удалить +слово - управление ключевыми словами (ДОБАВЛЕН control_chat_id)
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
            
        if delete_keyword(keyword, control_chat_id, source_chat_id): # <--- ИСПОЛЬЗУЕМ control_chat_id
            chat_name = await get_chat_title(source_chat_id)
            await evt.reply(f"✓ Удалено слово **'{keyword}'** для: **{chat_name}** (ID: `{source_chat_id}`) [Клиент: `{control_chat_id}`]", parse_mode='md')
        else:
            chat_name = await get_chat_title(source_chat_id)
            await evt.reply(f"⚠️ Слово **'{keyword}'** не найдено для: {chat_name} [Клиент: `{control_chat_id}`]", parse_mode='md')


    # /список слов - список ключевых слов (ДОБАВЛЕН control_chat_id)
    elif cmd == "/список" and len(parts) >= 2 and parts[1].lower() == "слов":
        
        source_chat_id = 0
        if len(parts) == 3:
            try:
                source_chat_id = int(parts[2])
            except ValueError:
                await evt.reply("⚠️ Ошибка: ID чата должен быть числом.", parse_mode='md')
                return
                
        # Получаем слова с привязкой к клиенту
        kws_global = get_keywords(control_chat_id, 0)
        kws_chat = get_keywords(control_chat_id, source_chat_id) if source_chat_id else []
        
        unique_kws = sorted(list(set(kws_global + kws_chat)))
        
        if source_chat_id == 0:
            title = f"📝 Глобальные ключевые слова [Клиент: `{control_chat_id}`]"
            
        else:
            chat_name = await get_chat_title(source_chat_id)
            title = f"📝 Ключевые слова для: **{chat_name}** (ID: `{source_chat_id}`) [Клиент: `{control_chat_id}`]"
            
        response = f"{title} (Всего: {len(unique_kws)}):\n\n"
        
        if source_chat_id != 0:
            local_only = [kw for kw in kws_chat if kw not in kws_global]
            
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

            
    # /минус слово - управление негативными словами (ДОБАВЛЕН control_chat_id)
    elif cmd == "/минус" and len(parts) >= 2 and parts[1].lower() == "слово":
        
        if len(parts) < 3:
            nws = get_negwords(control_chat_id) # <--- ИСПОЛЬЗУЕМ control_chat_id
            await evt.reply(f"🚫 Негативные слова [Клиент: `{control_chat_id}`] ({len(nws)}):\n" + "\n".join(f"• {nw}" for nw in nws) if nws else "Список пуст", parse_mode='md')
            await evt.reply("Используйте: `/минус слово <слово>` | `/удалить минус слово <слово>` | `/список минус слов`", parse_mode='md')
            return
            
        nw = parts[2].strip()
        if add_negword(nw, control_chat_id): # <--- ИСПОЛЬЗУЕМ control_chat_id
            await evt.reply(f"✓ Добавлено негативное слово: {nw} [Клиент: `{control_chat_id}`]")
        else:
            await evt.reply(f"⚠️ Уже существует: {nw} [Клиент: `{control_chat_id}`]")

    # /удалить минус слово - удаление негативных слов (ДОБАВЛЕН control_chat_id)
    elif cmd == "/удалить" and len(parts) >= 3 and parts[1].lower() == "минус" and parts[2].lower() == "слово":
        
        command_prefix = f"{cmd} {parts[1]} {parts[2]}"
        remaining_text = text[len(command_prefix):].strip()
        
        if not remaining_text:
            await evt.reply("⚠️ Неверный формат команды. Используйте: `/удалить минус слово <слово>`", parse_mode='md')
            return
            
        nw = remaining_text.strip()
        if delete_negword(nw, control_chat_id): # <--- ИСПОЛЬЗУЕМ control_chat_id
            await evt.reply(f"✓ Удалено негативное слово: {nw} [Клиент: `{control_chat_id}`]")
        else:
            await evt.reply(f"⚠️ Слово не найдено: {nw} [Клиент: `{control_chat_id}`]")


    # /список минус слов - список негативных слов (ДОБАВЛЕН control_chat_id)
    elif cmd == "/список" and len(parts) >= 3 and parts[1].lower() == "минус" and parts[2].lower() == "слов":
        nws = get_negwords(control_chat_id) # <--- ИСПОЛЬЗУЕМ control_chat_id
        await evt.reply(f"🚫 Негативные слова [Клиент: `{control_chat_id}`] ({len(nws)}):\n" + "\n".join(f"• {nw}" for nw in nws) if nws else "Список пуст", parse_mode='md')


    # /добавить чат - управление источниками (ДОБАВЛЕН control_chat_id)
    elif cmd == "/добавить" and len(parts) >= 2 and parts[1].lower() == "чат":
        
        if len(parts) < 3:
            sources = list_sources(control_chat_id) # <--- ИСПОЛЬЗУЕМ control_chat_id
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

            if add_source(source_chat_id, control_chat_id, title): # <--- ИСПОЛЬЗУЕМ control_chat_id
                await evt.reply(f"✓ Добавлен источник: **{title}** (ID: `{source_chat_id}`) [Клиент: `{control_chat_id}`]", parse_mode='md')
            else:
                await evt.reply(f"⚠️ Ошибка добавления источника")
        
        except ValueError:
            await evt.reply("⚠️ Неверный формат ID/ссылки.")
        except Exception as e:
            await evt.reply(f"⚠️ Ошибка: Не удалось найти чат по ссылке или ID. Возможно, бот не состоит в этом чате. Ошибка: {e}")
    
    # /удалить чат - управление источниками (ДОБАВЛЕН control_chat_id)
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
                
            if delete_source(source_chat_id, control_chat_id): # <--- ИСПОЛЬЗУЕМ control_chat_id
                await evt.reply(f"✓ Источник `{source_chat_id}` (**{get_display_name(entity)}**) удален [Клиент: `{control_chat_id}`].", parse_mode='md')
            else:
                await evt.reply(f"⚠️ Источник `{source_chat_id}` не найден в списке [Клиент: `{control_chat_id}`].", parse_mode='md')
        except ValueError:
            await evt.reply("⚠️ Неверный формат ID/ссылки.")
        except Exception:
            try:
                source_chat_id = int(chat_input)
                if delete_source(source_chat_id, control_chat_id): # <--- ИСПОЛЬЗУЕМ control_chat_id
                    await evt.reply(f"✓ Источник `{source_chat_id}` удален [Клиент: `{control_chat_id}`].", parse_mode='md')
                else:
                    await evt.reply(f"⚠️ Источник `{source_chat_id}` не найден в списке [Клиент: `{control_chat_id}`].", parse_mode='md')
            except ValueError:
                await evt.reply("⚠️ Не удалось определить ID источника. Используйте ID, @username или ссылку.")

    # /список чатов - список источников (ДОБАВЛЕН control_chat_id)
    elif cmd == "/список" and len(parts) >= 2 and parts[1].lower() == "чатов":
        sources = list_sources(control_chat_id) # <--- ИСПОЛЬЗУЕМ control_chat_id
        await evt.reply(f"📢 Источники [Клиент: `{control_chat_id}`] ({len(sources)}):\n" + 
                      "\n".join(f"• {title} (ID: `{cid}`)" for cid, title in sources), parse_mode='md')
    
    # /ai - управление AI правилами (ДОБАВЛЕН control_chat_id)
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
                
                # Устанавливаем правило с привязкой к клиенту
                set_ai_rule(source_chat_id, control_chat_id, rule) 
                
                await evt.reply(f"✓ AI правило установлено для источника `{source_chat_id}` [Клиент: `{control_chat_id}`]")
            except ValueError:
                await evt.reply("⚠️ Неверный формат ID чата")
                
        elif subcmd == "show" and len(parts) == 3:
            try:
                source_chat_id = int(parts[2])
                rule = get_ai_rule(source_chat_id, control_chat_id) # <--- ИСПОЛЬЗУЕМ control_chat_id
                if rule:
                    await evt.reply(f"AI правило для `{source_chat_id}` [Клиент: `{control_chat_id}`]:\n{rule}")
                else:
                    await evt.reply(f"Нет правила для чата `{source_chat_id}` [Клиент: `{control_chat_id}`]")
            except ValueError:
                await evt.reply("⚠️ Неверный формат ID чата")
                
        elif subcmd == "clear" and len(parts) == 3:
            try:
                source_chat_id = int(parts[2])
                clear_ai_rule(source_chat_id, control_chat_id) # <--- ИСПОЛЬЗУЕМ control_chat_id
                await evt.reply(f"✓ AI правило удалено для чата `{source_chat_id}` [Клиент: `{control_chat_id}`]")
            except ValueError:
                await evt.reply("⚠️ Неверный формат ID чата")
                
    # /owner - управление администраторами (ГЛОБАЛЬНОЕ УПРАВЛЕНИЕ)
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
                
                if add_admin(user_id, username):
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
                
                if remove_admin(user_id):
                    await evt.reply(f"✅ Пользователь с ID `{user_id}` удален из администраторов.")
                else:
                    await evt.reply(f"⚠️ Пользователь с ID `{user_id}` не был в списке администраторов.")
            except ValueError:
                await evt.reply("⚠️ Неверный формат ID пользователя.")
                
        elif subcmd == "list":
            admins = list_admins()
            response = f"**👤 Администраторы** ({len(admins)}):\n\n"
            response += f"**• Главный Администратор** (ID: `{ADMIN_USER_ID}`) *из .env*\n"
            
            for uid, username in admins:
                if uid != ADMIN_USER_ID:
                    response += f"• **{username}** (ID: `{uid}`)\n"

            await evt.reply(response, parse_mode='md')

    # /бан - управление заблокированными пользователями (ГЛОБАЛЬНО)
    elif cmd == "/бан":
        if len(parts) < 2:
            await evt.reply("Используйте: `/бан <ID>` | `/ban remove <ID>` | `/список бан`", parse_mode='md')
            return
            
        try:
            user_id = int(parts[1])
            if ban_user(user_id):
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

    # /ban remove - удаление из заблокированных
    elif cmd == "/ban" and len(parts) >= 2 and parts[1].lower() == "remove":
        if len(parts) < 3:
            await evt.reply("Используйте: `/ban remove <ID>`", parse_mode='md')
            return
        
        try:
            user_id = int(parts[2])
            if unban_user(user_id):
                await evt.reply(f"✅ Пользователь с ID `{user_id}` удален из списка заблокированных.")
            else:
                await evt.reply(f"⚠️ Пользователь с ID `{user_id}` не был в списке заблокированных.")
        except ValueError:
            await evt.reply("⚠️ Неверный формат ID пользователя.")
        
    # /список бан - список заблокированных пользователей
    elif cmd == "/список" and len(parts) >= 2 and parts[1].lower() == "бан":
        banned_ids = list_banned_users()
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
    
    # /help - помощь
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
    await client.start(phone=PHONE)
    me = await client.get_me()
    log.info(f"✓ Started as {me.id} @ {get_display_name(me)}")
    log.info(f"📤 Target chat (Legacy): {TARGET_CHAT_ID}")
    log.info(f"🎛 Control chat (Legacy): {CONTROL_CHAT_ID}")
    log.info(f"👤 Admin user: {ADMIN_USER_ID}")
    
    # 1. Зарегистрируем главного клиента (себя) при первом запуске
    if CONTROL_CHAT_ID != 0 and TARGET_CHAT_ID != 0:
         if add_client(CONTROL_CHAT_ID, TARGET_CHAT_ID, "Главный Админ"):
             log.info("✓ Legacy client (Main Admin) registered.")
         # else:
             # log.info("Legacy client already registered.")

    await client.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        conn.close()