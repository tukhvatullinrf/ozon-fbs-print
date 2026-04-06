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

# ВСТАВЬ СВОЙ ТОКЕН СЮДА
import os

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "-5103186317"))

DELETE_MARKERS = {"удалить", "delete", "ошибка"}

app = FastAPI()
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


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
    conn.commit()

    columns = [row["name"] for row in conn.execute("PRAGMA table_info(postings)").fetchall()]
    migrations = [
        ("source_file_name", "ALTER TABLE postings ADD COLUMN source_file_name TEXT"),
        ("sender_name", "ALTER TABLE postings ADD COLUMN sender_name TEXT"),
        ("telegram_message_id", "ALTER TABLE postings ADD COLUMN telegram_message_id INTEGER"),
        ("caption_text", "ALTER TABLE postings ADD COLUMN caption_text TEXT"),
        ("created_at", "ALTER TABLE postings ADD COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP"),
        ("telegram_date", "ALTER TABLE postings ADD COLUMN telegram_date INTEGER"),
        ("telegram_local_time", "ALTER TABLE postings ADD COLUMN telegram_local_time TEXT"),
    ]
    for column_name, sql in migrations:
        if column_name not in columns:
            conn.execute(sql)

    conn.commit()
    conn.close()


@app.on_event("startup")
def startup():
    init_db()


def tg_api_url(method: str) -> str:
    if not TELEGRAM_BOT_TOKEN or "ВСТАВЬ_СЮДА" in TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=500, detail="Заполни TELEGRAM_BOT_TOKEN в app.py")
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"


def tg_file_url(file_path: str) -> str:
    return f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"


def get_updates():
    r = requests.get(tg_api_url("getUpdates"), timeout=60)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise HTTPException(status_code=500, detail=f"Telegram getUpdates error: {data}")
    return data.get("result", [])


def get_file_path(file_id: str) -> str:
    r = requests.get(tg_api_url("getFile"), params={"file_id": file_id}, timeout=60)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise HTTPException(status_code=500, detail=f"Telegram getFile error: {data}")
    result = data.get("result", {})
    file_path = result.get("file_path")
    if not file_path:
        raise HTTPException(status_code=500, detail=f"Telegram did not return file_path: {data}")
    return file_path


def download_file(file_path: str) -> bytes:
    r = requests.get(tg_file_url(file_path), timeout=60)
    r.raise_for_status()
    return r.content


def get_cyrillic_font_name():
    font_path = BASE_DIR / "fonts" / "DejaVuSans.ttf"
    font_name = "DejaVuSans"

    if not font_path.exists():
        return "Helvetica"

    try:
        pdfmetrics.registerFont(TTFont(font_name, str(font_path)))
        return font_name
    except Exception:
        return "Helvetica"


def wrap_text(text: str, max_width: float, font_name: str, font_size: int):
    words = text.split()
    lines = []
    current = ""

    for word in words:
        test = word if not current else current + " " + word
        if stringWidth(test, font_name, font_size) <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    return lines


def telegram_ts_to_almaty_str(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=ALMATY_TZ)
    return dt.strftime("%d.%m.%Y %H:%M")


def is_delete_caption(caption: str) -> bool:
    normalized = (caption or "").strip().lower()
    return normalized in DELETE_MARKERS or normalized == ""


def fit_label_pdf_to_landscape_a4(label_pdf_bytes: bytes) -> bytes:
    reader = PdfReader(io.BytesIO(label_pdf_bytes))
    if len(reader.pages) == 0:
        raise HTTPException(status_code=500, detail="Label PDF has no pages")

    src_page = reader.pages[0]
    src_w = float(src_page.mediabox.width)
    src_h = float(src_page.mediabox.height)

    page_w, page_h = landscape(A4)
    scale = min(page_w / src_w, page_h / src_h) * 0.95
    tx = (page_w - src_w * scale) / 2
    ty = (page_h - src_h * scale) / 2

    writer = PdfWriter()
    new_page = writer.add_blank_page(width=page_w, height=page_h)
    new_page.merge_transformed_page(
        src_page,
        Transformation().scale(scale).translate(tx, ty)
    )

    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def make_back_page(product_name: str, source_id: str, telegram_local_time: str) -> bytes:
    buf = io.BytesIO()
    page_size = landscape(A4)
    c = canvas.Canvas(buf, pagesize=page_size)
    page_w, page_h = page_size

    font_name = get_cyrillic_font_name()
    title = (product_name or "Без названия").strip()
    title = " ".join(title.split())

    max_width = page_w - 100
    font_size = 34

    while font_size >= 16:
        lines = wrap_text(title, max_width, font_name, font_size)
        if len(lines) <= 4:
            break
        font_size -= 2

    if font_size < 16:
        font_size = 16
        lines = wrap_text(title, max_width, font_name, font_size)

    line_height = font_size + 10
    total_height = len(lines) * line_height
    start_y = (page_h / 2) + (total_height / 2)

    c.setFont(font_name, font_size)
    y = start_y

    for line in lines[:5]:
        c.drawString(50, y, line[:160])
        y -= line_height

    c.setFont(font_name, 18)
    c.drawString(50, y - 20, f"Время сообщения: {telegram_local_time}")
    c.drawString(50, y - 50, f"Telegram ID: {source_id}")

    c.showPage()
    c.save()
    return buf.getvalue()


def merge_front_and_back(front_pdf: bytes, back_pdf: bytes) -> bytes:
    writer = PdfWriter()
    front_reader = PdfReader(io.BytesIO(front_pdf))
    back_reader = PdfReader(io.BytesIO(back_pdf))

    writer.add_page(front_reader.pages[0])
    writer.add_page(back_reader.pages[0])

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def build_merged_pdf(file_id: str, caption: str, source_id: str, telegram_local_time: str) -> bytes:
    file_path = get_file_path(file_id)
    label_raw = download_file(file_path)
    front_pdf = fit_label_pdf_to_landscape_a4(label_raw)
    back_pdf = make_back_page(caption, source_id, telegram_local_time)
    return merge_front_and_back(front_pdf, back_pdf)


def save_new_posting(
    source_id: str,
    product_name: str,
    pdf_bytes: bytes,
    source_file_name: str,
    sender_name: str,
    telegram_message_id: int,
    telegram_date: int,
    telegram_local_time: str,
):
    safe_name = source_id.replace("/", "_").replace("\\", "_")
    filename = f"{safe_name}.pdf"
    file_path = PDF_DIR / filename
    file_path.write_bytes(pdf_bytes)

    conn = db()
    conn.execute("""
        INSERT OR REPLACE INTO postings (
            source_id, product_name, pdf_file, status, source_file_name, sender_name,
            telegram_message_id, caption_text, telegram_date, telegram_local_time
        )
        VALUES (?, ?, ?, 'new', ?, ?, ?, ?, ?, ?)
    """, (
        source_id, product_name, filename, source_file_name, sender_name,
        telegram_message_id, product_name, telegram_date, telegram_local_time
    ))
    conn.commit()
    conn.close()


def mark_deleted_if_needed(existing_row, item):
    source_id = existing_row["source_id"]
    current_status = existing_row["status"]

    if current_status == "printed":
        return "skipped_printed"

    conn = db()
    conn.execute("""
        UPDATE postings
        SET status = 'deleted',
            caption_text = ?,
            product_name = ?,
            sender_name = ?,
            source_file_name = ?,
            telegram_date = ?,
            telegram_local_time = ?
        WHERE source_id = ?
    """, (
        item["caption"],
        item["caption"] or existing_row["product_name"],
        item["sender_name"],
        item["file_name"],
        item["telegram_date"],
        item["telegram_local_time"],
        source_id
    ))
    conn.commit()
    conn.close()
    return "marked_deleted"


def update_existing_posting_if_needed(existing_row, item):
    source_id = existing_row["source_id"]
    current_status = existing_row["status"]
    old_caption = existing_row["caption_text"] or existing_row["product_name"] or ""
    new_caption = item["caption"]

    if is_delete_caption(new_caption):
        return mark_deleted_if_needed(existing_row, item)

    if current_status == "printed":
        return "skipped_printed"

    metadata_changed = (
        (existing_row["source_file_name"] or "") != item["file_name"] or
        (existing_row["sender_name"] or "") != item["sender_name"] or
        (existing_row["telegram_local_time"] or "") != item["telegram_local_time"] or
        current_status == "deleted"
    )

    if old_caption.strip() == new_caption.strip() and not metadata_changed:
        return "unchanged"

    if old_caption.strip() == new_caption.strip() and metadata_changed:
        conn = db()
        conn.execute("""
            UPDATE postings
            SET source_file_name = ?, sender_name = ?, telegram_date = ?, telegram_local_time = ?, status = 'new'
            WHERE source_id = ?
        """, (
            item["file_name"],
            item["sender_name"],
            item["telegram_date"],
            item["telegram_local_time"],
            source_id
        ))
        conn.commit()
        conn.close()
        return "metadata_updated"

    merged_pdf = build_merged_pdf(
        item["file_id"],
        new_caption,
        source_id,
        item["telegram_local_time"]
    )
    file_name = existing_row["pdf_file"]
    file_path = PDF_DIR / file_name
    file_path.write_bytes(merged_pdf)

    conn = db()
    conn.execute("""
        UPDATE postings
        SET product_name = ?, caption_text = ?, source_file_name = ?, sender_name = ?,
            telegram_date = ?, telegram_local_time = ?, status = 'new'
        WHERE source_id = ?
    """, (
        new_caption,
        new_caption,
        item["file_name"],
        item["sender_name"],
        item["telegram_date"],
        item["telegram_local_time"],
        source_id
    ))
    conn.commit()
    conn.close()
    return "updated"


def extract_items_from_updates():
    updates = get_updates()
    items_by_source_id = {}

    for upd in updates:
        msg = upd.get("edited_message") or upd.get("message") or {}
        if not msg:
            continue

        chat = msg.get("chat", {})
        if chat.get("id") != TELEGRAM_CHAT_ID:
            continue

        document = msg.get("document")
        if not document:
            continue
        if document.get("mime_type") != "application/pdf":
            continue

        message_id = msg.get("message_id")
        if not message_id:
            continue

        sender = msg.get("from", {})
        sender_name = " ".join(filter(None, [
            sender.get("first_name", ""),
            sender.get("last_name", "")
        ])).strip() or sender.get("username", "") or "Unknown"

        telegram_date = msg.get("date")
        if not telegram_date:
            continue

        caption = (msg.get("caption") or "").strip()
        telegram_local_time = telegram_ts_to_almaty_str(telegram_date)
        source_id = f"tg_{message_id}"

        items_by_source_id[source_id] = {
            "source_id": source_id,
            "message_id": message_id,
            "file_id": document.get("file_id"),
            "file_name": document.get("file_name", "label.pdf"),
            "caption": caption,
            "sender_name": sender_name,
            "telegram_date": telegram_date,
            "telegram_local_time": telegram_local_time,
        }

    return list(items_by_source_id.values())


@app.get("/", response_class=HTMLResponse)
def index(request: Request, show: str = "active"):
    conn = db()

    if show == "printed":
        rows = conn.execute("""
            SELECT source_id, product_name, pdf_file, status, created_at, source_file_name,
                   sender_name, telegram_local_time
            FROM postings
            WHERE status = 'printed'
            ORDER BY created_at DESC
        """).fetchall()
    else:
        rows = conn.execute("""
            SELECT source_id, product_name, pdf_file, status, created_at, source_file_name,
                   sender_name, telegram_local_time
            FROM postings
            WHERE status NOT IN ('printed', 'deleted')
            ORDER BY created_at DESC
        """).fetchall()

    active_count = conn.execute("""
        SELECT COUNT(*) AS cnt FROM postings WHERE status NOT IN ('printed', 'deleted')
    """).fetchone()["cnt"]

    printed_count = conn.execute("""
        SELECT COUNT(*) AS cnt FROM postings WHERE status = 'printed'
    """).fetchone()["cnt"]

    conn.close()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "rows": rows,
        "show": show,
        "active_count": active_count,
        "printed_count": printed_count,
    })


@app.post("/sync")
def sync():
    items = extract_items_from_updates()

    conn = db()
    existing_rows = conn.execute("""
        SELECT source_id, product_name, pdf_file, status, source_file_name, sender_name,
               caption_text, telegram_local_time
        FROM postings
    """).fetchall()
    conn.close()

    existing_map = {row["source_id"]: row for row in existing_rows}

    for item in items:
        source_id = item["source_id"]
        existing_row = existing_map.get(source_id)

        if existing_row is None:
            if is_delete_caption(item["caption"]):
                continue

            merged_pdf = build_merged_pdf(
                item["file_id"],
                item["caption"],
                source_id,
                item["telegram_local_time"]
            )
            save_new_posting(
                source_id=source_id,
                product_name=item["caption"],
                pdf_bytes=merged_pdf,
                source_file_name=item["file_name"],
                sender_name=item["sender_name"],
                telegram_message_id=item["message_id"],
                telegram_date=item["telegram_date"],
                telegram_local_time=item["telegram_local_time"],
            )
        else:
            update_existing_posting_if_needed(existing_row, item)

    return RedirectResponse(url="/", status_code=303)


@app.get("/pdf/{source_id}")
def open_pdf(source_id: str):
    conn = db()
    row = conn.execute("""
        SELECT pdf_file FROM postings WHERE source_id = ?
    """, (source_id,)).fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="PDF not found")

    conn.execute("""
        UPDATE postings
        SET status = 'opened'
        WHERE source_id = ? AND status NOT IN ('printed', 'deleted')
    """, (source_id,))
    conn.commit()
    conn.close()

    file_path = PDF_DIR / row["pdf_file"]
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File missing on disk")

    return StreamingResponse(
        io.BytesIO(file_path.read_bytes()),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{file_path.name}"'}
    )


@app.get("/pdf-batch-active")
def pdf_batch_active():
    conn = db()
    rows = conn.execute("""
        SELECT source_id, pdf_file
        FROM postings
        WHERE status NOT IN ('printed', 'deleted')
        ORDER BY created_at ASC
    """).fetchall()
    conn.close()

    if not rows:
        raise HTTPException(status_code=404, detail="Нет ненапечатанных файлов")

    writer = PdfWriter()

    for row in rows:
        file_path = PDF_DIR / row["pdf_file"]
        if not file_path.exists():
            continue

        reader = PdfReader(str(file_path))
        for page in reader.pages:
            writer.add_page(page)

    out = io.BytesIO()
    writer.write(out)
    out.seek(0)

    return StreamingResponse(
        out,
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="all_active_labels.pdf"'}
    )


@app.post("/mark-printed/{source_id}")
def mark_printed(source_id: str):
    conn = db()
    conn.execute("UPDATE postings SET status = 'printed' WHERE source_id = ?", (source_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)


@app.post("/mark-all-active-printed")
def mark_all_active_printed():
    conn = db()
    conn.execute("""
        UPDATE postings
        SET status = 'printed'
        WHERE status NOT IN ('printed', 'deleted')
    """)
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)


@app.post("/return-to-new/{source_id}")
def return_to_new(source_id: str):
    conn = db()
    conn.execute("UPDATE postings SET status = 'new' WHERE source_id = ?", (source_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/?show=printed", status_code=303)