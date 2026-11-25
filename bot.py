"""
Бот реєстрації на ABSURD PARTY 917.

Питає: ім'я -> скільки людей -> що чекаєш від вечора (можна пропустити).
У день вечірки сам надсилає всім нагадування з кнопками «Йду» / «Не вийде».
Зберігає все в SQLite. Повторна реєстрація оновлює запис.

Команди для гостей:
  /start  — зареєструватися (або оновити реєстрацію)
  /cancel — скасувати реєстрацію

Команди для адмінів (ID з ADMIN_IDS):
  /count        — скільки людей прийде
  /list         — список гостей
  /export       — список у CSV-файлі
  /remind_test  — надіслати нагадування тільки собі (перевірити, як виглядає)
  /remind_now   — надіслати нагадування всім зараз (якщо треба вручну)
  /battle       — анонс батлу костюмів (спершу показує прев'ю, розсилає після підтвердження)
"""

import asyncio
import csv
import io
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Forbidden, BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ---------- Налаштування ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x
}
# На Railway, якщо підключено Volume, база автоматично лежить на ньому
_VOLUME = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
DB_PATH = os.environ.get("DB_PATH") or (
    os.path.join(_VOLUME, "guests.db") if _VOLUME else "guests.db"
)

TZ = ZoneInfo("Europe/Kyiv")
# Коли надіслати нагадування (за київським часом). Формат: РРРР-ММ-ДД ГГ:ХХ
REMIND_AT = os.environ.get("REMIND_AT", "2026-11-27 10:00")

EVENT_INFO = "27 листопада · 18:00 · 154 PUB (ТЦ «Мармелад»)"

EXPECT_OPTIONS = {
    "exp_party": "Відірватись 🔥",
    "exp_meet": "Познайомитись 🤝",
    "exp_absurd": "Подивитись, що за абсурд 🌀",
    "exp_chill": "Повтикати 🫠",
}

REMINDER_TEXT = (
    "Сьогодні той самий вечір 🌀\n\n"
    "<b>ABSURD PARTY 917</b>\n"
    f"{EVENT_INFO}\n\n"
    "Welcome drink чекає тих, хто в списку. Ти з нами?"
)

BATTLE_TEXT = (
    "🎭 <b>БАТЛ КОСТЮМІВ</b>\n\n"
    "Приходь у найабсурднішому костюмі.\n"
    "🏆 Переможця чекає <b>секретний приз</b>.\n\n"
    f"📍 {EVENT_INFO}"
)

# Стани розмови
NAME, PEOPLE, PEOPLE_CUSTOM, EXPECT = range(4)

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)


# ---------- База даних ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS guests (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            name        TEXT NOT NULL,
            people      INTEGER NOT NULL,
            expectation TEXT,
            created_at  TEXT NOT NULL
        )"""
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(guests)")}
    if "status" not in cols:  # 'yes' / 'no' / NULL (ще не відповів)
        conn.execute("ALTER TABLE guests ADD COLUMN status TEXT")
    if "reminded" not in cols:
        conn.execute("ALTER TABLE guests ADD COLUMN reminded INTEGER NOT NULL DEFAULT 0")
    return conn


def save_guest(user_id, username, name, people, expectation):
    with db() as conn:
        conn.execute(
            """INSERT INTO guests (user_id, username, name, people, expectation, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username=excluded.username, name=excluded.name,
                 people=excluded.people, expectation=excluded.expectation,
                 status=NULL""",
            (user_id, username, name, people, expectation,
             datetime.now(TZ).strftime("%Y-%m-%d %H:%M")),
        )


def delete_guest(user_id) -> bool:
    with db() as conn:
        return conn.execute("DELETE FROM guests WHERE user_id = ?", (user_id,)).rowcount > 0


def set_status(user_id, status) -> bool:
    with db() as conn:
        return conn.execute(
            "UPDATE guests SET status = ? WHERE user_id = ?", (status, user_id)
        ).rowcount > 0


def all_guests():
    with db() as conn:
        return conn.execute(
            "SELECT name, username, people, expectation, created_at, status "
            "FROM guests ORDER BY created_at"
        ).fetchall()


def guests_to_remind():
    with db() as conn:
        return [r[0] for r in conn.execute(
            "SELECT user_id FROM guests WHERE reminded = 0"
        )]


def mark_reminded(user_id):
    with db() as conn:
        conn.execute("UPDATE guests SET reminded = 1 WHERE user_id = ?", (user_id,))


# ---------- Клавіатури ----------
PEOPLE_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("Сам(а)", callback_data="ppl_1"),
     InlineKeyboardButton("Нас 2", callback_data="ppl_2")],
    [InlineKeyboardButton("Нас 3", callback_data="ppl_3"),
     InlineKeyboardButton("Нас 4", callback_data="ppl_4")],
    [InlineKeyboardButton("Більше", callback_data="ppl_more")],
])

EXPECT_KB = InlineKeyboardMarkup(
    [[InlineKeyboardButton(text, callback_data=key)] for key, text in EXPECT_OPTIONS.items()]
    + [[InlineKeyboardButton("Пропустити ⏭", callback_data="exp_skip")]]
)

RSVP_KB = InlineKeyboardMarkup([[
    InlineKeyboardButton("Йду 🔥", callback_data="rsvp_yes"),
    InlineKeyboardButton("Не вийде 😔", callback_data="rsvp_no"),
]])


# ---------- Реєстрація ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Привіт! Це реєстрація на <b>ABSURD PARTY 917</b> 🌀\n"
        f"{EVENT_INFO}\n\n"
        "Займе 30 секунд.\n\n"
        "Як тебе записати? Ім'я або нік.",
        parse_mode="HTML",
    )
    return NAME


async def got_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name or len(name) > 60:
        await update.message.reply_text("Напиши ім'я трохи коротше 🙂")
        return NAME
    context.user_data["name"] = name
    await update.message.reply_text(
        f"Приємно, {name}! Ти сам(а) чи з компанією?", reply_markup=PEOPLE_KB
    )
    return PEOPLE


async def got_people_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "ppl_more":
        await q.edit_message_text("Скільки вас буде разом із тобою? Напиши число.")
        return PEOPLE_CUSTOM
    people = int(q.data.split("_")[1])
    context.user_data["people"] = people
    await q.edit_message_text(f"Записали: {people_label(people)} ✅")
    return await ask_expectation(q.message)


async def got_people_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit() or not (1 <= int(text) <= 30):
        await update.message.reply_text("Напиши просто число, наприклад 6.")
        return PEOPLE_CUSTOM
    context.user_data["people"] = int(text)
    return await ask_expectation(update.message)


async def ask_expectation(message):
    await message.reply_text(
        "І останнє, якщо хочеш: <b>чого чекаєш від вечора?</b>\n"
        "Обери варіант, напиши свій або пропусти.",
        reply_markup=EXPECT_KB,
        parse_mode="HTML",
    )
    return EXPECT


async def got_expect_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    expectation = None if q.data == "exp_skip" else EXPECT_OPTIONS[q.data]
    await q.edit_message_reply_markup(reply_markup=None)
    return await finish(q.from_user, q.message, context, expectation)


async def got_expect_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    expectation = update.message.text.strip()[:300]
    return await finish(update.effective_user, update.message, context, expectation)


async def finish(user, message, context, expectation):
    data = context.user_data
    save_guest(user.id, user.username, data["name"], data["people"], expectation)
    await message.reply_text(
        "Готово, ти в списку ✅\n"
        f"👤 {data['name']}\n"
        f"👥 {people_label(data['people'])}\n"
        + (f"💭 {expectation}\n" if expectation else "")
        + f"\n📍 {EVENT_INFO}\n\n"
        "У день вечірки ми нагадаємо 🔔\n"
        "Якщо плани зміняться, натисни /cancel, щоб звільнити місце іншим.\n"
        "Змінити дані: /start"
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_registration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if delete_guest(update.effective_user.id):
        await update.message.reply_text(
            "Реєстрацію скасовано. Шкода! Якщо передумаєш, тисни /start 🙌"
        )
    else:
        await update.message.reply_text("Ти ще не зареєстрований(а). Тисни /start 🙂")
    return ConversationHandler.END


def people_label(n: int) -> str:
    return "сам(а)" if n == 1 else f"вас {n}"


# ---------- Нагадування ----------
async def send_reminders(context: ContextTypes.DEFAULT_TYPE):
    ids = guests_to_remind()
    sent = failed = 0
    for user_id in ids:
        try:
            await context.bot.send_message(
                user_id, REMINDER_TEXT, parse_mode="HTML", reply_markup=RSVP_KB
            )
            sent += 1
        except (Forbidden, BadRequest):  # заблокував бота або видалив чат
            failed += 1
        except Exception as e:
            log.warning("Не вдалося надіслати %s: %s", user_id, e)
            failed += 1
            continue
        mark_reminded(user_id)
        await asyncio.sleep(0.05)  # ліміт Telegram ~30 повідомлень/с
    log.info("Нагадування: надіслано %s, не вдалося %s", sent, failed)
    for admin in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin, f"🔔 Нагадування розіслано: {sent}\nНе дійшло: {failed}"
            )
        except Exception:
            pass


async def rsvp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    status = "yes" if q.data == "rsvp_yes" else "no"
    if not set_status(q.from_user.id, status):
        await q.answer("Тебе вже немає в списку. Тисни /start, щоб записатися", show_alert=True)
        return
    await q.answer()
    if status == "yes":
        text = "Чекаємо на тебе о 18:00 🔥\n📍 154 PUB (ТЦ «Мармелад»)"
    else:
        text = "Шкода 😔 Дякуємо, що попередив(ла)! Наступного разу точно побачимось"
    # кнопки лишаються, щоб можна було передумати
    await q.edit_message_text(
        REMINDER_TEXT + f"\n\n<i>Твоя відповідь: {text}</i>",
        parse_mode="HTML",
        reply_markup=RSVP_KB,
    )


def schedule_reminder(app: Application):
    remind_at = datetime.strptime(REMIND_AT, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    now = datetime.now(TZ)
    if now < remind_at:
        app.job_queue.run_once(send_reminders, when=remind_at, name="reminder")
        log.info("Нагадування заплановано на %s (Київ)", remind_at)
    elif now < remind_at + timedelta(hours=6):
        # бот перезапустився трохи пізніше, ніж треба: дошлемо тим, кому ще не надіслали
        app.job_queue.run_once(send_reminders, when=5, name="reminder")
        log.info("Час нагадування минув нещодавно: надсилаю зараз")
    else:
        log.info("Час нагадування (%s) вже минув, не планую", remind_at)


# ---------- Адмін ----------
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            return
        return await func(update, context)
    return wrapper


@admin_only
async def count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    guests = all_guests()
    total = sum(g[2] for g in guests)
    yes = sum(g[2] for g in guests if g[5] == "yes")
    no = sum(g[2] for g in guests if g[5] == "no")
    stats = {}
    for g in guests:
        key = g[3] or "— пропустили"
        stats[key] = stats.get(key, 0) + 1
    lines = "\n".join(f"  {k}: {v}" for k, v in sorted(stats.items(), key=lambda x: -x[1]))
    await update.message.reply_text(
        f"📊 Реєстрацій: {len(guests)}\n"
        f"👥 Людей разом: {total}\n\n"
        f"Після нагадування:\n"
        f"  ✅ Йдуть: {yes}\n"
        f"  ❌ Не вийде: {no}\n"
        f"  ❔ Не відповіли: {total - yes - no}\n\n"
        f"Чого чекають:\n{lines or '  поки нічого'}"
    )


STATUS_ICON = {"yes": "✅", "no": "❌", None: ""}


@admin_only
async def list_guests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    guests = all_guests()
    if not guests:
        await update.message.reply_text("Поки ніхто не зареєструвався.")
        return
    lines = []
    for i, (name, username, people, _exp, _c, status) in enumerate(guests, 1):
        uname = f" @{username}" if username else ""
        extra = f" (+{people - 1})" if people > 1 else ""
        icon = f" {STATUS_ICON[status]}" if status else ""
        lines.append(f"{i}. {name}{uname}{extra}{icon}")
    text = "\n".join(lines) + f"\n\nВсього людей: {sum(g[2] for g in guests)}"
    for i in range(0, len(text), 4000):  # ліміт довжини повідомлення
        await update.message.reply_text(text[i:i + 4000])


@admin_only
async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Ім'я", "Telegram", "Кількість людей", "Чого чекає",
                "Дата реєстрації", "Відповідь на нагадування"])
    status_text = {"yes": "Йде", "no": "Не вийде", None: ""}
    for name, username, people, exp, created, status in all_guests():
        w.writerow([name, f"@{username}" if username else "", people,
                    exp or "", created, status_text[status]])
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))  # щоб Excel бачив кирилицю
    data.name = "absurd_party_guests.csv"
    await update.message.reply_document(data)


def guests_for_broadcast():
    """Усі гості, крім тих, хто відповів «Не вийде»."""
    with db() as conn:
        return [r[0] for r in conn.execute(
            "SELECT user_id FROM guests WHERE status IS NULL OR status != 'no'"
        )]


async def broadcast(bot, text, reply_markup=None):
    sent = failed = 0
    for user_id in guests_for_broadcast():
        try:
            await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=reply_markup)
            sent += 1
        except Exception as e:
            log.warning("Не вдалося надіслати %s: %s", user_id, e)
            failed += 1
        await asyncio.sleep(0.05)
    return sent, failed


BATTLE_CONFIRM_KB = InlineKeyboardMarkup([[
    InlineKeyboardButton("✅ Розіслати всім", callback_data="battle_send"),
    InlineKeyboardButton("✖️ Скасувати", callback_data="battle_cancel"),
]])


@admin_only
async def battle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = len(guests_for_broadcast())
    await update.message.reply_text(BATTLE_TEXT, parse_mode="HTML")
    await update.message.reply_text(
        f"☝️ Так виглядатиме анонс. Розіслати {n} гостям?\n"
        "(Ті, хто натиснув «Не вийде», його не отримають.)",
        reply_markup=BATTLE_CONFIRM_KB,
    )


async def battle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer()
        return
    await q.answer()
    if q.data == "battle_cancel":
        await q.edit_message_text("Розсилку скасовано.")
        return
    await q.edit_message_text("Розсилаю анонс батлу…")
    sent, failed = await broadcast(context.bot, BATTLE_TEXT)
    await q.edit_message_text(f"🎭 Анонс батлу розіслано: {sent}\nНе дійшло: {failed}")


@admin_only
async def remind_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(REMINDER_TEXT, parse_mode="HTML", reply_markup=RSVP_KB)


@admin_only
async def remind_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Розсилаю нагадування…")
    await send_reminders(context)


# ---------- Запуск ----------
async def post_init(app: Application):
    db().close()  # створити/оновити таблицю
    log.info("База: %s (гостей у ній: %s)", os.path.abspath(DB_PATH), len(all_guests()))
    on_railway = any(k.startswith("RAILWAY_") for k in os.environ)
    if on_railway and not _VOLUME:
        log.warning("!!! Volume НЕ підключено: список гостей зітреться при перезапуску")
    elif _VOLUME and not os.path.abspath(DB_PATH).startswith(os.path.abspath(_VOLUME)):
        log.warning("!!! DB_PATH (%s) не на Volume (%s): список гостей зітреться при перезапуску",
                    DB_PATH, _VOLUME)
    schedule_reminder(app)


def main():
    if not BOT_TOKEN:
        raise SystemExit("Вкажи BOT_TOKEN у змінних середовища (див. README).")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_name)],
            PEOPLE: [CallbackQueryHandler(got_people_button, pattern=r"^ppl_")],
            PEOPLE_CUSTOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_people_custom)],
            EXPECT: [
                CallbackQueryHandler(got_expect_button, pattern=r"^exp_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_expect_text),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_registration),
            CommandHandler("start", start),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(rsvp, pattern=r"^rsvp_"))
    app.add_handler(CommandHandler("cancel", cancel_registration))
    app.add_handler(CommandHandler("count", count))
    app.add_handler(CommandHandler("list", list_guests))
    app.add_handler(CommandHandler("export", export))
    app.add_handler(CommandHandler("remind_test", remind_test))
    app.add_handler(CommandHandler("remind_now", remind_now))
    app.add_handler(CommandHandler("battle", battle))
    app.add_handler(CallbackQueryHandler(battle_confirm, pattern=r"^battle_"))

    log.info("Бот запущено")
    app.run_polling()


if __name__ == "__main__":
    main()
