"""
Учёт техники — backend (FastAPI + PostgreSQL).
Авторизация по сессионной cookie, REST API, отдача статического UI.
"""
import os, io, re, json, hashlib, hmac, secrets, datetime, pathlib
from typing import Optional, List

import barcode
from barcode.writer import SVGWriter

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
APP_VERSION = "1.1.0"
BASE = pathlib.Path(__file__).parent
DB_URL = os.environ.get("DATABASE_URL", "postgresql+psycopg2://inv:inv@db:5432/inventory")
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-please-" + secrets.token_hex(8))
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
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

class Employee(Base):
    __tablename__ = "employees"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200), default="")
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
    note: Mapped[str] = mapped_column(Text, default="")

class Printer(Base):
    __tablename__ = "printers"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    model: Mapped[str] = mapped_column(String(200), default="")
    location: Mapped[str] = mapped_column(String(128), default="")
    inv_no: Mapped[str] = mapped_column(String(64), default="")
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

# ---------------------------------------------------------------- serialization
def emp_dict(e: Employee):
    return {"id": e.id, "fullName": e.full_name, "login": e.login, "email": e.email,
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
    return {"id": t.id, "invNo": t.inv_no, "name": t.name, "innerNo": t.inner_no, "note": t.note}

def printer_dict(p: Printer):
    return {"id": p.id, "name": p.name, "model": p.model, "location": p.location,
            "invNo": p.inv_no, "note": p.note}

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

def send_telegram(cfg: dict, text: str):
    import urllib.request, urllib.parse
    token, chat = cfg.get("telegram_token"), cfg.get("telegram_chat_id")
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

def send_email(cfg: dict, subject: str, text: str):
    import smtplib
    from email.message import EmailMessage
    host = cfg.get("smtp_host")
    if not host:
        return False, "не задан SMTP host"
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = cfg.get("smtp_from") or cfg.get("smtp_user") or ""
        msg["To"] = cfg.get("email_to") or ""
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

def _notify_low(cfg: dict, cg, printer_name: str):
    text = ("⚠️ Низкий остаток картриджа\n"
            f"Картридж: {cg.name}\n"
            + (f"Принтер: {printer_name}\n" if printer_name else "")
            + f"Остаток: {cg.stock} (минимум: {cg.min_stock})")
    out = []
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
    res = _notify_low(cfg, cg, pr.name if pr else "")
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
        # inventory seed
        if db.scalar(select(func.count(Employee.id))) == 0:
            seed_path = BASE / "seed_data.json"
            if seed_path.exists():
                data = json.loads(seed_path.read_text(encoding="utf-8"))
                seen = set()
                for e in data.get("employees", []):
                    if e["id"] in seen:
                        continue
                    seen.add(e["id"])
                    db.add(Employee(id=e["id"], full_name=e.get("fullName", ""), login=e.get("login", ""),
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
app = FastAPI(title="Учёт техники")
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

@app.post("/api/login")
def login(body: LoginBody, request: Request, db: Session = Depends(get_db)):
    u = db.scalar(select(User).where(User.username == body.username))
    if not u or not verify_pw(body.password, u.password_hash):
        raise HTTPException(401, "Неверный логин или пароль")
    request.session["uid"] = u.id
    return {"username": u.username, "fullName": u.full_name, "role": u.role}

@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}

@app.get("/api/me")
def me(u: User = Depends(current_user)):
    return {"username": u.username, "fullName": u.full_name, "role": u.role}

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
    return {
        "version": APP_VERSION,
        "org": org.value if org else DEFAULT_ORG,
        "user": {"username": u.username, "fullName": u.full_name},
        "employees": [emp_dict(e) for e in db.scalars(select(Employee)).all()],
        "equipment": [eq_dict(e) for e in db.scalars(select(Equipment)).all()],
        "acts": [act_dict(a) for a in db.scalars(select(Act)).all()],
        "suppliers": [supplier_dict(s) for s in db.scalars(select(Supplier)).all()],
        "tsd": [tsd_dict(t) for t in db.scalars(select(Tsd)).all()],
        "printers": [printer_dict(p) for p in db.scalars(select(Printer)).all()],
        "cartridges": [cartridge_dict(cg, {p.id: p for p in db.scalars(select(Printer)).all()})
                       for cg in db.scalars(select(Cartridge)).all()],
        "notify": _notify_masked(_get_notify(db)),
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
EMP_FIELDS = [("fullName", "full_name"), ("login", "login"), ("email", "email"),
              ("dept", "dept"), ("position", "position"), ("location", "location"),
              ("passport", "passport"), ("passportIssued", "passport_issued"),
              ("passportCode", "passport_code"), ("contractNo", "contract_no"),
              ("contractDate", "contract_date")]

@app.post("/api/employees")
def emp_create(body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    e = Employee(id="emp-" + secrets.token_hex(5))
    for j, c in EMP_FIELDS:
        if j in body:
            setattr(e, c, (body[j] or "").strip())
    if not e.full_name:
        raise HTTPException(400, "Укажите ФИО")
    db.add(e); db.commit()
    return emp_dict(e)

@app.put("/api/employees/{eid}")
def emp_update(eid: str, body: dict, db: Session = Depends(get_db), u: User = Depends(current_user)):
    e = db.get(Employee, eid)
    if not e:
        raise HTTPException(404, "Не найдено")
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
TSD_FIELDS = [("invNo", "inv_no"), ("name", "name"), ("innerNo", "inner_no"), ("note", "note")]

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

def _make_act(db: Session, kind: str, emp: Employee, items: List[Equipment], who: str) -> Act:
    a = Act(no=_next_act_no(db), kind=kind, date=datetime.date.today().strftime("%d.%m.%Y"),
            emp_id=emp.id if emp else None, emp_name=emp.full_name if emp else "",
            items=[{"name": i.name, "type": i.type, "invno": i.invno, "serial": i.serial,
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
            act = _make_act(db, "issue", emp, [q], u.username)
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
    act = _make_act(db, kind, emp, items, u.username)
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
    for line in [f"Акт № {act.no}", "приема-передачи", "материальных ценностей работнику" + (" (возврат)" if is_ret else "")]:
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
    for s in body.get("suppliers", []):
        if s.get("id"):
            db.add(Supplier(id=s["id"], name=s.get("name", ""), contact=s.get("contact", ""),
                            phone=s.get("phone", ""), email=s.get("email", ""), note=s.get("note", "")))
    for t in body.get("tsd", []):
        if t.get("id"):
            db.add(Tsd(id=t["id"], inv_no=t.get("invNo", ""), name=t.get("name", ""),
                       inner_no=t.get("innerNo", ""), note=t.get("note", "")))
    supplier_ids = {s.get("id") for s in body.get("suppliers", []) if s.get("id")}
    for p in body.get("printers", []):
        if p.get("id"):
            db.add(Printer(id=p["id"], name=p.get("name", ""), model=p.get("model", ""),
                           location=p.get("location", ""), inv_no=p.get("invNo", ""), note=p.get("note", "")))
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
        db.add(Employee(id=e["id"], full_name=e.get("fullName", ""), login=e.get("login", ""),
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
