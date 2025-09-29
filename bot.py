import os
import logging
import time
import json
import re
import requests
import telebot
import lxml
import requests_toolbelt
import bs4
from dotenv import load_dotenv
from FunPayAPI import Account
from FunPayAPI.updater.runner import Runner
from FunPayAPI.updater.events import NewOrderEvent, NewMessageEvent

load_dotenv()

# Telegram bot
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_USER_ID = os.getenv("TELEGRAM_USER_ID")
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Логгирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

COOLDOWN_SECONDS = 1
TOKEN_FILE = "auth_token.json"
FRAGMENT_API_URL = "https://api.fragment-api.com/v1"
waiting_for_nick = {}

# Fragment auth
FRAGMENT_TOKEN = None
FRAGMENT_API_KEY = os.getenv("FRAGMENT_API_KEY")
FRAGMENT_PHONE = os.getenv("FRAGMENT_PHONE")
FRAGMENT_MNEMONICS = os.getenv("FRAGMENT_MNEMONICS")



def clean_username(username):
    """Очищает username от лишних символов @"""
    if username:
        return username.lstrip('@').strip()
    return username

def send_telegram_notification(message):
    """Отправляет уведомление в Telegram"""
    try:
        bot.send_message(TELEGRAM_USER_ID, message, parse_mode='HTML')
        logger.info("✅ Уведомление отправлено в Telegram")
    except Exception as e:
        logger.error(f"❌ Ошибка отправки в Telegram: {e}")

def get_fragment_balance():
    """Получает баланс Fragment"""
    global FRAGMENT_TOKEN
    url = f"{FRAGMENT_API_URL}/misc/wallet/"
    headers = {
        "Accept": "application/json",
        "Authorization": f"JWT {FRAGMENT_TOKEN}"
    }
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            return data.get("balance", 0)
        else:
            logger.error(f"❌ Ошибка получения баланса: {response.text}")
            return 0
    except Exception as e:
        logger.error(f"❌ Исключение при получении баланса: {e}")
        return 0

def load_fragment_token():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r") as f:
            return json.load(f).get("token")
    return None

def save_fragment_token(token):
    with open(TOKEN_FILE, "w") as f:
        json.dump({"token": token}, f)

def authenticate_fragment():
    try:
        mnemonics_list = FRAGMENT_MNEMONICS.strip().split()
        payload = {
            "api_key": FRAGMENT_API_KEY,
            "phone_number": FRAGMENT_PHONE,
            "mnemonics": mnemonics_list,
            "version": "V4R2"
        }
        res = requests.post(f"{FRAGMENT_API_URL}/auth/authenticate/", json=payload)
        if res.status_code == 200:
            token = res.json().get("token")
            save_fragment_token(token)
            logger.info("✅ Успешная авторизация Fragment.")

            # Отправляем уведомление о запуске
            balance = get_fragment_balance()
            send_telegram_notification(
                f"🤖 <b>Бот запущен!</b>\n"
                f"✅ Успешная авторизация Fragment\n"
                f"💰 Текущий баланс: <b>{balance} TON</b>"
            )
            return token
        logger.error(f"❌ Ошибка авторизации Fragment: {res.text}")
        return None
    except Exception as e:
        logger.error(f"❌ Исключение при авторизации Fragment: {e}")
        return None

def check_username_exists(username):
    global FRAGMENT_TOKEN
    clean_user = clean_username(username)
    url = f"{FRAGMENT_API_URL}/misc/user/{clean_user}/"
    headers = {
        "Accept": "application/json",
        "Authorization": f"JWT {FRAGMENT_TOKEN}"
    }
    try:
        res = requests.get(url, headers=headers)
        if res.status_code == 200:
            data = res.json()
            return "username" in data
        else:
            return False
    except Exception as e:
        logger.error(f"❌ Ошибка при проверке ника: {e}")
        return False

def direct_send_stars(token, username, quantity):
    try:
        clean_user = clean_username(username)
        data = {"username": clean_user, "quantity": quantity, "show_sender": "false"}
        headers = {
            "Authorization": f"JWT {token}",
            "Content-Type": "application/json"
        }
        res = requests.post(f"{FRAGMENT_API_URL}/order/stars/", json=data, headers=headers)
        if res.status_code == 200:
            return True, res.text
        return False, res.text
    except Exception as e:
        return False, str(e)

def parse_fragment_error(response_text):
    try:
        data = json.loads(response_text)
    except:
        return "❌ Неизвестная ошибка."

    if isinstance(data, dict):
        if "username" in data:
            return "❌ Неверный Telegram-тег. Сейчас оформим возврат средств."
        if "quantity" in data:
            return "❌ Минимум 50 ⭐ для покупки. Сейчас оформим возврат средств."
        if "errors" in data:
            for err in data["errors"]:
                if "Not enough funds" in err.get("error", ""):
                    return "❌ Извините, у нас резко закончились звёзды. Сейчас оформим возврат средств."
    if isinstance(data, list):
        if any("Unknown error" in str(e) for e in data):
            return "❌ Неизвестная ошибка. Сейчас оформим возврат средств."
    return "❌ Ошибка обработки заказа."

def refund_order(account, order_id, chat_id):
    try:
        account.refund(order_id)
        logger.info(f"✔️ Возврат оформлен для заказа {order_id}")

        # Уведомление в Telegram о возврате
        send_telegram_notification(
            f"↩️ <b>ВОЗВРАТ СРЕДСТВ</b>\n"
            f"📋 ID заказа: <code>{order_id}</code>\n"
            f"💬 Чат: https://funpay.com/orders/{order_id}/\n"
            f"✅ Средства успешно возвращены покупателю"
        )
        account.send_message(chat_id, "✅ Средства успешно возвращены.")
        return True
    except Exception as e:
        logger.error(f"❌ Не удалось вернуть средства за заказ {order_id}: {e}")
        send_telegram_notification(
            f"❌ <b>ОШИБКА ВОЗВРАТА</b>\n"
            f"📋 ID заказа: <code>{order_id}</code>\n"
            f"💬 Чат: https://funpay.com/orders/{order_id}/\n"
            f"⚠️ Ошибка: {str(e)[:100]}..."
        )
        account.send_message(chat_id, "❌ Ошибка возврата. Свяжитесь с админом.")
        return False

def process_order(account, chat_id, username, stars, order_id):
    """Обрабатывает заказ и отправляет звезды через Fragment API"""
    clean_user = clean_username(username)

    # Уведомление в Telegram о новом заказе
    send_telegram_notification(
        f"🛒 <b>НОВЫЙ ЗАКАЗ</b>\n"
        f"📋 ID: <code>{order_id}</code>\n"
        f"👤 Покупатель: @{clean_user}\n"
        f"⭐ Звезд: <b>{stars}</b>\n"
        f"💬 Чат: https://funpay.com/orders/{order_id}/\n"
        f"⏳ Обрабатывается..."
    )

    # Отправляем подтверждение покупателю
    account.send_message(chat_id, f"✅ Заказ принят в обработку!\n"
                                  f"👤 Username: @{clean_user}\n"
                                  f"⭐ Звезд: {stars}\n"
                                  f"⏰ Обработка займет несколько минут...")

    # Автоматически отправляем звезды
    logger.info(f"⌛ Автоматическая отправка {stars} ⭐ пользователю @{clean_user}...")
    success, response = direct_send_stars(FRAGMENT_TOKEN, clean_user, stars)

    if success:
        # Уведомление об успешной отправке
        send_telegram_notification(
            f"✅ <b>ЗВЕЗДЫ ОТПРАВЛЕНЫ</b>\n"
            f"📋 ID заказа: <code>{order_id}</code>\n"
            f"👤 Получатель: @{clean_user}\n"
            f"⭐ Отправлено: <b>{stars} ⭐</b>\n"
            f"🎉 Заказ выполнен успешно!"
        )
        account.send_message(chat_id, f"✅ Успешно отправлено {stars} ⭐ пользователю @{clean_user}!")
        logger.info(f"✅ @{clean_user} получил {stars} ⭐")
    else:
        short_error = parse_fragment_error(response)
        send_telegram_notification(
            f"❌ <b>ОШИБКА ОТПРАВКИ</b>\n"
            f"📋 ID заказа: <code>{order_id}</code>\n"
            f"👤 Получатель: @{clean_user}\n"
            f"⭐ Звезд: <b>{stars}</b>\n"
            f"⚠️ Ошибка: {short_error}\n"
            f"🔁 Оформляю возврат..."
        )
        account.send_message(chat_id, short_error + "\n🔁 Пытаюсь оформить возврат...")
        refund_order(account, order_id, chat_id)

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "🤖 Бот мониторинга FunPay\n\n"
                          "Доступные команды:\n"
                          "/balance - текущий баланс Fragment\n"
                          "/status - статус бота")

@bot.message_handler(commands=['balance'])
def send_balance(message):
    balance = get_fragment_balance()
    bot.reply_to(message, f"💰 Текущий баланс: <b>{balance} TON</b>", parse_mode='HTML')

@bot.message_handler(commands=['status'])
def send_status(message):
    bot.reply_to(message, "✅ Бот работает в штатном режиме\n"
                          "🤖 Мониторинг заказов активен")

def start_telegram_bot():
    """Запускает Telegram бота в фоновом режиме"""
    import threading

    def polling():
        try:
            bot.infinity_polling()
        except Exception as e:
            logger.error(f"❌ Ошибка Telegram бота: {e}")

    thread = threading.Thread(target=polling, daemon=True)
    thread.start()
    logger.info("✅ Telegram бот запущен в фоновом режиме")

def main():
    global FRAGMENT_TOKEN
    golden_key = os.getenv("FUNPAY_AUTH_TOKEN")
    if not golden_key:
        logger.error("❌ FUNPAY_AUTH_TOKEN не найден в .env")
        return

    # Запускаем Telegram бота
    start_telegram_bot()

    account = Account(golden_key)
    account.get()

    if not account.username:
        logger.error("❌ Не удалось получить имя пользователя. Проверьте токен.")
        return

    logger.info(f"✅ Авторизован как {account.username}")
    runner = Runner(account)

    FRAGMENT_TOKEN = load_fragment_token() or authenticate_fragment()
    if not FRAGMENT_TOKEN:
        logger.error("❌ Не удалось авторизоваться в Fragment.")
        return

    logger.info("🤖 Бот запущен. Ожидание заказов на звезды...")

    last_reply_time = 0

    for event in runner.listen(requests_delay=3.0):
        try:
            now = time.time()
            if now - last_reply_time < COOLDOWN_SECONDS:
                continue

            if isinstance(event, NewOrderEvent):
                try:
                    order = account.get_order(event.order.id)
                    username = None
                    stars = None

                    if hasattr(order, 'buyer_params') and order.buyer_params:
                        username = clean_username(order.buyer_params.get("Telegram Username"))

                    if hasattr(order, 'lot_params') and order.lot_params:
                        for param in order.lot_params:
                            if param[0] == "Количество звёзд":
                                stars_match = re.search(r"(\d+)", param[1])
                                if stars_match:
                                    stars = int(stars_match.group(1))
                                break

                    if username and stars:
                        print(f"\n🎯 Новый заказ - @{username} - {stars} звёзд")
                        print(f"📋 ID заказа: {order.id}")
                        print("=" * 50)
                        process_order(account, order.chat_id, username, stars, order.id)
                        last_reply_time = now
                    else:
                        print(f"\n⚠️ Не удалось извлечь данные из заказа {order.id}")
                        if not username:
                            print("❌ Username не найден")
                        if not stars:
                            print("❌ Количество звезд не найдено")
                        print("=" * 50)

                except Exception as e:
                    logger.error(f"❌ Ошибка при получении информации о заказе: {e}")
                    continue

            elif isinstance(event, NewMessageEvent):
                msg = event.message
                if msg.author_id != account.id:
                    send_telegram_notification(
                        f"💬 <b>НОВОЕ СООБЩЕНИЕ</b>\n"
                        f"👤 От: <code>{msg.author}</code>\n"
                        f"💬 Чат: <code>{msg.chat_id}</code>\n"
                        f"📝 Текст: {msg.text[:100]}..."
                    )

        except Exception as e:
            logger.error(f"❌ Ошибка обработки события: {e}")

if __name__ == "__main__":
    main()

