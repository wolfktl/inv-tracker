"""
Учёт техники — backend (FastAPI + PostgreSQL).
Авторизация по сессионной cookie, REST API, отдача статического UI.
"""
import os, io, re, json, base64, hashlib, hmac, secrets, datetime, pathlib
from typing import Optional, List

import barcode
from barcode.writer import SVGWriter
import pyotp

RU_MONTHS = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]

def _cost_num(s):
    d = re.sub(r"[^0-9]", "", str(s or ""))
    return int(d) if d else 0

def _date_parts(s):
    """'dd.mm.yyyy' -> ('dd','<месяц>','yyyy'); иначе — текущая дата."""
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", str(s or "").strip())
    if m:
        d, mo, y = m.groups()
        return d, RU_MONTHS[int(mo)], y
    t = datetime.date.today()
    return "%02d" % t.day, RU_MONTHS[t.month], str(t.year)

from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel
from sqlalchemy import (create_engine, String, Integer, Text, ForeignKey, DateTime, JSON, func, select)
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column, sessionmaker, Session)
from sqlalchemy.dialects.postgresql import JSONB

# JSONB на PostgreSQL, обычный JSON на остальных СУБД (для тестов/SQLite)
JSONType = JSON().with_variant(JSONB(), "postgresql")

# ---------------------------------------------------------------- config
APP_VERSION = "1.9.0"

# Разделы, доступ к которым можно выдавать обычным пользователям.
GRANTABLE_SECTIONS = ["equip", "people", "acts", "cart", "sup", "tsd"]
BASE = pathlib.Path(__file__).parent
DB_URL = os.environ.get("DATABASE_URL", "postgresql+psycopg2://inv:inv@db:5432/inventory")
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-please-" + secrets.token_hex(8))
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
# Загружать демонстрационные данные (seed_data.json) в пустую базу.
# По умолчанию ВЫКЛЮЧЕНО — боевая база стартует пустой. Для демо: SEED_DEMO=1.
SEED_DEMO = os.environ.get("SEED_DEMO", "").strip().lower() in ("1", "true", "yes", "on")
STATUSES = {"stock", "issued", "repair", "writeoff"}
STATUS_RU = {"stock": "На складе", "issued": "Выдано", "repair": "В ремонте", "writeoff": "Списано"}

_connect_args = {"check_same_thread": False} if DB_URL.startswith("sqlite") else {}
engine = create_engine(DB_URL, pool_pre_ping=True, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

# ---------------------------------------------------------------- models
class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(128), default="")
    role: Mapped[str] = mapped_column(String(32), default="admin")
    totp_secret: Mapped[str] = mapped_column(String(64), default="")
    totp_enabled: Mapped[int] = mapped_column(Integer, default=0)
    recovery_codes: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    perms: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)   # разделы для role=user

class Employee(Base):
    __tablename__ = "employees"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200), default="")      # собирается из частей
    last_name: Mapped[str] = mapped_column(String(80), default="")       # фамилия
    first_name: Mapped[str] = mapped_column(String(80), default="")      # имя
    middle_name: Mapped[str] = mapped_column(String(80), default="")     # отчество
    login: Mapped[str] = mapped_column(String(64), default="")
    email: Mapped[str] = mapped_column(String(128), default="")
    dept: Mapped[str] = mapped_column(String(128), default="")
    position: Mapped[str] = mapped_column(String(128), default="")
    location: Mapped[str] = mapped_column(String(128), default="")
    passport: Mapped[str] = mapped_column(String(64), default="")          # серия и номер
    passport_issued: Mapped[str] = mapped_column(String(255), default="")  # кем и когда выдан
    passport_code: Mapped[str] = mapped_column(String(32), default="")     # код подразделения
    contract_no: Mapped[str] = mapped_column(String(64), default="")       # № трудового договора
    contract_date: Mapped[str] = mapped_column(String(32), default="")     # дата трудового договора

class Equipment(Base):
    __tablename__ = "equipment"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(32), default="Прочее")
    name: Mapped[str] = mapped_column(String(300), default="")
    invno: Mapped[str] = mapped_column(String(64), default="")
    serial: Mapped[str] = mapped_column(String(128), default="")
    purchase: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(16), default="stock")
    emp_id: Mapped[Optional[str]] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"), nullable=True)
    holder_text: Mapped[str] = mapped_column(String(200), default="")
    workplace: Mapped[str] = mapped_column(String(200), default="")
    assign_type: Mapped[str] = mapped_column(String(16), default="")   # employee | workplace | ""
    cost: Mapped[str] = mapped_column(String(32), default="")          # стоимость, руб.
    inner_name: Mapped[str] = mapped_column(String(300), default="")   # внутреннее наименование
    supplier_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    location: Mapped[str] = mapped_column(String(128), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

class Act(Base):
    __tablename__ = "acts"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    no: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))           # issue | return
    date: Mapped[str] = mapped_column(String(16))
    emp_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    emp_name: Mapped[str] = mapped_column(String(200), default="")
    items: Mapped[list] = mapped_column(JSONType, default=list)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())

class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONType, default=dict)

class Supplier(Base):
    __tablename__ = "suppliers"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    contact: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(64), default="")
    email: Mapped[str] = mapped_column(String(128), default="")
    note: Mapped[str] = mapped_column(Text, default="")

class Tsd(Base):
    __tablename__ = "tsd"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    inv_no: Mapped[str] = mapped_column(String(64), default="")      # инвентарный номер
    name: Mapped[str] = mapped_column(String(200), default="")       # наименование
    inner_no: Mapped[str] = mapped_column(String(64), default="")    # внутренний номер
    location: Mapped[str] = mapped_column(String(128), default="")
    note: Mapped[str] = mapped_column(Text, default="")

class Location(Base):
    __tablename__ = "locations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    note: Mapped[str] = mapped_column(Text, default="")

class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    telegram_chat_id: Mapped[str] = mapped_column(String(64), default="")
    email: Mapped[str] = mapped_column(String(128), default="")
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    note: Mapped[str] = mapped_column(Text, default="")

class Printer(Base):
    __tablename__ = "printers"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    model: Mapped[str] = mapped_column(String(200), default="")
    location: Mapped[str] = mapped_column(String(128), default="")
    inv_no: Mapped[str] = mapped_column(String(64), default="")
    notify_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")

class Cartridge(Base):
    __tablename__ = "cartridges"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    printer_id: Mapped[Optional[str]] = mapped_column(ForeignKey("printers.id", ondelete="SET NULL"), nullable=True)
    stock: Mapped[int] = mapped_column(Integer, default=0)
    min_stock: Mapped[int] = mapped_column(Integer, default=0)
    alerted: Mapped[int] = mapped_column(Integer, default=0)   # 1 — уведомление уже отправлено
    note: Mapped[str] = mapped_column(Text, default="")

# ---------------------------------------------------------------- password
def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
    return salt.hex() + "$" + dk.hex()

def verify_pw(pw: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt_hex), 200_000)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False

# ---------------------------------------------------------------- ФИО helpers
def split_fio(full: str):
    """Разбить строку ФИО на (фамилия, имя, отчество) по пробелам."""
    parts = (full or "").split()
    last = parts[0] if parts else ""
    first = parts[1] if len(parts) > 1 else ""
    middle = " ".join(parts[2:]) if len(parts) > 2 else ""
    return last, first, middle

def join_fio(last: str, first: str, middle: str):
    """Собрать ФИО из частей, схлопывая лишние пробелы."""
    return " ".join(p for p in ((last or "").strip(), (first or "").strip(), (middle or "").strip()) if p)

def emp_name_fields(src: dict):
    """Собрать поля имени из тела запроса/сида: части приоритетнее, иначе разбираем fullName."""
    has_parts = any(k in src for k in ("lastName", "firstName", "middleName"))
    if has_parts:
        last = (src.get("lastName") or "").strip()
        first = (src.get("firstName") or "").strip()
        middle = (src.get("middleName") or "").strip()
    else:
        last, first, middle = split_fio(src.get("fullName", ""))
    return {"last_name": last, "first_name": first, "middle_name": middle,
            "full_name": join_fio(last, first, middle)}

# ---------------------------------------------------------------- serialization
def emp_dict(e: Employee):
    return {"id": e.id, "fullName": e.full_name,
            "lastName": e.last_name, "firstName": e.first_name, "middleName": e.middle_name,
            "login": e.login, "email": e.email,
            "dept": e.dept, "position": e.position, "location": e.location,
            "passport": e.passport, "passportIssued": e.passport_issued, "passportCode": e.passport_code,
            "contractNo": e.contract_no, "contractDate": e.contract_date}

def eq_dict(e: Equipment):
    return {"id": e.id, "type": e.type, "name": e.name, "invno": e.invno, "serial": e.serial,
            "purchase": e.purchase, "status": e.status, "empId": e.emp_id or "",
            "holderText": e.holder_text, "workplace": e.workplace, "assignType": e.assign_type,
            "cost": e.cost, "innerName": e.inner_name, "supplierId": e.supplier_id or "",
            "location": e.location, "note": e.note}

def supplier_dict(s: Supplier):
    return {"id": s.id, "name": s.name, "contact": s.contact, "phone": s.phone,
            "email": s.email, "note": s.note}

def tsd_dict(t: Tsd):
    return {"id": t.id, "invNo": t.inv_no, "name": t.name, "innerNo": t.inner_no,
            "location": t.location, "note": t.note}

def location_dict(l: Location):
    return {"id": l.id, "name": l.name, "note": l.note}

def printer_dict(p: Printer):
    return {"id": p.id, "name": p.name, "model": p.model, "location": p.location,
            "invNo": p.inv_no, "notifyId": p.notify_id or "", "note": p.note}

def notification_dict(n: Notification):
    return {"id": n.id, "name": n.name, "telegramChatId": n.telegram_chat_id,
            "email": n.email, "enabled": bool(n.enabled), "note": n.note}

def cartridge_dict(cg: Cartridge, printers_by_id: dict = None):
    pr = (printers_by_id or {}).get(cg.printer_id) if cg.printer_id else None
    return {"id": cg.id, "name": cg.name, "printerId": cg.printer_id or "",
            "printerName": (pr.name if pr else ""), "stock": cg.stock, "minStock": cg.min_stock,
            "low": cg.stock <= cg.min_stock, "note": cg.note}

def act_dict(a: Act):
    return {"id": a.id, "no": a.no, "kind": a.kind, "date": a.date, "empId": a.emp_id or "",
            "empName": a.emp_name, "items": a.items or [], "createdBy": a.created_by}

DEFAULT_ORG = {
    "name": "ИП Эстрин Кирилл Вадимович",
    "inn": "770100448470",
    "address": "129085, Москва г, Мира пр-кт, дом 81, квартира 101",
    "city": "Москва",
    "signatory": "Эстрина Кирилла Вадимовича",         # в лице предпринимателя … (род. падеж)
    "signatoryShort": "К.В. Эстрин",                    # в строке подписи
    "basis": "Свидетельства о регистрации",
}
ORG_KEYS = ["name", "inn", "address", "city", "signatory", "signatoryShort", "basis"]

DEFAULT_NOTIFY = {
    "telegram_enabled": bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
    "telegram_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
    "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
    "email_enabled": bool(os.environ.get("SMTP_HOST")),
    "smtp_host": os.environ.get("SMTP_HOST", ""),
    "smtp_port": int(os.environ.get("SMTP_PORT", "587") or 587),
    "smtp_user": os.environ.get("SMTP_USER", ""),
    "smtp_password": os.environ.get("SMTP_PASSWORD", ""),
    "smtp_from": os.environ.get("SMTP_FROM", ""),
    "email_to": os.environ.get("EMAIL_TO", ""),
    "smtp_tls": os.environ.get("SMTP_TLS", "1") not in ("0", "false", "False"),
}
NOTIFY_SECRET_KEYS = ["telegram_token", "smtp_password"]

def _get_notify(db: Session) -> dict:
    cfg = dict(DEFAULT_NOTIFY)
    s = db.get(Setting, "notify")
    if s and isinstance(s.value, dict):
        cfg.update(s.value)
    return cfg

def send_telegram(cfg: dict, text: str, chat_id: str = None):
    import urllib.request, urllib.parse
    token = cfg.get("telegram_token")
    chat = chat_id or cfg.get("telegram_chat_id")
    if not token or not chat:
        return False, "не задан токен или chat_id"
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        return True, "ok"
    except Exception as ex:
        return False, str(ex)

def send_email(cfg: dict, subject: str, text: str, to: str = None):
    import smtplib
    from email.message import EmailMessage
    host = cfg.get("smtp_host")
    if not host:
        return False, "не задан SMTP host"
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = cfg.get("smtp_from") or cfg.get("smtp_user") or ""
        msg["To"] = to or cfg.get("email_to") or ""
        msg.set_content(text)
        port = int(cfg.get("smtp_port") or 587)
        srv = smtplib.SMTP_SSL(host, port, timeout=15) if port == 465 else smtplib.SMTP(host, port, timeout=15)
        if port != 465 and cfg.get("smtp_tls"):
            srv.starttls()
        if cfg.get("smtp_user"):
            srv.login(cfg.get("smtp_user"), cfg.get("smtp_password") or "")
        srv.send_message(msg)
        srv.quit()
        return True, "ok"
    except Exception as ex:
        return False, str(ex)

def _notify_low(cfg: dict, cg, printer_name: str, dest: "Notification" = None):
    text = ("⚠️ Низкий остаток картриджа\n"
            f"Картридж: {cg.name}\n"
            + (f"Принтер: {printer_name}\n" if printer_name else "")
            + f"Остаток: {cg.stock} (минимум: {cg.min_stock})")
    out = []
    if dest is not None:
        # адресат из справочника уведомлений принтера — шлём и в Telegram, и на почту сразу
        if cfg.get("telegram_token") and dest.telegram_chat_id:
            out.append(("telegram",) + send_telegram(cfg, text, chat_id=dest.telegram_chat_id))
        if cfg.get("smtp_host") and dest.email:
            out.append(("email",) + send_email(cfg, "Низкий остаток картриджа", text, to=dest.email))
    else:
        # глобальные настройки по умолчанию (принтер без привязки к уведомлению)
        if cfg.get("telegram_enabled"):
            out.append(("telegram",) + send_telegram(cfg, text))
        if cfg.get("email_enabled"):
            out.append(("email",) + send_email(cfg, "Низкий остаток картриджа", text))
    return out

def _evaluate_alert(db: Session, cg):
    """Шлёт уведомление при переходе в состояние «остаток ≤ минимума». Возвращает список результатов."""
    if cg.stock > cg.min_stock:
        cg.alerted = 0
        return []
    if cg.alerted:
        return []
    cfg = _get_notify(db)
    pr = db.get(Printer, cg.printer_id) if cg.printer_id else None
    dest = None
    if pr and pr.notify_id:
        d = db.get(Notification, pr.notify_id)
        if d and d.enabled:
            dest = d
    res = _notify_low(cfg, cg, pr.name if pr else "", dest)
    cg.alerted = 1
    return [{"channel": c, "ok": ok, "info": info} for (c, ok, info) in res]

# ---------------------------------------------------------------- migrations
def _alembic_config():
    from alembic.config import Config
    cfg = Config(str(BASE / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE / "alembic"))
    cfg.set_main_option("sqlalchemy.url", DB_URL)
    return cfg

# Столбцы, добавленные после первой версии (для апгрейда legacy-базы, созданной до Alembic).
_LEGACY_COLUMNS = [
    ("equipment", "assign_type", "VARCHAR(16)"),
    ("equipment", "cost", "VARCHAR(32)"),
    ("employees", "passport", "VARCHAR(64)"),
    ("employees", "passport_issued", "VARCHAR(255)"),
    ("employees", "passport_code", "VARCHAR(32)"),
    ("employees", "contract_no", "VARCHAR(64)"),
    ("employees", "contract_date", "VARCHAR(32)"),
]

def _reconcile_legacy(conn):
    """Добить недостающие столбцы на базе, созданной прежней версией без Alembic."""
    from sqlalchemy import inspect as sa_inspect, text
    insp = sa_inspect(conn)
    is_pg = conn.dialect.name == "postgresql"
    for table, col, typ in _LEGACY_COLUMNS:
        if col in [c["name"] for c in insp.get_columns(table)]:
            continue
        ifne = "IF NOT EXISTS " if is_pg else ""
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ifne}{col} {typ} NOT NULL DEFAULT ''"))

def run_migrations():
    from alembic import command
    from sqlalchemy import inspect as sa_inspect
    insp = sa_inspect(engine)
    has_core = insp.has_table("equipment")
    has_version = insp.has_table("alembic_version")
    cfg = _alembic_config()
    if has_core and not has_version:
        # База существует, но создана прежней версией без Alembic:
        # добираем недостающие столбцы базовой схемы, помечаем её ревизией 0001
        # и доводим остальными миграциями (новые таблицы и т.п.) до head.
        with engine.begin() as conn:
            _reconcile_legacy(conn)
        command.stamp(cfg, "0001")
        command.upgrade(cfg, "head")
    else:
        command.upgrade(cfg, "head")

# ---------------------------------------------------------------- seed
def seed_if_empty():
    with SessionLocal() as db:
        # admin user
        if not db.scalar(select(User).where(User.username == ADMIN_USER)):
            db.add(User(username=ADMIN_USER, password_hash=hash_pw(ADMIN_PASSWORD),
                        full_name="Администратор", role="admin"))
        # org settings
        if not db.get(Setting, "org"):
            db.add(Setting(key="org", value=DEFAULT_ORG))
        if not db.get(Setting, "seq"):
            db.add(Setting(key="seq", value={"act": 0, "inv": 1}))
        if not db.get(Setting, "notify"):
            db.add(Setting(key="notify", value=DEFAULT_NOTIFY))
        # inventory seed — только демо-режим (SEED_DEMO=1) и только в пустую базу
        if SEED_DEMO and db.scalar(select(func.count(Employee.id))) == 0:
            seed_path = BASE / "seed_data.json"
            if seed_path.exists():
                data = json.loads(seed_path.read_text(encoding="utf-8"))
                seen = set()
                for e in data.get("employees", []):
                    if e["id"] in seen:
                        continue
                    seen.add(e["id"])
                    db.add(Employee(id=e["id"], **emp_name_fields(e), login=e.get("login", ""),
                                    email=e.get("email", ""), dept=e.get("dept", ""),
                                    position=e.get("position", ""), location=e.get("location", ""),
                                    passport=e.get("passport", ""), passport_issued=e.get("passportIssued", ""),
                                    passport_code=e.get("passportCode", ""), contract_no=e.get("contractNo", ""),
                                    contract_date=e.get("contractDate", "")))
                for q in data.get("equipment", []):
                    emp = q.get("empId") or None
                    db.add(Equipment(id=q["id"], type=q.get("type", "Прочее"), name=q.get("name", ""),
                                     invno=q.get("invno", ""), serial=q.get("serial", ""),
                                     purchase=q.get("purchase", ""), status=q.get("status", "stock"),
                                     emp_id=emp, holder_text=q.get("holderText", ""),
                                     workplace=q.get("workplace", ""),
                                     assign_type="employee" if emp else "",
                                     cost=q.get("cost", ""),
                                     location=q.get("location", ""), note=q.get("note", "")))
                seq = db.get(Setting, "seq")
                if seq:
                    val = dict(seq.value or {}); val["inv"] = int(data.get("nextInv", 1)); seq.value = val
        db.commit()

# ---------------------------------------------------------------- app
# Проверка доступа к разделам по пути запроса. "admin" — только администратор,
# иначе — наличие раздела в perms пользователя (у admin — полный доступ).
PERM_PREFIXES = [
    ("/api/users", "admin"), ("/api/settings", "admin"), ("/api/export", "admin"),
    ("/api/import", "admin"), ("/api/wipe", "admin"),
    ("/api/equipment", "equip"), ("/api/employees", "people"), ("/api/acts", "acts"),
    ("/api/cartridges", "cart"), ("/api/printers", "cart"), ("/api/notifications", "cart"),
    ("/api/suppliers", "sup"), ("/api/tsd", "tsd"), ("/api/locations", "equip"),
]

async def _perm_dispatch(request: Request, call_next):
    path = request.url.path
    need = None
    for prefix, sec in PERM_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            need = sec
            break
    if need:
        uid = request.session.get("uid")
        if not uid:
            return JSONResponse({"detail": "Требуется авторизация"}, status_code=401)
        with SessionLocal() as db:
            u = db.get(User, uid)
        if not u:
            return JSONResponse({"detail": "Сессия недействительна"}, status_code=401)
        allowed = (u.role == "admin") or (need != "admin" and need in (u.perms or []))
        if not allowed:
            return JSONResponse({"detail": "Нет доступа к этому разделу"}, status_code=403)
    return await call_next(request)

app = FastAPI(title="Учёт техники")
from starlette.middleware.base import BaseHTTPMiddleware
# добавляем сначала проверку прав (внутренний слой), затем сессии (внешний слой,
# выполняется первым) — так в проверке прав доступна request.session.
app.add_middleware(BaseHTTPMiddleware, dispatch=_perm_dispatch)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=60 * 60 * 24 * 14, same_site="lax")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    if not uid:
        raise HTTPException(401, "Требуется авторизация")
    u = db.get(User, uid)
    if not u:
        request.session.clear()
        raise HTTPException(401, "Сессия недействительна")
    return u

@app.on_event("startup")
def _startup():
    import time
    for _ in range(20):
        try:
            run_migrations()
            break
        except Exception as ex:          # ждём пока поднимется БД
            print("DB not ready:", ex)
            time.sleep(2)
    else:
        raise RuntimeError("Не удалось подключиться к БД")
    seed_if_empty()

# ---------- auth
class LoginBody(BaseModel):
    username: str
    password: str

def _user_info(u: User):
    admin = u.role == "admin"
    return {"username": u.username, "fullName": u.full_name, "role": u.role,
            "twofa": bool(u.totp_enabled), "isAdmin": admin,
            "perms": list(GRANTABLE_SECTIONS) if admin else list(u.perms or [])}

def _has(u: User, sec: str) -> bool:
    return u.role == "admin" or sec in (u.perms or [])

def _verify_2fa(u: User, code: str) -> bool:
    code = (code or "").strip().replace(" ", "").replace("-", "")
    if not code:
        return False
    if u.totp_secret and pyotp.TOTP(u.totp_secret).verify(code, valid_window=1):
        return True
    codes = list(u.recovery_codes or [])           # одноразовые резервные коды
    for h in codes:
        if verify_pw(code, h):
            codes.remove(h); u.recovery_codes = codes
            return True
    return False

@app.post("/api/login")
def login(body: LoginBody, request: Request, db: Session = Depends(get_db)):
    u = db.scalar(select(User).where(User.username == body.username))
    if not u or not verify_pw(body.password, u.password_hash):
        raise HTTPException(401, "Неверный логин или пароль")
    if u.totp_enabled:
        request.session.pop("uid", None)
        request.session["pending_uid"] = u.id
        return {"need2fa": True}
    request.session.pop("pending_uid", None)
    request.session["uid"] = u.id
    return _user_info(u)

@app.post("/api/login/2fa")
def login_2fa(body: dict, request: Request, db: Session = Depends(get_db)):
    uid = request.session.get("pending_uid")
    if not uid:
        raise HTTPException(401, "Сессия входа истекла — войдите заново")
    u = db.get(User, uid)
    if not u or not u.totp_enabled:
        raise HTTPException(401, "Требуется вход заново")
    if not _verify_2fa(u, body.get("code", "")):
        raise HTTPException(400, "Неверный код")
    db.commit()                                     # фиксируем расход резервного кода
    request.session.pop("pending_uid", None)
    request.session["uid"] = u.id
    return _user_info(u)

# ---------- 2FA management
@app.post("/api/2fa/setup")
def twofa_setup(db: Session = Depends(get_db), u: User = Depends(current_user)):
    if u.totp_enabled:
        raise HTTPException(400, "2FA уже включена — сначала отключите")
    u.totp_secret = pyotp.random_base32()
    db.commit()
    uri = pyotp.TOTP(u.totp_secret).provisioning_uri(name=u.username, issuer_name="Учёт техники")
    return {"secret": u.totp_secret, "otpauth": uri}

@app.get("/api/2fa/qr.svg")
def twofa_qr(db: Session = Depends(get_db), u: User = Depends(current_user)):
    if not u.totp_secret:
        raise HTTPException(400, "Сначала запустите настройку")
    import qrcode, qrcode.image.svg
    uri = pyotp.TOTP(u.totp_secret).provisioning_uri(name=u.username, issuer_name="Учёт техники")
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO(); img.save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})

@app.post("/api/2fa/enable")
def twofa_enable(body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    if u.totp_enabled:
        raise HTTPException(400, "2FA уже включена")
    if not u.totp_secret:
        raise HTTPException(400, "Сначала запустите настройку (setup)")
    if not pyotp.TOTP(u.totp_secret).verify((body.get("code") or "").strip().replace(" ", ""), valid_window=1):
        raise HTTPException(400, "Неверный код")
    codes = [secrets.token_hex(4) for _ in range(10)]
    u.recovery_codes = [hash_pw(c) for c in codes]
    u.totp_enabled = 1
    db.commit()
    return {"recovery_codes": codes}

@app.post("/api/2fa/disable")
def twofa_disable(body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    if not verify_pw(body.get("password", ""), u.password_hash):
        raise HTTPException(400, "Неверный пароль")
    u.totp_enabled = 0; u.totp_secret = ""; u.recovery_codes = []
    db.commit()
    return {"ok": True}

# ---------- users management (только администратор; доступ гарантирует middleware)
def _admin_count(db: Session) -> int:
    return db.scalar(select(func.count(User.id)).where(User.role == "admin")) or 0

def _user_admin_dict(u: User):
    return {"id": u.id, "username": u.username, "fullName": u.full_name, "role": u.role,
            "perms": list(u.perms or []), "twofa": bool(u.totp_enabled)}

@app.get("/api/users")
def users_list(db: Session = Depends(get_db), cu: User = Depends(current_user)):
    return [_user_admin_dict(x) for x in db.scalars(select(User).order_by(User.username)).all()]

@app.post("/api/users")
def user_create(body: dict, db: Session = Depends(get_db), cu: User = Depends(current_user)):
    uname = (body.get("username") or "").strip()
    if not uname:
        raise HTTPException(400, "Укажите логин")
    if db.scalar(select(User).where(User.username == uname)):
        raise HTTPException(400, "Логин уже занят")
    pw = (body.get("password") or "").strip()
    if len(pw) < 4:
        raise HTTPException(400, "Пароль слишком короткий")
    role = "admin" if body.get("role") == "admin" else "user"
    perms = [s for s in (body.get("perms") or []) if s in GRANTABLE_SECTIONS]
    nu = User(username=uname, password_hash=hash_pw(pw), full_name=(body.get("fullName") or "").strip(),
              role=role, perms=perms)
    db.add(nu); db.commit()
    return _user_admin_dict(nu)

@app.put("/api/users/{uid}")
def user_update(uid: int, body: dict, db: Session = Depends(get_db), cu: User = Depends(current_user)):
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "Не найдено")
    if "fullName" in body:
        u.full_name = (body["fullName"] or "").strip()
    if "role" in body:
        new_role = "admin" if body["role"] == "admin" else "user"
        if u.role == "admin" and new_role != "admin" and _admin_count(db) <= 1:
            raise HTTPException(400, "Нельзя снять роль у последнего администратора")
        u.role = new_role
    if "perms" in body:
        u.perms = [s for s in (body["perms"] or []) if s in GRANTABLE_SECTIONS]
    if body.get("password"):
        if len((body["password"] or "").strip()) < 4:
            raise HTTPException(400, "Пароль слишком короткий")
        u.password_hash = hash_pw(body["password"].strip())
    db.commit()
    return _user_admin_dict(u)

@app.delete("/api/users/{uid}")
def user_delete(uid: int, db: Session = Depends(get_db), cu: User = Depends(current_user)):
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "Не найдено")
    if u.id == cu.id:
        raise HTTPException(400, "Нельзя удалить самого себя")
    if u.role == "admin" and _admin_count(db) <= 1:
        raise HTTPException(400, "Нельзя удалить последнего администратора")
    db.delete(u); db.commit()
    return {"ok": True}

@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}

@app.get("/api/me")
def me(u: User = Depends(current_user)):
    return _user_info(u)

@app.post("/api/password")
def change_password(body: dict, request: Request, db: Session = Depends(get_db), u: User = Depends(current_user)):
    if not verify_pw(body.get("old", ""), u.password_hash):
        raise HTTPException(400, "Старый пароль неверен")
    new = (body.get("new") or "").strip()
    if len(new) < 4:
        raise HTTPException(400, "Пароль слишком короткий")
    u.password_hash = hash_pw(new)
    db.commit()
    return {"ok": True}

# ---------- state
@app.get("/api/state")
def state(db: Session = Depends(get_db), u: User = Depends(current_user)):
    org = db.get(Setting, "org")
    admin = u.role == "admin"
    def allow(sec):
        return admin or sec in (u.perms or [])
    # сотрудники: полный список при доступе к «people»; иначе краткий (для подстановки
    # имён в технике/актах) если есть смежный раздел; иначе пусто
    if allow("people"):
        employees = [emp_dict(e) for e in db.scalars(select(Employee)).all()]
    elif allow("equip") or allow("acts") or allow("cart"):
        employees = [{"id": e.id, "fullName": e.full_name, "login": e.login}
                     for e in db.scalars(select(Employee)).all()]
    else:
        employees = []
    # техника: помечаем, есть ли по единице акт выдачи (связь по id позиции акта, запасной — по инв.№)
    if allow("equip"):
        act_label = {}
        for a in db.scalars(select(Act).where(Act.kind == "issue")).all():
            lbl = act_number(a)
            for it in (a.items or []):
                if it.get("id"):
                    act_label[("id", it["id"])] = lbl
                if it.get("invno"):
                    act_label[("inv", it["invno"])] = lbl
        equipment_list = []
        for e in db.scalars(select(Equipment)).all():
            d = eq_dict(e)
            lbl = act_label.get(("id", e.id)) or (act_label.get(("inv", e.invno)) if e.invno else None)
            d["hasAct"] = bool(lbl)
            d["actLabel"] = lbl or ""
            equipment_list.append(d)
    else:
        equipment_list = []
    return {
        "version": APP_VERSION,
        "org": org.value if org else DEFAULT_ORG,
        "user": _user_info(u),
        "employees": employees,
        "equipment": equipment_list,
        "acts": [act_dict(a) for a in db.scalars(select(Act)).all()] if allow("acts") else [],
        "suppliers": [supplier_dict(s) for s in db.scalars(select(Supplier)).all()] if (allow("sup") or allow("equip")) else [],
        "tsd": [tsd_dict(t) for t in db.scalars(select(Tsd)).all()] if allow("tsd") else [],
        "locations": [location_dict(l) for l in db.scalars(select(Location)).all()] if (allow("equip") or allow("tsd") or allow("cart")) else [],
        "printers": [printer_dict(p) for p in db.scalars(select(Printer)).all()] if allow("cart") else [],
        "cartridges": [cartridge_dict(cg, _printers_map(db))
                       for cg in db.scalars(select(Cartridge)).all()] if allow("cart") else [],
        "notifications": [notification_dict(n) for n in db.scalars(select(Notification)).all()] if allow("cart") else [],
        "notify": _notify_masked(_get_notify(db)) if admin else None,
    }

# ---------- printers & cartridges
PRINTER_FIELDS = [("name", "name"), ("model", "model"), ("location", "location"),
                  ("invNo", "inv_no"), ("note", "note")]

@app.post("/api/printers")
def printer_create(body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    p = Printer(id="prn-" + secrets.token_hex(5))
    for j, c in PRINTER_FIELDS:
        if j in body:
            setattr(p, c, (body[j] or "").strip())
    if "notifyId" in body:
        p.notify_id = body["notifyId"] or None
    if not p.name:
        raise HTTPException(400, "Укажите название принтера")
    db.add(p); db.commit()
    return printer_dict(p)

@app.put("/api/printers/{pid}")
def printer_update(pid: str, body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    p = db.get(Printer, pid)
    if not p:
        raise HTTPException(404, "Не найдено")
    for j, c in PRINTER_FIELDS:
        if j in body:
            setattr(p, c, (body[j] or "").strip())
    if "notifyId" in body:
        p.notify_id = body["notifyId"] or None
    db.commit()
    return printer_dict(p)

@app.delete("/api/printers/{pid}")
def printer_delete(pid: str, db: Session = Depends(get_db), u: User = Depends(current_user)):
    p = db.get(Printer, pid)
    if not p:
        raise HTTPException(404, "Не найдено")
    for cg in db.scalars(select(Cartridge).where(Cartridge.printer_id == pid)).all():
        cg.printer_id = None
    db.delete(p); db.commit()
    return {"ok": True}

def _printers_map(db):
    return {p.id: p for p in db.scalars(select(Printer)).all()}

@app.post("/api/cartridges")
def cartridge_create(body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    cg = Cartridge(id="cg-" + secrets.token_hex(5),
                   name=(body.get("name") or "").strip(),
                   printer_id=(body.get("printerId") or None),
                   stock=max(0, int(body.get("stock") or 0)),
                   min_stock=max(0, int(body.get("minStock") or 0)),
                   note=(body.get("note") or "").strip())
    if not cg.name:
        raise HTTPException(400, "Укажите название картриджа")
    alerts = _evaluate_alert(db, cg)
    db.add(cg); db.commit()
    return {"cartridge": cartridge_dict(cg, _printers_map(db)), "alerts": alerts}

@app.put("/api/cartridges/{cid}")
def cartridge_update(cid: str, body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    cg = db.get(Cartridge, cid)
    if not cg:
        raise HTTPException(404, "Не найдено")
    if "name" in body:
        cg.name = (body["name"] or "").strip()
    if "printerId" in body:
        cg.printer_id = body["printerId"] or None
    if "stock" in body:
        cg.stock = max(0, int(body["stock"] or 0))
    if "minStock" in body:
        cg.min_stock = max(0, int(body["minStock"] or 0))
    if "note" in body:
        cg.note = (body["note"] or "").strip()
    alerts = _evaluate_alert(db, cg)
    db.commit()
    return {"cartridge": cartridge_dict(cg, _printers_map(db)), "alerts": alerts}

@app.post("/api/cartridges/{cid}/stock")
def cartridge_stock(cid: str, body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    cg = db.get(Cartridge, cid)
    if not cg:
        raise HTTPException(404, "Не найдено")
    if "set" in body:
        cg.stock = max(0, int(body["set"]))
    else:
        cg.stock = max(0, cg.stock + int(body.get("delta", 0)))
    alerts = _evaluate_alert(db, cg)
    db.commit()
    return {"cartridge": cartridge_dict(cg, _printers_map(db)), "alerts": alerts}

@app.delete("/api/cartridges/{cid}")
def cartridge_delete(cid: str, db: Session = Depends(get_db), u: User = Depends(current_user)):
    cg = db.get(Cartridge, cid)
    if not cg:
        raise HTTPException(404, "Не найдено")
    db.delete(cg); db.commit()
    return {"ok": True}

@app.post("/api/cartridges/check")
def cartridge_check(db: Session = Depends(get_db), u: User = Depends(current_user)):
    fired = 0
    for cg in db.scalars(select(Cartridge)).all():
        if _evaluate_alert(db, cg):
            fired += 1
    db.commit()
    return {"notified": fired}

# ---------- notifications directory (справочник адресатов уведомлений)
NOTIF_FIELDS = [("name", "name"), ("telegramChatId", "telegram_chat_id"), ("email", "email"), ("note", "note")]

@app.post("/api/notifications")
def notif_create(body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    n = Notification(id="ntf-" + secrets.token_hex(5), enabled=0 if body.get("enabled") is False else 1)
    for j, c in NOTIF_FIELDS:
        if j in body:
            setattr(n, c, (body[j] or "").strip())
    if not n.name:
        raise HTTPException(400, "Укажите название")
    db.add(n); db.commit()
    return notification_dict(n)

@app.put("/api/notifications/{nid}")
def notif_update(nid: str, body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    n = db.get(Notification, nid)
    if not n:
        raise HTTPException(404, "Не найдено")
    for j, c in NOTIF_FIELDS:
        if j in body:
            setattr(n, c, (body[j] or "").strip())
    if "enabled" in body:
        n.enabled = 1 if body["enabled"] else 0
    db.commit()
    return notification_dict(n)

@app.delete("/api/notifications/{nid}")
def notif_delete(nid: str, db: Session = Depends(get_db), u: User = Depends(current_user)):
    n = db.get(Notification, nid)
    if not n:
        raise HTTPException(404, "Не найдено")
    for p in db.scalars(select(Printer).where(Printer.notify_id == nid)).all():
        p.notify_id = None
    db.delete(n); db.commit()
    return {"ok": True}

# ---------- notify settings
def _notify_masked(cfg: dict) -> dict:
    out = dict(cfg)
    for k in NOTIFY_SECRET_KEYS:
        out[k + "_set"] = bool(cfg.get(k))
        out[k] = ""
    return out

@app.get("/api/settings/notify")
def notify_get(db: Session = Depends(get_db), u: User = Depends(current_user)):
    return _notify_masked(_get_notify(db))

@app.put("/api/settings/notify")
def notify_put(body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    cfg = _get_notify(db)
    for k in ["telegram_enabled", "telegram_chat_id", "email_enabled", "smtp_host",
              "smtp_port", "smtp_user", "smtp_from", "email_to", "smtp_tls"]:
        if k in body:
            cfg[k] = body[k]
    for k in NOTIFY_SECRET_KEYS:        # секрет меняем только если передан непустой
        if body.get(k):
            cfg[k] = body[k]
    cfg["smtp_port"] = int(cfg.get("smtp_port") or 587)
    s = db.get(Setting, "notify") or Setting(key="notify", value={})
    s.value = cfg; db.add(s); db.commit()
    return _notify_masked(cfg)

@app.post("/api/settings/notify/test")
def notify_test(db: Session = Depends(get_db), u: User = Depends(current_user)):
    cfg = _get_notify(db)
    text = "Проверка уведомлений — учёт техники/картриджей."
    res = []
    if cfg.get("telegram_enabled"):
        res.append(("telegram",) + send_telegram(cfg, text))
    if cfg.get("email_enabled"):
        res.append(("email",) + send_email(cfg, "Проверка уведомлений", text))
    if not res:
        raise HTTPException(400, "Ни один канал не включён")
    return {"results": [{"channel": c, "ok": ok, "info": info} for (c, ok, info) in res]}

# ---------- employees
# поля без имени (имя обрабатывается отдельно через emp_name_fields)
EMP_FIELDS = [("login", "login"), ("email", "email"),
              ("dept", "dept"), ("position", "position"), ("location", "location"),
              ("passport", "passport"), ("passportIssued", "passport_issued"),
              ("passportCode", "passport_code"), ("contractNo", "contract_no"),
              ("contractDate", "contract_date")]

@app.post("/api/employees")
def emp_create(body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    e = Employee(id="emp-" + secrets.token_hex(5))
    for k, v in emp_name_fields(body).items():
        setattr(e, k, v)
    for j, c in EMP_FIELDS:
        if j in body:
            setattr(e, c, (body[j] or "").strip())
    if not e.last_name:
        raise HTTPException(400, "Укажите фамилию")
    db.add(e); db.commit()
    return emp_dict(e)

@app.put("/api/employees/{eid}")
def emp_update(eid: str, body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    e = db.get(Employee, eid)
    if not e:
        raise HTTPException(404, "Не найдено")
    if any(k in body for k in ("lastName", "firstName", "middleName", "fullName")):
        for k, v in emp_name_fields(body).items():
            setattr(e, k, v)
    for j, c in EMP_FIELDS:
        if j in body:
            setattr(e, c, (body[j] or "").strip())
    db.commit()
    return emp_dict(e)

@app.delete("/api/employees/{eid}")
def emp_delete(eid: str, db: Session = Depends(get_db), u: User = Depends(current_user)):
    e = db.get(Employee, eid)
    if not e:
        raise HTTPException(404, "Не найдено")
    for q in db.scalars(select(Equipment).where(Equipment.emp_id == eid)).all():
        q.emp_id = None; q.status = "stock"; q.workplace = ""
    db.delete(e); db.commit()
    return {"ok": True}

# ---------- ТСД (справочник терминалов сбора данных)
TSD_FIELDS = [("invNo", "inv_no"), ("name", "name"), ("innerNo", "inner_no"),
              ("location", "location"), ("note", "note")]

# ---------- locations (справочник локаций)
@app.post("/api/locations")
def location_create(body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    l = Location(id="loc-" + secrets.token_hex(5), name=(body.get("name") or "").strip(),
                 note=(body.get("note") or "").strip())
    if not l.name:
        raise HTTPException(400, "Укажите название локации")
    db.add(l); db.commit()
    return location_dict(l)

@app.put("/api/locations/{lid}")
def location_update(lid: str, body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    l = db.get(Location, lid)
    if not l:
        raise HTTPException(404, "Не найдено")
    if "name" in body:
        l.name = (body["name"] or "").strip()
    if "note" in body:
        l.note = (body["note"] or "").strip()
    db.commit()
    return location_dict(l)

@app.delete("/api/locations/{lid}")
def location_delete(lid: str, db: Session = Depends(get_db), u: User = Depends(current_user)):
    l = db.get(Location, lid)
    if not l:
        raise HTTPException(404, "Не найдено")
    db.delete(l); db.commit()   # локация у техники — строкой, не ссылкой: записи не трогаем
    return {"ok": True}

@app.post("/api/tsd")
def tsd_create(body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    t = Tsd(id="tsd-" + secrets.token_hex(5))
    for j, c in TSD_FIELDS:
        if j in body:
            setattr(t, c, (body[j] or "").strip())
    if not (t.name or t.inv_no or t.inner_no):
        raise HTTPException(400, "Укажите наименование или номер")
    db.add(t); db.commit()
    return tsd_dict(t)

@app.put("/api/tsd/{tid}")
def tsd_update(tid: str, body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    t = db.get(Tsd, tid)
    if not t:
        raise HTTPException(404, "Не найдено")
    for j, c in TSD_FIELDS:
        if j in body:
            setattr(t, c, (body[j] or "").strip())
    db.commit()
    return tsd_dict(t)

@app.delete("/api/tsd/{tid}")
def tsd_delete(tid: str, db: Session = Depends(get_db), u: User = Depends(current_user)):
    t = db.get(Tsd, tid)
    if not t:
        raise HTTPException(404, "Не найдено")
    db.delete(t); db.commit()
    return {"ok": True}

# ---------- suppliers (справочник поставщиков)
SUPPLIER_FIELDS = [("name", "name"), ("contact", "contact"), ("phone", "phone"),
                   ("email", "email"), ("note", "note")]

@app.post("/api/suppliers")
def supplier_create(body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    s = Supplier(id="sup-" + secrets.token_hex(5))
    for j, c in SUPPLIER_FIELDS:
        if j in body:
            setattr(s, c, (body[j] or "").strip())
    if not s.name:
        raise HTTPException(400, "Укажите название поставщика")
    db.add(s); db.commit()
    return supplier_dict(s)

@app.put("/api/suppliers/{sid}")
def supplier_update(sid: str, body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    s = db.get(Supplier, sid)
    if not s:
        raise HTTPException(404, "Не найдено")
    for j, c in SUPPLIER_FIELDS:
        if j in body:
            setattr(s, c, (body[j] or "").strip())
    db.commit()
    return supplier_dict(s)

@app.delete("/api/suppliers/{sid}")
def supplier_delete(sid: str, db: Session = Depends(get_db), u: User = Depends(current_user)):
    s = db.get(Supplier, sid)
    if not s:
        raise HTTPException(404, "Не найдено")
    for q in db.scalars(select(Equipment).where(Equipment.supplier_id == sid)).all():
        q.supplier_id = None
    db.delete(s); db.commit()
    return {"ok": True}

# ---------- equipment
EQ_FIELDS = [("type", "type"), ("name", "name"), ("innerName", "inner_name"), ("invno", "invno"),
             ("serial", "serial"), ("purchase", "purchase"), ("status", "status"), ("cost", "cost"),
             ("location", "location"), ("note", "note")]

@app.post("/api/equipment")
def eq_create(body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    if not (body.get("name") or "").strip():
        raise HTTPException(400, "Укажите наименование")
    q = Equipment(id="eq-" + secrets.token_hex(5))
    for j, c in EQ_FIELDS:
        if j in body:
            setattr(q, c, (body[j] or "").strip())
    if q.status not in STATUSES:
        q.status = "stock"
    if "supplierId" in body:
        q.supplier_id = body["supplierId"] or None
    if not q.invno:                       # ИНВ.№ — порядковый номер, присваивается автоматически
        q.invno = _next_inv(db)
    db.add(q); db.commit()
    return eq_dict(q)

@app.put("/api/equipment/{qid}")
def eq_update(qid: str, body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    q = db.get(Equipment, qid)
    if not q:
        raise HTTPException(404, "Не найдено")
    for j, c in EQ_FIELDS:
        if j in body:
            setattr(q, c, (body[j] or "").strip())
    if "supplierId" in body:
        q.supplier_id = body["supplierId"] or None
    db.commit()
    return eq_dict(q)

@app.delete("/api/equipment/{qid}")
def eq_delete(qid: str, db: Session = Depends(get_db), u: User = Depends(current_user)):
    q = db.get(Equipment, qid)
    if not q:
        raise HTTPException(404, "Не найдено")
    db.delete(q); db.commit()
    return {"ok": True}

def _next_act_no(db: Session) -> int:
    seq = db.get(Setting, "seq") or Setting(key="seq", value={"act": 0, "inv": 1})
    val = dict(seq.value or {})
    val["act"] = int(val.get("act", 0)) + 1
    seq.value = val
    db.add(seq)
    return val["act"]

def _next_inv(db: Session) -> str:
    seq = db.get(Setting, "seq") or Setting(key="seq", value={"act": 0, "inv": 1})
    val = dict(seq.value or {})
    n = int(val.get("inv", 1))
    val["inv"] = n + 1
    seq.value = val
    db.add(seq)
    return "%04d" % n

def _norm_date(s):
    s = (s or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)      # yyyy-mm-dd (из <input type=date>)
    if m:
        return f"{m.group(3)}.{m.group(2)}.{m.group(1)}"
    if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", s):          # уже dd.mm.yyyy
        return s
    return None

def act_number(a) -> str:
    return f"{a.no}-" + (a.date or "").replace(".", "/")  # X-дд/мм/гггг

def _make_act(db: Session, kind: str, emp: Employee, items: List[Equipment], who: str, date: str = None) -> Act:
    a = Act(no=_next_act_no(db), kind=kind, date=_norm_date(date) or datetime.date.today().strftime("%d.%m.%Y"),
            emp_id=emp.id if emp else None, emp_name=emp.full_name if emp else "",
            items=[{"id": i.id, "name": i.name, "type": i.type, "invno": i.invno, "serial": i.serial,
                    "workplace": i.workplace, "cost": i.cost} for i in items], created_by=who)
    db.add(a)
    return a

@app.post("/api/equipment/{qid}/issue")
def eq_issue(qid: str, body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    q = db.get(Equipment, qid)
    if not q:
        raise HTTPException(404, "Не найдено")
    mode = body.get("mode") or ("workplace" if (body.get("workplace") and not body.get("empId")) else "employee")
    act = None
    if mode == "workplace":
        wp = (body.get("workplace") or "").strip()
        if not wp:
            raise HTTPException(400, "Укажите рабочее место")
        q.emp_id = None; q.holder_text = ""; q.workplace = wp; q.assign_type = "workplace"; q.status = "issued"
    else:
        emp = db.get(Employee, body.get("empId", ""))
        if not emp:
            raise HTTPException(400, "Выберите сотрудника")
        q.emp_id = emp.id; q.holder_text = ""; q.workplace = (body.get("workplace") or "").strip()
        q.assign_type = "employee"; q.status = "issued"
        if body.get("makeAct"):
            act = _make_act(db, "issue", emp, [q], u.username, date=body.get("date"))
    db.commit()
    return {"equipment": eq_dict(q), "act": act_dict(act) if act else None}

@app.post("/api/equipment/{qid}/return")
def eq_return(qid: str, body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    q = db.get(Equipment, qid)
    if not q:
        raise HTTPException(404, "Не найдено")
    q.emp_id = None; q.holder_text = ""; q.workplace = ""; q.assign_type = ""; q.status = "stock"
    db.commit()
    return {"equipment": eq_dict(q)}

@app.get("/api/equipment/{qid}/barcode.svg")
def eq_barcode(qid: str, db: Session = Depends(get_db), u: User = Depends(current_user)):
    q = db.get(Equipment, qid)
    if not q:
        raise HTTPException(404, "Не найдено")
    # Code128 кодирует только ASCII: инв.№ (порядковый номер) всегда безопасен,
    # на всякий случай отсекаем не-ASCII и откатываемся на ID.
    value = re.sub(r"[^\x20-\x7e]", "", (q.invno or "")).strip()
    if not value:
        value = re.sub(r"[^\x20-\x7e]", "", q.id).strip() or "NA"
    buf = io.BytesIO()
    barcode.get("code128", value, writer=SVGWriter()).write(
        buf, options={"write_text": False, "module_height": 9.0, "module_width": 0.28, "quiet_zone": 1.0})
    return Response(buf.getvalue(), media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store"})

# ---------- acts
@app.post("/api/acts")
def act_create(body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    emp = db.get(Employee, body.get("empId", ""))
    if not emp:
        raise HTTPException(400, "Выберите сотрудника")
    ids = body.get("itemIds") or []
    items = [db.get(Equipment, i) for i in ids]
    items = [i for i in items if i]
    if not items:
        raise HTTPException(400, "Нет позиций")
    kind = "return" if body.get("kind") == "return" else "issue"
    for q in items:
        if kind == "issue":
            q.emp_id = emp.id; q.status = "issued"; q.holder_text = ""; q.assign_type = "employee"
        else:
            q.emp_id = None; q.status = "stock"; q.workplace = ""; q.assign_type = ""
    act = _make_act(db, kind, emp, items, u.username, date=body.get("date"))
    db.commit()
    return act_dict(act)

@app.delete("/api/acts/{aid}")
def act_delete(aid: int, db: Session = Depends(get_db), u: User = Depends(current_user)):
    a = db.get(Act, aid)
    if a:
        db.delete(a); db.commit()
    return {"ok": True}

def _blank(v, width=12):
    v = (v or "").strip()
    return v if v else "_" * width

def build_act_docx(org: dict, emp, act) -> bytes:
    from docx import Document
    from docx.shared import Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_TABLE_ALIGNMENT

    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"; normal.font.size = Pt(11)
    is_ret = act.kind == "return"
    day, mon, yr = _date_parts(act.date)
    fio = (emp.full_name if emp else "") or (act.emp_name or "")

    p = doc.add_paragraph(); r = p.add_run(f"{org.get('name','')}" + (f", ИНН {org.get('inn','')}" if org.get("inn") else "")); r.bold = True
    if org.get("address"):
        doc.add_paragraph(org["address"])
    for line in [f"Акт № {act_number(act)}", "приема-передачи", "материальных ценностей работнику" + (" (возврат)" if is_ret else "")]:
        tp = doc.add_paragraph(); tp.alignment = WD_ALIGN_PARAGRAPH.CENTER; tr = tp.add_run(line); tr.bold = True
    cp = doc.add_paragraph(); cp.add_run(f"г. {org.get('city','')}"); cp.add_run("\t\t\t\t"); cp.add_run(f"«{day}» {mon} {yr} г.")

    pos = f", {emp.position}" if (emp and emp.position) else ""
    parties = (f"{org.get('name','')}, именуемый в дальнейшем «Работодатель», в лице предпринимателя "
               f"{org.get('signatory','')}, действующего на основании {org.get('basis','')}, с одной стороны и "
               f"{fio}{pos}, паспорт: {_blank(emp.passport if emp else '')}, выдан: {_blank(emp.passport_issued if emp else '')}, "
               f"Код подразделения: {_blank(emp.passport_code if emp else '', 8)}, именуемый в дальнейшем «Работник», "
               f"с другой стороны, совместно именуемые в дальнейшем «Стороны», "
               + ("" if is_ret else "руководствуясь ст. 244 Трудового кодекса Российской Федерации, ")
               + "составили настоящий Акт о нижеследующем:")
    doc.add_paragraph(parties)

    if is_ret:
        doc.add_paragraph("1. Работник возвратил, а Работодатель принял следующие материальные ценности:")
    else:
        cd = f"от {emp.contract_date}" if (emp and emp.contract_date) else "от «____» ______________ ______ г."
        doc.add_paragraph(f"1. В соответствии с Трудовым договором {cd} № {_blank(emp.contract_no if emp else '', 6)} "
                          f"Работодатель передал, а Работник принял следующие материальные ценности для выполнения "
                          f"своих должностных обязанностей:")

    tbl = doc.add_table(rows=1, cols=6); tbl.style = "Table Grid"; tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, t in enumerate(["N п/п", "Наименование материальных ценностей", "Серийный номер",
                           "Единица измерения", "Количество", "Стоимость, руб."]):
        tbl.rows[0].cells[i].text = t
        tbl.rows[0].cells[i].paragraphs[0].runs[0].bold = True
    total = 0
    for i, it in enumerate(act.items or [], 1):
        total += _cost_num(it.get("cost"))
        nm = it.get("name", "")
        if it.get("invno"):
            nm = f"{nm} (инв. № {it['invno']})"
        cells = tbl.add_row().cells
        for j, v in enumerate([f"{i}.", nm, it.get("serial") or "—", "шт.", "1", it.get("cost") or "—"]):
            cells[j].text = str(v)
    fr = tbl.add_row().cells
    fr[0].merge(fr[4])
    fr[0].text = "Итоговая стоимость переданных материальных ценностей:"
    fr[0].paragraphs[0].runs[0].bold = True
    fr[5].text = (f"{total:,}".replace(",", " ") if total else "—")
    fr[5].paragraphs[0].runs[0].bold = True

    doc.add_paragraph("2. Материальные ценности проверены и просчитаны в присутствии Сторон. "
                      "Замечания: (при наличии) – передано в новом состоянии.")
    doc.add_paragraph("3. Настоящий Акт составлен в двух экземплярах, имеющих равную юридическую силу, "
                      "по одному для каждой Стороны.")
    doc.add_paragraph("4. Подписи Сторон:")
    doc.add_paragraph("")
    doc.add_paragraph(f"Работодатель: ____________ / {org.get('signatoryShort','')}   (Ф.И.О./подпись)")
    doc.add_paragraph(f"Работник: ____________ / {fio or '__________'}   (Ф.И.О./подпись)")

    buf = io.BytesIO(); doc.save(buf); return buf.getvalue()

@app.get("/api/acts/{aid}/docx")
def act_docx(aid: int, db: Session = Depends(get_db), u: User = Depends(current_user)):
    a = db.get(Act, aid)
    if not a:
        raise HTTPException(404, "Не найдено")
    org_s = db.get(Setting, "org")
    org = org_s.value if org_s else DEFAULT_ORG
    emp = db.get(Employee, a.emp_id) if a.emp_id else None
    data = build_act_docx(org, emp, a)
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": f'attachment; filename="akt-{a.no}.docx"'})

# ---------- settings / data
@app.put("/api/settings/org")
def org_update(body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    s = db.get(Setting, "org") or Setting(key="org", value={})
    s.value = {k: (body.get(k, "") or "") for k in ORG_KEYS}
    db.add(s); db.commit()
    return s.value

@app.get("/api/export")
def export_all(db: Session = Depends(get_db), u: User = Depends(current_user)):
    org = db.get(Setting, "org")
    payload = {
        "org": org.value if org else DEFAULT_ORG,
        "employees": [emp_dict(e) for e in db.scalars(select(Employee)).all()],
        "equipment": [eq_dict(e) for e in db.scalars(select(Equipment)).all()],
        "acts": [act_dict(a) for a in db.scalars(select(Act)).all()],
        "suppliers": [supplier_dict(s) for s in db.scalars(select(Supplier)).all()],
        "tsd": [tsd_dict(t) for t in db.scalars(select(Tsd)).all()],
        "locations": [location_dict(l) for l in db.scalars(select(Location)).all()],
        "notifications": [notification_dict(n) for n in db.scalars(select(Notification)).all()],
        "printers": [printer_dict(p) for p in db.scalars(select(Printer)).all()],
        "cartridges": [cartridge_dict(cg, _printers_map(db)) for cg in db.scalars(select(Cartridge)).all()],
    }
    fn = "inventory-" + datetime.date.today().strftime("%d-%m-%Y") + ".json"
    return JSONResponse(payload, headers={"Content-Disposition": f'attachment; filename="{fn}"'})

def _holder_label(e: Equipment, emp_by_id: dict) -> str:
    if e.emp_id:
        emp = emp_by_id.get(e.emp_id)
        return emp.full_name if emp else e.emp_id
    if e.assign_type == "workplace" and e.workplace:
        return "РМ: " + e.workplace
    return e.holder_text or ""

EXPORT_HEADERS = ["Тип", "Наименование", "Внутр. наименование", "Инв. №", "S/N", "Статус",
                  "Сотрудник / место", "Логин", "E-mail", "Рабочее место", "Локация",
                  "Поставщик", "Заказ / дата покупки", "Стоимость, руб.", "Примечание"]

def _equipment_rows(db: Session):
    emps = {e.id: e for e in db.scalars(select(Employee)).all()}
    sups = {s.id: s for s in db.scalars(select(Supplier)).all()}
    for e in db.scalars(select(Equipment)).all():
        emp = emps.get(e.emp_id) if e.emp_id else None
        sup = sups.get(e.supplier_id) if e.supplier_id else None
        yield [e.type, e.name, e.inner_name, e.invno, e.serial, STATUS_RU.get(e.status, e.status),
               _holder_label(e, emps), (emp.login if emp else ""), (emp.email if emp else ""),
               e.workplace, e.location, (sup.name if sup else ""), e.purchase, e.cost, e.note]

@app.get("/api/export/equipment.xlsx")
def export_xlsx(db: Session = Depends(get_db), u: User = Depends(current_user)):
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Техника"
    ws.append(EXPORT_HEADERS)
    for row in _equipment_rows(db):
        ws.append(row)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="2563EB")
        c.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    for i, w in enumerate([14, 34, 28, 10, 16, 12, 22, 18, 26, 20, 16, 22, 26, 14, 24], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    if ws.max_row > 1:
        ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO(); wb.save(buf)
    fn = "technika-" + datetime.date.today().strftime("%d-%m-%Y") + ".xlsx"
    return Response(buf.getvalue(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{fn}"'})

# ---------- массовая загрузка техники из XLSX ----------
# Один лист = один тип техники (по названию листа), одна строка = одна единица
# с данными сотрудника (создаётся/находится по логину → e-mail → ФИО).
XLSX_SHEET_TYPE = {
    "телефоны": "Телефон", "телефон": "Телефон", "смартфоны": "Телефон",
    "ноутбуки": "Ноутбук", "ноутбук": "Ноутбук",
    "пк": "ПК", "компьютеры": "ПК", "десктопы": "ПК",
    "мониторы": "Монитор", "монитор": "Монитор",
    "планшеты": "Планшет", "планшет": "Планшет",
    "аксессуары": "Аксессуар", "аксессуар": "Аксессуар",
}

# Заголовок столбца (нижний регистр, без лишних пробелов) -> внутренний ключ.
XLSX_HEADERS = {
    "логин": "login", "login": "login",
    "имя": "first", "фамилия": "last", "отчество": "middle",
    "фио": "fio", "сотрудник": "fio",
    "e-mail": "email", "email": "email", "почта": "email", "e_mail": "email",
    "внутреннее наименование": "inner", "внутр. наименование": "inner", "внутренее наименование": "inner",
    "наименование": "name", "модель": "name", "наименование / модель": "name",
    "серийный номер": "serial", "серийный №": "serial", "s/n": "serial", "sn": "serial", "серийник": "serial",
    "заказ": "order", "номер заказа": "order", "№ заказа": "order",
    "дата покупки": "pdate", "дата": "pdate",
    "заказ / дата покупки": "purchase", "номер заказа / дата покупки": "purchase",
    "поставщик": "supplier", "поставщики": "supplier",
    "кто выдал": "issued_by", "выдал": "issued_by",
    "цена": "cost", "стоимость": "cost", "стоимость, руб.": "cost",
    "локация": "location", "город": "location", "локация / город": "location",
    "тип": "type", "статус": "status", "примечание": "note",
}


def _xlsx_cell(v):
    """Значение ячейки -> строка; даты форматируем как дд.мм.гггг."""
    if v is None:
        return ""
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.strftime("%d.%m.%Y")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _sheet_type(title: str) -> str:
    t = (title or "").strip()
    return XLSX_SHEET_TYPE.get(t.lower(), t if t in {"ПК", "Ноутбук", "Монитор", "Телефон",
                                                      "Планшет", "Аксессуар", "Прочее"} else "Прочее")


@app.post("/api/import/equipment-xlsx")
def import_equipment_xlsx(body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    """Добавить технику из XLSX по шаблону (дополняет базу, не заменяет).
    Сотрудники и поставщики создаются при необходимости; дубли сотрудников
    не плодятся — поиск по логину, затем e-mail, затем ФИО."""
    import openpyxl
    raw = body.get("dataB64") or ""
    if "," in raw and raw.strip().startswith("data:"):
        raw = raw.split(",", 1)[1]
    try:
        data = base64.b64decode(raw)
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception:
        raise HTTPException(400, "Не удалось прочитать XLSX-файл")

    # индексы существующих сотрудников/поставщиков
    emps = db.scalars(select(Employee)).all()
    by_login = {e.login.strip().lower(): e for e in emps if e.login}
    by_email = {e.email.strip().lower(): e for e in emps if e.email}
    by_fio = {e.full_name.strip().lower(): e for e in emps if e.full_name}
    sups = db.scalars(select(Supplier)).all()
    by_sup = {s.name.strip().lower(): s for s in sups if s.name}
    # индекс существующей техники для обновления (без дублей): по S/N, затем по
    # внутреннему наименованию
    eqs_all = db.scalars(select(Equipment)).all()
    by_serial = {e.serial.strip().lower(): e for e in eqs_all if (e.serial or "").strip()}
    by_inner = {e.inner_name.strip().lower(): e for e in eqs_all if (e.inner_name or "").strip()}

    n_new = n_upd = n_emp = n_sup = 0
    errors = []

    def get_supplier(name):
        nonlocal n_sup
        key = name.strip().lower()
        if not key:
            return None
        s = by_sup.get(key)
        if not s:
            s = Supplier(id="sup-" + secrets.token_hex(5), name=name.strip())
            db.add(s); db.flush(); by_sup[key] = s; n_sup += 1
        return s

    def get_employee(row):
        nonlocal n_emp
        login = row.get("login", ""); email = row.get("email", "")
        last = row.get("last", ""); first = row.get("first", ""); middle = row.get("middle", "")
        if not any([last, first, middle]) and row.get("fio"):
            last, first, middle = split_fio(row["fio"])
        if not any([login, email, last, first, middle]):
            return None  # строки без сотрудника -> техника на склад
        e = (by_login.get(login.lower()) if login else None) \
            or (by_email.get(email.lower()) if email else None) \
            or (by_fio.get(join_fio(last, first, middle).lower()) if (last or first or middle) else None)
        if e:
            return e
        e = Employee(id="emp-" + secrets.token_hex(5), last_name=last, first_name=first,
                     middle_name=middle, full_name=join_fio(last, first, middle),
                     login=login, email=email)
        db.add(e); db.flush()
        n_emp += 1
        if login: by_login[login.lower()] = e
        if email: by_email[email.lower()] = e
        if e.full_name: by_fio[e.full_name.lower()] = e
        return e

    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        try:
            header = next(rows)
        except StopIteration:
            continue
        cols = {}  # индекс столбца -> ключ
        for i, h in enumerate(header):
            key = XLSX_HEADERS.get(_xlsx_cell(h).lower())
            if key:
                cols[i] = key
        if "name" not in cols.values():
            errors.append(f"Лист «{ws.title}»: не найден столбец НАИМЕНОВАНИЕ — пропущен")
            continue
        sheet_type = _sheet_type(ws.title)
        rnum = 1
        for raw_row in rows:
            rnum += 1
            row = {}
            for i, key in cols.items():
                if i < len(raw_row):
                    row[key] = _xlsx_cell(raw_row[i])
            if not any(row.values()):
                continue
            name = row.get("name", "")
            if not name:
                errors.append(f"«{ws.title}», строка {rnum}: пустое наименование — пропущена")
                continue
            emp = get_employee(row)
            sup = get_supplier(row.get("supplier", "")) if row.get("supplier") else None
            purchase = row.get("purchase", "")
            if not purchase:
                order = row.get("order", ""); pdate = row.get("pdate", "")
                purchase = f"{order} от {pdate}" if order and pdate else (order or pdate)
            note = row.get("note", "")
            if row.get("issued_by"):
                note = (note + "; " if note else "") + "Выдал: " + row["issued_by"]
            serial = row.get("serial", ""); inner = row.get("inner", "")
            # поиск существующей единицы: по S/N, затем по внутр. наименованию
            q = (by_serial.get(serial.strip().lower()) if serial.strip() else None) \
                or (by_inner.get(inner.strip().lower()) if inner.strip() else None)
            creating = q is None
            if creating:
                q = Equipment(id="eq-" + secrets.token_hex(5), invno=_next_inv(db),
                              type=sheet_type, status="stock")
                db.add(q)
            # обновляем поля; пустые ячейки не затирают уже заполненные значения
            if row.get("type"):
                q.type = row["type"]
            q.name = name
            if inner:
                q.inner_name = inner
            if serial:
                q.serial = serial
            if purchase:
                q.purchase = purchase
            if row.get("cost"):
                q.cost = row["cost"]
            if row.get("location"):
                q.location = row["location"]
            if note:
                q.note = note
            if sup:
                q.supplier_id = sup.id
            if emp:
                q.emp_id = emp.id; q.assign_type = "employee"; q.status = "issued"
            db.flush()
            if (q.serial or "").strip():
                by_serial[q.serial.strip().lower()] = q
            if (q.inner_name or "").strip():
                by_inner[q.inner_name.strip().lower()] = q
            n_new += creating
            n_upd += not creating

    db.commit()
    return {"created": n_new, "updated": n_upd, "equipment": n_new + n_upd,
            "employees": n_emp, "suppliers": n_sup, "errors": errors}


@app.get("/api/import/equipment-template.xlsx")
def import_template(db: Session = Depends(get_db), u: User = Depends(current_user)):
    """Пустой шаблон для массовой загрузки техники."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    headers = ["Логин", "Имя", "Фамилия", "Отчество", "E-Mail", "Внутреннее наименование",
               "НАИМЕНОВАНИЕ", "Серийный номер", "Заказ", "Дата покупки", "Поставщики",
               "Кто выдал", "Цена", "Локация"]
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Телефоны"
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="2563EB")
        c.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    for i, w in enumerate([16, 14, 16, 16, 24, 26, 26, 18, 16, 14, 18, 18, 12, 16], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    buf = io.BytesIO(); wb.save(buf)
    return Response(buf.getvalue(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="shablon-zagruzki.xlsx"'})


@app.post("/api/wipe")
def wipe(body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    """Очистка базы (техника, сотрудники, акты) — только после подтверждения паролем.
    Пользователи и реквизиты организации сохраняются."""
    if not verify_pw(body.get("password", ""), u.password_hash):
        raise HTTPException(400, "Неверный пароль")
    db.query(Equipment).delete()
    db.query(Act).delete()
    db.query(Employee).delete()
    db.query(Cartridge).delete()
    db.query(Printer).delete()
    db.query(Supplier).delete()
    db.query(Tsd).delete()
    db.query(Notification).delete()
    db.query(Location).delete()
    seq = db.get(Setting, "seq")
    if seq:
        seq.value = {"act": 0, "inv": 1}
    db.commit()
    return {"ok": True}

@app.post("/api/import")
def import_all(body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    """Полная замена данных (техника/сотрудники/акты/реквизиты). Пользователи не затрагиваются."""
    if "equipment" not in body or "employees" not in body:
        raise HTTPException(400, "Неверный формат файла")
    db.query(Equipment).delete()
    db.query(Act).delete()
    db.query(Employee).delete()
    db.query(Cartridge).delete()
    db.query(Printer).delete()
    db.query(Supplier).delete()
    db.query(Tsd).delete()
    db.query(Notification).delete()
    db.query(Location).delete()
    for l in body.get("locations", []):
        if l.get("id"):
            db.add(Location(id=l["id"], name=l.get("name", ""), note=l.get("note", "")))
    for n in body.get("notifications", []):
        if n.get("id"):
            db.add(Notification(id=n["id"], name=n.get("name", ""), telegram_chat_id=n.get("telegramChatId", ""),
                                email=n.get("email", ""), enabled=1 if n.get("enabled", True) else 0, note=n.get("note", "")))
    for s in body.get("suppliers", []):
        if s.get("id"):
            db.add(Supplier(id=s["id"], name=s.get("name", ""), contact=s.get("contact", ""),
                            phone=s.get("phone", ""), email=s.get("email", ""), note=s.get("note", "")))
    for t in body.get("tsd", []):
        if t.get("id"):
            db.add(Tsd(id=t["id"], inv_no=t.get("invNo", ""), name=t.get("name", ""),
                       inner_no=t.get("innerNo", ""), location=t.get("location", ""), note=t.get("note", "")))
    supplier_ids = {s.get("id") for s in body.get("suppliers", []) if s.get("id")}
    for p in body.get("printers", []):
        if p.get("id"):
            db.add(Printer(id=p["id"], name=p.get("name", ""), model=p.get("model", ""),
                           location=p.get("location", ""), inv_no=p.get("invNo", ""),
                           notify_id=p.get("notifyId") or None, note=p.get("note", "")))
    printer_ids = {p.get("id") for p in body.get("printers", []) if p.get("id")}
    for cg in body.get("cartridges", []):
        if not cg.get("id"):
            continue
        pid = cg.get("printerId") or None
        if pid and pid not in printer_ids:
            pid = None
        db.add(Cartridge(id=cg["id"], name=cg.get("name", ""), printer_id=pid,
                         stock=int(cg.get("stock") or 0), min_stock=int(cg.get("minStock") or 0),
                         alerted=1 if (int(cg.get("stock") or 0) <= int(cg.get("minStock") or 0)) else 0,
                         note=cg.get("note", "")))
    seen = set()
    for e in body.get("employees", []):
        if not e.get("id") or e["id"] in seen:
            continue
        seen.add(e["id"])
        db.add(Employee(id=e["id"], **emp_name_fields(e), login=e.get("login", ""),
                        email=e.get("email", ""), dept=e.get("dept", ""),
                        position=e.get("position", ""), location=e.get("location", ""),
                        passport=e.get("passport", ""), passport_issued=e.get("passportIssued", ""),
                        passport_code=e.get("passportCode", ""), contract_no=e.get("contractNo", ""),
                        contract_date=e.get("contractDate", "")))
    emp_ids = seen
    for q in body.get("equipment", []):
        emp = q.get("empId") or None
        if emp and emp not in emp_ids:
            emp = None
        sup = q.get("supplierId") or None
        if sup and sup not in supplier_ids:
            sup = None
        db.add(Equipment(id=q.get("id") or ("eq-" + secrets.token_hex(5)), type=q.get("type", "Прочее"),
                         name=q.get("name", ""), inner_name=q.get("innerName", ""),
                         invno=q.get("invno", ""), serial=q.get("serial", ""),
                         purchase=q.get("purchase", ""), status=q.get("status", "stock"), emp_id=emp,
                         holder_text=q.get("holderText", ""), workplace=q.get("workplace", ""),
                         assign_type=q.get("assignType") or ("employee" if emp else ""),
                         cost=q.get("cost", ""), supplier_id=sup,
                         location=q.get("location", ""), note=q.get("note", "")))
    maxno = 0
    for a in body.get("acts", []):
        maxno = max(maxno, int(a.get("no", 0) or 0))
        db.add(Act(no=a.get("no", 0), kind=a.get("kind", "issue"), date=a.get("date", ""),
                   emp_id=(a.get("empId") or None) if (a.get("empId") in emp_ids) else None,
                   emp_name=a.get("empName", ""), items=a.get("items", []), created_by=a.get("createdBy", "")))
    if "org" in body and isinstance(body["org"], dict):
        s = db.get(Setting, "org") or Setting(key="org", value={})
        s.value = body["org"]; db.add(s)
    maxinv = 0
    for q in body.get("equipment", []):
        m = re.fullmatch(r"\d+", str(q.get("invno", "")).strip())
        if m:
            maxinv = max(maxinv, int(m.group()))
    seq = db.get(Setting, "seq") or Setting(key="seq", value={"act": 0, "inv": 1})
    seq.value = {"act": maxno, "inv": int(body.get("nextInv") or (maxinv + 1))}; db.add(seq)
    db.commit()
    return {"ok": True}

# ---------- static UI
@app.get("/api/version")
def version():
    return {"version": APP_VERSION}

@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")

app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
