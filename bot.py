import os
import logging
import time
import json
import re
import requests
import telebot
from dotenv import load_dotenv
from FunPayAPI import Account, types  # Добавляем types
from FunPayAPI.updater.runner import Runner
from FunPayAPI.updater.events import NewOrderEvent, NewMessageEvent
from queue import Queue  # Импортируем очередь
import threading  # Для потока обработки очереди

load_dotenv()

# --- Константы и Настройка ---
# Telegram bot
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_USER_ID = os.getenv("TELEGRAM_USER_ID")
LOT_ID_TO_DEACTIVATE = os.getenv("LOT_ID_TO_DEACTIVATE")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Логгирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

COOLDOWN_SECONDS = 1
TOKEN_FILE = "auth_token.json"
FRAGMENT_API_URL = "https://api.fragment-api.com/v1"

# Fragment auth
FRAGMENT_TOKEN = None
FRAGMENT_API_KEY = os.getenv("FRAGMENT_API_KEY")
FRAGMENT_PHONE = os.getenv("FRAGMENT_PHONE")
FRAGMENT_MNEMONICS = os.getenv("FRAGMENT_MNEMONICS")

# Очередь для обработки заказов (FIFO)
order_queue = Queue()


# --- Вспомогательные функции ---

def clean_username(username):
    """Очищает username от лишних символов @"""
    if username:
        return username.lstrip('@').strip()
    return username


def send_telegram_notification(message):
    """Отправляет уведомление в Telegram"""
    try:
        bot.send_message(TELEGRAM_USER_ID, message, parse_mode='HTML')
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
    global FRAGMENT_TOKEN
    FRAGMENT_TOKEN = load_fragment_token()
    if FRAGMENT_TOKEN:
        logger.info("✅ Токен Fragment загружен из файла.")
        return FRAGMENT_TOKEN

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


def direct_send_stars(token, username, quantity):
    """Отправляет звезды через Fragment API"""
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
    """Парсит ошибку Fragment API и возвращает удобное сообщение."""
    try:
        data = json.loads(response_text)
    except:
        return "❌ Неизвестная ошибка Fragment API", False  # False - не ошибка "нет звезд"

    if isinstance(data, dict):
        if "username" in data:
            return "❌ Неверный Telegram-тег. Проверьте правильность тега и напишите в чат.", False
        if "quantity" in data:
            return "❌ Минимум 50 ⭐ для покупки. Заказ отменен. Проверьте лот.", False
        if "errors" in data:
            for err in data["errors"]:
                if "Not enough funds" in err.get("error", ""):
                    # Это критическая ошибка, требующая деактивации лота
                    return "❌ Извините, у нас закончились звёзды. Лот будет деактивирован", True

    # Если ошибка не распознана, отправляем полный текст ошибки в Telegram
    send_telegram_notification(f"⚠️ **Неизвестная ошибка Fragment** при отправке: {response_text}")
    return "❌ Неизвестная ошибка. Ожидайте ответа.", False


def deactivate_lot(account):
    """Деактивирует лот на FunPay при критической ошибке."""
    if not LOT_ID_TO_DEACTIVATE:
        logger.error("❌ Не удалось деактивировать лот: LOT_ID_TO_DEACTIVATE не установлен.")
        return False

    try:
        # 1. Получение полей лота
        lot_fields: types.LotFields = account.get_lot_fields(lot_id=LOT_ID_TO_DEACTIVATE)

        if not lot_fields.active:
            logger.info("❗ Лот уже деактивирован.")
            return True

        # 2. Деактивация лота
        lot_fields.active = False
        lot_fields.renew_fields()
        account.save_lot(lot_fields)

        logger.info(f"✅ Лот ID {LOT_ID_TO_DEACTIVATE} успешно деактивирован.")
        send_telegram_notification(
            f"⛔️ <b>ЛОТ ДЕАКТИВИРОВАН!</b>\n"
            f"📋 ID: <code>{LOT_ID_TO_DEACTIVATE}</code> - {lot_fields.title_ru}\n"
            f"Причина: Закончились звезды на Fragment. Пополните баланс."
        )
        return True

    except Exception as e:
        logger.error(f"❌ Ошибка деактивации лота {LOT_ID_TO_DEACTIVATE}: {e}")
        send_telegram_notification(
            f"❌ <b>КРИТИЧЕСКАЯ ОШИБКА ДЕАКТИВАЦИИ ЛОТА</b>\n"
            f"📋 ID: <code>{LOT_ID_TO_DEACTIVATE}</code>\n"
            f"⚠️ Ошибка: {str(e)[:100]}..."
        )
        return False


def process_order(account, chat_id, username, stars, order_id, quantity_multiplier):
    """
    Обрабатывает заказ, отправляет звезды через Fragment API.
    """
    global FRAGMENT_TOKEN
    clean_user = clean_username(username)
    total_stars = stars * quantity_multiplier

    # Уведомление в Telegram о новом заказе
    send_telegram_notification(
        f"🛒 <b>НОВЫЙ ЗАКАЗ</b>\n"
        f"📋 ID: <code>{order_id}</code>\n"
        f"👤 Покупатель: @{clean_user}\n"
        f"⭐ Звезд: <b>{total_stars} ⭐</b>\n"
        f"💬 Чат: https://funpay.com/orders/{order_id}/\n"
        f"⏳ Обрабатывается..."
    )

    # Отправляем подтверждение покупателю
    account.send_message(chat_id, f"✅ Заказ принят в обработку!\n"
                                  f"👤 Username: @{clean_user}\n"
                                  f"⭐ Звезд: {total_stars} ⭐\n"
                                  f"⏰ Обработка займет некоторое время...")

    # Автоматически отправляем звезды
    logger.info(f"⌛ Автоматическая отправка {total_stars} ⭐ пользователю @{clean_user}...")
    success, response = direct_send_stars(FRAGMENT_TOKEN, clean_user, total_stars)

    if success:
        # Уведомление об успешной отправке
        send_telegram_notification(
            f"✅ <b>ЗВЕЗДЫ ОТПРАВЛЕНЫ</b>\n"
            f"📋 ID заказа: <code>{order_id}</code>\n"
            f"👤 Получатель: @{clean_user}\n"
            f"⭐ Отправлено: <b>{total_stars} ⭐</b>\n"
            f"🎉 Заказ выполнен успешно!"
        )
        account.send_message(chat_id, f"✅ Успешно отправлено {total_stars} ⭐ пользователю @{clean_user}!")
        logger.info(f"✅ @{clean_user} получил {total_stars} ⭐")
    else:
        # Ошибка отправки
        error_message, is_out_of_stars = parse_fragment_error(response)

        send_telegram_notification(
            f"❌ <b>ОШИБКА ОТПРАВКИ</b>\n"
            f"📋 ID заказа: <code>{order_id}</code>\n"
            f"👤 Получатель: @{clean_user}\n"
            f"⭐ Звезд: <b>{total_stars}</b>\n"
            f"⚠️ Ошибка: {error_message}"
        )

        # Отправляем ошибку в FunPay чат
        account.send_message(chat_id, f"❌ **Произошла ошибка при отправке звезд:**\n{error_message}\n"
                                      f"Просьба подождать, администратор скоро свяжется с вами для решения проблемы.")

        logger.error(f"❌ Ошибка отправки ⭐ для заказа {order_id}: {error_message}")

        # Проверяем, нужно ли деактивировать лот
        if is_out_of_stars:
            deactivate_lot(account)


# --- Логика очереди ---

def order_worker(account):
    """Поток для обработки заказов из очереди."""
    while True:
        # Ожидаем новый заказ
        order_data = order_queue.get()
        if order_data is None:  # Сигнал для завершения потока
            break

        chat_id, username, stars, order_id, quantity_multiplier = order_data

        # Обработка заказа
        try:
            process_order(account, chat_id, username, stars, order_id, quantity_multiplier)
        except Exception as e:
            logger.error(f"❌ Критическая ошибка в обработчике очереди заказа {order_id}: {e}")

        # Сообщаем, что задача выполнена
        order_queue.task_done()
        time.sleep(COOLDOWN_SECONDS)  # Небольшая задержка между заказами


# --- Telegram Bot ---

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "🤖 Бот мониторинга FunPay\n\n"
                          "Доступные команды:\n"
                          "/balance - текущий баланс Fragment\n"
                          "/status - статус бота")


@bot.message_handler(commands=['balance'])
def send_balance(message):
    if str(message.chat.id) != TELEGRAM_USER_ID: return  # Только для админа
    balance = get_fragment_balance()
    bot.reply_to(message, f"💰 Текущий баланс: <b>{balance} TON</b>", parse_mode='HTML')


@bot.message_handler(commands=['status'])
def send_status(message):
    if str(message.chat.id) != TELEGRAM_USER_ID: return  # Только для админа
    status_message = "✅ Бот работает в штатном режиме\n"
    status_message += f"🤖 Мониторинг заказов активен\n"
    status_message += f"⏳ Заказов в очереди: {order_queue.qsize()}"
    if LOT_ID_TO_DEACTIVATE:
        status_message += f"\n🔗 ID контролируемого лота: {LOT_ID_TO_DEACTIVATE}"
    else:
        status_message += "\n⚠️ LOT_ID_TO_DEACTIVATE не установлен в .env!"

    bot.reply_to(message, status_message)


def start_telegram_bot():
    """Запускает Telegram бота в фоновом режиме"""

    def polling():
        try:
            bot.infinity_polling()
        except Exception as e:
            logger.error(f"❌ Ошибка Telegram бота: {e}")

    thread = threading.Thread(target=polling, daemon=True)
    thread.start()
    logger.info("✅ Telegram бот запущен в фоновом режиме")


# --- Основной запуск ---

def main():
    golden_key = os.getenv("FUNPAY_AUTH_TOKEN")
    if not golden_key:
        logger.error("❌ FUNPAY_AUTH_TOKEN не найден в .env")
        return

    if not LOT_ID_TO_DEACTIVATE:
        logger.warning("⚠️ LOT_ID_TO_DEACTIVATE не установлен в .env. Автоматическая деактивация лота невозможна.")

    # Запускаем Telegram бота
    start_telegram_bot()

    # Авторизация FunPay
    account = Account(golden_key=golden_key).get()
    if not account.username:
        logger.error("❌ Не удалось получить имя пользователя FunPay. Проверьте токен.")
        return

    logger.info(f"✅ Авторизован FunPay как {account.username}")

    # Авторизация Fragment
    global FRAGMENT_TOKEN
    FRAGMENT_TOKEN = authenticate_fragment()
    if not FRAGMENT_TOKEN:
        logger.error("❌ Не удалось авторизоваться в Fragment. Бот FunPay не запускается.")
        return

    # Запуск потока обработки очереди
    worker = threading.Thread(target=order_worker, args=(account,), daemon=True)
    worker.start()
    logger.info("✅ Поток обработки заказов запущен.")

    logger.info("🤖 Бот запущен. Ожидание заказов на звезды...")

    runner = Runner(account)

    for event in runner.listen(requests_delay=3.0):
        try:
            # Обработка нового заказа
            if isinstance(event, NewOrderEvent):
                try:
                    order = account.get_order(event.order.id)
                    username = None
                    stars = None
                    quantity_multiplier = 1

                    # Извлечение данных заказа
                    if hasattr(order, 'buyer_params') and order.buyer_params:
                        username = clean_username(order.buyer_params.get("Telegram Username"))

                    if hasattr(order, 'lot_params') and order.lot_params:
                        for param in order.lot_params:
                            if param[0] == "Количество звёзд":
                                stars_match = re.search(r"(\d+)", param[1])
                                if stars_match:
                                    stars = int(stars_match.group(1))
                                break
                        quantity_multiplier = order.amount

                    if username and stars:
                        total_stars = stars * quantity_multiplier
                        print(f"\n🎯 Новый заказ добавлен в очередь: @{username} - {total_stars} ⭐ (ID: {order.id})")
                        print("=" * 50)

                        # 1. Добавление заказа в очередь
                        order_queue.put((order.chat_id, username, stars, order.id, quantity_multiplier))

                    else:
                        print(f"\n⚠️ Не удалось извлечь данные из заказа {order.id}. Игнорирую.")
                        print("=" * 50)

                except Exception as e:
                    logger.error(f"❌ Ошибка при получении информации о заказе: {e}")
                    continue

            # Обработка нового сообщения
            elif isinstance(event, NewMessageEvent):
                msg = event.message
                if msg.author_id != account.id:
                    send_telegram_notification(
                        f"💬 <b>НОВОЕ СООБЩЕНИЕ</b>\n"
                        f"👤 От: <code>{msg.author}</code>\n"
                        f"💬 Чат: https://funpay.com/orders/{msg.chat_id}/\n"  # Ссылка на чат заказа
                        f"📝 Текст: {msg.text[:100]}..."
                    )

        except Exception as e:
            logger.error(f"❌ Ошибка обработки события: {e}")


if __name__ == "__main__":
    main()
