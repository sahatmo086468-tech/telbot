import telebot
from telebot import types
import sqlite3
from datetime import datetime
from collections import defaultdict
from threading import Lock

# ═══════════════════════════════════════════════════════
# تنظیمات اولیه
# ═══════════════════════════════════════════════════════

TOKEN = "bot token"
BOT_USERNAME = "bot id"
ADMINS = [1111, ]

bot = telebot.TeleBot(TOKEN)

# سیستم محدودیت ارسال پیام (ضد اسپم)
message_tracker = defaultdict(list)
MAX_MESSAGES = 3
TIME_WINDOW = 30

# سیستم قفل برای جلوگیری از دستورات همزمان
user_locks = defaultdict(Lock)
user_states = {}

# ═══════════════════════════════════════════════════════
# تنظیم دیتابیس SQLite
# ═══════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect('anonymous_bot.db', check_same_thread=False)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()

def get_db():
    return sqlite3.connect('anonymous_bot.db', check_same_thread=False)

# ═══════════════════════════════════════════════════════
# توابع کمکی دیتابیس
# ═══════════════════════════════════════════════════════

def add_user(user_id, username, first_name):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT OR IGNORE INTO users (user_id, username, first_name)
            VALUES (?, ?, ?)
        ''', (user_id, username, first_name))
        conn.commit()
    finally:
        conn.close()

def user_exists(user_id):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,))
        return cursor.fetchone() is not None
    finally:
        conn.close()

def get_user_info(user_id):
    """دریافت اطلاعات کاربر از دیتابیس"""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT first_name, username FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        if result:
            return {'first_name': result[0], 'username': result[1]}
        return None
    finally:
        conn.close()

# ═══════════════════════════════════════════════════════
# مدیریت وضعیت کاربران
# ═══════════════════════════════════════════════════════

def set_user_state(user_id, state, data=None):
    user_states[user_id] = {
        'state': state,
        'data': data or {},
        'timestamp': datetime.now()
    }

def get_user_state(user_id):
    return user_states.get(user_id)

def clear_user_state(user_id):
    if user_id in user_states:
        del user_states[user_id]

# ═══════════════════════════════════════════════════════
# سیستم ضد اسپم
# ═══════════════════════════════════════════════════════

def check_spam(user_id):
    from time import time
    current_time = time()
    
    message_tracker[user_id] = [
        timestamp for timestamp in message_tracker[user_id]
        if current_time - timestamp < TIME_WINDOW
    ]
    
    if len(message_tracker[user_id]) >= MAX_MESSAGES:
        return True
    
    message_tracker[user_id].append(current_time)
    return False

# ═══════════════════════════════════════════════════════
# دکمه‌های زیبا
# ═══════════════════════════════════════════════════════

def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    btn1 = types.KeyboardButton('🔗 دریافت لینک من')
    btn2 = types.KeyboardButton('❓ راهنما')
    btn3 = types.KeyboardButton('ℹ️ درباره ربات')
    markup.add(btn1)
    markup.add(btn2, btn3)
    return markup

def get_admin_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    btn1 = types.KeyboardButton('🔗 دریافت لینک من')
    btn2 = types.KeyboardButton('❓ راهنما')
    btn3 = types.KeyboardButton('ℹ️ درباره ربات')
    markup.add(btn1)
    markup.add(btn2, btn3)
    return markup

def get_cancel_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton('❌ لغو'))
    return markup

# ═══════════════════════════════════════════════════════
# دستور /start
# ═══════════════════════════════════════════════════════

@bot.message_handler(commands=['start'])
def start_command(message):
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name or "کاربر"
    
    clear_user_state(user_id)
    add_user(user_id, username, first_name)
    
    # بررسی پارامتر لینک
    if len(message.text.split()) > 1:
        target_user_id = message.text.split()[1]
        
        try:
            target_user_id = int(target_user_id)
            
            if not user_exists(target_user_id):
                bot.send_message(
                    user_id,
                    "❌ کاربر مورد نظر در ربات ثبت نام نکرده است\n\n"
                    "💡 لطفاً ابتدا از او بخواهید ربات را استارت بزند",
                    reply_markup=get_main_keyboard() if user_id not in ADMINS else get_admin_keyboard()
                )
                return
            
            if target_user_id == user_id:
                bot.send_message(
                    user_id,
                    "❌ نمی‌توانید برای خودتان پیام ناشناس ارسال کنید 😅",
                    reply_markup=get_main_keyboard() if user_id not in ADMINS else get_admin_keyboard()
                )
                return
            
            # دریافت اطلاعات کاربر مقصد
            target_info = get_user_info(target_user_id)
            target_name = target_info['first_name'] if target_info else "کاربر"
            
            set_user_state(user_id, 'waiting_for_message', {'target': target_user_id})
            
            bot.send_message(
                user_id,
                f"📤 شما در حال ارسال پیام ناشناس به:\n"
                f"👤 {target_name}\n\n"
                f"💬 لطفاً پیام خود را ارسال کنید\n\n"
                f"📝 انواع پیام قابل ارسال:\n"
                f"   • متن\n"
                f"   • عکس\n"
                f"   • وویس یا ویدیو مسیج\n"
                f"   • استیکر\n\n"
                f"✨ هویت شما کاملاً محفوظ می‌ماند",
                reply_markup=get_cancel_keyboard()
            )
            
        except ValueError:
            bot.send_message(
                user_id,
                "❌ لینک نامعتبر است",
                reply_markup=get_main_keyboard() if user_id not in ADMINS else get_admin_keyboard()
            )
    else:
        welcome_text = f"👋 سلام {first_name} عزیز\n\n"
        welcome_text += "🌟 به ربات چت ناشناس خوش آمدید\n\n"
        welcome_text += "🔹 امکانات ربات:\n\n"
        welcome_text += "   💬 دریافت پیام ناشناس از دیگران\n"
        welcome_text += "   📤 ارسال پیام ناشناس برای دیگران\n"
        welcome_text += "   🔒 محیط امن و بدون نگرانی\n\n"
        
        if user_id in ADMINS:
            welcome_text += "👑 شما ادمین ربات هستید\n\n"
            markup = get_admin_keyboard()
        else:
            markup = get_main_keyboard()
        
        welcome_text += "💡 برای شروع، روی دکمه «🔗 دریافت لینک من» کلیک کنید"
        
        bot.send_message(user_id, welcome_text, reply_markup=markup)

# ═══════════════════════════════════════════════════════
# دکمه لغو
# ═══════════════════════════════════════════════════════

def handle_cancel(message):
    user_id = message.from_user.id
    clear_user_state(user_id)
    
    markup = get_admin_keyboard() if user_id in ADMINS else get_main_keyboard()
    
    bot.send_message(
        user_id,
        "❌ عملیات لغو شد\n\n"
        "شما به منوی اصلی بازگشتید",
        reply_markup=markup
    )

# ═══════════════════════════════════════════════════════
# پردازش پیام‌های عادی
# ═══════════════════════════════════════════════════════

@bot.message_handler(func=lambda message: True, content_types=['text', 'photo', 'voice', 'sticker', 'video_note'])
def handle_all_messages(message):
    user_id = message.from_user.id
    
    # ابتدا بررسی می‌کنیم که آیا پیام متنی است و دکمه لغو زده شده
    if message.content_type == 'text' and message.text == '❌ لغو':
        handle_cancel(message)
        return
    
    user_state = get_user_state(user_id)
    
    # اگر کاربر در حال ارسال پیام است
    if user_state and user_state['state'] == 'waiting_for_message':
        process_anonymous_message(message)
        return
    
    # پردازش دکمه‌های دیگر فقط در صورتی که کاربر در حالت عادی است
    if message.content_type == 'text':
        text = message.text
        
        if text == '🔗 دریافت لینک من':
            get_link(message)
        elif text == '❓ راهنما':
            show_help(message)
        elif text == 'ℹ️ درباره ربات':
            about_bot(message)
        else:
            bot.send_message(
                user_id,
                "❓ دستور نامعتبر\n\n"
                "💡 لطفاً از دکمه‌های زیر استفاده کنید یا /start را بزنید",
                reply_markup=get_main_keyboard() if user_id not in ADMINS else get_admin_keyboard()
            )

# ═══════════════════════════════════════════════════════
# پردازش پیام ناشناس
# ═══════════════════════════════════════════════════════

def process_anonymous_message(message):
    sender_id = message.from_user.id
    
    user_state = get_user_state(sender_id)
    if not user_state or user_state['state'] != 'waiting_for_message':
        return
    
    target_user_id = user_state['data'].get('target')
    
    if not target_user_id:
        bot.send_message(sender_id, "❌ خطا! لطفاً دوباره از ابتدا شروع کنید")
        clear_user_state(sender_id)
        return
    
    if check_spam(sender_id):
        bot.send_message(
            sender_id,
            f"⚠️ لطفاً کمی صبر کنید\n\n"
            f"شما در {TIME_WINDOW} ثانیه گذشته بیش از {MAX_MESSAGES} پیام ارسال کرده‌اید",
            reply_markup=get_main_keyboard() if sender_id not in ADMINS else get_admin_keyboard()
        )
        return
    
    try:
        # دریافت اطلاعات گیرنده
        target_info = get_user_info(target_user_id)
        target_name = target_info['first_name'] if target_info else "کاربر"
        
        # تهیه هدر پیام
        header = "📨 شما یک پیام ناشناس جدید دارید:\n\n"
        
        # اگر گیرنده ادمین است، اطلاعات فرستنده را اضافه کن
        if target_user_id in ADMINS:
            sender_username = message.from_user.username or "ندارد"
            sender_firstname = message.from_user.first_name or "نامشخص"
            header += f"🔍 اطلاعات فرستنده (ویژه ادمین)\n\n"
            header += f"👤 نام: {sender_firstname}\n"
            header += f"🆔 یوزرنیم: @{sender_username}\n"
            header += f"🔢 شناسه: {sender_id}\n"
            header += f"📅 زمان: {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}\n\n"
        
        header += "👇 پیام شما:\n\n"
        
        message_type_name = ""
        
        if message.content_type == 'text':
            bot.send_message(target_user_id, header + message.text)
            message_type_name = "متن"
            
        elif message.content_type == 'photo':
            caption = message.caption or ""
            bot.send_photo(target_user_id, message.photo[-1].file_id, caption=header + caption)
            message_type_name = "عکس"
            
        elif message.content_type == 'voice':
            # ابتدا هدر را بفرست
            bot.send_message(target_user_id, header + "🎤 پیام صوتی 👇")
            # سپس وویس را بفرست
            bot.send_voice(target_user_id, message.voice.file_id)
            message_type_name = "وویس"
            
        elif message.content_type == 'sticker':
            # ابتدا هدر را بفرست
            bot.send_message(target_user_id, header + "😊 استیکر 👇")
            # سپس استیکر را بفرست
            bot.send_sticker(target_user_id, message.sticker.file_id)
            message_type_name = "استیکر"
            
        elif message.content_type == 'video_note':
            # ابتدا هدر را بفرست
            bot.send_message(target_user_id, header + "🎬 ویدیو مسیج 👇")
            # سپس ویدیو مسیج را بفرست
            bot.send_video_note(target_user_id, message.video_note.file_id)
            message_type_name = "ویدیو مسیج"
        
        else:
            bot.send_message(sender_id, "❌ این نوع پیام پشتیبانی نمی‌شود")
            return
        
        # تأیید ارسال با اطلاعات کامل
        success_text = "✅ پیام شما با موفقیت ارسال شد\n\n"
        success_text += f"📤 گیرنده: {target_name}\n"
        success_text += f"📝 نوع پیام: {message_type_name}\n"
        success_text += f"🕐 زمان: {datetime.now().strftime('%H:%M:%S')}\n\n"
        success_text += "🎉 پیام‌تان به صورت کاملاً ناشناس تحویل داده شد\n\n"
        success_text += "💡 برای دریافت پیام ناشناس، لینک خود را با دیگران به اشتراک بگذارید"
        
        markup = get_admin_keyboard() if sender_id in ADMINS else get_main_keyboard()
        
        bot.send_message(sender_id, success_text, reply_markup=markup)
        
        clear_user_state(sender_id)
        
    except Exception as e:
        error_msg = "❌ خطا در ارسال پیام\n\n"
        error_msg += "ممکن است کاربر مقصد ربات را بلاک کرده باشد"
        bot.send_message(sender_id, error_msg)
        clear_user_state(sender_id)

# ═══════════════════════════════════════════════════════
# دکمه‌های اصلی
# ═══════════════════════════════════════════════════════

def get_link(message):
    user_id = message.from_user.id
    personal_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    
    link_text = f"🔗 لینک اختصاصی شما\n\n"
    link_text += f"{personal_link}\n\n"
    link_text += "📢 این لینک را با دوستان خود به اشتراک بگذارید\n"
    link_text += "📩 آن‌ها می‌توانند برای شما پیام ناشناس بفرستند\n\n"
    link_text += "💡 روی لینک کلیک کنید تا کپی شود"
    
    markup = types.InlineKeyboardMarkup()
    share_btn = types.InlineKeyboardButton(
        "📤 اشتراک‌گذاری لینک",
        url=f"https://t.me/share/url?url={personal_link}&text=✨ برای من پیام ناشناس بفرست 👇"
    )
    markup.add(share_btn)
    
    bot.send_message(user_id, link_text, reply_markup=markup)

def show_help(message):
    help_text = """
📖 راهنمای استفاده از ربات

🔹 دریافت پیام ناشناس

   • روی «🔗 دریافت لینک من» کلیک کنید
   • لینک خود را کپی و به اشتراک بگذارید
   • منتظر پیام‌های ناشناس باشید 📬

🔹 ارسال پیام ناشناس

   • روی لینک شخص مورد نظر کلیک کنید
   • پیام خود را ارسال کنید
   • هویت شما کاملاً محفوظ می‌ماند ✨

🔹 انواع پیام پشتیبانی شده

   📝 متن  •  🖼 عکس  •  🎤 وویس
   🎬 ویدیو مسیج  •  😊 استیکر

⚠️ نکات مهم

   • اسپم نکنید (حد مجاز: 3 پیام در دقیقه)
   • پیام‌های نامناسب ارسال نکنید
   • ادمین‌ها هویت فرستندگان را می‌بینند

💡 سوال دارید؟ با ادمین تماس بگیرید
"""
    bot.send_message(message.chat.id, help_text)

def about_bot(message):
    about_text = """
🤖 ربات چت ناشناس

🔹 نسخه: 4.0 (نسخه ساده شده)
🔹 زبان: Python 3
🔹 کتابخانه: pyTelegramBotAPI
🔹 دیتابیس: SQLite3

✨ امکانات ویژه

   • ارسال ناشناس انواع رسانه
   • سیستم ضد اسپم هوشمند
   • محافظت از حریم خصوصی
   • رابط کاربری زیبا و فارسی

💻 منبع باز و رایگان
🔒 امن و قابل اعتماد
🚀 بروزرسانی مستمر

🌟 از ربات لذت ببرید
"""
    bot.send_message(message.chat.id, about_text)

# ═══════════════════════════════════════════════════════
# اجرای ربات
# ═══════════════════════════════════════════════════════

if __name__ == '__main__':
    print("🤖 ربات چت ناشناس در حال راه‌اندازی...")
    init_db()
    print("✅ دیتابیس با موفقیت آماده شد")
    print("✅ ربات آماده دریافت پیام است")
    print("🚀 ربات شروع به کار کرد\n")
    
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except KeyboardInterrupt:
        print("\n⚠️ ربات توسط کاربر متوقف شد")
    except Exception as e:
        print(f"❌ خطای غیرمنتظره: {e}")
