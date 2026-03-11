import os
import re
import json
import uuid
import base64
from pathlib import Path
from typing import Dict, Any, List

from openai import OpenAI
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================================================
# ENV / PATHS
# =========================================================
TMP_DIR = Path("tmp_visuals")
TMP_DIR.mkdir(exist_ok=True)

MAX_VARIANTS = 3


def must_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} не знайдено. Додай у ~/.zshrc або export у терміналі.")
    return value


OPENAI_API_KEY = must_env("OPENAI_API_KEY")
TG_BOT_TOKEN = must_env("TG_BOT_TOKEN")
ADMIN_CHAT_ID = int(must_env("ADMIN_CHAT_ID"))

oa = OpenAI(api_key=OPENAI_API_KEY)

# =========================================================
# MEMORY
# =========================================================
DEFAULT_SESSION = {
    "photo_path": "",
    "products": [],
    "mood": "",
    "tulle": "",
    "visual_requested": False,
    "analysis_done": False,
    "analysis_json": {},
    "suggested_products": [],
    "pending_visual_path": "",
    "pending_visual_id": "",
    "visual_waiting_admin": False,
    "variant_count": 0,
    "awaiting_changes": False,
    "change_notes": "",
    "last_question": "",
    "client_messages": [],
}

SESSIONS: Dict[int, Dict[str, Any]] = {}


def get_session(chat_id: int) -> Dict[str, Any]:
    if chat_id not in SESSIONS:
        SESSIONS[chat_id] = json.loads(json.dumps(DEFAULT_SESSION))
    return SESSIONS[chat_id]


def reset_session(chat_id: int):
    SESSIONS[chat_id] = json.loads(json.dumps(DEFAULT_SESSION))


# =========================================================
# HELPERS
# =========================================================
def safe_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def normalize_text(text: str) -> str:
    t = (text or "").lower().strip()
    replacements = {
        "ё": "е",
        "ъ": "",
        "'": "",
        "’": "",
        "`": "",
    }
    for old, new in replacements.items():
        t = t.replace(old, new)
    return re.sub(r"\s+", " ", t)


def clamp_350(text: str) -> str:
    return safe_text(text)[:350].rstrip()


def set_last_question(session: Dict[str, Any], text: str):
    session["last_question"] = safe_text(text)


def same_question(session: Dict[str, Any], text: str) -> bool:
    return safe_text(session.get("last_question", "")) == safe_text(text)


def b64_to_file(b64_data: str, file_path: Path) -> Path:
    file_path.write_bytes(base64.b64decode(b64_data))
    return file_path


def add_client_message(session: Dict[str, Any], text: str):
    text = safe_text(text)
    if not text:
        return
    session["client_messages"].append(text)
    session["client_messages"] = session["client_messages"][-20:]


def product_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        ["Штори", "Ролети"],
        ["День-ніч", "Жалюзі"],
        ["Плісе", "Не знаю, що краще"],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def mood_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        ["Світлий", "Теплий натуральний"],
        ["Темніший"],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def tulle_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [["З тюлем", "Без тюлю"]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# =========================================================
# RECOGNITION
# =========================================================
PRODUCT_ORDER = ["штори", "ролети", "день-ніч", "жалюзі", "плісе"]


def extract_products(text: str) -> List[str]:
    t = normalize_text(text)
    found: List[str] = []

    if "день-ніч" in t or "день ніч" in t or "деньнiч" in t:
        found.append("день-ніч")
    elif "ролет" in t:
        found.append("ролети")

    if "жалюз" in t:
        found.append("жалюзі")

    if "плісе" in t or "плисе" in t:
        found.append("плісе")

    if "штор" in t or "тюль" in t:
        found.append("штори")

    out = []
    for item in found:
        if item not in out:
            out.append(item)
    return out


def extract_mood(text: str) -> str:
    t = normalize_text(text)
    if "світл" in t:
        return "світлий"
    if "тепл" in t or "натурал" in t or "беж" in t or "молоч" in t:
        return "теплий натуральний"
    if "темн" in t or "графіт" in t or "контраст" in t:
        return "темніший"
    return ""


def extract_tulle(text: str) -> str:
    t = normalize_text(text)
    if "без тюл" in t or "без тюлю" in t:
        return "без тюлю"
    if "з тюл" in t or "з тюлем" in t or t == "тюль":
        return "з тюлем"
    return ""


def wants_recommendation(text: str) -> bool:
    t = normalize_text(text)
    keys = [
        "не знаю",
        "що краще",
        "порадь",
        "підкажи",
        "як краще",
        "не впевнена",
        "не впевнений",
    ]
    return any(k in t for k in keys)


def wants_add_mode(text: str) -> bool:
    t = normalize_text(text)
    keys = ["ще", "додай", "також", "і ", "разом", "плюс", "ще й"]
    return any(k in t for k in keys)


def wants_replace_mode(text: str) -> bool:
    t = normalize_text(text)
    keys = ["замість", "тільки", "лише", "хочу тепер", "передумала", "передумав"]
    return any(k in t for k in keys)


def wants_remove_product(text: str) -> bool:
    t = normalize_text(text)
    keys = ["без ", "не треба ", "не хочу "]
    return any(k in t for k in keys)


def apply_products_from_text(session: Dict[str, Any], text: str):
    found = extract_products(text)
    if not found:
        return

    existing = session.get("products", [])[:]
    lower = normalize_text(text)

    if wants_remove_product(text) and existing:
        for p in found:
            if p in existing:
                existing.remove(p)
        session["products"] = existing
        return

    if wants_replace_mode(text) or not existing:
        session["products"] = found
    elif wants_add_mode(text):
        for p in found:
            if p not in existing:
                existing.append(p)
        session["products"] = existing
    else:
        # якщо клієнт просто пише новий продукт окремим повідомленням,
        # вважаємо це заміною, щоб бот не застрявав на старому.
        session["products"] = found

    # тюль має сенс тільки якщо є штори
    if "штори" not in session["products"]:
        session["tulle"] = "без тюлю"


# =========================================================
# AI ANALYSIS
# =========================================================
PHOTO_ANALYSIS_SYSTEM = """
Ти — дизайнер інтер'єру та текстильний візуалізатор.
Подивись на фото кімнати/вікна і поверни лише JSON.

Формат:
{
  "space_type": "",
  "style_hint": "",
  "color_direction": "",
  "recommended_products": ["", "", ""],
  "short_client_text": ""
}

Правила:
- запропонуй до 3 варіантів продуктів тільки з цього списку:
  ["штори", "ролети", "день-ніч", "жалюзі", "плісе"]
- не пропонуй більше 3
- текст короткий
- без зайвих пояснень
"""


def analyze_photo(photo_bytes: bytes) -> Dict[str, Any]:
    try:
        b64 = base64.b64encode(photo_bytes).decode("utf-8")
        data_url = f"data:image/jpeg;base64,{b64}"

        resp = oa.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": PHOTO_ANALYSIS_SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Проаналізуй фото і поверни JSON."},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
        )
        data = json.loads(resp.choices[0].message.content)
        recs = data.get("recommended_products", []) or []
        clean_recs = [p for p in recs if p in PRODUCT_ORDER][:3]
        if not clean_recs:
            clean_recs = ["ролети", "жалюзі", "штори"]
        data["recommended_products"] = clean_recs
        return data
    except Exception:
        return {
            "space_type": "",
            "style_hint": "",
            "color_direction": "",
            "recommended_products": ["ролети", "жалюзі", "штори"],
            "short_client_text": "По вашому фото тут можуть добре виглядати ролети, жалюзі або штори.",
        }


# =========================================================
# IMAGE EDIT
# =========================================================
def build_edit_prompt(session: Dict[str, Any]) -> str:
    products = session.get("products", [])
    mood = session.get("mood", "")
    tulle = session.get("tulle", "")
    change_notes = session.get("change_notes", "")
    products_text = ", ".join(products) if products else "window treatment"

    combo_rule = ""
    if "жалюзі" in products and "штори" in products:
        combo_rule = "Show both blinds and curtains together on the same existing window in a realistic layered way."
    elif "ролети" in products and "штори" in products:
        combo_rule = "Show both classic roller blinds and curtains together on the same existing window in a realistic layered way."
    elif "день-ніч" in products and "штори" in products:
        combo_rule = "Show both zebra blinds and curtains together on the same existing window in a realistic layered way."
    elif "плісе" in products and "штори" in products:
        combo_rule = "Show both pleated blinds and curtains together on the same existing window in a realistic layered way."
    elif "штори" in products and tulle == "з тюлем":
        combo_rule = "Show curtains together with realistic light tulle."
    elif "штори" in products and tulle == "без тюлю":
        combo_rule = "Show only curtains. Do not add tulle."
    elif "жалюзі" in products and "штори" not in products:
        combo_rule = "Show only blinds. Do not add curtains or tulle."
    elif "ролети" in products and "штори" not in products:
        combo_rule = "Show only classic roller blinds. Do not add curtains or tulle."
    elif "день-ніч" in products and "штори" not in products:
        combo_rule = "Show only zebra blinds. Do not add curtains or tulle."
    elif "плісе" in products and "штори" not in products:
        combo_rule = "Show only pleated blinds. Do not add curtains or tulle."

    return f"""
Edit this exact client photo.

Keep exactly the same:
- windows
- walls
- doors
- room geometry
- perspective
- camera angle
- composition

Do not:
- create new windows
- move windows
- move doors
- move walls
- change architecture
- redesign the whole room

You may:
- add only the requested window solution
- add subtle decor only if appropriate
- add a small rug, plant or vase if needed
- slightly improve lighting, but not dramatically

Requested products: {products_text}
Mood / color direction: {mood}
Tulle: {tulle}
Change notes from client: {change_notes}

Product meanings:
- "ролети" = classic roller blinds
- "день-ніч" = zebra blinds with alternating horizontal stripes
- "жалюзі" = elegant modern blinds
- "плісе" = pleated blinds
- "штори" = classic curtains

Special combination rule:
{combo_rule}

Result requirements:
- edit only the existing client photo
- place the products only on the existing window
- premium, realistic, tasteful
- no text on image
"""


def generate_visual_preview(session: Dict[str, Any]) -> Path:
    photo_path = Path(session["photo_path"])
    prompt = build_edit_prompt(session)

    with photo_path.open("rb") as image_file:
        result = oa.images.edit(
            model="gpt-image-1",
            image=image_file,
            prompt=prompt,
            size="1536x1024",
        )

    b64_data = result.data[0].b64_json
    visual_id = str(uuid.uuid4())[:8]
    out_path = TMP_DIR / f"visual_{visual_id}.png"
    b64_to_file(b64_data, out_path)

    session["pending_visual_path"] = str(out_path)
    session["pending_visual_id"] = visual_id
    session["visual_waiting_admin"] = True
    return out_path


# =========================================================
# ADMIN
# =========================================================
def admin_summary(session: Dict[str, Any], chat_id: int) -> str:
    products = ", ".join(session.get("products", [])) or "-"
    return (
        f"🧾 Запит на візуалізацію\n"
        f"💬 chat_id: {chat_id}\n"
        f"Продукти: {products}\n"
        f"Настрій: {session.get('mood') or '-'}\n"
        f"Тюль: {session.get('tulle') or '-'}\n"
        f"Зміни: {session.get('change_notes') or '-'}\n"
        f"Варіант №: {session.get('variant_count', 0)}"
    )


async def send_admin_text(context: ContextTypes.DEFAULT_TYPE, text: str):
    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text[:4000])


async def send_visual_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, session: Dict[str, Any]):
    visual_path = Path(session["pending_visual_path"])
    client_chat_id = update.effective_chat.id
    visual_id = session["pending_visual_id"]

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Надіслати клієнту", callback_data=f"send|{client_chat_id}|{visual_id}")],
        [InlineKeyboardButton("🎨 Ще варіант", callback_data=f"more|{client_chat_id}|{visual_id}")],
        [InlineKeyboardButton("🔁 Попросити уточнення", callback_data=f"revise|{client_chat_id}|{visual_id}")],
    ])

    caption = admin_summary(session, client_chat_id)[:1024]

    with visual_path.open("rb") as f:
        await context.bot.send_photo(
            chat_id=ADMIN_CHAT_ID,
            photo=f,
            caption=caption,
            reply_markup=keyboard,
        )


# =========================================================
# FLOW HELPERS
# =========================================================
def need_tulle_question(session: Dict[str, Any]) -> bool:
    products = session.get("products", [])
    return "штори" in products and not session.get("tulle")


def need_mood_question(session: Dict[str, Any]) -> bool:
    return not session.get("mood")


def can_generate(session: Dict[str, Any]) -> bool:
    if not session.get("photo_path"):
        return False
    if not session.get("products"):
        return False
    if need_tulle_question(session):
        return False
    if need_mood_question(session):
        return False
    return True


async def ask_next_needed(update: Update, session: Dict[str, Any]):
    if not session.get("products"):
        msg = "Що хочете побачити на візуалізації: штори, ролети, день-ніч, жалюзі чи плісе?"
        set_last_question(session, msg)
        await update.message.reply_text(clamp_350(msg), reply_markup=product_keyboard())
        return True

    if need_tulle_question(session):
        products_text = ", ".join(session["products"])
        msg = f"Добре 🙌 Бачу, що хочете: {products_text}. Чи потрібен тюль до штор?"
        set_last_question(session, msg)
        await update.message.reply_text(clamp_350(msg), reply_markup=tulle_keyboard())
        return True

    if need_mood_question(session):
        products_text = ", ".join(session["products"])
        msg = f"Добре 🙌 Бачу, що хочете: {products_text}. Який настрій ближчий: світлий, теплий натуральний чи темніший?"
        set_last_question(session, msg)
        await update.message.reply_text(clamp_350(msg), reply_markup=mood_keyboard())
        return True

    return False


async def maybe_generate(update: Update, context: ContextTypes.DEFAULT_TYPE, session: Dict[str, Any]):
    if not can_generate(session):
        return False

    if session.get("variant_count", 0) >= MAX_VARIANTS:
        await update.message.reply_text("Ми вже зробили 3 варіанти 🙌 Напишіть, будь ласка, що саме хочете змінити.")
        return True

    wait_msg = "Роблю візуалізацію, зачекайте, будь ласка 🙌"
    set_last_question(session, wait_msg)
    await update.message.reply_text(clamp_350(wait_msg))

    try:
        session["variant_count"] += 1
        generate_visual_preview(session)
        await send_visual_to_admin(update, context, session)

        done_msg = "Візуалізацію підготовлено і передано на погодження."
        set_last_question(session, done_msg)
        await update.message.reply_text(clamp_350(done_msg))
        session["awaiting_changes"] = False
        return True
    except Exception as e:
        await send_admin_text(context, f"⚠️ Помилка генерації: {e}")
        await update.message.reply_text("Сталася технічна пауза. Спробуйте ще раз.")
        return True


# =========================================================
# START
# =========================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_session(update.effective_chat.id)
    text = "Вітаю! Надішліть фото вікна або інтер’єру, і я підготую візуальний варіант для вашого простору."
    await update.message.reply_text(clamp_350(text))


# =========================================================
# PHOTO
# =========================================================
async def handle_photo_common(update: Update, context: ContextTypes.DEFAULT_TYPE, is_document: bool):
    chat_id = update.effective_chat.id
    session = get_session(chat_id)

    try:
        if is_document:
            file_id = update.message.document.file_id
            tg_file = await context.bot.get_file(file_id)
        else:
            file_id = update.message.photo[-1].file_id
            tg_file = await context.bot.get_file(file_id)

        image_bytes = await tg_file.download_as_bytearray()
        local_path = TMP_DIR / f"client_{chat_id}_{uuid.uuid4().hex[:8]}.jpg"
        local_path.write_bytes(bytes(image_bytes))

        session["photo_path"] = str(local_path)
        print("DEBUG PHOTO SAVED:", chat_id, session["photo_path"])

        analysis = analyze_photo(bytes(image_bytes))
        session["analysis_done"] = True
        session["analysis_json"] = analysis
        session["suggested_products"] = analysis.get("recommended_products", [])[:3]

        recs = session["suggested_products"] or ["ролети", "жалюзі", "штори"]
        recs_text = ", ".join(recs)

        # якщо вже є достатньо даних і клієнт просто оновив фото — не починати з нуля
        if can_generate(session):
            msg = f"Фото оновила 🙌 Продукти: {', '.join(session['products'])}. Роблю візуалізацію, зачекайте, будь ласка."
            await update.message.reply_text(clamp_350(msg))
            await maybe_generate(update, context, session)
            return

        if session.get("products"):
            asked = await ask_next_needed(update, session)
            if asked:
                return

        text = (
            f"Дякую 🙌 По вашому фото тут можуть добре виглядати: {recs_text}. "
            f"Що хочете побачити на візуалізації?"
        )
        set_last_question(session, text)
        await update.message.reply_text(clamp_350(text), reply_markup=product_keyboard())

    except Exception as e:
        await send_admin_text(context, f"⚠️ Помилка обробки фото: {e}")
        await update.message.reply_text("Дякую! Фото отримала. Напишіть, будь ласка, що хочете побачити: штори, ролети, день-ніч, жалюзі чи плісе.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_photo_common(update, context, is_document=False)


async def handle_photo_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_photo_common(update, context, is_document=True)


# =========================================================
# TEXT
# =========================================================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    user_text = safe_text(update.message.text or "")
    print("DEBUG TEXT:", chat_id, user_text, "photo_path=", session.get("photo_path"))
    lower = normalize_text(user_text)

    if not user_text:
        return

    add_client_message(session, user_text)

    # якщо фото ще нема
    if not session.get("photo_path"):
        msg = "Надішліть, будь ласка, фото вікна або інтер’єру, і я підготую візуальний варіант."
        set_last_question(session, msg)
        await update.message.reply_text(clamp_350(msg))
        return

    # клієнт не знає, що хоче
    if wants_recommendation(user_text) or lower == "не знаю, що краще":
        recs = session.get("suggested_products", []) or ["ролети", "жалюзі", "штори"]
        recs_text = ", ".join(recs)
        msg = f"По вашому фото тут можуть добре виглядати: {recs_text}. Що хочете спробувати на візуалізації?"
        set_last_question(session, msg)
        await update.message.reply_text(clamp_350(msg), reply_markup=product_keyboard())
        return

    # якщо бот чекає уточнення до нового варіанту
    if session.get("awaiting_changes"):
        session["change_notes"] = user_text

        # одночасно дозволяємо змінити продукт/настрій/тюль текстом
        apply_products_from_text(session, user_text)

        tulle = extract_tulle(user_text)
        if tulle:
            session["tulle"] = tulle

        mood = extract_mood(user_text)
        if mood:
            session["mood"] = mood

        asked = await ask_next_needed(update, session)
        if asked:
            return

        await maybe_generate(update, context, session)
        return

    # оновлюємо продукти
    before_products = session.get("products", [])[:]
    apply_products_from_text(session, user_text)
    after_products = session.get("products", [])

    if after_products != before_products:
        # якщо клієнт змінив продукт — дозволяємо новий варіант
        if "штори" not in after_products:
            session["tulle"] = "без тюлю"

        # якщо клієнт додав штори після іншого продукту, спитаємо про тюль
        asked = await ask_next_needed(update, session)
        if asked:
            return

    # тюль
    tulle = extract_tulle(user_text)
    if tulle and "штори" in session.get("products", []):
        session["tulle"] = tulle
        asked = await ask_next_needed(update, session)
        if asked:
            return

    # настрій
    mood = extract_mood(user_text)
    if mood:
        session["mood"] = mood

    # якщо клієнт після отримання візуалізації пише "ще штори", "ще жалюзі" то теж маємо згенерувати
    if session.get("products"):
        asked = await ask_next_needed(update, session)
        if asked:
            return

        if can_generate(session):
            await maybe_generate(update, context, session)
            return

    # fallback
    msg = "Напишіть, будь ласка, що хочете побачити: штори, ролети, день-ніч, жалюзі чи плісе."
    if not same_question(session, msg):
        set_last_question(session, msg)
        await update.message.reply_text(clamp_350(msg), reply_markup=product_keyboard())


# =========================================================
# CALLBACKS
# =========================================================
async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.message.chat_id != ADMIN_CHAT_ID:
        await query.answer("Це доступно тільки адміну.", show_alert=True)
        return

    try:
        action, client_chat_id_str, visual_id = query.data.split("|", 2)
        client_chat_id = int(client_chat_id_str)
    except Exception:
        return

    session = get_session(client_chat_id)

    if action == "send":
        path = session.get("pending_visual_path", "")
        if not path or not Path(path).exists():
            await query.answer("Файл не знайдено.", show_alert=True)
            return

        with Path(path).open("rb") as f:
            await context.bot.send_photo(
                chat_id=client_chat_id,
                photo=f,
                caption=clamp_350(
                    "Ось варіант для вашого простору 🙌 "
                    "Можу ще показати інший варіант або змінити деталі. "
                    "Напишіть, будь ласка, що саме хочете змінити."
                ),
            )

        session["visual_waiting_admin"] = False
        await query.edit_message_caption(caption=(query.message.caption or "") + "\n\n✅ Надіслано клієнту.")
        return

    if action == "more":
        if session.get("variant_count", 0) >= MAX_VARIANTS:
            await context.bot.send_message(
                chat_id=client_chat_id,
                text="Ми вже зробили 3 варіанти 🙌 Напишіть, будь ласка, що саме хочете змінити.",
            )
            await query.edit_message_caption(caption=(query.message.caption or "") + "\n\nℹ️ Досягнуто 3 варіанти.")
            return

        session["visual_waiting_admin"] = False
        session["pending_visual_path"] = ""
        session["pending_visual_id"] = ""
        session["awaiting_changes"] = True

        await context.bot.send_message(
            chat_id=client_chat_id,
            text="Можу ще показати інший варіант 🙌 Напишіть, будь ласка, що саме хочете змінити.",
        )
        await query.edit_message_caption(caption=(query.message.caption or "") + "\n\n🎨 Попросили ще варіант.")
        return

    if action == "revise":
        session["visual_waiting_admin"] = False
        session["pending_visual_path"] = ""
        session["pending_visual_id"] = ""
        session["awaiting_changes"] = True

        await context.bot.send_message(
            chat_id=client_chat_id,
            text="Напишіть, будь ласка, що саме хочете змінити у наступному варіанті 🙌",
        )
        await query.edit_message_caption(caption=(query.message.caption or "") + "\n\n🔁 Запросили уточнення.")
        return


# =========================================================
# MAIN
# =========================================================
def main():
    app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_admin_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_photo_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("✅ Telegram visual bot is running…")
    app.run_polling()


if __name__ == "__main__":
    main()