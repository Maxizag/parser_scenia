import asyncio
import logging
import sqlite3
import os
import re 
from dotenv import load_dotenv
from telethon import TelegramClient, events
import openai
from telethon.utils import get_peer_id
from telethon.tl.types import User, Channel, Chat # Добавил Channel, Chat для проверки Entity

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
if OPENAI_API_KEY:
   openai.api_key = OPENAI_API_KEY

# ОЧЕНЬ ВАЖНАЯ СТРОКА ДЛЯ ОТЛАДКИ:
print(f"DEBUG: Key status: {bool(OPENAI_API_KEY)} | Prefix: {OPENAI_API_KEY[:7]}")


# Логирование
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

# ====== База данных ======
DB_FILE = "bot_data.db"
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cursor = conn.cursor()


# Создание таблиц
cursor.execute("""
    CREATE TABLE IF NOT EXISTS keywords (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        keyword TEXT UNIQUE NOT NULL
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS negwords (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        negword TEXT UNIQUE NOT NULL
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS sources (
        chat_id INTEGER PRIMARY KEY,
        chat_title TEXT
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
        chat_id INTEGER PRIMARY KEY,
        rule TEXT NOT NULL
    )
""")

# НОВАЯ ТАБЛИЦА: Заблокированные пользователи
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
conn.commit()

# НОВАЯ ТАБЛИЦА: Причины пересылки (для команды /why)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS forward_reasons (
        target_msg_id INTEGER PRIMARY KEY,
        reason TEXT
    )
""")


conn.commit()

# ====== Клиент ======
client = TelegramClient("parser_session", API_ID, API_HASH)

# ====== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (СИНХРОННЫЕ И АСИНХРОННЫЕ) ======

def get_keywords():
    cursor.execute("SELECT keyword FROM keywords")
    return [row[0].lower() for row in cursor.fetchall()]

def add_keyword(kw):
    try:
        cursor.execute("INSERT INTO keywords (keyword) VALUES (?)", (kw.lower(),))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def delete_keyword(kw):
    cursor.execute("DELETE FROM keywords WHERE keyword = ?", (kw.lower(),))
    conn.commit()
    return cursor.rowcount > 0 

def get_negwords():
    cursor.execute("SELECT negword FROM negwords")
    return [row[0].lower() for row in cursor.fetchall()]

def add_negword(nw):
    try:
        cursor.execute("INSERT INTO negwords (negword) VALUES (?)", (nw.lower(),))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def delete_negword(nw):
    cursor.execute("DELETE FROM negwords WHERE negword = ?", (nw.lower(),))
    conn.commit()
    return cursor.rowcount > 0 

def list_sources():
    cursor.execute("SELECT chat_id, chat_title FROM sources")
    return cursor.fetchall()

def add_source(chat_id, chat_title):
    try:
        cursor.execute("INSERT OR REPLACE INTO sources (chat_id, chat_title) VALUES (?, ?)", 
                      (chat_id, chat_title))
        conn.commit()
        return True
    except Exception as e:
        log.error(f"Error adding source: {e}")
        return False

def delete_source(chat_id): # <-- НОВАЯ ФУНКЦИЯ
    cursor.execute("DELETE FROM sources WHERE chat_id = ?", (chat_id,))
    conn.commit()
    return cursor.rowcount > 0

def is_seen(msg_key):
    cursor.execute("SELECT 1 FROM seen_messages WHERE msg_key = ?", (msg_key,))
    return cursor.fetchone() is not None

def mark_seen(msg_key):
    cursor.execute("INSERT OR IGNORE INTO seen_messages (msg_key) VALUES (?)", (msg_key,))
    conn.commit()

def get_ai_rule(chat_id):
    cursor.execute("SELECT rule FROM ai_rules WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    return row[0] if row else None

def set_ai_rule(chat_id, rule):
    cursor.execute("INSERT OR REPLACE INTO ai_rules (chat_id, rule) VALUES (?, ?)", 
                  (chat_id, rule))
    conn.commit()

def clear_ai_rule(chat_id):
    cursor.execute("DELETE FROM ai_rules WHERE chat_id = ?", (chat_id,))
    conn.commit()

def get_display_name(entity):
    if hasattr(entity, 'title'):
        return entity.title
    name = getattr(entity, 'first_name', '') or ''
    last = getattr(entity, 'last_name', '') or ''
    return f"{name} {last}".strip() or str(entity.id)

def is_admin(user_id):
    """Проверяет, является ли пользователь администратором."""
    # Также проверяем ADMIN_USER_ID из .env для совместимости
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

# --- БЛОКИРОВКА И ПРИЧИНЫ ПЕРЕСЫЛКИ ---
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
# ----------------------------------------


# --- АСИНХРОННЫЕ ФУНКЦИИ ДЛЯ ПЕРЕСЫЛКИ ---
async def get_chat_title(chat_id):
    """Получает название чата по ID."""
    try:
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


# ====== AI фильтр (ЗАГЛУШКА) ======
async def ai_filter(text, chat_id):
    # Эта функция теперь всегда возвращает True, пропуская сообщение
    # и сообщая в лог, что AI-проверка заглушена.
    # Чтобы включить AI, верните оригинальный код ai_filter
    return True, "SKIPPED (AI check is intentionally disabled)"


# ====== Обработка сообщений ======
@client.on(events.NewMessage())
async def on_message(evt: events.NewMessage.Event):
    # Проверяем, что сообщение из отслеживаемого чата (но не из контрольного)
    source_ids = [s[0] for s in list_sources()]
    if evt.chat_id not in source_ids:
        return
    
    # Игнорируем сообщения из контрольного чата
    if evt.chat_id == CONTROL_CHAT_ID:
        return

    # НОВАЯ ПРОВЕРКА: Блокировка пользователя
    if evt.sender_id and is_banned(evt.sender_id):
        log.info(f"✗ Skipped message from banned user: {evt.sender_id}")
        return

    msg_key = f"{evt.chat_id}:{evt.id}"
    if is_seen(msg_key):
        return

    text = (evt.message.message or "").strip()
    if not text:
        return
    
    text_lower = text.lower()
    
    # Этап 1: ключевые слова
    keywords = get_keywords()
    negwords = get_negwords()
    
    if not keywords:
        log.debug("No keywords set, skipping message")
        return
    
    # Находим точное совпадение для /why
    match_keywords = False
    forward_reason = "Ключевое слово не найдено." # Причина по умолчанию
    for kw in keywords:
        if kw in text_lower:
            match_keywords = True
            forward_reason = f"Сообщение содержит ключевое слово: **{kw}**"
            break

    match_negwords = any(nw in text_lower for nw in negwords)

    if not match_keywords:
        log.debug(f"Message doesn't match keywords: {text[:50]}")
        return
    
    if match_negwords:
        log.debug(f"Message contains negwords: {text[:50]}")
        return

    # Этап 2: проверка ИИ (теперь заглушена/пропущена)
    ai_passed, ai_verdict = True, "SKIPPED (AI check is intentionally disabled)"
    
    if ai_passed:
        try:
            # --- ЛОГИКА ПЕРЕСЫЛКИ: ФОРМАТИРОВАНИЕ ТЕКСТА ---
            
            # 1. Получаем необходимые данные (асинхронные вызовы)
            chat_title = await get_chat_title(evt.chat_id)
            sender_info = await get_sender_info(evt.message.sender_id)
            
            # ID канала для ссылки (убираем '-100' и делаем ссылку)
            channel_id_for_link = str(evt.chat_id).replace('-100', '')
            original_link = f"https://t.me/c/{channel_id_for_link}/{evt.id}"

            # 2. Формируем структурированное сообщение (используем Markdown)
            header = f"**Монитор чатов**"
            chat_line = f"Чат: [{chat_title}]({original_link})"
            
            # Форматирование отправителя: всегда включаем ID для команды /ban
            sender_display = f"@{sender_info['username']}" if sender_info['username'] else f"ID {sender_info['id']}"
            sender_line = f"Отправитель: {sender_display}\nUID: `{sender_info['id']}`" 
            
            separator = "—" * 20
            
            # Собираем финальное сообщение
            final_text = (
                f"{header}\n"
                f"{chat_line}\n"
                f"{sender_line}\n"
                f"{separator}\n\n"
                f"{text}" # Оригинальный текст сообщения
            )
            
            # 3. Отправляем новое сообщение (вместо форварда)
            sent_msg = await client.send_message(
                TARGET_CHAT_ID, 
                final_text, 
                link_preview=False, 
                parse_mode='md' 
            )
            
            # 4. Сохраняем причину для команды /why
            store_forward_reason(sent_msg.id, forward_reason)

            mark_seen(msg_key)
            log.info(f"✓ Formatted message sent from {evt.chat_id}: {text[:50]}... | AI: {ai_verdict}")
            
        except Exception as e:
            log.error(f"Failed to process message: {e}")
    else:
        log.info(f"✗ Filtered out from {evt.chat_id}: {text[:50]}... | AI: {ai_verdict}")
        mark_seen(msg_key)

    await asyncio.sleep(0.6)

# ====== ОТДЕЛЬНЫЙ ОБРАБОТЧИК ДЛЯ КОМАНДЫ /WHY (РАБОТАЕТ В ОБОИХ ЧАТАХ) ======
@client.on(events.NewMessage(chats=[CONTROL_CHAT_ID, TARGET_CHAT_ID], pattern=r'^/why'))
async def on_command_why(evt: events.NewMessage.Event):
    # Проверка: только администратор (или основной владелец из .env) может использовать команды
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
        await evt.reply("Используйте /why, ответив на пересланное сообщение в этом чате.", parse_mode='md')

# ====== ОСНОВНОЙ ОБРАБОТЧИК КОМАНД ======
@client.on(events.NewMessage(chats=CONTROL_CHAT_ID, pattern=r'^/'))
async def on_command(evt: events.NewMessage.Event):
    # Проверка: только администратор (или основной владелец из .env) может использовать команды
    if not is_admin(evt.sender_id): 
        log.warning(f"⚠️ Unauthorized command attempt from user {evt.sender_id}")
        await evt.reply("❌ У вас нет прав для управления ботом")
        return
    
    text = evt.message.message.strip()
    parts = text.split(maxsplit=2)
    
    if len(parts) < 1:
        return
    
    cmd = parts[0].lower()
    
    # /kw - управление ключевыми словами (без изменений)
    if cmd == "/kw":
        if len(parts) < 2:
            kws = get_keywords()
            await evt.reply(f"📝 Ключевые слова ({len(kws)}):\n" + "\n".join(f"• {kw}" for kw in kws) if kws else "Список пуст")
            return
        
        subcmd = parts[1].lower()
        if subcmd == "add" and len(parts) == 3:
            kw = parts[2].strip()
            if add_keyword(kw):
                await evt.reply(f"✓ Добавлено: {kw}")
            else:
                await evt.reply(f"⚠️ Уже существует: {kw}")
        elif subcmd == "del" and len(parts) == 3:
            kw = parts[2].strip()
            if delete_keyword(kw):
                await evt.reply(f"✓ Удалено: {kw}")
            else:
                await evt.reply(f"⚠️ Слово не найдено: {kw}")
        elif subcmd == "list":
            kws = get_keywords()
            await evt.reply(f"📝 Ключевые слова ({len(kws)}):\n" + "\n".join(f"• {kw}" for kw in kws) if kws else "Список пуст")
    
    # /neg - управление негативными словами (без изменений)
    elif cmd == "/neg":
        if len(parts) < 2:
            nws = get_negwords()
            await evt.reply(f"🚫 Негативные слова ({len(nws)}):\n" + "\n".join(f"• {nw}" for nw in nws) if nws else "Список пуст")
            return
        
        subcmd = parts[1].lower()
        if subcmd == "add" and len(parts) == 3:
            nw = parts[2].strip()
            if add_negword(nw):
                await evt.reply(f"✓ Добавлено: {nw}")
            else:
                await evt.reply(f"⚠️ Уже существует: {nw}")
        elif subcmd == "del" and len(parts) == 3:
            nw = parts[2].strip()
            if delete_negword(nw):
                await evt.reply(f"✓ Удалено: {nw}")
            else:
                await evt.reply(f"⚠️ Слово не найдено: {nw}")
        elif subcmd == "list":
            nws = get_negwords()
            await evt.reply(f"🚫 Негативные слова ({len(nws)}):\n" + "\n".join(f"• {nw}" for nw in nws) if nws else "Список пуст")
            
    # /src - управление источниками
    elif cmd == "/src":
        if len(parts) < 2:
            sources = list_sources()
            await evt.reply(f"📢 Источники ({len(sources)}):\n" + 
                          "\n".join(f"• {title} (ID: `{cid}`)" for cid, title in sources), parse_mode='md')
            return
        
        subcmd = parts[1].lower()
        
        if subcmd == "add" and len(parts) == 3:
            chat_input = parts[2].strip()
            
            try:
                # 1. Получаем сущность (entity) по ID или ссылке
                entity = await client.get_entity(chat_input) 
                
                # 2. Проверка, что это группа/канал, а не просто пользователь
                if isinstance(entity, User):
                    await evt.reply(f"⚠️ Ошибка: '{chat_input}' — это ID/username пользователя. Требуется ID чата, ссылка на канал/группу.", parse_mode='md')
                    return
                
                # 3. Извлекаем числовой ID, используя peer_id для надежности
                # Мы используем get_peer_id, но без add_mark, так как entity.id уже должен быть правильным, 
                # а проверка на тип Entity надежнее.
                chat_id = entity.id 
                
                # Проверка: ID должен быть отрицательным (для каналов/групп)
                if chat_id > 0:
                    chat_id = get_peer_id(entity, add_mark=True)
                
                # 4. Получаем название
                title = get_display_name(entity)

                if add_source(chat_id, title):
                    await evt.reply(f"✓ Добавлен источник: **{title}** (ID: `{chat_id}`)", parse_mode='md')
                else:
                    await evt.reply(f"⚠️ Ошибка добавления источника")
            
            except ValueError:
                await evt.reply("⚠️ Неверный формат ID/ссылки.")
            except Exception as e:
                # Telethon может выдать ошибку, если не может найти чат или бот не в нем
                await evt.reply(f"⚠️ Ошибка: Не удалось найти чат по ссылке или ID. Возможно, бот не состоит в этом чате. Ошибка: {e}")
        
        elif subcmd == "del" and len(parts) == 3: # <-- НОВАЯ КОМАНДА
            chat_input = parts[2].strip()
            try:
                # Попытка получить ID из ссылки/ID
                entity = await client.get_entity(chat_input)
                chat_id = entity.id 
                if chat_id > 0: # Если это не канал/группа, пытаемся получить ID
                    chat_id = get_peer_id(entity, add_mark=True)
                    
                if delete_source(chat_id):
                    await evt.reply(f"✓ Источник `{chat_id}` (**{get_display_name(entity)}**) удален.", parse_mode='md')
                else:
                    await evt.reply(f"⚠️ Источник `{chat_id}` не найден в списке.", parse_mode='md')
            except ValueError:
                await evt.reply("⚠️ Неверный формат ID/ссылки.")
            except Exception:
                # Если не удалось получить сущность, ищем по введенному ID, если это число
                try:
                    chat_id = int(chat_input)
                    if delete_source(chat_id):
                        await evt.reply(f"✓ Источник `{chat_id}` удален.", parse_mode='md')
                    else:
                        await evt.reply(f"⚠️ Источник `{chat_id}` не найден в списке.", parse_mode='md')
                except ValueError:
                    await evt.reply("⚠️ Не удалось определить ID источника. Используйте ID, @username или ссылку.")

        elif subcmd == "list":
            sources = list_sources()
            await evt.reply(f"📢 Источники ({len(sources)}):\n" + 
                          "\n".join(f"• {title} (ID: `{cid}`)" for cid, title in sources), parse_mode='md')
    
    # /ai - управление AI правилами (без изменений)
    elif cmd == "/ai":
        if len(parts) < 2:
            await evt.reply("Используйте: /ai set <chat_id> <правило> | /ai show <chat_id> | /ai clear <chat_id>")
            return
        
        subcmd = parts[1].lower()
        if subcmd == "set" and len(parts) == 3:
            try:
                chat_id_and_rule = parts[2].split(maxsplit=1)
                if len(chat_id_and_rule) < 2:
                    await evt.reply("Формат: /ai set <chat_id> <правило>")
                    return
                chat_id = int(chat_id_and_rule[0])
                rule = chat_id_and_rule[1]
                set_ai_rule(chat_id, rule)
                await evt.reply(f"✓ AI правило установлено для чата {chat_id}")
            except ValueError:
                await evt.reply("⚠️ Неверный формат ID чата")
        elif subcmd == "show" and len(parts) == 3:
            try:
                chat_id = int(parts[2])
                rule = get_ai_rule(chat_id)
                if rule:
                    await evt.reply(f"AI правило для {chat_id}:\n{rule}")
                else:
                    await evt.reply(f"Нет правила для чата {chat_id}")
            except ValueError:
                await evt.reply("⚠️ Неверный формат ID чата")
        elif subcmd == "clear" and len(parts) == 3:
            try:
                chat_id = int(parts[2])
                clear_ai_rule(chat_id)
                await evt.reply(f"✓ AI правило удалено для чата {chat_id}")
            except ValueError:
                await evt.reply("⚠️ Неверный формат ID чата")
                
    # /why команда была перенесена в отдельный обработчик on_command_why
    # ...

    # НОВАЯ КОМАНДА: /ban - блокировка пользователя
    elif cmd == "/ban":
        if len(parts) < 2 or parts[1].lower() == "list":
            banned_list = list_banned_users()
            if banned_list:
                usernames = []
                for user_id in banned_list:
                    try:
                        entity = await client.get_entity(user_id)
                        usernames.append(f"• {get_display_name(entity)} (ID: {user_id})")
                    except Exception:
                        usernames.append(f"• Неизвестный пользователь (ID: {user_id})")
                await evt.reply(f"🔒 **Заблокированные пользователи** ({len(banned_list)}):\n" + "\n".join(usernames), parse_mode='md')
            else:
                await evt.reply("Список заблокированных пользователей пуст.")
            return

        subcmd = parts[1].lower()
        
        # /ban add
        if subcmd == "add":
            user_id_to_ban = None
            
            # 1. По ID: /ban add <user_id>
            if len(parts) == 3:
                try:
                    user_id_to_ban = int(parts[2])
                except ValueError:
                    pass
            
            # 2. По ответу на пересланное сообщение (ищем UID в тексте)
            if not user_id_to_ban and evt.reply_to_msg_id:
                try:
                    # Получаем сообщение, на которое ответили
                    replied_msg = await client.get_messages(CONTROL_CHAT_ID, ids=evt.reply_to_msg_id)
                    # Ищем ID отправителя в тексте пересланного сообщения: ищем UID: `12345`
                    match = re.search(r'UID: `(\d+)`', replied_msg.message)
                    if match:
                        user_id_to_ban = int(match.group(1))
                except Exception:
                    pass

            if user_id_to_ban:
                if ban_user(user_id_to_ban):
                    try:
                        entity = await client.get_entity(user_id_to_ban)
                        await evt.reply(f"✅ **Пользователь заблокирован!** Сообщения от {get_display_name(entity)} (ID: {user_id_to_ban}) будут игнорироваться.", parse_mode='md')
                    except Exception:
                        await evt.reply(f"✅ **Пользователь заблокирован!** Сообщения от ID {user_id_to_ban} будут игнорироваться.", parse_mode='md')
                else:
                    await evt.reply(f"⚠️ Пользователь ID {user_id_to_ban} уже был заблокирован.")
            else:
                await evt.reply("⚠️ Не удалось определить ID пользователя. Используйте: `/ban add <user_id>` или ответьте на пересланное сообщение.")

        # /ban remove
        elif subcmd == "remove" and len(parts) == 3:
            try:
                user_id_to_unban = int(parts[2])
                if unban_user(user_id_to_unban):
                    await evt.reply(f"✅ **Пользователь разблокирован.**")
                else:
                    await evt.reply(f"⚠️ Пользователь ID {user_id_to_unban} не найден в списке блокировки.")
            except ValueError:
                await evt.reply("⚠️ Неверный формат ID пользователя.")
        
        # /owner - управление администраторами
    elif cmd == "/owner":
        if len(parts) < 2 or parts[1].lower() == "list":
            # Показываем список всех админов
            admin_list = list_admins()
            
            # Добавляем основного админа из .env
            try:
                main_admin = await client.get_entity(ADMIN_USER_ID)
                main_admin_name = get_display_name(main_admin)
            except Exception:
                main_admin_name = f"ID: {ADMIN_USER_ID}"
            
            response = [f"👑 Главный владелец: {main_admin_name} (ID: {ADMIN_USER_ID})"]

            if admin_list:
                for user_id, username in admin_list:
                    response.append(f"• {username or f'ID: {user_id}'}")
            
            await evt.reply("👥 **Список администраторов**:\n" + "\n".join(response), parse_mode='md')
            return

        subcmd = parts[1].lower()
        
        # /owner add <user_id>
        if subcmd == "add" and len(parts) == 3:
            try:
                user_id = int(parts[2])
                entity = await client.get_entity(user_id)
                username = get_display_name(entity)
                
                if add_admin(user_id, username):
                    await evt.reply(f"✅ **Администратор добавлен:** {username} (ID: {user_id})", parse_mode='md')
                else:
                    await evt.reply(f"⚠️ Пользователь ID {user_id} уже был администратором.")
            except ValueError:
                await evt.reply("⚠️ Неверный формат ID пользователя.")
            except Exception as e:
                await evt.reply(f"⚠️ Ошибка получения данных пользователя: {e}")

        # /owner remove <user_id>
        elif subcmd == "remove" and len(parts) == 3:
            try:
                user_id = int(parts[2])
                if user_id == ADMIN_USER_ID:
                    await evt.reply("❌ Нельзя удалить основного владельца из `.env`.", parse_mode='md')
                    return
                
                if remove_admin(user_id):
                    await evt.reply(f"✅ **Администратор удален:** ID {user_id}", parse_mode='md')
                else:
                    await evt.reply(f"⚠️ Пользователь ID {user_id} не найден в списке администраторов.")
            except ValueError:
                await evt.reply("⚠️ Неверный формат ID пользователя.")
    
 # /help - справка
    elif cmd == "/help":
        help_text = """
📚 Доступные команды:

--- Управление фильтрами ---
/kw add <слово>    - добавить ключевое слово
/kw del <слово>    - удалить ключевое слово
/kw list           - показать все ключевые слова

/neg add <слово>   - добавить негативное слово
/neg del <слово>   - удалить негативное слово
/neg list          - показать все негативные слова

--- Управление источниками и AI ---
/src add <id|@user|t.me/link> - добавить источник для мониторинга
/src del <id|@user|t.me/link> - удалить источник
/src list          - показать все источники

/ai set <chat_id> <правило> - установить AI фильтр (сейчас заглушен)
/ai show <chat_id>          - показать AI правило
/ai clear <chat_id>         - удалить AI правило

--- Администрирование и Отладка ---
/why               - объяснить причину пересылки (ответьте на сообщение в целевом чате)

/ban add <id>      - заблокировать пользователя по ID (или ответьте на пересланное сообщение)
/ban remove <id>   - разблокировать пользователя
/ban list          - показать список заблокированных

/owner add <id>    - добавить нового администратора
/owner remove <id> - удалить администратора
/owner list        - показать всех администраторов

/help              - эта справка
        """
        await evt.reply(help_text, parse_mode='md')

# ====== Запуск ======
async def main():
    await client.start(phone=PHONE)
    me = await client.get_me()
    log.info(f"✓ Started as {me.id} @ {get_display_name(me)}")
    log.info(f"📤 Target chat: {TARGET_CHAT_ID}")
    log.info(f"🎛 Control chat: {CONTROL_CHAT_ID}")
    log.info(f"👤 Admin user: {ADMIN_USER_ID}")
    await client.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        conn.close()