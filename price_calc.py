"""
PriceCalc — калькулятор комиссии и чистой цены (плагин для FunPay Cardinal v0.1.17+)

💰 Считает, сколько покупатель заплатит с учётом комиссии FunPay для выбранного
раздела, и какую цену поставить, чтобы получить нужную итоговую сумму. Использует
штатный метод FunPay `calc` — те же данные, что показывает сайт при создании лота.

Возможности:
 • Прямой расчёт «цена → итог»: вы вводите цену лота, плагин показывает комиссию,
   итоговую сумму для покупателя по каждому способу оплаты и минимальную цену раздела.
 • Обратный расчёт «итог → цена»: вы вводите, сколько должен заплатить покупатель,
   плагин считает, какую цену поставить в лот.
 • Список разделов берётся из ваших реальных лотов — ничего вводить вручную не нужно.
 • Поддержка обычных лотов и валюты (чипсов).
 • 🧪 Кнопка «Тест» — быстрый расчёт для выбранного раздела.

Команды Telegram:
 • /calc — открыть калькулятор.

Установка: положить этот файл в папку plugins/ и перезапустить Кардинал.
Автор: yungspider
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from typing import TYPE_CHECKING

from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B, CallbackQuery, Message
from telebot.apihelper import ApiTelegramException

try:
    from tg_bot import CBT, static_keyboards as skb, utils as tg_utils
except Exception:
    CBT = None
    skb = None
    tg_utils = None

try:
    import FunPayAPI
    _SubTypes = FunPayAPI.enums.SubCategoryTypes
except Exception:
    try:
        from FunPayAPI.common import enums as _e
        _SubTypes = _e.SubCategoryTypes
    except Exception:
        _SubTypes = None

if TYPE_CHECKING:
    from cardinal import Cardinal

logger = logging.getLogger("FPC.PriceCalc")

# ══════════════════════════════ МЕТАДАННЫЕ ПЛАГИНА ══════════════════════════════
NAME = "PriceCalc (Commission & Net Price Calculator)"
VERSION = "1.0.0"
DESCRIPTION = ("Калькулятор комиссии FunPay: прямой расчёт «цена → итог для покупателя» "
               "и обратный «итог → цена». Разделы берутся из ваших лотов, данные — "
               "штатным методом calc. Команда: /calc")
CREDITS = "yungspider"
UUID = "44da78c0-dc6b-4198-8307-a2a95ee16340"
SETTINGS_PAGE = True

# ══════════════════════════════ НАСТРОЙКИ ПЛАГИНА ═══════════════════════════════
PLUGIN_DIR = os.path.join("storage", "plugins", "price_calc")
SETTINGS_PATH = os.path.join(PLUGIN_DIR, "settings.json")

DEFAULT_SETTINGS = {
    "last_sub": None,     # {"type":"common"/"currency","id":int,"game_id":int,"name":str}
    "last_price": 1000.0,
}
PRICE_RANGE = (1.0, 10_000_000.0)
REF_PRICE = 1000.0         # опорная цена для вычисления коэффициента комиссии
PAGE_SIZE = 8

CB_HOME = "pc:home"
CB_PICK = "pc:pick"
CB_SUBS = "pc:subs"        # + :{page}
CB_SUB = "pc:sub"          # + :{type}:{id}
CB_FWD = "pc:fwd"
CB_REV = "pc:rev"
CB_MIN = "pc:min"
CB_TEST = "pc:test"
CB_NOOP = "pc:noop"
ST_PRICE = "pc:st:price"
ST_TARGET = "pc:st:target"

_stop_event = threading.Event()
_settings_lock = threading.Lock()
_cardinal: "Cardinal | None" = None

CUR_SYM = {"RUB": "₽", "USD": "$", "EUR": "€", "UAH": "₴", "KZT": "₸", "BYN": "Br",
           "UNKNOWN": "", "NONE": ""}


# ══════════════════════════════ УТИЛИТЫ ═════════════════════════════════════════
def _clamp(value, min_v, max_v):
    return max(min_v, min(max_v, value))


def _load_settings() -> dict:
    with _settings_lock:
        data = {}
        if os.path.exists(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                logger.error("PriceCalc: не удалось прочитать настройки, использую значения по умолчанию.")
                logger.debug("TRACEBACK", exc_info=True)
                data = {}
        settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        if isinstance(data, dict):
            settings.update({k: v for k, v in data.items() if k in settings})
        if not isinstance(settings["last_sub"], dict):
            settings["last_sub"] = None
        try:
            settings["last_price"] = float(settings["last_price"])
        except (TypeError, ValueError):
            settings["last_price"] = DEFAULT_SETTINGS["last_price"]
        return settings


def _save_settings(settings: dict) -> None:
    with _settings_lock:
        try:
            os.makedirs(PLUGIN_DIR, exist_ok=True)
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.error("PriceCalc: не удалось сохранить настройки.")
            logger.debug("TRACEBACK", exc_info=True)


def _escape(text) -> str:
    if tg_utils:
        return tg_utils.escape(str(text))
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _money(value, currency=None) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    sym = CUR_SYM.get(getattr(currency, "name", str(currency or "")).upper(), "")
    txt = f"{v:,.2f}".rstrip("0").rstrip(".") if v != int(v) else f"{int(v)}"
    return f"{txt} {sym}".strip()


def _fmt_num(value) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{v:,.2f}".rstrip("0").rstrip(".") if v != int(v) else f"{int(v)}"


def _plugin_enabled(crd: "Cardinal") -> bool:
    try:
        return crd.plugins[UUID].enabled
    except Exception:
        return False


# ══════════════════════════════ РАЗДЕЛЫ ПРОДАВЦА ════════════════════════════════
def _seller_subcategories(crd: "Cardinal") -> dict:
    """Возвращает {key: {"type","id","game_id","name"}} — уникальные разделы из лотов продавца."""
    result: dict[str, dict] = {}
    try:
        profile = getattr(crd, "profile", None)
        lots = profile.get_lots() if profile is not None else []
    except Exception:
        logger.debug("PriceCalc: не удалось получить лоты профиля.", exc_info=True)
        lots = []
    for lot in lots or []:
        try:
            sub = getattr(lot, "subcategory", None)
            if sub is None:
                continue
            is_common = bool(getattr(sub, "is_common", lambda: True)())
            stype = "common" if is_common else "currency"
            sid = int(sub.id)
            cat = getattr(sub, "category", None)
            gid = int(cat.id) if cat is not None else sid
            key = f"{stype}:{sid}"
            if key in result:
                continue
            name = getattr(sub, "fullname", None) or getattr(sub, "name", None) or f"#{sid}"
            result[key] = {"type": stype, "id": sid, "game_id": gid, "name": str(name)}
        except Exception:
            logger.debug("PriceCalc: пропуск лота при сборе разделов.", exc_info=True)
            continue
    return result


# ══════════════════════════════ РАСЧЁТЫ ═════════════════════════════════════════
def _do_calc(crd: "Cardinal", sub: dict, price: float):
    """Вызывает штатный calc FunPay для раздела. Возвращает CalcResult или бросает исключение."""
    if _SubTypes is None:
        raise RuntimeError("FunPayAPI.enums недоступен")
    price = float(price)
    if sub.get("type") == "currency":
        return crd.account.calc(_SubTypes.CURRENCY, game_id=int(sub["game_id"]), price=price)
    return crd.account.calc(_SubTypes.COMMON, subcategory_id=int(sub["id"]), price=price)


def _coefficient(crd: "Cardinal", sub: dict) -> float:
    """Коэффициент комиссии (итог/цена) для раздела — по опорной цене."""
    res = _do_calc(crd, sub, REF_PRICE)
    try:
        k = float(res.commission_coefficient)
    except Exception:
        k = 1.0
    return k if k and k >= 1.0 else 1.0


def forward_calc(crd: "Cardinal", sub: dict, price: float) -> dict:
    """Прямой расчёт: цена лота → итог для покупателя. Возвращает словарь для вывода."""
    price = _clamp(float(price), *PRICE_RANGE)
    res = _do_calc(crd, sub, price)
    methods = []
    for m in getattr(res, "methods", []) or []:
        mp = float(getattr(m, "price", 0) or 0)
        methods.append({
            "name": str(getattr(m, "name", "") or "Способ"),
            "price": mp,
            "surcharge": mp - price,
            "percent": ((mp / price - 1) * 100) if price else 0.0,
            "currency": getattr(m, "currency", None),
        })
    methods.sort(key=lambda x: x["price"])
    try:
        comm_percent = float(res.commission_percent)
    except Exception:
        comm_percent = methods[0]["percent"] if methods else 0.0
    return {
        "price": price,
        "currency": getattr(res, "account_currency", None),
        "commission_percent": comm_percent,
        "min_price": getattr(res, "min_price_with_commission", None),
        "min_currency": getattr(res, "min_price_currency", None),
        "methods": methods,
        "sub": sub,
    }


def reverse_calc(crd: "Cardinal", sub: dict, target: float) -> dict:
    """Обратный расчёт: желаемый итог для покупателя → цена лота."""
    target = _clamp(float(target), *PRICE_RANGE)
    k = _coefficient(crd, sub)
    price = target / k if k else target
    price = round(price, 2)
    fwd = forward_calc(crd, sub, price)
    return {"target": target, "coefficient": k, "price": price, "forward": fwd, "sub": sub}


# ══════════════════════════════ ТЕКСТЫ ВЫВОДА ═══════════════════════════════════
def _forward_text(data: dict) -> str:
    sub = data["sub"]
    lines = [f"💰 <b>Расчёт комиссии</b>\n",
             f"📂 Раздел: <b>{_escape(sub.get('name'))}</b>",
             f"🏷 Цена лота: <b>{_fmt_num(data['price'])}</b> {_escape(CUR_SYM.get(getattr(data['currency'],'name','').upper(),''))}".rstrip(),
             f"📈 Комиссия: <b>{data['commission_percent']:.2f}%</b>"]
    if data.get("min_price"):
        lines.append(f"⬇️ Мин. цена раздела: <b>{_money(data['min_price'], data.get('min_currency'))}</b>")
    lines.append("\n<b>Покупатель заплатит:</b>")
    if data["methods"]:
        for m in data["methods"][:8]:
            lines.append(f"• {_escape(_short(m['name'], 28))}: <b>{_money(m['price'], m['currency'])}</b> "
                         f"<i>(+{_fmt_num(m['surcharge'])}, {m['percent']:.1f}%)</i>")
    else:
        lines.append("• <i>нет данных о способах оплаты</i>")
    return "\n".join(lines)


def _reverse_text(data: dict) -> str:
    sub = data["sub"]
    fwd = data["forward"]
    lines = [f"🎯 <b>Обратный расчёт</b>\n",
             f"📂 Раздел: <b>{_escape(sub.get('name'))}</b>",
             f"💵 Хочу, чтобы покупатель заплатил: <b>{_fmt_num(data['target'])}</b>",
             f"📈 Коэффициент комиссии: <b>{data['coefficient']:.4f}</b>",
             f"\n👉 <b>Поставьте цену лота: {_fmt_num(data['price'])}</b>"]
    if fwd["methods"]:
        cheapest = fwd["methods"][0]
        lines.append(f"ℹ️ Итог для покупателя (мин. способ): <b>{_money(cheapest['price'], cheapest['currency'])}</b>")
    return "\n".join(lines)


def _short(text: str, limit: int = 28) -> str:
    text = str(text or "").replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 1] + "…"


# ══════════════════════════ TELEGRAM-ИНТЕРФЕЙС ══════════════════════════════════
def _home_text(crd: "Cardinal") -> str:
    settings = _load_settings()
    sub = settings.get("last_sub")
    subs = _seller_subcategories(crd)
    sub_line = f"📂 Раздел: <b>{_escape(sub['name'])}</b>" if sub else "📂 Раздел: <i>не выбран</i>"
    return (f"💰 <b>{_escape(NAME)}</b> <i>v{VERSION}</i>\n\n"
            f"Калькулятор комиссии FunPay. Выберите раздел, затем сделайте расчёт.\n\n"
            f"{sub_line}\n"
            f"🏷 Последняя цена: <b>{_fmt_num(settings['last_price'])}</b>\n"
            f"📦 Разделов доступно: <b>{len(subs)}</b>")


def _home_kb() -> K:
    settings = _load_settings()
    kb = K(row_width=2)
    kb.add(B("📂 Выбрать раздел", None, CB_PICK))
    has_sub = bool(settings.get("last_sub"))
    kb.add(B("🔢 Цена → итог", None, CB_FWD if has_sub else CB_NOOP))
    kb.add(B("🎯 Итог → цена", None, CB_REV if has_sub else CB_NOOP))
    kb.add(B("ℹ️ Мин. цена раздела", None, CB_MIN if has_sub else CB_NOOP))
    kb.add(B("🧪 Тест", None, CB_TEST if has_sub else CB_NOOP))
    kb.add(B("🔄 Обновить", None, CB_HOME))
    if CBT:
        kb.add(B("🔙 Назад к плагину", None, f"{CBT.EDIT_PLUGIN}:{UUID}:0"))
    return kb


def _subs_kb(crd: "Cardinal", page: int) -> tuple[str, K]:
    subs = _seller_subcategories(crd)
    items = sorted(subs.values(), key=lambda s: s["name"].lower())
    if not items:
        return ("📂 У вас пока нет лотов — разделы появятся после создания первого лота.", None)
    pages = max(1, math.ceil(len(items) / PAGE_SIZE))
    page = _clamp(page, 0, pages - 1)
    chunk = items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    text = f"📂 <b>Выберите раздел</b> (стр. {page + 1}/{pages}):"
    kb = K(row_width=1)
    for s in chunk:
        kb.add(B(_short(s["name"], 32), None, f"{CB_SUB}:{s['type']}:{s['id']}"))
    nav = []
    if page > 0:
        nav.append(B("⬅️", None, f"{CB_SUBS}:{page - 1}"))
    nav.append(B("🏠 Меню", None, CB_HOME))
    if page < pages - 1:
        nav.append(B("➡️", None, f"{CB_SUBS}:{page + 1}"))
    kb.row(*nav)
    return text, kb


def _answer(bot, c: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    try:
        bot.answer_callback_query(c.id, text, show_alert=alert)
    except Exception:
        pass


def _edit_or_send(bot, c: CallbackQuery, text: str, kb: K | None) -> None:
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.id, parse_mode="HTML", reply_markup=kb)
    except ApiTelegramException as e:
        if "message is not modified" not in str(e):
            try:
                bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                logger.debug("TRACEBACK", exc_info=True)
    except Exception:
        try:
            bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)


def init(crd: "Cardinal", *args) -> None:
    """Инициализация плагина (BIND_TO_PRE_INIT)."""
    global _cardinal
    _cardinal = crd
    _load_settings()

    tg = crd.telegram
    if tg is not None:
        bot = tg.bot

        def open_home(c: CallbackQuery) -> None:
            _answer(bot, c)
            _edit_or_send(bot, c, _home_text(crd), _home_kb())

        def open_picker(c: CallbackQuery) -> None:
            _answer(bot, c)
            text, kb = _subs_kb(crd, 0)
            _edit_or_send(bot, c, text, kb)

        def nav_subs(c: CallbackQuery) -> None:
            _answer(bot, c)
            try:
                page = int(c.data.split(":")[-1])
            except (ValueError, IndexError):
                page = 0
            text, kb = _subs_kb(crd, page)
            _edit_or_send(bot, c, text, kb)

        def pick_sub(c: CallbackQuery) -> None:
            subs = _seller_subcategories(crd)
            parts = c.data.split(":")
            key = f"{parts[2]}:{parts[3]}" if len(parts) >= 4 else ""
            sub = subs.get(key)
            if not sub:
                _answer(bot, c, "Раздел не найден — обновите меню.", alert=True)
                return
            settings = _load_settings()
            settings["last_sub"] = sub
            _save_settings(settings)
            _answer(bot, c, f"📂 Раздел: {sub['name']}")
            _edit_or_send(bot, c, _home_text(crd), _home_kb())

        def _need_sub(c: CallbackQuery) -> dict | None:
            sub = _load_settings().get("last_sub")
            if not sub:
                _answer(bot, c, "Сначала выберите раздел.", alert=True)
                _edit_or_send(bot, c, _home_text(crd), _home_kb())
            return sub

        def act_forward(c: CallbackQuery) -> None:
            if not _need_sub(c):
                return
            s = _load_settings()
            result = bot.send_message(
                c.message.chat.id,
                f"🔢 Введите цену лота для расчёта "
                f"(<code>{_fmt_num(PRICE_RANGE[0])}</code> - <code>{_fmt_num(PRICE_RANGE[1])}</code>).\n\n"
                f"Последняя цена: <b>{_fmt_num(s['last_price'])}</b>",
                parse_mode="HTML", reply_markup=skb.CLEAR_STATE_BTN() if skb else None)
            tg.set_state(c.message.chat.id, result.id, c.from_user.id, ST_PRICE)
            _answer(bot, c)

        def act_reverse(c: CallbackQuery) -> None:
            if not _need_sub(c):
                return
            result = bot.send_message(
                c.message.chat.id,
                "🎯 Введите, сколько должен заплатить покупатель (итоговая сумма с комиссией).\n"
                "Плагин посчитает, какую цену поставить в лот.",
                parse_mode="HTML", reply_markup=skb.CLEAR_STATE_BTN() if skb else None)
            tg.set_state(c.message.chat.id, result.id, c.from_user.id, ST_TARGET)
            _answer(bot, c)

        def act_min(c: CallbackQuery) -> None:
            sub = _need_sub(c)
            if not sub:
                return
            _answer(bot, c)
            try:
                data = forward_calc(crd, sub, _load_settings()["last_price"])
                if data.get("min_price"):
                    text = (f"⬇️ Минимальная цена раздела <b>{_escape(sub['name'])}</b>:\n"
                            f"<b>{_money(data['min_price'], data.get('min_currency'))}</b>")
                else:
                    text = "ℹ️ FunPay не вернул минимальную цену для этого раздела."
            except Exception as e:
                text = f"❌ Не удалось получить данные: {_escape(_short(str(e), 80))}"
                logger.debug("TRACEBACK", exc_info=True)
            _edit_or_send(bot, c, text, _home_kb())

        def act_test(c: CallbackQuery) -> None:
            sub = _need_sub(c)
            if not sub:
                return
            _answer(bot, c)
            try:
                data = forward_calc(crd, sub, REF_PRICE)
                _edit_or_send(bot, c, _forward_text(data), _home_kb())
            except Exception as e:
                _edit_or_send(bot, c, f"❌ Ошибка расчёта: {_escape(_short(str(e), 80))}", _home_kb())
                logger.debug("TRACEBACK", exc_info=True)

        def _state_eq(m: Message, state: str) -> bool:
            try:
                return tg.check_state(m.chat.id, m.from_user.id, state)
            except Exception:
                return False

        def _parse_amount(text) -> float | None:
            s = str(text or "").strip().replace(",", ".").replace(" ", "").replace("\u2009", "")
            for sym in CUR_SYM.values():
                if sym:
                    s = s.replace(sym, "")
            try:
                return float(s)
            except (ValueError, AttributeError):
                return None

        def edit_price(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            value = _parse_amount(m.text)
            if value is None:
                bot.reply_to(m, "❌ Не удалось распознать число. Пример: <code>1500</code> или <code>1500.50</code>",
                             parse_mode="HTML")
                return
            settings = _load_settings()
            sub = settings.get("last_sub")
            if not sub:
                bot.reply_to(m, "❌ Сначала выберите раздел.")
                return
            settings["last_price"] = _clamp(value, *PRICE_RANGE)
            _save_settings(settings)
            try:
                data = forward_calc(crd, sub, settings["last_price"])
                bot.send_message(m.chat.id, _forward_text(data), parse_mode="HTML", reply_markup=_home_kb())
            except Exception as e:
                bot.reply_to(m, f"❌ Ошибка расчёта: {_short(str(e), 80)}")
                logger.debug("TRACEBACK", exc_info=True)

        def edit_target(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            value = _parse_amount(m.text)
            if value is None:
                bot.reply_to(m, "❌ Не удалось распознать число. Пример: <code>1500</code>",
                             parse_mode="HTML")
                return
            settings = _load_settings()
            sub = settings.get("last_sub")
            if not sub:
                bot.reply_to(m, "❌ Сначала выберите раздел.")
                return
            try:
                data = reverse_calc(crd, sub, value)
                bot.send_message(m.chat.id, _reverse_text(data), parse_mode="HTML", reply_markup=_home_kb())
            except Exception as e:
                bot.reply_to(m, f"❌ Ошибка расчёта: {_short(str(e), 80)}")
                logger.debug("TRACEBACK", exc_info=True)

        def cmd_open(m: Message) -> None:
            bot.send_message(m.chat.id, _home_text(crd), parse_mode="HTML", reply_markup=_home_kb())

        def noop(c: CallbackQuery) -> None:
            _answer(bot, c, "Сначала выберите раздел 🙂")

        tg.cbq_handler(open_home, lambda c: c.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
        tg.cbq_handler(open_home, lambda c: c.data == CB_HOME)
        tg.cbq_handler(open_picker, lambda c: c.data == CB_PICK)
        tg.cbq_handler(nav_subs, lambda c: c.data.startswith(f"{CB_SUBS}:"))
        tg.cbq_handler(pick_sub, lambda c: c.data.startswith(f"{CB_SUB}:"))
        tg.cbq_handler(act_forward, lambda c: c.data == CB_FWD)
        tg.cbq_handler(act_reverse, lambda c: c.data == CB_REV)
        tg.cbq_handler(act_min, lambda c: c.data == CB_MIN)
        tg.cbq_handler(act_test, lambda c: c.data == CB_TEST)
        tg.cbq_handler(noop, lambda c: c.data == CB_NOOP)
        tg.msg_handler(edit_price, func=lambda m: _state_eq(m, ST_PRICE))
        tg.msg_handler(edit_target, func=lambda m: _state_eq(m, ST_TARGET))
        tg.msg_handler(cmd_open, commands=["calc"])

        try:
            crd.add_telegram_commands(UUID, [("calc", "Калькулятор комиссии и чистой цены.", True)])
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)

        logger.info(f"PriceCalc v{VERSION}: Telegram-меню зарегистрировано (команда /calc).")
    else:
        logger.info(f"PriceCalc v{VERSION}: Telegram выключен — калькулятор недоступен.")


def on_delete(crd: "Cardinal", c=None) -> None:
    """Вызывается при удалении плагина через ПУ (BIND_TO_DELETE)."""
    _stop_event.set()


# ══════════════════════════ ПРИВЯЗКА К СОБЫТИЯМ КАРДИНАЛА ═══════════════════════
BIND_TO_PRE_INIT = [init]
BIND_TO_DELETE = on_delete
