#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🎭 ТЕАТРАЛЬНАЯ СТУДИЯ - VK БОТ
+ Удаление кнопок И текстового ввода в рамках состояний.
+ Сообщения родителю от админа: копия текста остаётся у админа.
+ После отправки родителю восстанавливается главное меню админа.
+ 🔔 НАПОМИНАНИЯ О ЗАНЯТИЯХ (пятница/суббота вечером).
"""
import os
import logging
import json
import re
import shutil
import threading
import time
import sys
import traceback
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from collections import defaultdict

try:
    import vk_api
    from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
    from vk_api.utils import get_random_id
    VK_AVAILABLE = True
    print("✅ VK API импортирован успешно")
except ImportError:
    print("❌ VK API не установлен. Установите: pip install vk-api")
    sys.exit(1)

# Через сколько секунд удалять СВОЁ служебное уведомление бота
NOTIFY_DELETE_AFTER = 30

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.FileHandler('teatr_bot.log', encoding='utf-8'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

CONFIG = {}
TEATR_DATA = {}
SCHEDULE_DATA = {}
RULES_TEXT = ""
VK_PENDING_ACTIONS = {}
VK_ADMIN_STATE = {}
VK_LAST_STUDENT = {}
METRICS = None
BACKUP_MANAGER = None
VK_BOT_INSTANCE = None
REMINDER_MANAGER = None
user_last_message = defaultdict(float)

STUDY_MONTHS = {
    1: "январь", 2: "февраль", 3: "март", 4: "апрель", 5: "май", 6: "июнь",
    9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь"
}
VACATION_MONTHS = [7, 8]
STUDENT_GROUPS = ["новички", "младшая группа", "театр"]
STUDY_MONTHS_ORDER = ['сентябрь', 'октябрь', 'ноябрь', 'декабрь',
                      'январь', 'февраль', 'март', 'апрель', 'май', 'июнь']

# ==================== НОРМАЛИЗАЦИЯ ГРУПП ====================
def normalize_group_name(text) -> str:
    if not text:
        return ""
    s = str(text).strip().lower()
    s = re.sub(r'[^\w\sа-яё]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    for g in STUDENT_GROUPS:
        if g == s or g in s:
            return g
    if 'младш' in s:
        return 'младшая группа'
    if 'нов' in s:
        return 'новички'
    if 'театр' in s:
        return 'театр'
    return s


def migrate_groups_in_db() -> int:
    changed = 0
    for uid, ud in TEATR_DATA.get("users", {}).items():
        old = ud.get('group', '')
        new = normalize_group_name(old)
        if new and old != new:
            ud['group'] = new
            changed += 1
            logger.info(f"🔧 Группа {ud.get('child_name', uid)}: '{old}' → '{new}'")
    if changed:
        save_teatr_data()
    return changed


# ==================== УТИЛИТЫ ====================
def check_rate_limit(user_id: int, cooldown: int = 2) -> bool:
    now = datetime.now().timestamp()
    if now - user_last_message[user_id] < cooldown:
        return False
    user_last_message[user_id] = now
    return True


def get_month_name(month_num: int) -> str:
    months = {1: "январь", 2: "февраль", 3: "март", 4: "апрель",
              5: "май", 6: "июнь", 7: "июль", 8: "август",
              9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь"}
    return months.get(month_num, "неизвестно")


def get_current_month_name() -> str:
    months = ["январь", "февраль", "март", "апрель", "май", "июнь",
              "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
    return months[datetime.now().month - 1]


def get_next_month_name() -> str:
    current = datetime.now().month
    if current in [6, 7, 8]:
        return "сентябрь"
    if current == 12:
        return "январь"
    nxt = current + 1
    if nxt in [7, 8]:
        return "сентябрь"
    return get_month_name(nxt)


def get_current_study_index() -> int:
    m = datetime.now().month
    if m in VACATION_MONTHS:
        return -1
    if m >= 9:
        return m - 9
    return m + 3


def has_paid_current_month(user_id) -> bool:
    if datetime.now().month in VACATION_MONTHS:
        return True
    return get_current_month_name() in get_paid_months(user_id)


def get_pending_payments_count() -> int:
    count = 0
    for payments in TEATR_DATA.get("payments", {}).values():
        if isinstance(payments, list):
            for p in payments:
                if isinstance(p, dict) and p.get('status') == 'ожидает подтверждения':
                    count += 1
    return count


def get_paid_months(user_id) -> set:
    return set(p['month'] for p in get_user_payments(user_id)
               if p.get('status') == 'подтвержден')


def get_pending_months(user_id) -> set:
    return set(p['month'] for p in get_user_payments(user_id)
               if p.get('status') == 'ожидает подтверждения')


def get_rejected_months(user_id) -> set:
    return set(p['month'] for p in get_user_payments(user_id)
               if p.get('status') == 'отклонен')


def get_latest_payment_for_month(user_id, month: str) -> Optional[Dict]:
    mp = [p for p in get_user_payments(user_id) if p.get('month') == month]
    if not mp:
        return None
    return sorted(mp, key=lambda x: x.get('date', ''), reverse=True)[0]


def get_available_months_for_payment(user_id) -> List[str]:
    paid = get_paid_months(user_id)
    pending = get_pending_months(user_id)
    current_idx = get_current_study_index()
    if current_idx < 0:
        max_idx = 2
    else:
        max_idx = min(current_idx + 3, 9)
    available = []
    for i, month in enumerate(STUDY_MONTHS_ORDER):
        if i > max_idx:
            break
        if month in paid or month in pending:
            continue
        available.append(month)
    return available


# ==================== КОНФИГ ====================
class ConfigManager:
    def __init__(self):
        self.config = {}
        self.load_config()

    def load_config(self):
        try:
            if not os.path.exists('config.json'):
                self.create_default_config()
            with open('config.json', 'r', encoding='utf-8') as f:
                self.config = json.load(f)
            for f in ['vk_group_id', 'vk_token']:
                if f not in self.config:
                    raise ValueError(f"Отсутствует поле: {f}")
            logger.info("Конфигурация загружена")
        except Exception as e:
            logger.error(f"Ошибка конфига: {e}")
            raise

    def create_default_config(self):
        self.config = {
            "vk_group_id": 123456789, "vk_token": "ваш_токен_vk",
            "admin_vk_id": None, "admin_name": "Максим Владимирович",
            "group_link": "https://vk.com/your_group",
            "prices": {"суббота": 1400, "воскресенье": 1400, "оба дня": 2000},
            "backup_days": 7, "rules_file": "rules.txt",
            "schedule_file": "schedule.json", "database_file": "teatr_data.json",
            "backup_enabled": True, "auto_backup_hours": 24,
            # ★ Напоминания о занятиях
            "reminders_enabled": True,
            "reminder_friday_hour": 18,
            "reminder_saturday_hour": 18,
        }
        with open('config.json', 'w', encoding='utf-8') as f:
            json.dump(self.config, f, ensure_ascii=False, indent=2)


# ==================== ДАННЫЕ ====================
def load_teatr_data() -> None:
    global TEATR_DATA
    try:
        db_file = CONFIG.get("database_file", "teatr_data.json")
        if os.path.exists(db_file) and os.path.getsize(db_file) > 0:
            with open(db_file, 'r', encoding='utf-8') as f:
                TEATR_DATA = json.load(f)
            logger.info("Данные загружены")
        else:
            create_default_teatr_data()
        for key in ["users", "payments", "absences"]:
            if key not in TEATR_DATA:
                TEATR_DATA[key] = {}
    except Exception as e:
        logger.error(f"Ошибка загрузки: {e}")
        create_default_teatr_data()


def create_default_teatr_data() -> None:
    global TEATR_DATA
    TEATR_DATA = {"users": {}, "payments": {}, "absences": {}}
    save_teatr_data()


def save_teatr_data() -> None:
    try:
        db_file = CONFIG.get("database_file", "teatr_data.json")
        with open(db_file, 'w', encoding='utf-8') as f:
            json.dump(TEATR_DATA, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}")


def find_user_by_phone(phone: str) -> Optional[Dict]:
    normalized = normalize_phone(phone)
    for uid, ud in TEATR_DATA.get("users", {}).items():
        if ud.get('parent_phone') == normalized:
            return {"user_id": uid, "user_data": ud}
    return None


def is_user_registered(user_id) -> bool:
    return str(user_id) in TEATR_DATA.get("users", {}) or is_admin(user_id)


def register_user(user_id, user_data) -> None:
    uid = str(user_id)
    phone = user_data.get('parent_phone')
    existing = find_user_by_phone(phone)
    if existing:
        old_id = existing['user_id']
        old_data = existing['user_data']
        new_data = old_data.copy()
        new_data.update(user_data)
        new_data['registered_date'] = datetime.now().isoformat()
        new_data['group'] = normalize_group_name(new_data.get('group', 'новички'))
        if old_id in TEATR_DATA["users"]:
            del TEATR_DATA["users"][old_id]
        if old_id in TEATR_DATA["payments"]:
            TEATR_DATA["payments"][uid] = TEATR_DATA["payments"].pop(old_id)
        if old_id in TEATR_DATA["absences"]:
            TEATR_DATA["absences"][uid] = TEATR_DATA["absences"].pop(old_id)
        TEATR_DATA["users"][uid] = new_data
        save_teatr_data()
        return
    TEATR_DATA["users"][uid] = {
        'child_name': user_data['child_name'],
        'child_age': user_data['child_age'],
        'child_school': user_data.get('child_school', ''),
        'parent_name': user_data['parent_name'],
        'parent_phone': phone,
        'group': 'новички',
        'visit_days': user_data.get('visit_days', ''),
        'price': user_data.get('price', 0),
        'registered_date': datetime.now().isoformat()
    }
    TEATR_DATA["payments"].setdefault(uid, [])
    TEATR_DATA["absences"].setdefault(uid, [])
    save_teatr_data()
    logger.info(f"Зарегистрирован: {user_data['child_name']} (ID {uid})")


def get_user_data(user_id) -> Dict:
    if is_admin(user_id):
        return {"is_admin": True, "parent_name": "Администратор"}
    return TEATR_DATA["users"].get(str(user_id), {})


def update_user_group(user_id: str, new_group: str) -> bool:
    uid = str(user_id)
    if uid not in TEATR_DATA["users"]:
        return False
    canonical = normalize_group_name(new_group)
    old = TEATR_DATA["users"][uid].get('group', 'неизвестно')
    TEATR_DATA["users"][uid]['group'] = canonical
    TEATR_DATA["users"][uid]['group_updated'] = datetime.now().isoformat()
    save_teatr_data()
    logger.info(f"Группа {uid}: '{old}' → '{canonical}'")
    return True


def add_payment(user_id, month: str, amount: int, days: str,
                status: str = "ожидает подтверждения") -> str:
    uid = str(user_id)
    pid = f"{uid}_{month}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    payment = {
        "id": pid, "date": datetime.now().isoformat(),
        "month": month, "amount": amount, "days": days,
        "status": status, "timestamp": datetime.now().isoformat()
    }
    TEATR_DATA["payments"].setdefault(uid, [])
    TEATR_DATA["payments"][uid].append(payment)
    save_teatr_data()
    return pid


def update_payment_status(payment_id: str, status: str) -> bool:
    try:
        for _, payments in TEATR_DATA.get("payments", {}).items():
            if isinstance(payments, list):
                for payment in payments:
                    if isinstance(payment, dict) and payment.get("id") == payment_id:
                        payment["status"] = status
                        payment["processed_date"] = datetime.now().isoformat()
                        save_teatr_data()
                        return True
        return False
    except Exception as e:
        logger.error(f"Ошибка обновления платежа: {e}")
        return False


def add_absence(user_id, date: str, reason: str = "") -> None:
    uid = str(user_id)
    TEATR_DATA["absences"].setdefault(uid, [])
    TEATR_DATA["absences"][uid].append({
        "date": datetime.now().isoformat(), "absence_date": date,
        "reason": reason, "recorded": datetime.now().strftime("%d.%m.%Y %H:%M")
    })
    save_teatr_data()


def get_user_payments(user_id) -> List:
    return TEATR_DATA["payments"].get(str(user_id), [])


def get_user_absences(user_id) -> List:
    return TEATR_DATA["absences"].get(str(user_id), [])


def get_marked_absence_dates(user_id) -> set:
    return set(a['absence_date'] for a in get_user_absences(user_id))


def delete_student_data(student_id: str, send_notification: bool = True, vk_bot=None) -> Dict:
    uid = str(student_id)
    sd = TEATR_DATA["users"].get(uid, {})
    if not sd:
        return None
    deleted = sd.copy()
    if send_notification and vk_bot:
        msg = (f"Спасибо, что были с нами!\n"
               "Администратор удалил вашу регистрацию. "
               "Вы больше не будете получать сообщения от бота.\n"
               "При желании можно зарегистрироваться снова. 💫")
        vk_bot.send_message(int(student_id), msg)
    TEATR_DATA["users"].pop(uid, None)
    TEATR_DATA["payments"].pop(uid, None)
    TEATR_DATA["absences"].pop(uid, None)
    save_teatr_data()
    return deleted


def get_users_by_group(group: str) -> List[str]:
    users = TEATR_DATA.get("users", {})
    if group == "all":
        return list(users.keys())
    target = normalize_group_name(group)
    result = []
    for uid, ud in users.items():
        g = normalize_group_name(ud.get('group', ''))
        if g == target:
            result.append(uid)
    logger.info(f"🔍 get_users_by_group('{group}') → '{target}' → {len(result)}")
    return result


def get_users_by_groups() -> Dict[str, List[tuple]]:
    result = {g: [] for g in STUDENT_GROUPS}
    result["другие"] = []
    for uid, ud in TEATR_DATA.get("users", {}).items():
        g = normalize_group_name(ud.get('group', ''))
        if g in result:
            result[g].append((uid, ud))
        else:
            result["другие"].append((uid, ud))
    return result


def is_admin(user_id) -> bool:
    aid = CONFIG.get("admin_vk_id")
    return bool(aid and str(user_id) == str(aid))


def get_user_absences_for_month(user_id, month: str) -> List[str]:
    mr = get_month_date_range(month)
    if not mr:
        return []
    start, end = mr
    result = []
    for a in get_user_absences(user_id):
        try:
            d = datetime.strptime(a.get('absence_date', ''), '%Y-%m-%d')
            if start <= d <= end:
                result.append(a['absence_date'])
        except ValueError:
            continue
    return result


def get_month_date_range(month: str) -> tuple:
    months = {'январь': 1, 'февраль': 2, 'март': 3, 'апрель': 4, 'май': 5, 'июнь': 6,
              'июль': 7, 'август': 8, 'сентябрь': 9, 'октябрь': 10, 'ноябрь': 11, 'декабрь': 12}
    mnum = months.get(month.lower())
    if not mnum:
        return None
    year = datetime.now().year
    if mnum < datetime.now().month:
        year += 1
    start = datetime(year, mnum, 1)
    end = (datetime(year + 1, 1, 1) if mnum == 12
           else datetime(year, mnum + 1, 1)) - timedelta(days=1)
    return start, end


def get_total_classes_for_month(month: str) -> int:
    return 8


def validate_phone_number(phone: str) -> bool:
    c = re.sub(r'[\s\-\(\)+]', '', phone)
    if len(c) == 10 and c.startswith('9'):
        return True
    if len(c) == 11 and (c.startswith('79') or c.startswith('89')):
        return True
    if len(c) == 12 and c.startswith('+79'):
        return True
    return False


def normalize_phone(phone: str) -> str:
    c = re.sub(r'[\s\-\(\)+]', '', phone)
    if len(c) == 10 and c.startswith('9'):
        return '+7' + c
    if len(c) == 11 and c.startswith('89'):
        return '+7' + c[1:]
    if len(c) == 11 and c.startswith('79'):
        return '+' + c
    if len(c) == 12 and c.startswith('+79'):
        return c
    return phone


# ==================== ПЕРЕРАСЧЁТ ====================
class PaymentRecalculation:
    @staticmethod
    def calculate_next_month_payment(user_id, next_month: str) -> Dict[str, Any]:
        ud = get_user_data(user_id)
        if not ud or ud.get('is_admin'):
            return {"amount": 0, "details": "Ученик не найден"}
        vd = ud.get('visit_days', '')
        base = ud.get('price', 0)
        if vd in ['суббота', 'воскресенье']:
            return {"amount": base, "details": f"Один день - {base} руб.", "recalculated": False}
        if vd != 'оба дня':
            return {"amount": base, "details": f"Тариф '{vd}' - {base} руб.", "recalculated": False}
        cm = get_current_month_name()
        if cm in VACATION_MONTHS:
            return {"amount": base, "details": f"Каникулы - {base} руб.", "recalculated": False}
        absences = get_user_absences_for_month(user_id, cm)
        total = get_total_classes_for_month(cm)
        if total == 0:
            return {"amount": base, "details": f"Нет данных - {base} руб.", "recalculated": False}
        cnt = len(absences)
        pct = ((total - cnt) / total) * 100
        if pct <= 50:
            return {"amount": 1400,
                    "details": f"Пропусков в {cm}: {cnt}/{total} ({pct:.1f}%) - перерасчёт до 1400 руб.",
                    "recalculated": True, "absence_count": cnt, "total_classes": total,
                    "attendance_percentage": pct, "base_price": base, "calculation_month": cm}
        return {"amount": base,
                "details": f"Пропусков в {cm}: {cnt}/{total} ({pct:.1f}%) - полная оплата {base} руб.",
                "recalculated": False, "absence_count": cnt, "total_classes": total,
                "attendance_percentage": pct, "base_price": base, "calculation_month": cm}

    @staticmethod
    def get_next_month_payment_forecast(user_id) -> str:
        nxt = get_next_month_name()
        calc = PaymentRecalculation.calculate_next_month_payment(user_id, nxt)
        ud = get_user_data(user_id)
        if not ud or ud.get('is_admin'):
            return "❌ Данные не найдены"
        forecast = (f"📊 ПРОГНОЗ ОПЛАТЫ - {nxt.upper()}\n"
                    f"👶 {ud['child_name']}\n🗓️ {ud['visit_days']}\n"
                    f"💰 Базовая: {ud['price']} руб.\n")
        if calc.get('recalculated'):
            forecast += (f"📈 За {calc['calculation_month']}: "
                         f"{calc['absence_count']}/{calc['total_classes']} "
                         f"({calc['attendance_percentage']:.1f}%)\n"
                         f"💰 Перерасчёт: {calc['base_price']} → {calc['amount']} руб.\n"
                         f"ℹ️ {calc['details']}")
        else:
            forecast += f"💰 Оплата за {nxt}: {calc['amount']} руб.\nℹ️ {calc['details']}"
        return forecast


# ==================== ЗАГРУЗКА ====================
def load_schedule() -> None:
    global SCHEDULE_DATA
    try:
        sf = CONFIG.get("schedule_file", "schedule.json")
        if os.path.exists(sf):
            with open(sf, 'r', encoding='utf-8') as f:
                SCHEDULE_DATA = json.load(f)
            logger.info("Расписание загружено")
        else:
            create_default_schedule()
    except Exception as e:
        logger.error(f"Ошибка расписания: {e}")
        create_default_schedule()


def create_default_schedule() -> None:
    global SCHEDULE_DATA
    SCHEDULE_DATA = {
        "groups": {
            "новички": {"суббота": "10:00-11:30", "воскресенье": "10:00-11:30"},
            "младшая группа": {"суббота": "12:00-13:30", "воскресенье": "12:00-13:30"},
            "театр": {"суббота": "14:00-16:00", "воскресенье": "14:00-16:00"}
        },
        "address": "ул. Театральная, д. 1",
        "contacts": "+7 (XXX) XXX-XX-XX"
    }
    with open(CONFIG.get("schedule_file", "schedule.json"), 'w', encoding='utf-8') as f:
        json.dump(SCHEDULE_DATA, f, ensure_ascii=False, indent=2)


def load_rules() -> None:
    global RULES_TEXT
    try:
        rf = CONFIG.get("rules_file", "rules.txt")
        if os.path.exists(rf):
            with open(rf, 'r', encoding='utf-8') as f:
                RULES_TEXT = f.read().strip()
        else:
            create_default_rules()
    except Exception as e:
        logger.error(f"Ошибка правил: {e}")
        create_default_rules()


def create_default_rules() -> None:
    global RULES_TEXT
    RULES_TEXT = """📋 ПРАВИЛА ТЕАТРАЛЬНОЙ СТУДИИ
1. 🕐 Приходите за 10 минут до начала занятия
2. 📱 Отключайте звук мобильных устройств
3. 👕 Спортивная форма для занятий
4. 💰 Оплата до 10 числа каждого месяца
5. ❌ Предупреждайте о пропусках заранее
6. 🎭 Участвуйте в итоговых показах
⚠️ За нарушение правил - предупреждение!"""
    with open(CONFIG.get("rules_file", "rules.txt"), 'w', encoding='utf-8') as f:
        f.write(RULES_TEXT)


# ==================== ХЕЛПЕРЫ ====================
class BackupManager:
    def __init__(self, config):
        self.config = config
        self.backup_dir = "backups"
        os.makedirs(self.backup_dir, exist_ok=True)

    def create_backup(self):
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            for f in ['teatr_data.json', 'config.json', 'schedule.json', 'rules.txt']:
                if os.path.exists(f):
                    shutil.copy2(f, os.path.join(self.backup_dir, f"{ts}_{f}"))
            return True
        except Exception as e:
            logger.error(f"Бэкап: {e}")
            return False


class MetricsCollector:
    def __init__(self):
        self.metrics = {'users_registered': 0, 'payments_processed': 0,
                        'absences_marked': 0, 'messages_processed': 0}

    def increment(self, m: str):
        if m in self.metrics:
            self.metrics[m] += 1


# ==================== НАПОМИНАНИЯ (занятия + оплата) ====================
PAYMENT_REMINDER_DEFAULT_DAYS = [5, 10, 15, 20]


class ReminderManager:
    """
    Напоминания родителям:
      • о занятиях — в пятницу вечером (про субботу) и в субботу вечером (про воскресенье);
      • утром перед занятием — повтор для тех, кому вечером не дошло;
      • об оплате  — в дни из config['payment_reminder_days'].

    Правила надёжности:
      1. Отметка «отправлено» ставится ТОЛЬКО тому родителю, которому ВК реально
         принял сообщение. Если не ушло — попытка повторяется в тот же день.
      2. Если бот в момент отправки был выключен и включился позже в тот же день,
         напоминание всё равно уйдёт (окно — до часа REMINDER_WINDOW_END_HOUR).
      3. Повторов нет: в reminders_sent.json хранится ключ для каждого получателя.
    """

    RETRY_INTERVAL_SECONDS = 300      # как часто повторять неудачную попытку
    REMINDER_WINDOW_END_HOUR = 22     # позже этого часа напоминания не отправляем

    def __init__(self, vk_bot):
        self.vk_bot = vk_bot
        self.sent_log_file = "reminders_sent.json"
        self.sent_log = self._load_log()
        self.running = False
        self._last_try = {}
        self._warned_not_ready = False
        self.unreachable = set()      # кому ВК запрещает писать (проверяется раз в сутки)

    # ---------- журнал отправок ----------
    def _load_log(self) -> dict:
        try:
            if os.path.exists(self.sent_log_file):
                with open(self.sent_log_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logger.error(f"Ошибка загрузки лога напоминаний: {e}")
        return {}

    def _save_log(self):
        try:
            with open(self.sent_log_file, 'w', encoding='utf-8') as f:
                json.dump(self.sent_log, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения лога напоминаний: {e}")

    def _prune_log(self):
        """Чистим журнал: записи старше 60 дней больше не нужны."""
        cutoff = (datetime.now() - timedelta(days=60)).isoformat()
        self.sent_log = {k: v for k, v in self.sent_log.items()
                         if isinstance(v, str) and v >= cutoff}

    def already_sent(self, key: str) -> bool:
        return key in self.sent_log

    def mark_sent(self, key: str):
        self.sent_log[key] = datetime.now().isoformat()
        self._prune_log()
        self._save_log()

    def _can_try(self, key: str) -> bool:
        """Не даём долбить ВК одной и той же попыткой чаще, чем раз в 5 минут."""
        last = self._last_try.get(key)
        if last and (datetime.now() - last).total_seconds() < self.RETRY_INTERVAL_SECONDS:
            return False
        self._last_try[key] = datetime.now()
        return True

    # ---------- отправка ----------
    def is_api_ready(self) -> bool:
        return getattr(self.vk_bot, 'vk', None) is not None

    def _deliver(self, uid, text) -> bool:
        """True — только если ВК подтвердил отправку (вернул id сообщения)."""
        try:
            result = self.vk_bot.send_message(int(uid), text)
            if result is None:
                logger.warning(f"⚠️ Напоминание не доставлено: {uid}")
                return False
            return True
        except Exception as e:
            logger.error(f"Ошибка отправки напоминания {uid}: {e}")
            return False

    def _notify_admin(self, text):
        admin_id = CONFIG.get("admin_vk_id")
        if not admin_id:
            return
        try:
            self.vk_bot.send_message(int(admin_id), text)
        except Exception as e:
            logger.error(f"Не удалось уведомить админа: {e}")

    def _report_once(self, key: str, text: str):
        """Одно сообщение админу об итогах рассылки (без повторов каждые 5 минут)."""
        if self.already_sent(key):
            return
        self._notify_admin(text)
        self.mark_sent(key)

    # ---------- почему сообщение не дошло ----------
    PERMISSION_HINT = ("\nЧтобы сообщения доходили, родителю нужно открыть переписку с группой "
                       "и нажать «Разрешить сообщения» — достаточно написать боту любое сообщение.")

    @staticmethod
    def child_name(uid) -> str:
        ud = TEATR_DATA.get("users", {}).get(str(uid), {})
        name = str(ud.get('child_name', '') if isinstance(ud, dict) else '').strip()
        return name or f"id {uid}"

    @staticmethod
    def _failed_block(failed_count: int, failed_names: list) -> str:
        """Строка отчёта: кому именно не дошло и что с этим делать."""
        if not failed_count:
            return ""
        text = f"\n⚠️ Не доставлено: {failed_count}"
        if failed_names:
            shown = ', '.join(failed_names[:10])
            if len(failed_names) > 10:
                shown += f" и ещё {len(failed_names) - 10}"
            text += f" — {shown}"
        return text + ReminderManager.PERMISSION_HINT

    def check_reachability(self) -> list:
        """Раз в сутки проверяет, кому ВК вообще запрещает писать, и говорит об этом админу."""
        unreachable = []
        names = []
        for uid, ud in sorted(TEATR_DATA.get("users", {}).items()):
            if not isinstance(ud, dict):
                continue
            try:
                res = self.vk_bot.vk.messages.isMessagesFromGroupAllowed(
                    group_id=self.vk_bot.group_id, user_id=int(uid))
                if not res.get('is_allowed'):
                    unreachable.append(str(uid))
                    names.append(self.child_name(uid))
            except Exception as e:
                logger.warning(f"Не удалось проверить связь с {uid}: {e}")
        self.unreachable = set(unreachable)
        if names:
            logger.warning(f"⚠️ ВК не даёт писать родителям ({len(names)}): {', '.join(names)}")
            self._notify_admin(
                f"⚠️ ВК пока не даёт боту писать этим родителям ({len(names)}): "
                f"{', '.join(names[:10])}"
                + (f" и ещё {len(names) - 10}" if len(names) > 10 else "")
                + self.PERMISSION_HINT)
        else:
            logger.info("✅ Всем родителям ВК разрешает получать сообщения")
        return names

    # ---------- напоминания о занятиях ----------
    def get_reminder_targets(self, day_name: str) -> list:
        """Кто занимается в указанный день ('суббота' / 'воскресенье')."""
        pairs = []
        for uid, ud in TEATR_DATA.get("users", {}).items():
            if not isinstance(ud, dict):
                continue
            vd = str(ud.get('visit_days') or '').strip().lower()
            if day_name == 'суббота' and vd in ('суббота', 'оба дня', 'сб'):
                pairs.append((str(ud.get('child_name', '')), uid))
            elif day_name == 'воскресенье' and vd in ('воскресенье', 'оба дня', 'вс'):
                pairs.append((str(ud.get('child_name', '')), uid))
        return [uid for _, uid in sorted(pairs)]

    def build_class_message(self, uid, day_name) -> str:
        ud = TEATR_DATA.get("users", {}).get(uid, {})
        group = normalize_group_name(ud.get('group', ''))
        group_schedule = SCHEDULE_DATA.get("groups", {}).get(group) or {}
        time_str = group_schedule.get(day_name) or "—"
        return (
            f"🔔 НАПОМИНАНИЕ О ЗАНЯТИИ\n"
            f"👶 {ud.get('child_name', 'Ученик')}\n"
            f"🎭 Группа: {group}\n"
            f"📅 {day_name.capitalize()}: {time_str}\n"
            f"📍 {SCHEDULE_DATA.get('address', '—')}\n\n"
            f"Ждём вас на занятии! 🎭"
        )

    @staticmethod
    def short_date(iso_date: str) -> str:
        """'2026-10-04' -> '04.10' для текстов отчётов."""
        return f"{iso_date[8:10]}.{iso_date[5:7]}" if len(iso_date) >= 10 else iso_date

    @staticmethod
    def class_date(day_name: str) -> str:
        """Дата ближайшего занятия в этот день — по ней хранится отметка об отправке,
        поэтому вечерняя рассылка и утренняя страховка не задваивают напоминания."""
        target = 5 if day_name == 'суббота' else 6
        now = datetime.now()
        return (now + timedelta(days=(target - now.weekday()) % 7)).strftime('%Y-%m-%d')

    def send_class_reminders(self, day_name: str, window: str = 'вечер') -> dict:
        class_date = self.class_date(day_name)
        targets = self.get_reminder_targets(day_name)
        result = {'kind': f'занятие ({day_name})', 'targets': len(targets),
                  'sent': 0, 'failed': 0, 'skipped': 0, 'repeat': False, 'failed_names': []}
        if not targets:
            logger.info(f"ℹ️ Нет учеников для напоминания на {day_name}")
            return result

        pending_retry = False
        for uid in targets:
            key = f"class:{class_date}:{day_name}:{uid}"
            if self.already_sent(key):
                result['skipped'] += 1
                continue
            if str(uid) in self.unreachable:
                result['skipped'] += 1          # ВК всё равно не даст отправить
                continue
            if not self._can_try(key):
                pending_retry = True
                continue
            if self._deliver(uid, self.build_class_message(uid, day_name)):
                self.mark_sent(key)
                result['sent'] += 1
            else:
                result['failed'] += 1
                result['failed_names'].append(self.child_name(uid))
            time.sleep(0.05)
        result['repeat'] = pending_retry

        if result['sent'] or result['failed']:
            logger.info(f"🔔 Напоминание о занятии ({day_name}, {self.short_date(class_date)}, "
                        f"{window}): отправлено {result['sent']}, "
                        f"не доставлено {result['failed']}, уже было {result['skipped']}")

        label = f"{day_name.capitalize()}, {self.short_date(class_date)}"
        day_key = f"report:class:{class_date}:{day_name}:{window}"
        if result['sent'] > 0:
            text = f"🔔 Напоминания о занятии ({label}) отправлены: {result['sent']} чел."
            text += self._failed_block(result['failed'], result['failed_names'])
            if result['repeat']:
                text += "\nПовторю попытку позже."
            self._report_once(day_key, text)
        elif result['failed'] > 0:
            self._report_once(day_key,
                              f"⚠️ Напоминания о занятии ({label}) не ушли: "
                              f"{result['failed']}"
                              + self._failed_block(result['failed'], result['failed_names']))
        return result

    # ---------- напоминания об оплате ----------
    def get_unpaid_targets(self) -> list:
        """Кому нужно напомнить про оплату текущего месяца."""
        cm = get_current_month_name()
        pairs = []
        for uid, ud in TEATR_DATA.get("users", {}).items():
            if not isinstance(ud, dict):
                continue
            payments = TEATR_DATA.get("payments", {}).get(uid) or []
            statuses = [p.get('status') for p in payments
                        if isinstance(p, dict) and p.get('month') == cm]
            if 'подтвержден' in statuses:               # уже оплатил
                continue
            if 'ожидает подтверждения' in statuses:     # отправил, ждёт проверки
                continue
            pairs.append((str(ud.get('child_name', '')), uid))
        return [uid for _, uid in sorted(pairs)]

    def build_payment_message(self, uid) -> str:
        ud = TEATR_DATA.get("users", {}).get(uid, {})
        cm = get_current_month_name()
        return (
            f"💰 НАПОМИНАНИЕ ОБ ОПЛАТЕ\n"
            f"👶 {ud.get('child_name', 'Ученик')}\n"
            f"🎭 Группа: {normalize_group_name(ud.get('group', ''))}\n"
            f"📅 {cm.capitalize()}: {ud.get('price', 0)} руб.\n"
            f"🗓️ Дни: {ud.get('visit_days', '—')}\n\n"
            f"Оплатить можно в личном кабинете — нажмите «💰 Оплатить».\n"
            f"Если вы уже оплатили, дождитесь подтверждения администратора. 🙏"
        )

    def send_payment_reminders(self) -> dict:
        today = datetime.now().strftime('%Y-%m-%d')
        cm = get_current_month_name()
        result = {'kind': f'оплата ({cm})', 'targets': 0,
                  'sent': 0, 'failed': 0, 'skipped': 0, 'repeat': False, 'failed_names': []}
        if datetime.now().month in VACATION_MONTHS:
            logger.info("☀️ Каникулы — напоминания об оплате не отправляются")
            return result

        targets = self.get_unpaid_targets()
        result['targets'] = len(targets)
        pending_retry = False
        for uid in targets:
            key = f"pay:{today}:{cm}:{uid}"
            if self.already_sent(key):
                result['skipped'] += 1
                continue
            if str(uid) in self.unreachable:
                result['skipped'] += 1          # ВК всё равно не даст отправить
                continue
            if not self._can_try(key):
                pending_retry = True
                continue
            if self._deliver(uid, self.build_payment_message(uid)):
                self.mark_sent(key)
                result['sent'] += 1
            else:
                result['failed'] += 1
                result['failed_names'].append(self.child_name(uid))
            time.sleep(0.05)
        result['repeat'] = pending_retry

        if result['sent'] or result['failed']:
            logger.info(f"💰 Напоминание об оплате ({cm}): отправлено {result['sent']}, "
                        f"не доставлено {result['failed']}, уже было {result['skipped']}")

        day_key = f"report:pay:{today}:{cm}"
        if result['sent'] > 0:
            text = f"💰 Напоминания об оплате за {cm} отправлены: {result['sent']} чел."
            text += self._failed_block(result['failed'], result['failed_names'])
            if result['repeat']:
                text += "\nПовторю попытку позже."
            self._report_once(day_key, text)
        elif result['failed'] > 0:
            self._report_once(day_key,
                              f"⚠️ Напоминания об оплате за {cm} не ушли: {result['failed']}"
                              + self._failed_block(result['failed'], result['failed_names']))
        return result

    # ---------- расписание отправки ----------
    def check_and_send(self):
        """Смотрит на день недели, число и время — и отправляет то, что пора."""
        if not self.is_api_ready():
            if not self._warned_not_ready:
                self._warned_not_ready = True
                logger.warning("⏳ Напоминания ждут подключения к VK")
            return
        self._warned_not_ready = False

        now = datetime.now()

        # Раз в сутки проверяем, кому ВК вообще запрещает писать (в любое время суток)
        reach_key = f"reach:{now.strftime('%Y-%m-%d')}"
        if not self.already_sent(reach_key):
            self.check_reachability()
            self.mark_sent(reach_key)

        window_end = int(CONFIG.get("reminder_window_end_hour", self.REMINDER_WINDOW_END_HOUR))
        if now.hour > window_end:
            return                      # поздно: ночью напоминания не шлём
        weekday = now.weekday()         # 0=Пн ... 4=Пт, 5=Сб, 6=Вс

        if CONFIG.get("reminders_enabled", True):
            friday_hour = int(CONFIG.get("reminder_friday_hour", 18))
            saturday_hour = int(CONFIG.get("reminder_saturday_hour", 18))
            morning_hour = int(CONFIG.get("reminder_morning_hour", 8))
            morning_window = morning_hour <= now.hour < morning_hour + 2

            if weekday == 4 and friday_hour <= now.hour <= window_end:
                self.send_class_reminders('суббота', window='вечер')
            elif weekday == 5:
                if saturday_hour <= now.hour <= window_end:
                    self.send_class_reminders('воскресенье', window='вечер')
                if morning_window:
                    # утренняя страховка: тем, кому не дошло вчера вечером
                    self.send_class_reminders('суббота', window='утро')
            elif weekday == 6 and morning_window:
                self.send_class_reminders('воскресенье', window='утро')

        if CONFIG.get("payment_reminder_enabled", True):
            days = CONFIG.get("payment_reminder_days") or PAYMENT_REMINDER_DEFAULT_DAYS
            try:
                days = [int(d) for d in days]
            except (TypeError, ValueError):
                days = list(PAYMENT_REMINDER_DEFAULT_DAYS)
            pay_hour = int(CONFIG.get("payment_reminder_hour", 18))
            if now.day in days and pay_hour <= now.hour <= window_end:
                self.send_payment_reminders()

    def run(self):
        """Фоновый цикл: проверяет расписание раз в минуту."""
        self.running = True
        logger.info("🔔 Менеджер напоминаний запущен (проверка раз в минуту)")
        while self.running:
            try:
                self.check_and_send()
            except Exception as e:
                logger.error(f"Ошибка в цикле напоминаний: {e}")
            time.sleep(60)


# ==================== VK БОТ ====================
class VkBotHandler:
    def __init__(self, group_id, token):
        self.group_id = abs(group_id)
        self.token = token
        self.vk_session = None
        self.vk = None
        self.longpoll = None
        self.running = False
        self.user_last_message = defaultdict(float)
        self.last_keyboard_msg = {}
        self.pending_deletions = []
        self._deletion_lock = threading.Lock()

    def check_rate_limit(self, user_id: int, cooldown: int = 2) -> bool:
        now = datetime.now().timestamp()
        if now - self.user_last_message[user_id] < cooldown:
            return False
        self.user_last_message[user_id] = now
        return True

    def init_api(self):
        try:
            self.vk_session = vk_api.VkApi(token=self.token)
            self.vk = self.vk_session.get_api()
            self.vk.groups.getById(group_id=str(self.group_id))
            self.longpoll = VkBotLongPoll(self.vk_session, self.group_id)
            logger.info("✅ VK LongPoll OK")
            return True
        except Exception as e:
            logger.error(f"❌ VK init: {e}")
            return False

    # ==================== УДАЛЕНИЕ СВОИХ СООБЩЕНИЙ ====================
    def delete_bot_message(self, peer_id, message_id):
        """Удаляет сообщение, отправленное самим ботом.
        ВК разрешает боту удалять только свои сообщения, поэтому сообщения
        родителей не удаляются в принципе — здесь удаляются меню и уведомления бота.
        """
        if not message_id:
            return False
        try:
            peer = int(peer_id)
        except (TypeError, ValueError):
            return False
        try:
            self.vk.messages.delete(peer_id=peer, message_ids=[message_id], delete_for_all=1)
            return True
        except Exception as e:
            logger.debug(f"delete message_ids fail ({message_id}): {e}")
        try:
            self.vk.messages.delete(peer_id=peer, cmids=[message_id], delete_for_all=1)
            return True
        except Exception as e:
            logger.debug(f"delete cmids fail ({message_id}): {e}")
        return False

    def schedule_delete(self, peer_id, message_id, delay_seconds=30):
        if not message_id:
            return
        try:
            peer_int = int(peer_id)
        except (TypeError, ValueError):
            return
        with self._deletion_lock:
            self.pending_deletions.append(
                (time.time() + delay_seconds, peer_int, message_id)
            )
        logger.info(f"⏱️ Запланировано удаление msg {message_id} у {peer_int} через {delay_seconds}с")

    def _deletion_worker(self):
        while self.running:
            time.sleep(3)
            now = time.time()
            ready = []
            with self._deletion_lock:
                still = []
                for item in self.pending_deletions:
                    if item[0] <= now:
                        ready.append(item)
                    else:
                        still.append(item)
                self.pending_deletions = still
            for _, peer, mid in ready:
                self.delete_bot_message(peer, mid)

    def create_keyboard(self, buttons, one_time=False):
        if len(buttons) > 10:
            buttons = buttons[:10]
        kb = {"one_time": one_time, "buttons": []}
        for row in buttons:
            krow = []
            for btn in row:
                if isinstance(btn, dict):
                    label = btn.get('text', '')
                    color = btn.get('color', 'secondary')
                else:
                    label = btn
                    color = "primary"
                label = label[:40]
                krow.append({
                    "action": {"type": "text", "payload": json.dumps({"button": label}),
                               "label": label},
                    "color": color
                })
            kb["buttons"].append(krow)
        return kb

    # ==================== ОТПРАВКА ====================
    def send_message(self, user_id, text, keyboard=None, clear_keyboard=False):
        """Отправка сообщения. Возвращает id сообщения или None, если отправить не удалось
        (None важен: по нему напоминания понимают, что сообщение не дошло)."""
        try:
            if user_id is None:
                return None
            uid_int = int(user_id)
            if not text or not text.strip():
                return None

            # Убираем предыдущее меню бота, чтобы чат не разрастался
            if keyboard is not None or clear_keyboard:
                prev = self.last_keyboard_msg.pop(uid_int, None)
                if prev:
                    self.delete_bot_message(uid_int, prev)

            params = {"peer_id": uid_int, "message": text[:4096],
                      "random_id": get_random_id()}

            if keyboard:
                params["keyboard"] = json.dumps(keyboard, ensure_ascii=False)
            elif clear_keyboard:
                params["keyboard"] = json.dumps(
                    {"buttons": [], "one_time": True}, ensure_ascii=False)

            resp = self.vk.messages.send(**params)
            sent_id = resp if isinstance(resp, int) else None
            if keyboard and sent_id:
                self.last_keyboard_msg[uid_int] = sent_id
            logger.info(f"✅ VK→{user_id}: {text[:50]}...")
            return sent_id
        except vk_api.exceptions.ApiError as e:
            if e.code == 901:
                logger.warning(f"Пользователь {user_id} заблокировал бота "
                               f"или запретил сообщения от группы")
            else:
                logger.error(f"VK API ошибка {getattr(e, 'code', '?')} при отправке {user_id}: {e}")
            return None
        except Exception as e:
            logger.error(f"Send: {e}")
            return None

    def split_long_message(self, text, max_length=4000):
        if len(text) <= max_length:
            return [text]
        parts, cur = [], ""
        for line in text.split('\n'):
            if len(cur) + len(line) + 1 <= max_length:
                cur = cur + "\n" + line if cur else line
            else:
                if cur:
                    parts.append(cur)
                cur = line
        if cur:
            parts.append(cur)
        return parts

    # ==================== DASHBOARD РОДИТЕЛЯ ====================
    def _build_parent_dashboard(self, user_id) -> str:
        ud = get_user_data(user_id)
        if not ud or ud.get('is_admin'):
            return ""
        pname = ud.get('parent_name', 'Неизвестно')
        cname = ud.get('child_name', 'Ученик')
        group = ud.get('group', 'новички')
        vdays = ud.get('visit_days', 'не указаны')

        text = "🎭 ТЕАТРАЛЬНАЯ СТУДИЯ — ЛИЧНЫЙ КАБИНЕТ\n"
        text += "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        text += f"👤 Родитель: {pname}\n"
        text += f"👶 Ребенок: {cname}\n"
        text += f"🎭 Группа: {group}\n"
        text += f"🗓️ Дни: {vdays}\n"

        text += "\n📅 РАСПИСАНИЕ:\n"
        groups = SCHEDULE_DATA.get("groups", {})
        if group in groups:
            for day, t in groups[group].items():
                text += f"   • {day.capitalize()}: {t}\n"
        else:
            text += "   Расписание не найдено\n"

        cm = get_current_month_name()
        if datetime.now().month in VACATION_MONTHS:
            text += "\n☀️ КАНИКУЛЫ (июль-август)\n"
        else:
            abs_ = get_user_absences_for_month(user_id, cm)
            text += f"\n❌ ПРОПУСКИ ЗА {cm.upper()}: {len(abs_)}\n"
            if abs_:
                for a in sorted(abs_):
                    try:
                        d = datetime.strptime(a, '%Y-%m-%d')
                        text += f"   • {d.strftime('%d.%m.%Y')}\n"
                    except Exception:
                        text += f"   • {a}\n"
            else:
                text += "   ✅ Пропусков нет\n"

        text += "\n💳 ОПЛАТЫ:\n"
        paid = get_paid_months(user_id)
        pending = get_pending_months(user_id)
        rejected = get_rejected_months(user_id)
        current_idx = get_current_study_index()
        months_to_show = set()
        for m in paid:
            if m in STUDY_MONTHS_ORDER:
                months_to_show.add(m)
        for i, m in enumerate(STUDY_MONTHS_ORDER):
            if current_idx < 0:
                if m == 'сентябрь':
                    months_to_show.add(m)
                continue
            if i < current_idx and m not in paid:
                months_to_show.add(m)
            if i == current_idx:
                months_to_show.add(m)

        if not months_to_show:
            if current_idx < 0:
                text += "   ☀️ Каникулы — оплата не требуется\n"
            else:
                text += "   ✅ Все оплачено!\n"

        for month in STUDY_MONTHS_ORDER:
            if month not in months_to_show:
                continue
            if month in paid:
                latest = get_latest_payment_for_month(user_id, month)
                amount = latest.get('amount', 0) if latest else 0
                text += f"   ✅ {month.capitalize()} — оплачен ({amount} руб.)\n"
            elif month in pending:
                text += f"   ⏳ {month.capitalize()} — ожидает подтверждения\n"
            elif month in rejected:
                text += f"   ❗ {month.capitalize()} — платёж отклонён, оплатите заново\n"
            else:
                idx = STUDY_MONTHS_ORDER.index(month)
                if current_idx >= 0 and idx < current_idx:
                    text += f"   ❗ {month.capitalize()} — ПРОПУЩЕН\n"
                else:
                    text += f"   ❗ {month.capitalize()} — не оплачен\n"

        if current_idx >= 0:
            if cm not in paid:
                day = datetime.now().day
                if day > 15:
                    text += f"\n🔴 ВНИМАНИЕ! Оплата за {cm} просрочена (до 15 числа)!"
                else:
                    text += f"\n🟢 Оплатите {cm} до 15 числа."
            else:
                text += f"\n✅ Оплата за {cm} произведена. Спасибо!"

        text += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━\nВыберите действие:"
        return text

    def send_menu(self, user_id, show_forecast=True):
        uid = str(user_id)
        if uid in VK_ADMIN_STATE:
            if VK_ADMIN_STATE[uid].get('action') not in ['send_message_to_parent']:
                del VK_ADMIN_STATE[uid]
        try:
            ud = get_user_data(user_id)

            if is_admin(user_id):
                pending_count = get_pending_payments_count()
                confirm_label = "💰 Подтвердить платежи"
                if pending_count > 0:
                    confirm_label = f"💰 Подтвердить платежи ({pending_count})"
                confirm_color = "negative" if pending_count > 0 else "positive"
                kb = self.create_keyboard([
                    [{"text": "📊 Статистика", "color": "primary"},
                     {"text": "📋 Список учеников", "color": "secondary"}],
                    [{"text": "📢 Рассылка", "color": "secondary"},
                     {"text": confirm_label, "color": confirm_color}]
                ])
                text = "👨‍💼 ПАНЕЛЬ АДМИНИСТРАТОРА VK\nВыберите раздел:"
                if pending_count > 0:
                    text += f"\n\n🔴 Неподтверждённых платежей: {pending_count}"
                self.send_message(user_id, text, kb)
                return

            if ud and ud.get('child_name'):
                dashboard = self._build_parent_dashboard(user_id)
                cm = datetime.now().month
                if cm in VACATION_MONTHS:
                    pc = "secondary"
                elif has_paid_current_month(user_id):
                    pc = "positive"
                elif datetime.now().day > 15:
                    pc = "negative"
                else:
                    pc = "positive"
                kb = self.create_keyboard([
                    [{"text": "💰 Оплатить", "color": pc},
                     {"text": "❌ Отметить пропуск", "color": "secondary"}],
                    [{"text": "📋 Правила", "color": "primary"},
                     {"text": "📞 Связь с админом", "color": "secondary"}]
                ])
                self.send_message(user_id, dashboard, kb)
                return

            kb = self.create_keyboard([
                [{"text": "📋 Правила и регистрация", "color": "primary"}],
                [{"text": "📞 Связь с админом", "color": "secondary"}]
            ])
            self.send_message(user_id,
                              "🎭 Добро пожаловать в студию Фигаро!\n",
                              kb)
        except Exception as e:
            logger.error(f"Ошибка меню: {e}")
            self.send_message(user_id, "❌ Ошибка загрузки меню")

    # ==================== ПРАВИЛА / РЕГИСТРАЦИЯ ====================
    def show_rules_with_buttons(self, user_id):
        if is_user_registered(user_id):
            self.send_message(user_id, RULES_TEXT, None)
            self.send_menu(user_id, show_forecast=False)
            return
        text = RULES_TEXT + "\n📋 ВНИМАНИЕ! оплачивая месяц вы соглашаетесь с правилами посещения и оплаты студии."
        kb = self.create_keyboard([
            [{"text": "✅ Согласен, начинаем регистрацию", "color": "positive"}],
            [{"text": "❌ Не согласен", "color": "negative"}]
        ])
        self.send_message(user_id, text, kb)

    def start_vk_registration(self, user_id):
        VK_PENDING_ACTIONS[str(user_id)] = {
            'action': 'registration', 'step': 'child_name',
            'data': {}, 'timestamp': datetime.now()
        }
        self.send_message(user_id,
                          "🎭 ДОБРО ПОЖАЛОВАТЬ!\nНачинаем регистрацию.\n👶 Введите имя и фамилию ребенка:",
                          clear_keyboard=True)

    def handle_vk_registration_step_sync(self, user_id, text):
        uid = str(user_id)
        if uid not in VK_PENDING_ACTIONS:
            return False
        step = VK_PENDING_ACTIONS[uid]['step']
        data = VK_PENDING_ACTIONS[uid]['data']

        if step == 'child_name':
            if len(text) < 2:
                self.send_message(user_id, "❌ Минимум 2 символа:",
                                  clear_keyboard=True)
                return True
            data['child_name'] = text
            VK_PENDING_ACTIONS[uid]['step'] = 'child_age'
            self.send_message(user_id, "📅 Возраст ребенка? (6-13 лет):",
                              clear_keyboard=True)
            return True
        elif step == 'child_age':
            try:
                age = int(text)
                if not 6 <= age <= 13:
                    raise ValueError
                data['child_age'] = age
                VK_PENDING_ACTIONS[uid]['step'] = 'child_school'
                self.send_message(user_id, "🏫 номер школы?:",
                                  clear_keyboard=True)
                return True
            except ValueError:
                self.send_message(user_id, "❌ Число 6-13:", clear_keyboard=True)
                return True
        elif step == 'child_school':
            if not text.strip():
                self.send_message(user_id, "❌ Не может быть пустым:",
                                  clear_keyboard=True)
                return True
            data['child_school'] = text
            VK_PENDING_ACTIONS[uid]['step'] = 'parent_name'
            self.send_message(user_id, "👨‍👩‍👧 ФИО родителя:",
                              clear_keyboard=True)
            return True
        elif step == 'parent_name':
            if len(text) < 2:
                self.send_message(user_id, "❌ Минимум 2 символа:",
                                  clear_keyboard=True)
                return True
            data['parent_name'] = text
            VK_PENDING_ACTIONS[uid]['step'] = 'parent_phone'
            self.send_message(user_id, "📞 Телефон родителя (пример: 912 345 67 89):",
                              clear_keyboard=True)
            return True
        elif step == 'parent_phone':
            if not validate_phone_number(text):
                self.send_message(user_id, "❌ Неверный формат. Пример: 912 345 67 89",
                                  clear_keyboard=True)
                return True
            data['parent_phone'] = normalize_phone(text)
            kb = self.create_keyboard([
                [{"text": "Суббота (1400 руб.)", "color": "primary"},
                 {"text": "Воскресенье (1400 руб.)", "color": "primary"}],
                [{"text": "Оба дня (2000 руб.)", "color": "positive"},
                 {"text": "❌ Отмена", "color": "negative"}]
            ])
            self.send_message(user_id,
                              "📅 Выберите дни:\n• Суббота: 1400\n• Воскресенье: 1400\n• Оба дня: 2000",
                              kb)
            VK_PENDING_ACTIONS[uid]['step'] = 'visit_days'
            return True
        elif step == 'visit_days':
            if text == "❌ Отмена":
                del VK_PENDING_ACTIONS[uid]
                self.send_message(user_id, "❌ Отменено")
                self.send_menu(user_id, show_forecast=True)
                return True
            if "Суббота" in text:
                vd, price = "суббота", 1400
            elif "Воскресенье" in text:
                vd, price = "воскресенье", 1400
            elif "Оба дня" in text:
                vd, price = "оба дня", 2000
            else:
                self.send_message(user_id, "❌ Выберите из предложенных:")
                return True
            temp = {"child_name": data['child_name'], "child_age": data['child_age'],
                    "child_school": data['child_school'], "parent_name": data['parent_name'],
                    "parent_phone": data['parent_phone'], "visit_days": vd, "price": price}
            register_user(user_id, temp)
            if METRICS:
                METRICS.increment('users_registered')
            self.send_message(user_id,
                              f"🎉 РЕГИСТРАЦИЯ ЗАВЕРШЕНА!\n👶 {data['child_name']}\n"
                              f"🎭 Группа: новички\n🗓️ {vd}\n💰 {price} руб./мес.")
            self.send_menu(user_id, show_forecast=True)
            admin_id = CONFIG.get("admin_vk_id")
            if admin_id:
                self.send_message(admin_id,
                                  f"🔔 НОВАЯ РЕГИСТРАЦИЯ (VK)\n👤 {data['parent_name']}\n"
                                  f"👶 {data['child_name']}\n📞 {data['parent_phone']}\n"
                                  f"🎭 {vd} | {price} руб.")
            del VK_PENDING_ACTIONS[uid]
            return True
        return False

    # ==================== СПИСОК УЧЕНИКОВ ====================
    def show_student_selection_menu_vk(self, user_id, page=0):
        uid = str(user_id)
        VK_ADMIN_STATE.pop(uid, None)
        by_group = get_users_by_groups()
        total = sum(len(s) for s in by_group.values())
        if total == 0:
            self.send_message(user_id, "❌ Нет зарегистрированных учеников.")
            self.send_menu(user_id, show_forecast=False)
            return
        msg = "👥 СПИСОК УЧЕНИКОВ (ответьте номером)\n"
        students = []
        n = 1
        for g in STUDENT_GROUPS:
            studs = by_group.get(g, [])
            if studs:
                icon = "👶" if g == "новички" else ("⭐" if g == "младшая группа" else "🎪")
                msg += f"\n{icon} {g.upper()} ({len(studs)}):\n"
                for sid, sd in sorted(studs, key=lambda x: x[1]['child_name']):
                    msg += f"{n}. {sd['child_name']} - {sd['parent_name']}\n"
                    students.append({'id': sid, 'name': sd['child_name'], 'number': n})
                    n += 1
        other = by_group.get("другие", [])
        if other:
            msg += "\n❓ ДРУГИЕ ГРУППЫ:\n"
            for sid, sd in sorted(other, key=lambda x: x[1]['child_name']):
                msg += f"{n}. {sd['child_name']} ({sd.get('group', '?')}) - {sd['parent_name']}\n"
                students.append({'id': sid, 'name': sd['child_name'], 'number': n})
                n += 1
        VK_ADMIN_STATE[uid] = {'action': 'student_selection', 'students': students, 'page': page}
        msg += f"\n📝 Всего: {total}\nВведите номер ученика (1-{total})."
        kb = self.create_keyboard([[{"text": "🔙 Назад в меню", "color": "secondary"}]])
        self.send_message(user_id, msg, kb)

    def show_student_info_vk(self, user_id, sid, sd):
        uid = str(user_id)
        payments = get_user_payments(sid)
        confirmed = [p for p in payments if p.get('status') == 'подтвержден']
        absences = get_user_absences(sid)
        by_month = {p['month']: p for p in confirmed}
        msg = (f"📋 ИНФОРМАЦИЯ ОБ УЧЕНИКЕ\n"
               f"👶 {sd['child_name']}\n"
               f"📅 Возраст: {sd['child_age']} лет\n"
               f"🏫 Школа/сад: {sd.get('child_school', 'Не указано')}\n"
               f"👤 Родитель: {sd['parent_name']}\n"
               f"📞 Телефон: {sd['parent_phone']}\n"
               f"🎭 Группа: {sd['group']}\n"
               f"🗓️ Дни: {sd['visit_days']}\n"
               f"💰 Тариф: {sd['price']} руб./мес.\n"
               f"💳 ОПЛАТЫ:\n")
        for month in STUDY_MONTHS_ORDER:
            if month in by_month:
                msg += f"   ✅ {month.capitalize()} — оплачен\n"
            else:
                msg += f"   ❗ {month.capitalize()} — не оплачен\n"
        msg += "\n❌ ПРОПУСКИ:\n"
        if absences:
            for a in sorted(absences, key=lambda x: x['absence_date'], reverse=True)[:10]:
                try:
                    d = datetime.strptime(a['absence_date'], '%Y-%m-%d')
                    msg += f"   📅 {d.strftime('%d.%m.%Y')}\n"
                except Exception:
                    msg += f"   📅 {a['absence_date']}\n"
        else:
            msg += "   ✅ Пропусков нет\n"
        msg += f"\n📊 Всего оплачено: {len(confirmed)} мес. | Пропусков: {len(absences)}"
        kb = self.create_keyboard([
            [{"text": "🔄 Изменить группу", "color": "primary"}],
            [{"text": "✉️ Написать родителям", "color": "primary"}],
            [{"text": "🗑️ Удалить ученика", "color": "negative"}],
            [{"text": "🔙 К списку учеников", "color": "secondary"}],
            [{"text": "🏠 В главное меню", "color": "secondary"}]
        ])
        VK_LAST_STUDENT[uid] = {
            'student_id': sid, 'student_name': sd['child_name'],
            'parent_phone': sd['parent_phone'], 'timestamp': datetime.now().isoformat()
        }
        VK_ADMIN_STATE[uid] = {
            'action': 'student_info_selected', 'student_id': sid,
            'student_name': sd['child_name'], 'parent_phone': sd['parent_phone']
        }
        self.send_message(user_id, msg, kb)
        logger.info(f"📌 VK_LAST_STUDENT[{uid}] сохранён для {sd['child_name']}")

    def change_group_start_vk(self, user_id, sid, sname):
        uid = str(user_id)
        VK_ADMIN_STATE.pop(uid, None)
        VK_ADMIN_STATE[uid] = {'action': 'change_group_select_group',
                               'student_id': sid, 'student_name': sname}
        kb = self.create_keyboard([
            [{"text": "👶 Новички", "color": "primary"}],
            [{"text": "⭐ Младшая группа", "color": "primary"}],
            [{"text": "🎪 Театр", "color": "primary"}],
            [{"text": "❌ Отмена", "color": "negative"}]
        ])
        self.send_message(user_id, f"🔄 ИЗМЕНЕНИЕ ГРУППЫ\n👶 {sname}\nВыберите новую группу:",
                          kb)

    def change_group_execute_vk(self, user_id, new_group_text):
        uid = str(user_id)
        state = VK_ADMIN_STATE.get(uid, {})
        sid = state.get('student_id')
        if not sid:
            self.send_message(user_id, "❌ Ученик не выбран")
            self.send_menu(user_id, show_forecast=False)
            return
        if new_group_text == "❌ Отмена":
            self.send_message(user_id, "❌ Отменено")
            VK_ADMIN_STATE.pop(uid, None)
            self.send_menu(user_id, show_forecast=False)
            return
        sd = TEATR_DATA["users"].get(sid)
        if not sd:
            self.send_message(user_id, "❌ Ученик не найден")
            self.send_menu(user_id, show_forecast=False)
            return
        canonical = normalize_group_name(new_group_text)
        old = sd.get('group', 'неизвестно')
        if old == canonical:
            self.send_message(user_id, f"❌ Ученик уже в группе '{canonical}'")
            self.send_menu(user_id, show_forecast=False)
            return
        if update_user_group(sid, canonical):
            notif = (f"🎭 ИЗМЕНЕНИЕ ГРУППЫ\nУважаемый(ая) {sd['parent_name']}!\n"
                     f"Ваш ребенок {sd['child_name']} переведен:\n"
                     f"📊 {old} → {canonical}\n"
                     f"📅 Новое расписание — в личном кабинете ниже.\n"
                     f"⏱️ Это сообщение будет удалено автоматически.")
            notif_id = self.send_message(int(sid), notif)
            if notif_id:
                self.schedule_delete(
                    int(sid), notif_id,
                    delay_seconds=NOTIFY_DELETE_AFTER
                )
            self.send_menu(int(sid), show_forecast=False)
            self.send_message(user_id,
                              f"✅ Группа изменена!\n👶 {sd['child_name']}\n🔄 {old} → {canonical}")
            logger.info(f"Группа {sd['child_name']}: {old} → {canonical}")
        else:
            self.send_message(user_id, "❌ Ошибка")
        VK_ADMIN_STATE.pop(uid, None)
        self.send_menu(user_id, show_forecast=False)

    def send_message_to_parent_vk(self, admin_id, sid, sname, pphone, text):
        full = f"📨 СООБЩЕНИЕ ОТ АДМИНИСТРАТОРА\n👶 Ученик: {sname}\n\n💬 {text}"
        self.send_message(int(sid), full)
        # ★ Подтверждение админу с копией текста
        self.send_message(admin_id,
                          f"✅ Сообщение для {sname} отправлено!\n\n"
                          f"📝 Текст сообщения:\n{text}")

    def delete_student_confirm_vk(self, user_id, sid, sname):
        sd = TEATR_DATA["users"].get(sid)
        if not sd:
            self.send_message(user_id, "❌ Ученик не найден")
            return
        VK_ADMIN_STATE[str(user_id)] = {'action': 'delete_student_confirm',
                                        'student_id': sid, 'student_name': sname}
        msg = (f"⚠️ ПОДТВЕРЖДЕНИЕ УДАЛЕНИЯ\n"
               f"👶 {sd['child_name']}\n👤 {sd['parent_name']}\n"
               f"🎭 {sd.get('group', '?')}\n📞 {sd.get('parent_phone', '?')}\n"
               f"❌ ВСЕ ДАННЫЕ БУДУТ УДАЛЕНЫ")
        kb = self.create_keyboard([
            [{"text": "✅ ДА, УДАЛИТЬ", "color": "negative"}],
            [{"text": "❌ НЕТ, ОТМЕНА", "color": "positive"}]
        ])
        self.send_message(user_id, msg, kb)

    def delete_student_execute_vk(self, user_id, confirm):
        uid = str(user_id)
        if confirm != "✅ ДА, УДАЛИТЬ":
            self.send_message(user_id, "❌ Удаление отменено")
            VK_ADMIN_STATE.pop(uid, None)
            self.send_menu(user_id, show_forecast=False)
            return
        state = VK_ADMIN_STATE.get(uid, {})
        sid = state.get('student_id')
        if not sid:
            self.send_message(user_id, "❌ Ученик не выбран")
            return
        deleted = delete_student_data(sid, send_notification=True, vk_bot=self)
        if not deleted:
            self.send_message(user_id, "❌ Ошибка")
            return
        self.send_message(user_id, f"✅ Ученик {deleted['child_name']} удалён")
        VK_ADMIN_STATE.pop(uid, None)
        VK_LAST_STUDENT.pop(uid, None)
        self.send_menu(user_id, show_forecast=False)

    # ==================== СТАТИСТИКА ====================
    def show_admin_stats_vk(self, user_id):
        total = len(TEATR_DATA.get("users", {}))
        groups = {}
        for ud in TEATR_DATA.get("users", {}).values():
            g = normalize_group_name(ud.get('group', 'неизвестно')) or 'неизвестно'
            groups[g] = groups.get(g, 0) + 1
        cm = get_current_month_name()
        paid = sum(1 for payments in TEATR_DATA.get("payments", {}).values()
                   for p in payments if p.get('month') == cm and p.get('status') == 'подтвержден')
        msg = f"📊 СТАТИСТИКА\n👥 Учеников: {total}\n💰 {cm}: {paid}/{total}\n🎭 ГРУППЫ:\n"
        for g, c in groups.items():
            msg += f"• {g}: {c}\n"
        self.send_message(user_id, msg)

    def show_admin_finance_vk(self, user_id):
        cm = get_current_month_name()
        income = 0
        paid_count = 0
        unpaid = []
        for uid, ud in TEATR_DATA.get("users", {}).items():
            is_paid = any(p.get('month') == cm and p.get('status') == 'подтвержден'
                          for p in TEATR_DATA.get("payments", {}).get(uid, []))
            if is_paid:
                for p in TEATR_DATA.get("payments", {}).get(uid, []):
                    if p.get('month') == cm and p.get('status') == 'подтвержден':
                        income += p.get('amount', 0)
                        break
                paid_count += 1
            else:
                unpaid.append((uid, ud))
        total = len(TEATR_DATA.get("users", {}))
        msg = (f"💰 ФИНАНСЫ ({cm})\n• Получено: {income} руб.\n"
               f"• Оплатили: {paid_count}/{total}\n")
        if unpaid:
            msg += f"❌ НЕ ОПЛАТИЛИ ({len(unpaid)}):\n"
            for i, (_, ud) in enumerate(sorted(unpaid, key=lambda x: x[1]['child_name']), 1):
                msg += f"{i}. {ud['child_name']} ({ud['parent_name']})\n"
        self.send_message(user_id, msg)

    def show_unpaid_users_vk(self, user_id):
        cm = get_current_month_name()
        unpaid = []
        for uid, ud in TEATR_DATA.get("users", {}).items():
            is_paid = any(p.get('month') == cm and p.get('status') == 'подтвержден'
                          for p in TEATR_DATA.get("payments", {}).get(uid, []))
            if not is_paid:
                unpaid.append((uid, ud))
        msg = f"❌ НЕОПЛАТИВШИЕ {cm.upper()}\n"
        if unpaid:
            msg += f"Всего: {len(unpaid)} учеников\n"
            for i, (_, ud) in enumerate(sorted(unpaid, key=lambda x: x[1]['child_name']), 1):
                msg += f"{i}. {ud['child_name']} ({ud['parent_name']})\n📞 {ud['parent_phone']}\n"
        else:
            msg += "✅ Все оплатили!"
        self.send_message(user_id, msg)

    def show_admin_absences_vk(self, user_id):
        cm = get_current_month_name()
        msg = f"❌ ПРОПУСКИ ЗА {cm.upper()}:\n"
        has = False
        for uid, ud in TEATR_DATA.get("users", {}).items():
            abs_ = get_user_absences_for_month(uid, cm)
            if abs_:
                has = True
                msg += f"👶 {ud['child_name']} ({ud['group']}):\n"
                for a in sorted(abs_):
                    try:
                        d = datetime.strptime(a, "%Y-%m-%d")
                        msg += f"   📅 {d.strftime('%d.%m.%Y')}\n"
                    except Exception:
                        msg += f"   📅 {a}\n"
                msg += "\n"
        if not has:
            msg += "✅ Пропусков нет"
        self.send_message(user_id, msg)

    def show_admin_stats_menu_vk(self, user_id):
        VK_ADMIN_STATE.pop(str(user_id), None)
        kb = self.create_keyboard([
            [{"text": "📈 Общая статистика", "color": "primary"},
             {"text": "💰 Финансы", "color": "secondary"}],
            [{"text": "❌ Пропуски", "color": "secondary"},
             {"text": "❌ Неоплатившие", "color": "primary"}],
            [{"text": "📤 Экспорт данных", "color": "primary"},
             {"text": "💾 Создать бэкап", "color": "secondary"}],
            [{"text": "🔙 Назад в меню", "color": "secondary"}]
        ])
        self.send_message(user_id, "📊 РАЗДЕЛ СТАТИСТИКИ\nВыберите действие:",
                          kb)

    # ==================== РАССЫЛКА ====================
    def show_broadcast_menu_vk(self, user_id):
        VK_ADMIN_STATE.pop(str(user_id), None)
        by_group = get_users_by_groups()
        n_all = len(TEATR_DATA.get("users", {}))
        n_nov = len(by_group.get("новички", []))
        n_ml = len(by_group.get("младшая группа", []))
        n_t = len(by_group.get("театр", []))
        kb = self.create_keyboard([
            [{"text": f"🎭 Всем ({n_all})", "color": "primary"}],
            [{"text": f"👶 Новички ({n_nov})", "color": "primary"},
             {"text": f"⭐ Младшая ({n_ml})", "color": "secondary"}],
            [{"text": f"🎪 Театр ({n_t})", "color": "secondary"}],
            [{"text": "🔙 Назад в меню", "color": "secondary"}]
        ])
        self.send_message(user_id, "📢 РАССЫЛКА\nВыберите группу:",
                          kb)

    def handle_broadcast_vk(self, user_id, text):
        if "Всем" in text:
            group, name = "all", "всем"
        elif "Новички" in text:
            group, name = "новички", "новичкам"
        elif "Младшая" in text:
            group, name = "младшая группа", "младшей группе"
        elif "Театр" in text:
            group, name = "театр", "театру"
        else:
            self.send_message(user_id, "❌ Неизвестная группа")
            return

        users = get_users_by_group(group)
        logger.info(f"🔍 Рассылка: '{text}' → '{group}' → {len(users)}")

        if not users:
            self.send_message(user_id, f"❌ В группе '{name}' нет учеников.")
            self.send_menu(user_id, show_forecast=False)
            return

        uid = str(user_id)
        VK_ADMIN_STATE.pop(uid, None)
        VK_ADMIN_STATE[uid] = {
            'action': 'broadcast_waiting', 'target_group': group,
            'target_name': name, 'users_to_send': users
        }
        self.send_message(user_id,
                          f"📝 Введите текст для рассылки ({name}, учеников: {len(users)}):",
                          clear_keyboard=True)

    def execute_broadcast_vk(self, user_id, state):
        uid = str(user_id)
        name = state.get('target_name')
        users = state.get('users_to_send', [])
        text = state.get('message_text', '')
        VK_ADMIN_STATE.pop(uid, None)
        self.send_message(user_id, f"📤 Начинаю рассылку '{name}' ({len(users)} уч.)...")
        sent, failed = 0, 0
        for u in users:
            try:
                self.send_message(int(u), f"📢 СООБЩЕНИЕ ОТ АДМИНИСТРАТОРА\n{text}")
                sent += 1
            except Exception as e:
                logger.error(f"Ошибка отправки {u}: {e}")
                failed += 1
            time.sleep(0.1)
        # ★ 1) Отчёт
        self.send_message(user_id,
                          f"✅ Рассылка '{name}' завершена!\n"
                          f"📤 Отправлено: {sent}\n❌ Не доставлено: {failed}\n"
                          f"📊 Всего: {len(users)}")
        # ★ 2) Копия текста — остаётся в чате админа как память о рассылке
        self.send_message(user_id,
                          f"📝 Текст рассылки '{name}' (копия для истории):\n\n{text}")
        self.send_menu(user_id, show_forecast=False)

    # ==================== ПЛАТЕЖИ ====================
    def show_pending_payments_vk(self, user_id):
        uid = str(user_id)
        VK_ADMIN_STATE.pop(uid, None)
        pending = []
        for uid_pay, payments in TEATR_DATA.get("payments", {}).items():
            for p in payments:
                if p.get('status') == 'ожидает подтверждения':
                    ud = get_user_data(uid_pay)
                    pending.append({
                        'user_id': uid_pay,
                        'user_name': ud.get('child_name', 'Неизвестно'),
                        'month': p['month'],
                        'amount': p['amount'],
                        'payment_id': p['id']
                    })
        if not pending:
            self.send_message(user_id, "✅ Нет платежей, ожидающих подтверждения.")
            self.send_menu(user_id, show_forecast=False)
            return
        msg = "💰 ПЛАТЕЖИ, ОЖИДАЮЩИЕ ПОДТВЕРЖДЕНИЯ:\n"
        buttons = []
        for i, p in enumerate(pending, 1):
            msg += f"{i}. {p['user_name']} — {p['month']} — {p['amount']} руб.\n"
            buttons.append([
                {"text": f"✅ Подтвердить #{i}", "color": "positive"},
                {"text": f"❌ Отклонить #{i}", "color": "negative"}
            ])
        msg += "\nНажмите кнопку или введите номер. Для отмены: отмена"
        VK_ADMIN_STATE[uid] = {'action': 'pending_payments', 'payments': pending}
        kb = self.create_keyboard(buttons + [[{"text": "❌ Отмена", "color": "negative"}]])
        self.send_message(user_id, msg, kb)

    def handle_payment_action_vk(self, user_id, text):
        uid = str(user_id)
        state = VK_ADMIN_STATE.get(uid, {})
        payments = state.get('payments', [])
        VK_ADMIN_STATE.pop(uid, None)
        if not payments:
            self.send_message(user_id, "❌ Сессия устарела")
            self.send_menu(user_id, show_forecast=False)
            return
        if text.lower() in ("отмена", "❌ отмена"):
            self.send_message(user_id, "❌ Отменено")
            self.send_menu(user_id, show_forecast=False)
            return
        is_reject = False
        number = None
        t = text.strip()
        m = re.match(r'^✅\s*Подтвердить\s*#?\s*(\d+)', t)
        if m:
            number = int(m.group(1))
        else:
            m = re.match(r'^❌\s*Отклонить\s*#?\s*(\d+)', t)
            if m:
                is_reject = True
                number = int(m.group(1))
            else:
                try:
                    number = int(t)
                except ValueError:
                    number = None
        if number is None or not (1 <= number <= len(payments)):
            self.send_message(user_id, f"❌ Введите номер 1-{len(payments)} или 'отмена'")
            return
        sel = payments[number - 1]
        pid = sel['payment_id']
        if is_reject:
            if update_payment_status(pid, "отклонен"):
                self.send_message(user_id,
                                  f"❌ Платёж {sel['user_name']} ({sel['month']}) отклонён")
                self.send_message(int(sel['user_id']),
                                  f"❌ Администратор не подтвердил ваш платеж за {sel['month']}.\n"
                                  f"Пожалуйста, свяжитесь с администратором или оплатите снова.")
        else:
            if update_payment_status(pid, "подтвержден"):
                self.send_message(user_id,
                                  f"✅ Платёж {sel['user_name']} ({sel['month']}) подтверждён!")
                self.send_message(int(sel['user_id']),
                                  f"✅ Ваш платеж за {sel['month']} подтвержден администратором!\n"
                                  f"💰 Сумма: {sel['amount']} руб.\nСпасибо за оплату!")
        self.send_menu(user_id, show_forecast=False)

    def create_backup_vk(self, user_id):
        if BACKUP_MANAGER is None:
            self.send_message(user_id, "❌ Бэкапы не инициализированы")
            return
        self.send_message(user_id, "💾 Создание бэкапа...")
        if BACKUP_MANAGER.create_backup():
            self.send_message(user_id, "✅ Бэкап создан")
        else:
            self.send_message(user_id, "❌ Ошибка")

    # ==================== ЭКСПОРТ ====================
    def show_admin_export_vk(self, user_id):
        VK_ADMIN_STATE.pop(str(user_id), None)
        VK_ADMIN_STATE[str(user_id)] = {'action': 'export_menu'}
        self.send_message(user_id,
                          "📋 ЭКСПОРТ\n1️⃣ Ученики\n2️⃣ Платежи\n3️⃣ Пропуски\nВведите 1-3:",
                          clear_keyboard=True)

    def handle_export_vk(self, user_id, choice):
        try:
            c = int(choice)
            if c == 1:
                rep = self._report_users()
            elif c == 2:
                rep = self._report_payments()
            elif c == 3:
                rep = self._report_absences()
            else:
                self.send_message(user_id, "❌ 1-3")
                return
            for chunk in self.split_long_message(rep):
                self.send_message(user_id, chunk)
        except ValueError:
            self.send_message(user_id, "❌ Число 1-3")
        VK_ADMIN_STATE.pop(str(user_id), None)
        self.send_menu(user_id, show_forecast=False)

    def _report_users(self) -> str:
        all_u = TEATR_DATA.get("users", {})
        if not all_u:
            return "❌ Нет учеников"
        rep = f"📊 ОТЧЁТ ПО УЧЕНИКАМ\n{datetime.now().strftime('%d.%m.%Y %H:%M')}\n" + "=" * 40 + "\n"
        for g in STUDENT_GROUPS:
            studs = [(uid, ud) for uid, ud in all_u.items()
                     if normalize_group_name(ud.get('group', '')) == g]
            if studs:
                rep += f"\n🎭 {g.upper()} ({len(studs)}):\n" + "-" * 30 + "\n"
                for i, (_, ud) in enumerate(sorted(studs, key=lambda x: x[1]['child_name']), 1):
                    rep += (f"{i}. {ud['child_name']}\n   👤 {ud['parent_name']}\n"
                            f"   📞 {ud['parent_phone']}\n   🗓️ {ud['visit_days']}\n"
                            f"   💰 {ud['price']} руб.\n")
        rep += "=" * 40 + f"\n📊 Всего: {len(all_u)}\n"
        return rep

    def _report_payments(self) -> str:
        all_u = TEATR_DATA.get("users", {})
        if not all_u:
            return "❌ Нет данных"
        rep = f"💰 ОТЧЁТ ПО ПЛАТЕЖАМ\n{datetime.now().strftime('%d.%m.%Y %H:%M')}\n" + "=" * 40 + "\n"
        for uid, ud in sorted(all_u.items(), key=lambda x: x[1]['child_name']):
            confirmed = [p for p in get_user_payments(uid) if p.get('status') == 'подтвержден']
            rep += f"\n👶 {ud['child_name']} ({ud['parent_name']})\n"
            if confirmed:
                for p in sorted(confirmed, key=lambda x: x['month']):
                    rep += f"   ✅ {p['month']} — {p['amount']} руб.\n"
            else:
                rep += "   ❌ Нет оплат\n"
        return rep

    def _report_absences(self) -> str:
        all_u = TEATR_DATA.get("users", {})
        if not all_u:
            return "❌ Нет данных"
        rep = f"❌ ОТЧЁТ ПО ПРОПУСКАМ\n{datetime.now().strftime('%d.%m.%Y %H:%M')}\n" + "=" * 40 + "\n"
        for uid, ud in sorted(all_u.items(), key=lambda x: x[1]['child_name']):
            abs_ = get_user_absences(uid)
            if abs_:
                rep += f"\n👶 {ud['child_name']}\n"
                for a in sorted(abs_, key=lambda x: x['absence_date'], reverse=True)[:20]:
                    try:
                        d = datetime.strptime(a['absence_date'], '%Y-%m-%d')
                        rep += f"   • {d.strftime('%d.%m.%Y')}\n"
                    except Exception:
                        rep += f"   • {a['absence_date']}\n"
        return rep

    # ==================== ОПЛАТА У РОДИТЕЛЯ ====================
    def show_payment_vk(self, user_id):
        if not is_user_registered(user_id):
            self.send_message(user_id, "❌ Требуется регистрация")
            return
        ud = get_user_data(user_id)
        available = get_available_months_for_payment(user_id)
        msg = (f"💰 ОПЛАТА\n👶 {ud['child_name']}\n🎭 {ud['group']}\n"
               f"🗓️ {ud['visit_days']}\n💵 Тариф: {ud['price']} руб./мес.\n\n")
        if not available:
            if datetime.now().month in VACATION_MONTHS:
                msg += "☀️ Каникулы — оплата не требуется."
            else:
                msg += "✅ Все доступные месяцы оплачены."
            self.send_message(user_id, msg)
            self.send_menu(user_id, show_forecast=False)
            return
        msg += "Выберите месяц для оплаты:"
        buttons = []
        for month in available:
            amount = ud['price']
            calc_note = ""
            if month == get_next_month_name():
                calc = PaymentRecalculation.calculate_next_month_payment(user_id, month)
                amount = calc['amount']
                if calc.get('recalculated'):
                    calc_note = " 🔄"
            buttons.append([{
                "text": f"💳 {month.capitalize()} - {amount} руб.{calc_note}",
                "color": "primary"
            }])
        buttons.append([{"text": "📋 История платежей", "color": "secondary"}])
        buttons.append([{"text": "🔙 Назад в меню", "color": "secondary"}])
        kb = self.create_keyboard(buttons)
        self.send_message(user_id, msg, kb)

    def handle_payment_vk(self, user_id, month):
        ud = get_user_data(user_id)
        if not ud or ud.get('is_admin'):
            self.send_message(user_id, "❌ Данные не найдены")
            return
        if month in VACATION_MONTHS:
            self.send_message(user_id, "❌ В июле и августе оплата не производится.")
            return
        if month in get_paid_months(user_id):
            self.send_message(user_id, f"❌ {month} уже оплачен!")
            return
        if month in get_pending_months(user_id):
            self.send_message(user_id, f"⏳ Платёж за {month} уже ожидает подтверждения.")
            return
        if month == get_next_month_name():
            calc = PaymentRecalculation.calculate_next_month_payment(user_id, month)
            amount = calc['amount']
            recalc = calc.get('recalculated', False)
            details = calc.get('details', '')
        else:
            amount = ud['price']
            recalc = False
            details = ''
        pid = add_payment(user_id, month, amount, ud['visit_days'], "ожидает подтверждения")
        if METRICS:
            METRICS.increment('payments_processed')
        confirm_msg = f"✅ Платёж отправлен администратору!\n💵 {amount} руб. | {month}\n"
        if recalc:
            confirm_msg += f"📊 {details}\n"
        confirm_msg += f"👶 {ud['child_name']}\n⏳ Ожидайте подтверждения"
        self.send_message(user_id, confirm_msg)
        self.send_menu(user_id, show_forecast=False)
        admin_id = CONFIG.get("admin_vk_id")
        if admin_id:
            pending_count = get_pending_payments_count()
            confirm_label = f"💰 Подтвердить платежи ({pending_count})"
            admin_kb = self.create_keyboard([
                [{"text": "📊 Статистика", "color": "primary"},
                 {"text": "📋 Список учеников", "color": "secondary"}],
                [{"text": "📢 Рассылка", "color": "secondary"},
                 {"text": confirm_label, "color": "negative"}]
            ])
            admin_text = (f"💰 НОВЫЙ ПЛАТЕЖ (ожидает подтверждения)\n"
                          f"👤 {ud['parent_name']}\n👶 {ud['child_name']}\n"
                          f"💵 {amount} руб. | {month}\n🗓️ {ud['visit_days']}\n")
            if recalc:
                admin_text += f"🔄 {details}\n"
            admin_text += "⏳ Нажмите кнопку ниже"
            self.send_message(admin_id, admin_text, admin_kb)

    def show_payment_history_vk(self, user_id):
        payments = get_user_payments(user_id)
        confirmed = [p for p in payments if p.get('status') == 'подтвержден']
        if not confirmed:
            self.send_message(user_id, "📋 История платежей пуста.")
            self.send_menu(user_id, show_forecast=False)
            return
        ud = get_user_data(user_id)
        msg = f"📋 ИСТОРИЯ ПЛАТЕЖЕЙ — {ud.get('child_name', 'Ученик')}\n"
        for i, p in enumerate(sorted(confirmed, key=lambda x: x['date'], reverse=True)[:15], 1):
            d = datetime.fromisoformat(p['date']).strftime('%d.%m.%Y')
            rc = " 🔄" if p.get('recalculated') else ""
            msg += f"{i}. {p['month']} — {p['amount']} руб.{rc}\n   {d} | {p['days']}\n"
        self.send_message(user_id, msg)
        self.send_menu(user_id, show_forecast=False)

    # ==================== ПРОПУСКИ / РАСПИСАНИЕ ====================
    def show_absence_vk(self, user_id):
        if not is_user_registered(user_id):
            self.send_message(user_id, "❌ Требуется регистрация")
            return
        if datetime.now().month in VACATION_MONTHS:
            self.send_message(user_id, "☀️ Каникулы — занятий нет.")
            self.send_menu(user_id, show_forecast=False)
            return
        today = datetime.now()
        dates = []
        for i in range(14):
            d = today + timedelta(days=i)
            if d.weekday() in [5, 6]:
                dates.append(d)
        marked = get_marked_absence_dates(user_id)
        buttons = []
        for d in dates[:6]:
            ds = d.strftime('%Y-%m-%d')
            if ds not in marked:
                buttons.append([{"text": d.strftime("%d.%m (%a)"), "color": "primary"}])
        if not buttons:
            self.send_message(user_id, "✅ Все занятия отмечены!")
            self.send_menu(user_id, show_forecast=False)
            return
        buttons.append([{"text": "📋 История пропусков", "color": "secondary"}])
        buttons.append([{"text": "🔙 Назад в меню", "color": "secondary"}])
        kb = self.create_keyboard(buttons)
        self.send_message(user_id, "❌ ОТМЕТИТЬ ПРОПУСК\nВыберите дату:",
                          kb)

    def handle_absence_vk(self, user_id, text):
        try:
            parts = text.split(' ')[0].split('.')
            if len(parts) != 2:
                raise ValueError
            day, month = int(parts[0]), int(parts[1])
            year = datetime.now().year
            if month == 1 and datetime.now().month == 12:
                year += 1
            absence_date = f"{year}-{month:02d}-{day:02d}"
        except Exception:
            self.send_message(user_id, "❌ Неверная дата")
            return
        if absence_date in get_marked_absence_dates(user_id):
            self.send_message(user_id, "❌ Пропуск уже отмечен!")
            return
        ud = get_user_data(user_id)
        d = datetime.strptime(absence_date, "%Y-%m-%d")
        add_absence(user_id, absence_date, "отправлено")
        if METRICS:
            METRICS.increment('absences_marked')
        self.send_message(user_id,
                          f"✅ Пропуск зафиксирован!\n👶 {ud['child_name']}\n📅 {d.strftime('%d.%m.%Y')}\n"
                          f"Спасибо за предупреждение!")
        self.send_menu(user_id, show_forecast=False)
        admin_id = CONFIG.get("admin_vk_id")
        if admin_id:
            self.send_message(admin_id,
                              f"🔔 НОВЫЙ ПРОПУСК\n👤 {ud['parent_name']}\n👶 {ud['child_name']}\n"
                              f"📞 {ud['parent_phone']}\n📅 {d.strftime('%d.%m.%Y')}")

    def show_absence_history_vk(self, user_id):
        abs_ = get_user_absences(user_id)
        if not abs_:
            self.send_message(user_id, "📋 История пропусков пуста.")
            self.send_menu(user_id, show_forecast=False)
            return
        ud = get_user_data(user_id)
        msg = f"📋 ПРОПУСКИ — {ud.get('child_name', 'Ученик')}\n"
        for i, a in enumerate(sorted(abs_, key=lambda x: x['absence_date'], reverse=True)[:15], 1):
            try:
                d = datetime.strptime(a['absence_date'], '%Y-%m-%d')
                msg += f"{i}. {d.strftime('%d.%m.%Y')}\n"
            except Exception:
                msg += f"{i}. {a['absence_date']}\n"
        self.send_message(user_id, msg)
        self.send_menu(user_id, show_forecast=False)

    def show_schedule_vk(self, user_id):
        if not is_user_registered(user_id):
            self.send_message(user_id, "❌ Требуется регистрация")
            return
        ud = get_user_data(user_id)
        g = ud.get('group', 'новички')
        msg = f"📅 РАСПИСАНИЕ — {g.upper()}\n"
        groups = SCHEDULE_DATA.get("groups", {})
        if g in groups:
            for day, t in groups[g].items():
                msg += f"   • {day.capitalize()}: {t}\n"
        else:
            msg += "   Расписание не найдено\n"
        msg += f"\n📍 {SCHEDULE_DATA.get('address', '—')}\n📞 {SCHEDULE_DATA.get('contacts', '—')}"
        self.send_message(user_id, msg)
        self.send_menu(user_id, show_forecast=False)

    def show_forecast_vk(self, user_id):
        if not is_user_registered(user_id):
            self.send_message(user_id, "❌ Требуется регистрация")
            return
        forecast = PaymentRecalculation.get_next_month_payment_forecast(user_id)
        self.send_message(user_id, forecast)
        self.send_menu(user_id, show_forecast=False)

    def contact_admin_vk(self, user_id, msg_text=None):
        ud = get_user_data(user_id)
        if ud and not ud.get('is_admin'):
            info = (f"👤 {ud.get('parent_name', '?')}\n👶 {ud.get('child_name', '?')}\n"
                    f"🎭 {ud.get('group', '?')}\n🆔 VK: {user_id}")
        else:
            info = f"🆔 Незарегистрированный VK: {user_id}"
        if msg_text:
            full = f"📩 СООБЩЕНИЕ ОТ КЛИЕНТА\n{info}\n💬 {msg_text}"
        else:
            full = f"📞 ЗАПРОС СВЯЗИ\n{info}"
        admin_id = CONFIG.get("admin_vk_id")
        if admin_id:
            self.send_message(admin_id, full)
        self.send_message(user_id, "✅ Сообщение отправлено администратору.")
        self.send_menu(user_id, show_forecast=False)

    # ==================== FALLBACK ====================
    def _handle_student_button_fallback(self, user_id, text):
        uid = str(user_id)
        info = VK_LAST_STUDENT.get(uid)
        if not info:
            return False
        sid = info['student_id']
        sname = info['student_name']
        pphone = info.get('parent_phone', '')

        if text == "🔄 Изменить группу":
            logger.info(f"🔄 FALLBACK: Изменить группу для {sname}")
            self.change_group_start_vk(user_id, sid, sname)
            return True
        if text == "✉️ Написать родителям":
            logger.info(f"✉️ FALLBACK: Написать родителям {sname}")
            VK_ADMIN_STATE[uid] = {
                'action': 'send_message_to_parent',
                'student_id': sid, 'student_name': sname, 'parent_phone': pphone
            }
            self.send_message(user_id,
                              f"✉️ Введите сообщение для родителей {sname}:",
                              clear_keyboard=True)
            return True
        if text == "🗑️ Удалить ученика":
            logger.info(f"🗑️ FALLBACK: Удалить ученика {sname}")
            self.delete_student_confirm_vk(user_id, sid, sname)
            return True
        if text == "🔙 К списку учеников":
            self.show_student_selection_menu_vk(user_id)
            return True
        if text == "🏠 В главное меню":
            self.send_menu(user_id, show_forecast=False)
            return True
        return False

    # ==================== СОСТОЯНИЯ АДМИНА ====================
    def handle_admin_state(self, user_id, text):
        uid = str(user_id)
        state = VK_ADMIN_STATE.get(uid, {})
        action = state.get('action')
        logger.info(f"🔍 handle_admin_state: uid={uid}, action={action}, text='{text[:40]}'")

        if text == "🔙 К списку учеников":
            VK_ADMIN_STATE.pop(uid, None)
            self.show_student_selection_menu_vk(user_id)
            return True
        if text == "🏠 В главное меню":
            VK_ADMIN_STATE.pop(uid, None)
            self.send_menu(user_id, show_forecast=False)
            return True
        if text == "❌ Отмена":
            VK_ADMIN_STATE.pop(uid, None)
            self.send_menu(user_id, show_forecast=False)
            return True

        if action is None:
            if self._handle_student_button_fallback(user_id, text):
                return True

        if action == 'export_menu':
            self.handle_export_vk(user_id, text)
            return True
        if action == 'broadcast_waiting':
            state['message_text'] = text
            self.execute_broadcast_vk(user_id, state)
            return True
        if action == 'student_selection':
            try:
                n = int(text)
                students = state.get('students', [])
                for s in students:
                    if s.get('number') == n:
                        sd = TEATR_DATA["users"].get(s['id'])
                        if sd:
                            self.show_student_info_vk(user_id, s['id'], sd)
                        else:
                            self.send_message(user_id, "❌ Ученик не найден")
                        return True
                self.send_message(user_id, f"❌ Введите 1-{len(students)}")
            except ValueError:
                if not self._handle_student_button_fallback(user_id, text):
                    self.send_message(user_id, "❌ Введите номер ученика")
            return True
        if action == 'student_info_selected':
            if text == "🔄 Изменить группу":
                self.change_group_start_vk(user_id, state['student_id'], state['student_name'])
            elif text == "✉️ Написать родителям":
                VK_ADMIN_STATE[uid] = {
                    'action': 'send_message_to_parent',
                    'student_id': state['student_id'],
                    'student_name': state['student_name'],
                    'parent_phone': state.get('parent_phone', '')
                }
                self.send_message(user_id,
                                  f"✉️ Введите сообщение для родителей {state['student_name']}:",
                                  clear_keyboard=True)
            elif text == "🗑️ Удалить ученика":
                self.delete_student_confirm_vk(user_id, state['student_id'], state['student_name'])
            else:
                self.send_message(user_id, "❌ Используйте кнопки")
            return True
        if action == 'send_message_to_parent':
            self.send_message_to_parent_vk(user_id, state['student_id'], state['student_name'],
                                           state.get('parent_phone', ''), text)
            VK_ADMIN_STATE.pop(uid, None)
            # ★ Восстанавливаем главное меню админа
            self.send_menu(user_id, show_forecast=False)
            return True
        if action == 'change_group_select_group':
            if text in ["👶 Новички", "⭐ Младшая группа", "🎪 Театр"]:
                self.change_group_execute_vk(user_id, text)
            else:
                VK_ADMIN_STATE.pop(uid, None)
                self.send_message(user_id, "❌ Используйте кнопки")
                self.send_menu(user_id, show_forecast=False)
            return True
        if action == 'delete_student_confirm':
            self.delete_student_execute_vk(user_id, text)
            return True
        if action == 'pending_payments':
            self.handle_payment_action_vk(user_id, text)
            return True
        return False

    # ==================== ГЛАВНЫЙ ОБРАБОТЧИК ====================
    def handle_message(self, event):
        user_id = None
        try:
            if not self.check_rate_limit(event.obj.message['from_id']):
                return
            raw = event.raw
            mobj = raw.get('object', {}).get('message', {})
            if not mobj or mobj.get('out'):
                return
            user_id = mobj.get('from_id') or mobj.get('peer_id')
            if not user_id or user_id == self.group_id:
                return

            text = mobj.get('text', '').strip()
            if not text:
                return
            logger.info(f"📩 VK от {user_id}: {text[:80]}")
            uid = str(user_id)
            is_vk_admin = is_admin(user_id)

            if uid in VK_PENDING_ACTIONS and VK_PENDING_ACTIONS[uid].get('action') == 'registration':
                self.handle_vk_registration_step_sync(user_id, text)
                return
            if uid in VK_PENDING_ACTIONS and VK_PENDING_ACTIONS[uid].get('action') == 'waiting_for_admin_message':
                # Сообщение родителя администратору
                self.contact_admin_vk(user_id, text)
                VK_PENDING_ACTIONS.pop(uid, None)
                return
            if is_vk_admin and self.handle_admin_state(user_id, text):
                return

            if is_vk_admin:
                if text == "📊 Статистика":
                    self.show_admin_stats_menu_vk(user_id)
                elif text == "📋 Список учеников":
                    self.show_student_selection_menu_vk(user_id)
                elif text == "📢 Рассылка":
                    self.show_broadcast_menu_vk(user_id)
                elif text.startswith("💰 Подтвердить платежи"):
                    self.show_pending_payments_vk(user_id)
                elif text == "📈 Общая статистика":
                    self.show_admin_stats_vk(user_id)
                elif text == "💰 Финансы":
                    self.show_admin_finance_vk(user_id)
                elif text == "❌ Пропуски":
                    self.show_admin_absences_vk(user_id)
                elif text == "❌ Неоплатившие":
                    self.show_unpaid_users_vk(user_id)
                elif text == "📤 Экспорт данных":
                    self.show_admin_export_vk(user_id)
                elif text == "💾 Создать бэкап":
                    self.create_backup_vk(user_id)
                elif text == "🔙 Назад в меню":
                    self.send_menu(user_id, show_forecast=False)
                elif text.startswith("🎭 Всем"):
                    self.handle_broadcast_vk(user_id, text)
                elif text.startswith("👶 Новички"):
                    self.handle_broadcast_vk(user_id, text)
                elif text.startswith("⭐ Младшая"):
                    self.handle_broadcast_vk(user_id, text)
                elif text.startswith("🎪 Театр"):
                    self.handle_broadcast_vk(user_id, text)
                else:
                    if self._handle_student_button_fallback(user_id, text):
                        return
                    self.send_message(user_id, "❌ Неизвестная команда")
                return

            # Родитель
            if text == "✅ Согласен, начинаем регистрацию" and not is_user_registered(user_id):
                self.start_vk_registration(user_id)
                return
            if text == "❌ Не согласен":
                self.send_message(user_id, "❌ Отменено")
                return
            if text == "📋 Правила и регистрация":
                self.show_rules_with_buttons(user_id)
                return
            if not is_user_registered(user_id) and not is_vk_admin:
                self.show_rules_with_buttons(user_id)
                return
            if text == "🔙 Назад в меню":
                self.send_menu(user_id, show_forecast=False)
                VK_PENDING_ACTIONS.pop(uid, None)
                VK_ADMIN_STATE.pop(uid, None)
                return
            if text in ["💰 Оплатить", "💰 Оплата"]:
                self.show_payment_vk(user_id)
            elif text in ["❌ Отметить пропуск", "❌ Пропуск"]:
                self.show_absence_vk(user_id)
            elif text == "📅 Расписание":
                self.show_schedule_vk(user_id)
            elif text == "📊 Прогноз оплаты":
                self.show_forecast_vk(user_id)
            elif text in ["📞 Связь с админом", "📞 Связь"]:
                VK_PENDING_ACTIONS[uid] = {'action': 'waiting_for_admin_message',
                                           'timestamp': datetime.now()}
                self.send_message(user_id, "💬 Введите сообщение администратору:",
                                  clear_keyboard=True)
            elif text == "📋 Правила":
                self.send_message(user_id, RULES_TEXT, None)
                self.send_menu(user_id, show_forecast=False)
            elif text == "📋 История платежей":
                self.show_payment_history_vk(user_id)
            elif text == "📋 История пропусков":
                self.show_absence_history_vk(user_id)
            elif text.startswith("💳") and "руб." in text:
                parts = text.replace("💳", "").split('-')
                if parts:
                    month_raw = parts[0].strip().replace("🔄", "").strip().lower()
                    month = re.sub(r'[^а-яё]', '', month_raw)
                    if month in STUDY_MONTHS_ORDER:
                        self.handle_payment_vk(user_id, month)
                    else:
                        logger.warning(f"❌ Неизвестный месяц: '{text}' → '{month}'")
                        self.send_message(user_id, "❌ Неверный месяц")
                else:
                    self.send_message(user_id, "❌ Неверный формат")
            elif "(" in text and ")" in text and len(text) <= 15:
                self.handle_absence_vk(user_id, text)
            else:
                self.send_menu(user_id, show_forecast=False)
        except Exception as e:
            logger.error(f"❌ Ошибка обработки: {e}")
            if user_id:
                try:
                    self.send_message(user_id, "❌ Ошибка обработки")
                except Exception:
                    pass
            traceback.print_exc()

    def run(self):
        if self.vk is None and not self.init_api():
            logger.error("❌ VK бот не запущен")
            return
        logger.info("🟣 VK бот запущен...")
        self.running = True
        threading.Thread(target=self._deletion_worker, daemon=True).start()
        while self.running:
            try:
                for event in self.longpoll.listen():
                    if event.type == VkBotEventType.MESSAGE_NEW:
                        self.handle_message(event)
            except KeyboardInterrupt:
                self.running = False
                break
            except Exception as e:
                logger.error(f"❌ LongPoll: {e}")
                if self.running:
                    time.sleep(5)
                    self.init_api()


# ==================== ОЧИСТКА ====================
def cleanup_pending():
    now = datetime.now()
    for uid in list(VK_PENDING_ACTIONS.keys()):
        act = VK_PENDING_ACTIONS[uid]
        if 'timestamp' in act and (now - act['timestamp']).total_seconds() > 3600:
            VK_PENDING_ACTIONS.pop(uid, None)


# ==================== ИНИЦИАЛИЗАЦИЯ ====================
def initialize_system():
    global CONFIG, METRICS, BACKUP_MANAGER
    CONFIG = ConfigManager().config
    load_teatr_data()
    load_schedule()
    load_rules()
    BACKUP_MANAGER = BackupManager(CONFIG)
    METRICS = MetricsCollector()

    logger.info("=" * 50)
    logger.info("📊 ГРУППЫ УЧЕНИКОВ В БАЗЕ:")
    for uid, ud in TEATR_DATA.get("users", {}).items():
        logger.info(f"   uid={uid}, child={ud.get('child_name')}, group='{ud.get('group')}'")
    logger.info("=" * 50)

    changed = migrate_groups_in_db()
    if changed:
        logger.info(f"🔧 Мигрировано групп: {changed}")

    if CONFIG.get('reminders_enabled', True):
        logger.info(f"🔔 Напоминания о занятиях: пт {CONFIG.get('reminder_friday_hour', 18)}:00 "
                    f"(о субботе), сб {CONFIG.get('reminder_saturday_hour', 18)}:00 (о воскресенье), "
                    f"утренний повтор в {CONFIG.get('reminder_morning_hour', 8)}:00")
    if CONFIG.get('payment_reminder_enabled', True):
        logger.info(f"💰 Напоминания об оплате: дни "
                    f"{CONFIG.get('payment_reminder_days', PAYMENT_REMINDER_DEFAULT_DAYS)} "
                    f"в {CONFIG.get('payment_reminder_hour', 18)}:00")

    if CONFIG.get('backup_enabled', True):
        BACKUP_MANAGER.create_backup()
    logger.info(f"✅ Система инициализирована. Учеников: {len(TEATR_DATA.get('users', {}))}")


def main():
    try:
        initialize_system()
        vk_group_id = CONFIG.get("vk_group_id")
        vk_token = CONFIG.get("vk_token")
        if not vk_group_id or not vk_token or vk_token == "ваш_токен_vk":
            logger.error("❌ Проверьте vk_group_id и vk_token в config.json")
            return
        global VK_BOT_INSTANCE, REMINDER_MANAGER
        VK_BOT_INSTANCE = VkBotHandler(vk_group_id, vk_token)

        # ★ Сначала подключаемся к VK: иначе напоминания «сгорят», не дойдя до родителей
        if not VK_BOT_INSTANCE.init_api():
            logger.error("❌ Не удалось подключиться к VK — напоминания не запущены")
            return

        # ★ Напоминания (о занятиях и об оплате) — только после подключения
        class_on = CONFIG.get("reminders_enabled", True)
        pay_on = CONFIG.get("payment_reminder_enabled", True)
        if class_on or pay_on:
            REMINDER_MANAGER = ReminderManager(VK_BOT_INSTANCE)
            threading.Thread(target=REMINDER_MANAGER.run, daemon=True).start()
            logger.info(f"🔔 Напоминания запущены: о занятиях={class_on}, об оплате={pay_on}")
        else:
            logger.info("🔔 Напоминания отключены в config.json")

        def loop():
            while True:
                time.sleep(3600)
                cleanup_pending()

        threading.Thread(target=loop, daemon=True).start()
        VK_BOT_INSTANCE.run()
    except KeyboardInterrupt:
        logger.info("Остановлено")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    main()