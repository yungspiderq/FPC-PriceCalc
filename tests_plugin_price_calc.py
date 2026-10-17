"""
Функциональные тесты плагина price_calc.py на моках.
Запуск: cd FPC && python3 tests_plugin_price_calc.py
"""
import importlib.util
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, ".")
sys.path.append("plugins")

spec = importlib.util.spec_from_file_location("plugins.price_calc", "plugins/price_calc.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules["plugins.price_calc"] = plugin
spec.loader.exec_module(plugin)

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("✓ " if cond else "✗ FAIL: ") + name)


def reset_settings(**kwargs):
    os.makedirs(plugin.PLUGIN_DIR, exist_ok=True)
    base = json.loads(json.dumps(plugin.DEFAULT_SETTINGS))
    base.update(kwargs)
    with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(base, f, ensure_ascii=False)


# ───────────────────────────── моки FunPayAPI ─────────────────────────────
class MockCurrency:
    def __init__(self, name):
        self.name = name


RUB = MockCurrency("RUB")


class MockPaymentMethod:
    def __init__(self, name, price, currency):
        self.name = name
        self.price = price
        self.currency = currency


class MockCalcResult:
    def __init__(self, price, methods, coef, pct, min_price, min_cur, acc_cur):
        self.price = price
        self.methods = methods
        self._coef = coef
        self._pct = pct
        self.min_price_with_commission = min_price
        self.min_price_currency = min_cur
        self.account_currency = acc_cur

    @property
    def commission_coefficient(self):
        return self._coef

    @property
    def commission_percent(self):
        return self._pct


class MockAccount:
    is_initiated = True
    id = 999

    def __init__(self, fail=False):
        self.fail = fail
        self.calc_calls = []

    def calc(self, subcategory_type, subcategory_id=None, game_id=None, price=1000):
        self.calc_calls.append((str(subcategory_type), subcategory_id, game_id, float(price)))
        if self.fail:
            raise Exception("mock: calc failed")
        p = float(price)
        # СБП дешевле карты → коэффициент 1.02
        methods = [MockPaymentMethod("Карта", round(p * 1.05, 2), RUB),
                   MockPaymentMethod("СБП", round(p * 1.02, 2), RUB)]
        return MockCalcResult(p, methods, 1.02, 2.0, 50.0, RUB, RUB)


class MockCategory:
    def __init__(self, id_, name):
        self.id = id_
        self.name = name


class MockSubCategory:
    def __init__(self, id_, name, common=True, cat=None):
        self.id = id_
        self.name = name
        self.fullname = name
        self.category = cat or MockCategory(id_, "Game")
        self._common = common

    def is_common(self):
        return self._common


class MockLot:
    def __init__(self, sub):
        self.subcategory = sub


class MockProfile:
    def __init__(self, lots):
        self._lots = lots

    def get_lots(self):
        return self._lots


# ───────────────────────────── моки Telegram ─────────────────────────────
class MockBot:
    def __init__(self):
        self.answers, self.edits, self.sent, self.replies = [], [], [], []

    def answer_callback_query(self, cb_id, text=None, show_alert=False):
        self.answers.append((cb_id, text, show_alert))

    def edit_message_text(self, text, chat_id, message_id, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))
        return SimpleNamespace(id=message_id)

    def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))
        return SimpleNamespace(id=len(self.sent) + 100, chat=SimpleNamespace(id=chat_id))

    def reply_to(self, m, text, parse_mode=None):
        self.replies.append(text)

    def delete_message(self, chat_id, message_id):
        pass


def kb_buttons(kb) -> str:
    if kb is None:
        return ""
    try:
        return " | ".join(b.text for row in kb.keyboard for b in row if b.text)
    except Exception:
        return str(kb)


def kb_callbacks(kb) -> str:
    if kb is None:
        return ""
    try:
        return " | ".join(str(b.callback_data) for row in kb.keyboard for b in row if b.callback_data)
    except Exception:
        return ""


class MockTelegram:
    def __init__(self):
        self.bot = MockBot()
        self.cbq_handlers, self.msg_handlers, self.user_states = [], [], {}

    def cbq_handler(self, handler, func, **kwargs):
        self.cbq_handlers.append((handler, func))

    def msg_handler(self, handler, **kwargs):
        self.msg_handlers.append((handler, kwargs))

    def set_state(self, chat_id, message_id, user_id, state, data=None):
        self.user_states[(chat_id, user_id)] = {"state": state, "mid": message_id, "data": data or {}}

    def get_state(self, chat_id, user_id):
        return self.user_states.get((chat_id, user_id))

    def check_state(self, chat_id, user_id, state):
        return self.user_states.get((chat_id, user_id), {}).get("state") == state

    def clear_state(self, chat_id, user_id, del_msg=False):
        st = self.user_states.pop((chat_id, user_id), None)
        return st.get("mid") if st else None


class MockCardinal:
    def __init__(self, account, telegram, profile):
        self.account = account
        self.telegram = telegram
        self.profile = profile
        self.plugins = {plugin.UUID: SimpleNamespace(enabled=True)}
        self.registered_commands = None

    def add_telegram_commands(self, uuid, commands):
        self.registered_commands = commands


SUB_A = MockSubCategory(101, "Аккаунты Steam")
SUB_B = MockSubCategory(202, "Валюта WoW", common=False, cat=MockCategory(55, "World of Warcraft"))
PROFILE = MockProfile([MockLot(SUB_A), MockLot(SUB_A), MockLot(SUB_B)])


def make_crd(with_tg=True, fail_calc=False):
    acc = MockAccount(fail=fail_calc)
    tg = MockTelegram() if with_tg else None
    return MockCardinal(acc, tg, PROFILE), acc, tg


# ═══════════════════════ ТЕСТ 1: метаданные ═══════════════════════
from uuid import UUID as _UUID
check("1.1 метаданные плагина", all(hasattr(plugin, f) for f in
      ["NAME", "VERSION", "DESCRIPTION", "CREDITS", "SETTINGS_PAGE", "UUID", "BIND_TO_DELETE"]))
check("1.2 UUID валиден", str(_UUID(plugin.UUID, version=4)) == plugin.UUID)
check("1.3 автор yungspider, название на английском",
      plugin.CREDITS == "yungspider" and plugin.NAME.isascii())
check("1.4 BIND_TO_PRE_INIT привязан", plugin.BIND_TO_PRE_INIT == [plugin.init])

# ═══════════════════════ ТЕСТ 2: разделы продавца ═══════════════════════
crd, acc, tg = make_crd(with_tg=False)
subs = plugin._seller_subcategories(crd)
check("2.1 собраны уникальные разделы из лотов", set(subs.keys()) == {"common:101", "currency:202"})
check("2.2 обычный раздел: type/id/name", subs["common:101"]["type"] == "common"
      and subs["common:101"]["id"] == 101 and subs["common:101"]["name"] == "Аккаунты Steam")
check("2.3 валютный раздел: game_id из категории", subs["currency:202"]["type"] == "currency"
      and subs["currency:202"]["game_id"] == 55)

crd_empty = MockCardinal(MockAccount(), None, MockProfile([]))
check("2.4 пустой профиль → нет разделов", plugin._seller_subcategories(crd_empty) == {})
crd_none = MockCardinal(MockAccount(), None, None)
check("2.5 profile=None не ломает", plugin._seller_subcategories(crd_none) == {})

# ═══════════════════════ ТЕСТ 3: прямой расчёт ═══════════════════════
crd, acc, tg = make_crd(with_tg=False)
data = plugin.forward_calc(crd, {"type": "common", "id": 101, "game_id": 101, "name": "Steam"}, 1000)
check("3.1 цена сохранена", data["price"] == 1000)
check("3.2 комиссия 2%", abs(data["commission_percent"] - 2.0) < 1e-9)
check("3.3 методы отсортированы по цене (СБП первый)",
      data["methods"][0]["name"] == "СБП" and data["methods"][0]["price"] == 1020.0)
check("3.4 наценка посчитана", abs(data["methods"][0]["surcharge"] - 20.0) < 1e-9
      and abs(data["methods"][1]["surcharge"] - 50.0) < 1e-9)
check("3.5 мин. цена раздела", data["min_price"] == 50.0)
check("3.6 calc вызван с subcategory_id для обычного раздела",
      acc.calc_calls and acc.calc_calls[-1][1] == 101 and acc.calc_calls[-1][2] is None)

data_cur = plugin.forward_calc(crd, {"type": "currency", "id": 202, "game_id": 55, "name": "WoW"}, 500)
check("3.7 calc вызван с game_id для валютного раздела",
      acc.calc_calls[-1][1] is None and acc.calc_calls[-1][2] == 55)

# зажим цены
data_clamp = plugin.forward_calc(crd, {"type": "common", "id": 101, "game_id": 101, "name": "x"}, -5)
check("3.8 отрицательная цена зажимается к минимуму", data_clamp["price"] == plugin.PRICE_RANGE[0])

# ═══════════════════════ ТЕСТ 4: обратный расчёт ═══════════════════════
acc.calc_calls.clear()
rev = plugin.reverse_calc(crd, {"type": "common", "id": 101, "game_id": 101, "name": "Steam"}, 1020)
check("4.1 коэффициент комиссии 1.02", abs(rev["coefficient"] - 1.02) < 1e-9)
check("4.2 цена = итог / коэффициент", abs(rev["price"] - 1000.0) < 0.01)
check("4.3 итог для покупателя ≈ цели",
      abs(rev["forward"]["methods"][0]["price"] - 1020) < 1.0)
check("4.4 обратный расчёт делает 2 вызова calc (опорный + финальный)", len(acc.calc_calls) == 2)

# ═══════════════════════ ТЕСТ 5: форматирование ═══════════════════════
check("5.1 _money с валютой", plugin._money(1234.5, RUB) == "1,234.5 ₽")
check("5.2 _money целое", plugin._money(1000, RUB) == "1000 ₽")
check("5.3 _fmt_num", plugin._fmt_num(1500) == "1500" and plugin._fmt_num(1500.50) == "1,500.5")
check("5.4 _money без валюты", plugin._money(99.99, None) == "99.99")

# ═══════════════════════ ТЕСТ 6: тексты вывода ═══════════════════════
ftext = plugin._forward_text(data)
check("6.1 прямой текст содержит раздел, комиссию и способы",
      "Steam" in ftext and "Комиссия" in ftext and "СБП" in ftext and "Покупатель заплатит" in ftext)
rtext = plugin._reverse_text(rev)
check("6.2 обратный текст содержит цену лота",
      "Поставьте цену лота" in rtext and "1000" in rtext)

# ═══════════════════════ ТЕСТ 7: Telegram UI ═══════════════════════
crd, acc, tg = make_crd(with_tg=True)
plugin.init(crd)
bot = tg.bot
reset_settings()
check("7.1 команда /calc зарегистрирована",
      any(kw.get("commands") == ["calc"] for _, kw in tg.msg_handlers))
check("7.2 add_telegram_commands вызван", crd.registered_commands
      and crd.registered_commands[0][0] == "calc")


def dispatch_cb(data, chat_id=111, msg_id=555, user_id=222):
    c = SimpleNamespace(data=data, id="cb1",
                        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), id=msg_id),
                        from_user=SimpleNamespace(id=user_id))
    for handler, func in tg.cbq_handlers:
        try:
            matched = func(c)
        except Exception:
            matched = False
        if matched:
            handler(c)
            return handler.__name__
    return None


def dispatch_msg(text, chat_id=111, user_id=222):
    m = SimpleNamespace(chat=SimpleNamespace(id=chat_id), from_user=SimpleNamespace(id=user_id),
                        text=text)
    for h, kw in tg.msg_handlers:
        f = kw.get("func")
        try:
            if f and f(m):
                h(m)
                return h.__name__
        except Exception:
            pass
    return None


def last_text():
    if bot.edits:
        return bot.edits[-1][0]
    return bot.sent[-1][1] if bot.sent else ""


name = dispatch_cb(f"47:{plugin.UUID}:0")
check("7.3 страница настроек открывает меню", name == "open_home" and "PriceCalc" in last_text())
check("7.4 без раздела кнопки расчёта неактивны (noop)",
      "Раздел: <i>не выбран</i>" in last_text())

# выбор раздела
name = dispatch_cb(plugin.CB_PICK)
_kb = bot.edits[-1][1] if bot.edits else None
check("7.5 «Выбрать раздел» показывает список", name == "open_picker"
      and "Выберите раздел" in last_text() and "Аккаунты Steam" in kb_buttons(_kb))
name = dispatch_cb(f"{plugin.CB_SUB}:common:101")
check("7.6 выбор раздела сохраняет last_sub", name == "pick_sub"
      and plugin._load_settings()["last_sub"]["id"] == 101)
check("7.7 после выбора раздел показан в меню", "Аккаунты Steam" in last_text())

# прямой расчёт через UI
dispatch_cb(plugin.CB_FWD)
check("7.8 «Цена → итог» ставит состояние ввода",
      tg.user_states.get((111, 222), {}).get("state") == plugin.ST_PRICE)
bot.sent.clear()
dispatch_msg("2000")
check("7.9 расчёт отправлен в чат", any("Расчёт комиссии" in t for _, t in bot.sent)
      and any("2000" in t for _, t in bot.sent))
check("7.10 последняя цена сохранена", plugin._load_settings()["last_price"] == 2000)
dispatch_cb(plugin.CB_FWD)
dispatch_msg("abc")
check("7.11 нечисловая цена отклоняется", "❌" in bot.replies[-1])
dispatch_cb(plugin.CB_FWD)
dispatch_msg("1500,50")
check("7.12 цена с запятой и пробелами распознаётся", plugin._load_settings()["last_price"] == 1500.50)

# обратный расчёт через UI
dispatch_cb(plugin.CB_REV)
check("7.13 «Итог → цена» ставит состояние",
      tg.user_states.get((111, 222), {}).get("state") == plugin.ST_TARGET)
bot.sent.clear()
dispatch_msg("1020")
check("7.14 обратный расчёт отправлен", any("Обратный расчёт" in t for _, t in bot.sent)
      and any("Поставьте цену лота" in t for _, t in bot.sent))

# мин. цена и тест
bot.edits.clear()
dispatch_cb(plugin.CB_MIN)
check("7.15 «Мин. цена» показывает значение", "Минимальная цена" in last_text() and "50" in last_text())
bot.edits.clear()
dispatch_cb(plugin.CB_TEST)
check("7.16 «Тест» делает расчёт по опорной цене", "Расчёт комиссии" in last_text())

# noop без раздела
reset_settings(last_sub=None)
bot.answers.clear()
name = dispatch_cb(plugin.CB_FWD)
check("7.17 расчёт без раздела → подсказка выбрать раздел", name == "act_forward"
      and any("выберите раздел" in (a[1] or "").lower() for a in bot.answers))

# ═══════════════════════ ТЕСТ 8: ошибки calc ═══════════════════════
crd_f, acc_f, tg_f = make_crd(with_tg=True, fail_calc=True)
plugin.init(crd_f)
reset_settings(last_sub={"type": "common", "id": 101, "game_id": 101, "name": "Steam"})
botf = tg_f.bot
# forward_calc должен бросить исключение (обрабатывается в UI)
raised = False
try:
    plugin.forward_calc(crd_f, {"type": "common", "id": 101, "game_id": 101, "name": "x"}, 100)
except Exception:
    raised = True
check("8.1 ошибка calc пробрасывается из forward_calc", raised)


def dispatch_cb_f(data, chat_id=111, msg_id=555, user_id=222):
    c = SimpleNamespace(data=data, id="cb1",
                        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), id=msg_id),
                        from_user=SimpleNamespace(id=user_id))
    for handler, func in tg_f.cbq_handlers:
        try:
            matched = func(c)
        except Exception:
            matched = False
        if matched:
            handler(c)
            return handler.__name__
    return None


botf.edits.clear()
dispatch_cb_f(plugin.CB_TEST)
check("8.2 «Тест» при ошибке calc показывает сообщение, а не падает",
      "Ошибка расчёта" in (botf.edits[-1][0] if botf.edits else ""))

# ═══════════════════════ ТЕСТ 9: нормализация настроек ═══════════════════════
os.makedirs(plugin.PLUGIN_DIR, exist_ok=True)
with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
    json.dump({"last_sub": "broken", "last_price": "abc"}, f)
s = plugin._load_settings()
check("9.1 битые значения нормализуются", s["last_sub"] is None and s["last_price"] == 1000.0)
with open(plugin.SETTINGS_PATH, "w") as f:
    f.write("{broken")
s = plugin._load_settings()
check("9.2 битый JSON → дефолты", s["last_sub"] is None and s["last_price"] == 1000.0)
reset_settings()

print(f"\n{'=' * 60}\nПройдено: {len(PASS)}  Провалено: {len(FAIL)}")
if FAIL:
    print("Проваленные тесты:")
    for f_ in FAIL:
        print("  ✗", f_)
    sys.exit(1)
print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ ✅")
