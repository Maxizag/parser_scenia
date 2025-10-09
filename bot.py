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


# Создание таблиц (ВНИМАНИЕ: Таблица keywords изменена для chat_id!)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS keywords (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL DEFAULT 0,
        keyword TEXT NOT NULL,
        UNIQUE(chat_id, keyword) 
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

# ====== Клиент ======
client = TelegramClient("parser_session", API_ID, API_HASH)

# ====== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (СИНХРОННЫЕ И АСИНХРОННЫЕ) ======

# --- ОБНОВЛЕННЫЕ ФУНКЦИИ КЛЮЧЕВЫХ СЛОВ ---
def get_keywords(chat_id=None):
    """
    Получает ключевые слова.
    Если chat_id не указан (None), возвращает только ГЛОБАЛЬНЫЕ.
    Если указан - возвращает ГЛОБАЛЬНЫЕ (chat_id=0) + слова для конкретного чата.
    """
    query = "SELECT keyword FROM keywords WHERE chat_id = 0"
    params = []
    
    if chat_id is not None and chat_id != 0:
        query += " OR chat_id = ?"
        params.append(chat_id)
        
    cursor.execute(query, params)
    return [row[0].lower() for row in cursor.fetchall()]

def add_keyword(kw, chat_id=0):
    """Добавляет слово. По умолчанию (chat_id=0) - глобально."""
    try:
        cursor.execute("INSERT INTO keywords (chat_id, keyword) VALUES (?, ?)", (chat_id, kw.lower()))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def delete_keyword(kw, chat_id=0):
    """
    Удаляет слово. По умолчанию (chat_id=0) - глобально.
    """
    cursor.execute("DELETE FROM keywords WHERE keyword = ? AND chat_id = ?", (kw.lower(), chat_id))
    conn.commit()
    return cursor.rowcount > 0 
# --- КОНЕЦ ОБНОВЛЕННЫХ ФУНКЦИЙ КЛЮЧЕВЫХ СЛОВ ---

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

def delete_source(chat_id):
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
        # Убедимся, что ID чата для entity корректен (например, не 0)
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


# ====== AI фильтр (ЗАГЛУШКА) ======
async def ai_filter(text, chat_id):
    # Эта функция теперь всегда возвращает True, пропуская сообщение
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

    # ПРОВЕРКА: Блокировка пользователя
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
    
    # Этап 1: ключевые слова. Получаем ГЛОБАЛЬНЫЕ + слова для текущего чата
    keywords = get_keywords(evt.chat_id)
    negwords = get_negwords()
    
    if not keywords:
        log.debug(f"No keywords set for chat {evt.chat_id}, skipping message")
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
            
            # Форматирование отправителя
            sender_display = f"@{sender_info['username']}" if sender_info['username'] else f"ID {sender_info['id']}"
            sender_line = f"Отправитель: {sender_display}\nUID: {sender_info['id']}" 
            
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

# ---

# ====== БЫСТРАЯ КОМАНДА 'бан' В ЦЕЛЕВОМ ЧАТЕ (TARGET_CHAT_ID) ======
@client.on(events.NewMessage(chats=TARGET_CHAT_ID)) 
async def on_quick_ban(evt: events.NewMessage.Event):
    # Проверка: сообщение должно быть ровно 'бан' (независимо от регистра)
    if (evt.message.message or "").strip().lower() != 'бан': # <--- ИЗМЕНЕНИЕ: 'бан'
        return
        
    # 1. Проверка прав (только администратор)
    if not is_admin(evt.sender_id):
        return
    
    # 2. Проверка, что это ответ на сообщение
    if not evt.reply_to_msg_id:
        return

    # 3. Находим сообщение, на которое ответили
    try:
        replied_msg = await client.get_messages(evt.chat_id, ids=evt.reply_to_msg_id)
        
        # 4. Ищем UID: 12345 в тексте пересланного сообщения
        text_to_search = replied_msg.message or ""
        match = re.search(r'UID: (\d+)', text_to_search) 
        
        if not match:
            await evt.reply("⚠️ Не удалось найти ID пользователя (`UID: <ID>`) в тексте этого сообщения. Проверьте форматирование.", parse_mode='md')
            return

        user_id_to_ban = int(match.group(1))

        # 5. Выполняем бан
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

# ====== ОТДЕЛЬНЫЙ ОБРАБОТЧИК ДЛЯ КОМАНДЫ 'Почему' (РАБОТАЕТ В ОБОИХ ЧАТАХ) ======
@client.on(events.NewMessage(chats=[CONTROL_CHAT_ID, TARGET_CHAT_ID], pattern=r'^/Почему')) # <--- ИЗМЕНЕНИЕ: /Почему
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
        await evt.reply("Используйте /Почему, ответив на пересланное сообщение в этом чате.", parse_mode='md')

# ---

# ====== ОСНОВНОЙ ОБРАБОТЧИК КОМАНД ======
@client.on(events.NewMessage(chats=CONTROL_CHAT_ID, pattern=r'^/'))
async def on_command(evt: events.NewMessage.Event):
    # Проверка: только администратор (или основной владелец из .env) может использовать команды
    if not is_admin(evt.sender_id): 
        log.warning(f"⚠️ Unauthorized command attempt from user {evt.sender_id}")
        await evt.reply("❌ У вас нет прав для управления ботом")
        return
    
    text = evt.message.message.strip()
    # parts: [0]=/cmd, [1]=subcmd, [2]=остаток текста
    parts = text.split(maxsplit=2) 
    
    if len(parts) < 1:
        return
    
    cmd = parts[0].lower()
    
    # /+слово - управление ключевыми словами
    if cmd == "/+слово": # <--- ИЗМЕНЕНИЕ: /+слово
        
        # Общий синтаксис: /+слово [ID] <слово> ИЛИ /удалить +слово [ID] <слово> ИЛИ /список слов [ID]
        
        subcmd_options = {
            "удалить": "удалить +слово", 
            "список": "список слов",
            "+слово": "+слово" # Для команды /+слово <ID> <слово>
        }
        
        subcmd_str = parts[0].lower().lstrip('/') # Получаем: +слово
        
        # Если команда - /+слово, то это "add"
        if subcmd_str == "+слово":
            subcmd = "add"
        else:
            await evt.reply("⚠️ Неизвестная подкоманда для ключевых слов. Используйте: `/+слово`, `/удалить +слово`, `/список слов`.", parse_mode='md')
            return
            
        target_id = 0 
        keyword = None
        
        # --- Парсинг команды ---

        if subcmd == "list":
             # Если используется команда /список слов [ID]
             # (Оставлено для совместимости, но теперь используется блок ниже)
             # Нам нужен только ID, который может быть в parts[1] или после
             pass 

        elif subcmd in ["add", "del"]:
            
            # Текст, который идет после /+слово или /удалить +слово
            command_prefix = f"/{subcmd_options[subcmd]}" if subcmd in subcmd_options else parts[0]
            remaining_text = text[len(command_prefix):].strip()

            if not remaining_text:
                await evt.reply(f"⚠️ Неверный формат команды. Используйте: `/{subcmd_options[subcmd]} [ID] <слово>`", parse_mode='md')
                return
            
            # 1. Сначала пытаемся разобрать как <ID> <слово>
            try:
                id_part, keyword_part = remaining_text.split(maxsplit=1)
                
                # Проверяем, является ли первая часть числом (ID чата)
                target_id = int(id_part)
                keyword = keyword_part.strip()
                
            except ValueError:
                # Если не получилось (нет пробела или не число), считаем, что это <слово> глобально
                target_id = 0
                keyword = remaining_text.strip()
                
                # Дополнительная проверка, чтобы избежать /+слово -1001810451666 (без слова)
                try:
                    _ = int(keyword)
                    await evt.reply("⚠️ Неверный формат. Если вы указываете только число, оно должно быть ID, за которым следует ключевое слово.", parse_mode='md')
                    return
                except ValueError:
                    pass # Отлично, это просто ключевое слово

            # Финальная проверка ключевого слова
            if not keyword:
                await evt.reply("⚠️ Ключевое слово не может быть пустым.", parse_mode='md')
                return

        # ----------------- ЛОГИКА ДЕЙСТВИЯ -----------------

        if subcmd == "add" and keyword:
            
            delete_keyword(f"{target_id} {keyword}", 0) 
            
            if add_keyword(keyword, target_id):
                chat_name = await get_chat_title(target_id)
                await evt.reply(f"✓ Добавлено слово **'{keyword}'** для: **{chat_name}** (ID: `{target_id}`)", parse_mode='md')
            else:
                chat_name = await get_chat_title(target_id)
                await evt.reply(f"⚠️ Уже существует: **{keyword}** для {chat_name}", parse_mode='md')

        elif subcmd == "del" and keyword:
            if delete_keyword(keyword, target_id): 
                chat_name = await get_chat_title(target_id)
                await evt.reply(f"✓ Удалено слово **'{keyword}'** для: **{chat_name}** (ID: `{target_id}`)", parse_mode='md')
            else:
                chat_name = await get_chat_title(target_id)
                await evt.reply(f"⚠️ Слово **'{keyword}'** не найдено для: {chat_name}", parse_mode='md')

    # /удалить +слово - управление ключевыми словами (удаление)
    elif cmd == "/удалить" and len(parts) >= 2 and parts[1].lower() == "+слово": # <--- ИЗМЕНЕНИЕ: /удалить +слово
        # Переиспользуем логику из блока /+слово (subcmd = "del")
        subcmd = "del"
        command_prefix = f"{cmd} {parts[1]}"
        
        target_id = 0 
        keyword = None
        
        # Текст, который идет после /удалить +слово
        remaining_text = text[len(command_prefix):].strip()

        if not remaining_text:
            await evt.reply(f"⚠️ Неверный формат команды. Используйте: `/удалить +слово [ID] <слово>`", parse_mode='md')
            return
        
        # 1. Сначала пытаемся разобрать как <ID> <слово>
        try:
            id_part, keyword_part = remaining_text.split(maxsplit=1)
            target_id = int(id_part)
            keyword = keyword_part.strip()
        except ValueError:
            target_id = 0
            keyword = remaining_text.strip()
            
        if not keyword:
            await evt.reply("⚠️ Ключевое слово не может быть пустым.", parse_mode='md')
            return
            
        # ----------------- ЛОГИКА ДЕЙСТВИЯ -----------------
        if delete_keyword(keyword, target_id): 
            chat_name = await get_chat_title(target_id)
            await evt.reply(f"✓ Удалено слово **'{keyword}'** для: **{chat_name}** (ID: `{target_id}`)", parse_mode='md')
        else:
            chat_name = await get_chat_title(target_id)
            await evt.reply(f"⚠️ Слово **'{keyword}'** не найдено для: {chat_name}", parse_mode='md')


    # /список слов - список ключевых слов
    elif cmd == "/список" and len(parts) >= 2 and parts[1].lower() == "слов": # <--- ИЗМЕНЕНИЕ: /список слов
        
        target_id = 0
        # /список слов <ID>
        if len(parts) == 3:
            try:
                target_id = int(parts[2])
            except ValueError:
                await evt.reply("⚠️ Ошибка: ID чата должен быть числом.", parse_mode='md')
                return
                
        kws_global = get_keywords(0)
        kws_chat = get_keywords(target_id) if target_id else []
        
        # Уникальный список, чтобы не дублировать слова
        unique_kws = sorted(list(set(kws_global + kws_chat)))
        
        if target_id == 0:
            title = "📝 Глобальные ключевые слова"
            
        else:
            chat_name = await get_chat_title(target_id)
            title = f"📝 Ключевые слова для: **{chat_name}** (ID: `{target_id}`)"
            
        
        response = f"{title} (Всего: {len(unique_kws)}):\n\n"
        
        # Разделяем на Global и Local для лучшей читаемости
        if target_id != 0:
            # Получаем только локальные слова, которых нет в глобальных
            local_only = [kw for kw in kws_chat if kw not in kws_global]
            
            response += "**— Локальные слова (только для этого чата):**\n"
            
            if local_only:
                 response += "\n".join(f"• {kw}" for kw in local_only) + "\n\n"
            else:
                response += "*(Локальных слов нет)*\n\n"
            
            response += "**— Глобальные слова (наследуются):**\n"
        
        if kws_global:
            response += "\n".join(f"• {kw}" for kw in kws_global)
        elif target_id == 0:
            response += "*(Список пуст)*"

        await evt.reply(response, parse_mode='md')

            
    # /минус слово - управление негативными словами (добавление)
    elif cmd == "/минус" and len(parts) >= 2 and parts[1].lower() == "слово": # <--- ИЗМЕНЕНИЕ: /минус слово
        
        # Проверяем, есть ли слово после /минус слово
        if len(parts) < 3:
            nws = get_negwords()
            await evt.reply(f"🚫 Негативные слова ({len(nws)}):\n" + "\n".join(f"• {nw}" for nw in nws) if nws else "Список пуст")
            await evt.reply("Используйте: `/минус слово <слово>` | `/удалить минус слово <слово>` | `/список минус слов`", parse_mode='md')
            return
            
        nw = parts[2].strip()
        if add_negword(nw):
            await evt.reply(f"✓ Добавлено негативное слово: {nw}")
        else:
            await evt.reply(f"⚠️ Уже существует: {nw}")

    # /удалить минус слово - удаление негативных слов
    elif cmd == "/удалить" and len(parts) >= 3 and parts[1].lower() == "минус" and parts[2].lower() == "слово": # <--- ИЗМЕНЕНИЕ: /удалить минус слово
        
        # Текст, который идет после /удалить минус слово
        command_prefix = f"{cmd} {parts[1]} {parts[2]}"
        remaining_text = text[len(command_prefix):].strip()
        
        if not remaining_text:
            await evt.reply("⚠️ Неверный формат команды. Используйте: `/удалить минус слово <слово>`", parse_mode='md')
            return
            
        nw = remaining_text.strip()
        if delete_negword(nw):
            await evt.reply(f"✓ Удалено негативное слово: {nw}")
        else:
            await evt.reply(f"⚠️ Слово не найдено: {nw}")


    # /список минус слов - список негативных слов
    elif cmd == "/список" and len(parts) >= 3 and parts[1].lower() == "минус" and parts[2].lower() == "слов": # <--- ИЗМЕНЕНИЕ: /список минус слов
        nws = get_negwords()
        await evt.reply(f"🚫 Негативные слова ({len(nws)}):\n" + "\n".join(f"• {nw}" for nw in nws) if nws else "Список пуст")


    # /добавить чат - управление источниками (добавление)
    elif cmd == "/добавить" and len(parts) >= 2 and parts[1].lower() == "чат": # <--- ИЗМЕНЕНИЕ: /добавить чат
        
        if len(parts) < 3:
            sources = list_sources()
            await evt.reply(f"📢 Источники ({len(sources)}):\n" + 
                          "\n".join(f"• {title} (ID: `{cid}`)" for cid, title in sources), parse_mode='md')
            await evt.reply("Используйте: `/добавить чат <id|@user|t.me/link>` | `/удалить чат <id|@user|t.me/link>` | `/список чатов`", parse_mode='md')
            return

        chat_input = parts[2].strip()
        
        try:
            entity = await client.get_entity(chat_input) 
            
            if isinstance(entity, User):
                await evt.reply(f"⚠️ Ошибка: '{chat_input}' — это ID/username пользователя. Требуется ID чата, ссылка на канал/группу.", parse_mode='md')
                return
            
            chat_id = entity.id 
            if chat_id > 0: 
                chat_id = get_peer_id(entity, add_mark=True)
            
            title = get_display_name(entity)

            if add_source(chat_id, title):
                await evt.reply(f"✓ Добавлен источник: **{title}** (ID: `{chat_id}`)", parse_mode='md')
            else:
                await evt.reply(f"⚠️ Ошибка добавления источника")
        
        except ValueError:
            await evt.reply("⚠️ Неверный формат ID/ссылки.")
        except Exception as e:
            await evt.reply(f"⚠️ Ошибка: Не удалось найти чат по ссылке или ID. Возможно, бот не состоит в этом чате. Ошибка: {e}")
    
    # /удалить чат - управление источниками (удаление)
    elif cmd == "/удалить" and len(parts) >= 2 and parts[1].lower() == "чат": # <--- ИЗМЕНЕНИЕ: /удалить чат
        
        if len(parts) < 3:
            await evt.reply("⚠️ Неверный формат команды. Используйте: `/удалить чат <id|@user|t.me/link>`", parse_mode='md')
            return

        chat_input = parts[2].strip()
        try:
            entity = await client.get_entity(chat_input)
            chat_id = entity.id 
            if chat_id > 0: 
                chat_id = get_peer_id(entity, add_mark=True)
                
            if delete_source(chat_id):
                await evt.reply(f"✓ Источник `{chat_id}` (**{get_display_name(entity)}**) удален.", parse_mode='md')
            else:
                await evt.reply(f"⚠️ Источник `{chat_id}` не найден в списке.", parse_mode='md')
        except ValueError:
            await evt.reply("⚠️ Неверный формат ID/ссылки.")
        except Exception:
            try:
                chat_id = int(chat_input)
                if delete_source(chat_id):
                    await evt.reply(f"✓ Источник `{chat_id}` удален.", parse_mode='md')
                else:
                    await evt.reply(f"⚠️ Источник `{chat_id}` не найден в списке.", parse_mode='md')
            except ValueError:
                await evt.reply("⚠️ Не удалось определить ID источника. Используйте ID, @username или ссылку.")

    # /список чатов - список источников
    elif cmd == "/список" and len(parts) >= 2 and parts[1].lower() == "чатов": # <--- ИЗМЕНЕНИЕ: /список чатов
        sources = list_sources()
        await evt.reply(f"📢 Источники ({len(sources)}):\n" + 
                      "\n".join(f"• {title} (ID: `{cid}`)" for cid, title in sources), parse_mode='md')
    
    # /ai - управление AI правилами (без изменений по вашей просьбе)
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
                
    # /бан - блокировка пользователя (добавление)
    elif cmd == "/бан": # <--- ИЗМЕНЕНИЕ: /бан
        
        # Если команда /бан без аргументов ИЛИ /бан список
        if len(parts) < 2 or parts[1].lower() == "список":
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
        
        # /бан <id>
        if len(parts) == 2:
            user_id_to_ban = None
            
            try:
                user_id_to_ban = int(parts[1])
            except ValueError:
                 await evt.reply("⚠️ Не удалось определить ID пользователя. Используйте: `/бан <user_id>` или ответьте на пересланное сообщение.")
                 return

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
                await evt.reply("⚠️ Не удалось определить ID пользователя. Используйте: `/бан <user_id>` или ответьте на пересланное сообщение.")


    # /ban remove - разблокировать пользователя (без изменений по вашей просьбе)
    elif cmd == "/ban" and len(parts) >= 2 and parts[1].lower() == "remove":
        
        if len(parts) < 3:
             await evt.reply("⚠️ Неверный формат команды. Используйте: `/ban remove <user_id>`", parse_mode='md')
             return
             
        try:
            user_id_to_unban = int(parts[2])
            if unban_user(user_id_to_unban):
                await evt.reply(f"✅ **Пользователь разблокирован.**")
            else:
                await evt.reply(f"⚠️ Пользователь ID {user_id_to_unban} не найден в списке блокировки.")
        except ValueError:
            await evt.reply("⚠️ Неверный формат ID пользователя.")
        
        
    # /список бан - список заблокированных
    elif cmd == "/список" and len(parts) >= 2 and parts[1].lower() == "бан": # <--- ИЗМЕНЕНИЕ: /список бан
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


    # /owner - управление администраторами (без изменений по вашей просьбе)
    elif cmd == "/owner":
        if len(parts) < 2 or parts[1].lower() == "list":
            # Показываем список всех админов
            admin_list = list_admins()
            
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
    
 # /help - справка (обновлена)
    elif cmd == "/help":
        help_text = """
📚 Доступные команды:

--- Управление фильтрами ---
** /+слово [ID] <слово> - добавить ключевое слово (ID опционален, по умолчанию - глобально)**
/удалить +слово [ID] <слово> - удалить ключевое слово
/список слов [ID]          - показать ключевые слова (ID опционален, по умолчанию - глобально)

/минус слово <слово>       - добавить негативное слово
/удалить минус слово <слово> - удалить негативное слово
/список минус слов         - показать все негативные слова

--- Управление источниками и AI ---
/добавить чат <id|@user|t.me/link> - добавить источник для мониторинга
/удалить чат <id|@user|t.me/link> - удалить источник
/список чатов              - показать все источники

/ai set <chat_id> <правило> - установить AI фильтр (сейчас заглушен)
/ai show <chat_id>          - показать AI правило
/ai clear <chat_id>         - удалить AI правило

--- Администрирование и Отладка ---
/Почему                    - объяснить причину пересылки (ответьте на сообщение в целевом чате)

** /бан <id> - заблокировать пользователя по ID (или ответьте на пересланное сообщение)**
/ban remove <id>           - разблокировать пользователя
/список бан                - показать список заблокированных

**Команда быстрого бана:**
Просто ответьте **бан** на сообщение в целевом чате.

/owner add <id>            - добавить нового администратора
/owner remove <id>         - удалить администратора
/owner list                - показать всех администраторов

/help                      - эта справка
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