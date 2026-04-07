import io
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pypdf import PdfReader, PdfWriter, Transformation
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "data.db"
PDF_DIR = DATA_DIR / "generated_pdfs"
PDF_DIR.mkdir(parents=True, exist_ok=True)

ALMATY_TZ = ZoneInfo("Asia/Almaty")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "-5103186317"))

DELETE_MARKERS = {"удалить", "delete", "ошибка"}

app = FastAPI()
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ================= DB =================

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS postings (
            source_id TEXT PRIMARY KEY,
            product_name TEXT,
            pdf_file TEXT,
            status TEXT DEFAULT 'new',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            source_file_name TEXT,
            sender_name TEXT,
            telegram_message_id INTEGER,
            caption_text TEXT,
            telegram_date INTEGER,
            telegram_local_time TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    conn.commit()
    conn.close()


@app.on_event("startup")
def startup():
    init_db()


# ================= TELEGRAM =================

def tg_api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"


def tg_file_url(file_path: str) -> str:
    return f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"


def get_last_update_id():
    conn = db()
    row = conn.execute("SELECT value FROM settings WHERE key='last_update_id'").fetchone()
    conn.close()
    return int(row["value"]) if row and row["value"] else None


def set_last_update_id(update_id):
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key,value) VALUES ('last_update_id',?)",
        (str(update_id),)
    )
    conn.commit()
    conn.close()


def get_updates():
    last_update_id = get_last_update_id()

    params = {}
    if last_update_id is not None:
        params["offset"] = last_update_id + 1

    r = requests.get(tg_api_url("getUpdates"), params=params, timeout=60)
    r.raise_for_status()
    data = r.json()

    updates = data.get("result", [])

    if updates:
        max_id = max(u["update_id"] for u in updates)
        set_last_update_id(max_id)

    return updates


def get_file_path(file_id):
    r = requests.get(tg_api_url("getFile"), params={"file_id": file_id})
    return r.json()["result"]["file_path"]


def download_file(file_path):
    r = requests.get(tg_file_url(file_path))
    return r.content


# ================= PDF =================

def get_cyrillic_font_name():
    font_path = BASE_DIR / "fonts" / "DejaVuSans.ttf"
    font_name = "DejaVuSans"

    if not font_path.exists():
        return "Helvetica"

    pdfmetrics.registerFont(TTFont(font_name, str(font_path)))
    return font_name


def make_back_page(text, source_id, time):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=landscape(A4))

    font = get_cyrillic_font_name()
    c.setFont(font, 24)

    c.drawString(50, 400, text[:120])
    c.drawString(50, 350, f"Время: {time}")
    c.drawString(50, 300, f"ID: {source_id}")

    c.showPage()
    c.save()
    return buf.getvalue()


def merge(front, back):
    writer = PdfWriter()

    r1 = PdfReader(io.BytesIO(front))
    r2 = PdfReader(io.BytesIO(back))

    writer.add_page(r1.pages[0])
    writer.add_page(r2.pages[0])

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def build_pdf(file_id, caption, source_id, time):
    file_path = get_file_path(file_id)
    raw = download_file(file_path)

    back = make_back_page(caption, source_id, time)
    return merge(raw, back)


# ================= LOGIC =================

def is_delete_caption(text):
    return (text or "").lower() in DELETE_MARKERS


def save_new(item):
    pdf = build_pdf(
        item["file_id"],
        item["caption"],
        item["source_id"],
        item["time"]
    )

    filename = f'{item["source_id"]}.pdf'
    (PDF_DIR / filename).write_bytes(pdf)

    conn = db()
    conn.execute("""
        INSERT OR REPLACE INTO postings
        VALUES (?, ?, ?, 'new', CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?)
    """, (
        item["source_id"],
        item["caption"],
        filename,
        item["file_name"],
        item["sender"],
        item["message_id"],
        item["caption"],
        item["date"],
        item["time"]
    ))
    conn.commit()
    conn.close()


def update_existing(existing, item):
    if existing["status"] == "printed":
        return

    new_caption = item["caption"]
    old_caption = existing["caption_text"] or ""

    if is_delete_caption(new_caption):
        conn = db()
        conn.execute("UPDATE postings SET status='deleted' WHERE source_id=?", (existing["source_id"],))
        conn.commit()
        conn.close()
        return

    if new_caption.strip() == old_caption.strip():
        return

    pdf = build_pdf(item["file_id"], new_caption, existing["source_id"], item["time"])
    (PDF_DIR / existing["pdf_file"]).write_bytes(pdf)

    conn = db()
    conn.execute("""
        UPDATE postings
        SET product_name=?, caption_text=?, status='new'
        WHERE source_id=?
    """, (new_caption, new_caption, existing["source_id"]))
    conn.commit()
    conn.close()


def extract_items():
    updates = get_updates()
    items = []

    for u in updates:
        msg = u.get("message") or u.get("edited_message")
        if not msg:
            continue

        if msg["chat"]["id"] != TELEGRAM_CHAT_ID:
            continue

        doc = msg.get("document")
        if not doc:
            continue

        items.append({
            "source_id": f'tg_{msg["message_id"]}',
            "message_id": msg["message_id"],
            "file_id": doc["file_id"],
            "file_name": doc.get("file_name", ""),
            "caption": msg.get("caption", ""),
            "sender": msg["from"].get("first_name", ""),
            "date": msg["date"],
            "time": datetime.fromtimestamp(msg["date"], tz=ALMATY_TZ).strftime("%d.%m %H:%M"),
        })

    return items


# ================= ROUTES =================

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    conn = db()
    rows = conn.execute("""
        SELECT * FROM postings
        WHERE status NOT IN ('printed','deleted')
        ORDER BY created_at DESC
    """).fetchall()
    conn.close()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "rows": rows
    })


@app.post("/sync")
def sync():
    items = extract_items()

    conn = db()
    existing = {r["source_id"]: r for r in conn.execute("SELECT * FROM postings")}
    conn.close()

    for item in items:
        if item["source_id"] in existing:
            update_existing(existing[item["source_id"]], item)
        else:
            save_new(item)

    return RedirectResponse("/", status_code=303)


@app.post("/mark-printed/{id}")
def mark_printed(id: str):
    conn = db()
    conn.execute("UPDATE postings SET status='printed' WHERE source_id=?", (id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/", status_code=303)


@app.get("/pdf/{id}")
def pdf(id: str):
    conn = db()
    row = conn.execute("SELECT pdf_file FROM postings WHERE source_id=?", (id,)).fetchone()
    conn.close()

    file_path = PDF_DIR / row["pdf_file"]

    return StreamingResponse(
        io.BytesIO(file_path.read_bytes()),
        media_type="application/pdf"
    )