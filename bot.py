# -*- coding: utf-8 -*-
"""
CV Telegram Bot — Render-Ready with Template Previews
=====================================================

• Stack: python-telegram-bot v21.x (async), SQLite, docxtpl + (fallback) python-docx,
  aiohttp mini server (/health), optional PDF via LibreOffice.
• Deploy: Render (run_polling). Recommended disk mount: /var/data

ENV VARS:
---------
BOT_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxx
DB_PATH=/var/data/bot.db
OWNER_USERNAME=Ferp0ks                 # optional
PAYLINK_UPGRADE_URL=https://pay.link   # optional
ENABLE_PDF=0                           # 1 to try PDF conversion
PORT=10000                             # Render sets PORT; we read it

Assets (optional but recommended):
----------------------------------
assets/templates/ATS_ar.docx, ATS_en.docx, Modern_ar.docx, Modern_en.docx, ...
assets/previews/ATS_ar.jpg  (وأمثالها: <slug>_<lang>.<jpg|png|jpeg|webp>)
"""
import json
import logging
import os
import shutil
import sqlite3
import textwrap
from datetime import datetime, date
from pathlib import Path

from aiohttp import web
from telegram import (
    Update,
    InlineKeyboardMarkup, InlineKeyboardButton,
    InputFile, BotCommand, InputMediaPhoto
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, ContextTypes, filters
)

# ============ Logging ============
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("cvbot")

# ============ Config ============
DB_PATH = os.getenv("DB_PATH", "/var/data/bot.db")
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "")
PAYLINK_UPGRADE_URL = os.getenv("PAYLINK_UPGRADE_URL", "")
ENABLE_PDF = os.getenv("ENABLE_PDF", "0") == "1"
PORT = int(os.getenv("PORT", os.getenv("RENDER_PORT", "10000")))

TEMPLATES_DIR = Path("assets/templates")
PREVIEWS_DIR = Path("assets/previews")

EXPORTS_DIR = Path(os.getenv("EXPORTS_DIR", "/var/data/exports"))
try:
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    EXPORTS_DIR = Path("./exports")
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

# ============ Conversation States ============
(
    ASK_LANG,
    ASK_TPL,
    ASK_NAME,
    ASK_TITLE,
    ASK_PHONE,
    ASK_EMAIL,
    ASK_CITY,
    ASK_LINKS,
    ASK_SUMMARY,
    MENU,
    EXP_ROLE,
    EXP_COMPANY,
    EXP_START,
    EXP_END,
    EXP_BULLETS,
    EDU_DEGREE,
    EDU_MAJOR,
    EDU_SCHOOL,
    EDU_YEAR,
    SKILLS_SET,
    CONFIRM_EXPORT,
) = range(21)

# Slug -> label per language
TEMPLATES_INDEX = {
    "ar": [
        ("ATS", "ATS (مطابق أنظمة التتبع)"),
        ("Modern", "حديث"),
        ("Minimal", "بسيط"),
        ("Navy", "احترافي (شريط جانبي أزرق)"),
        ("Elegant", "أنيق رمادي"),
    ],
    "en": [
        ("ATS", "ATS"),
        ("Modern", "Modern"),
        ("Minimal", "Minimal"),
        ("Navy", "Professional Navy Sidebar"),
        ("Elegant", "Elegant Gray"),
    ],
}

# ============ DB Helpers ============
class DB:
    def __init__(self, path: str):
        self.path = path
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            self.path = str(Path("./var_data/bot.db"))
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self):
        return sqlite3.connect(self.path)

    def _init(self):
        con = self._conn()
        cur = con.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
              user_id INTEGER PRIMARY KEY,
              lang TEXT DEFAULT 'ar',
              vip INTEGER DEFAULT 0,
              created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS cv_profile(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER,
              title TEXT,
              full_name TEXT,
              phone TEXT, email TEXT, city TEXT, links TEXT,
              summary TEXT,
              template TEXT DEFAULT 'ATS',
              lang TEXT DEFAULT 'ar',
              created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
              updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS cv_experience(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              profile_id INTEGER,
              company TEXT, role TEXT,
              start_date TEXT, end_date TEXT,
              bullets TEXT
            );

            CREATE TABLE IF NOT EXISTS cv_education(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              profile_id INTEGER,
              degree TEXT, major TEXT, school TEXT,
              year TEXT
            );

            CREATE TABLE IF NOT EXISTS cv_skills(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              profile_id INTEGER,
              skills TEXT
            );

            CREATE TABLE IF NOT EXISTS cv_quota(
              user_id INTEGER PRIMARY KEY,
              daily_used INTEGER DEFAULT 0,
              last_reset DATE
            );
            """
        )
        con.commit()
        con.close()

    # Users & VIP
    def ensure_user(self, user_id: int, lang: str = "ar"):
        con = self._conn(); cur = con.cursor()
        cur.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
        if not cur.fetchone():
            cur.execute("INSERT INTO users(user_id, lang, vip) VALUES(?,?,0)", (user_id, lang))
            con.commit()
        con.close()

    def is_vip(self, user_id: int) -> bool:
        con = self._conn(); cur = con.cursor()
        cur.execute("SELECT vip FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone(); con.close()
        return bool(row and row[0])

    def set_vip(self, user_id: int, vip: int):
        con = self._conn(); cur = con.cursor()
        cur.execute("UPDATE users SET vip=? WHERE user_id=?", (vip, user_id))
        con.commit(); con.close()

    # Quota
    def can_export_today(self, user_id: int, free_limit: int = 1) -> bool:
        today = date.today().isoformat()
        con = self._conn(); cur = con.cursor()
        cur.execute("SELECT daily_used, last_reset FROM cv_quota WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if not row:
            cur.execute("INSERT INTO cv_quota(user_id, daily_used, last_reset) VALUES(?,?,?)", (user_id, 0, today))
            con.commit(); con.close(); return True
        used, last = row
        if last != today:
            cur.execute("UPDATE cv_quota SET daily_used=0, last_reset=? WHERE user_id=?", (today, user_id))
            con.commit(); used = 0
        con.close()
        return used < free_limit

    def bump_export(self, user_id: int):
        today = date.today().isoformat()
        con = self._conn(); cur = con.cursor()
        cur.execute("SELECT daily_used, last_reset FROM cv_quota WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if not row:
            cur.execute("INSERT INTO cv_quota(user_id, daily_used, last_reset) VALUES(?,?,?)", (user_id, 1, today))
        else:
            used, last = row
            if last != today:
                cur.execute("UPDATE cv_quota SET daily_used=1, last_reset=? WHERE user_id=?", (today, user_id))
            else:
                cur.execute("UPDATE cv_quota SET daily_used=daily_used+1 WHERE user_id=?", (user_id,))
        con.commit(); con.close()

    # Profiles
    def new_profile(self, user_id: int, lang: str, template: str) -> int:
        con = self._conn(); cur = con.cursor()
        cur.execute(
            "INSERT INTO cv_profile(user_id, lang, template) VALUES(?,?,?)",
            (user_id, lang, template),
        )
        pid = cur.lastrowid
        con.commit(); con.close(); return pid

    def update_profile(self, pid: int, **fields):
        if not fields: return
        con = self._conn(); cur = con.cursor()
        keys = ", ".join([f"{k}=?" for k in fields.keys()])
        cur.execute(
            f"UPDATE cv_profile SET {keys}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (*fields.values(), pid),
        )
        con.commit(); con.close()

    def add_experience(self, pid: int, company: str, role: str, start_date: str, end_date: str, bullets: list[str]):
        con = self._conn(); cur = con.cursor()
        cur.execute(
            "INSERT INTO cv_experience(profile_id, company, role, start_date, end_date, bullets) VALUES(?,?,?,?,?,?)",
            (pid, company, role, start_date, end_date, json.dumps(bullets, ensure_ascii=False)),
        )
        con.commit(); con.close()

    def add_education(self, pid: int, degree: str, major: str, school: str, year: str):
        con = self._conn(); cur = con.cursor()
        cur.execute(
            "INSERT INTO cv_education(profile_id, degree, major, school, year) VALUES(?,?,?,?,?)",
            (pid, degree, major, school, year),
        )
        con.commit(); con.close()

    def set_skills(self, pid: int, skills_str: str):
        con = self._conn(); cur = con.cursor()
        cur.execute("SELECT id FROM cv_skills WHERE profile_id=?", (pid,))
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE cv_skills SET skills=? WHERE profile_id=?", (skills_str, pid))
        else:
            cur.execute("INSERT INTO cv_skills(profile_id, skills) VALUES(?,?)", (pid, skills_str))
        con.commit(); con.close()

    def fetch_full_profile(self, pid: int):
        con = self._conn(); cur = con.cursor()
        cur.execute("SELECT * FROM cv_profile WHERE id=?", (pid,))
        p = cur.fetchone()
        if not p:
            con.close(); return None
        cols = [d[0] for d in cur.description]
        profile = dict(zip(cols, p))
        cur.execute("SELECT company, role, start_date, end_date, bullets FROM cv_experience WHERE profile_id=?", (pid,))
        exps = [
            {
                "company": r[0],
                "role": r[1],
                "start_date": r[2],
                "end_date": r[3],
                "bullets": json.loads(r[4]) if r[4] else []
            } for r in cur.fetchall()
        ]
        cur.execute("SELECT degree, major, school, year FROM cv_education WHERE profile_id=?", (pid,))
        edus = [
            {"degree": r[0], "major": r[1], "school": r[2], "year": r[3]} for r in cur.fetchall()
        ]
        cur.execute("SELECT skills FROM cv_skills WHERE profile_id=?", (pid,))
        row = cur.fetchone()
        skills = row[0] if row else ""
        con.close()
        return profile, exps, edus, skills

# ============ Rendering ============
from docxtpl import DocxTemplate  # type: ignore
try:
    from docx import Document  # python-docx fallback
except Exception:
    Document = None

def _safe(s: str | None) -> str:
    return s or ""

def render_docx_for_profile(pid: int, db: DB) -> Path:
    data = db.fetch_full_profile(pid)
    if not data:
        raise RuntimeError("Profile not found")
    profile, exps, edus, skills = data
    lang = profile.get("lang", "ar")
    tpl_slug = profile.get("template", "ATS")

    ctx = {
        "full_name": _safe(profile.get("full_name")),
        "title": _safe(profile.get("title")),
        "phone": _safe(profile.get("phone")),
        "email": _safe(profile.get("email")),
        "city": _safe(profile.get("city")),
        "links": _safe(profile.get("links")),
        "summary": _safe(profile.get("summary")),
        "experiences": exps,
        "education": edus,
        "skills": skills,
    }
    skills_list = [s.strip() for s in (skills or "").replace("؛", ",").split(",") if s.strip()]
    ctx["skills_list"] = skills_list

    out_path = EXPORTS_DIR / f"cv_{pid}_{lang}.docx"
    tpl_path = TEMPLATES_DIR / f"{tpl_slug}_{lang}.docx"
    if tpl_path.exists():
        doc = DocxTemplate(tpl_path)
        doc.render(ctx)
        doc.save(out_path)
        return out_path

    # Fallback: professional layout (navy sidebar)
    if Document is None:
        raise RuntimeError("No template found and python-docx not installed")
    from docx.shared import Inches, Pt, RGBColor
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    def shade_cell(cell, color_hex: str):
        tcPr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), color_hex)
        tcPr.append(shd)

    docx = Document()
    for s in docx.sections:
        s.top_margin = s.bottom_margin = Inches(0.4)
        s.left_margin = s.right_margin = Inches(0.4)

    table = docx.add_table(rows=1, cols=2)
    table.autofit = False
    left, right = table.rows[0].cells
    table.columns[0].width = Inches(2.2)
    table.columns[1].width = Inches(4.8)

    NAVY = RGBColor(31, 58, 95)
    WHITE = RGBColor(255, 255, 255)

    shade_cell(left, "1f3a5f")

    def add_left_heading(text):
        p = left.add_paragraph()
        r = p.add_run(text.upper())
        r.font.bold = True
        r.font.size = Pt(10)
        r.font.color.rgb = WHITE
        p.space_after = Pt(2)

    def add_left_line(text):
        p = left.add_paragraph()
        r = p.add_run(text)
        r.font.size = Pt(9)
        r.font.color.rgb = WHITE
        p.space_after = Pt(1)

    # Sidebar: contact
    add_left_heading("Contact" if lang == "en" else "الاتصال")
    for item in [ctx["phone"], ctx["email"], ctx["city"]]:
        if item:
            add_left_line(item)
    if ctx["links"]:
        add_left_line(ctx["links"])
    left.add_paragraph().space_after = Pt(6)

    # Sidebar: education
    add_left_heading("Education" if lang == "en" else "التعليم")
    for ed in edus:
        add_left_line(f"{ed.get('degree','')} — {ed.get('school','')}")
        if ed.get("year"):
            add_left_line(str(ed.get("year")))
    left.add_paragraph().space_after = Pt(6)

    # Sidebar: skills
    add_left_heading("Skills" if lang == "en" else "المهارات")
    if skills_list:
        for s_item in skills_list:
            add_left_line(f"• {s_item}")
    elif ctx["skills"]:
        add_left_line(ctx["skills"])

    # Right: header
    p = right.add_paragraph()
    name_run = p.add_run(ctx["full_name"])
    from docx.shared import Pt  # safe reuse
    name_run.font.size = Pt(20)
    name_run.font.bold = True
    name_run.font.color.rgb = NAVY
    if ctx["title"]:
        p.add_run("\n")
        t = p.add_run(ctx["title"])
        t.font.size = Pt(12)
    right.add_paragraph()

    # Summary
    hdr = right.add_paragraph("Summary" if lang == "en" else "الملخص")
    hdr.runs[0].font.size = Pt(12)
    hdr.runs[0].font.bold = True
    if ctx["summary"]:
        for line in textwrap.wrap(ctx["summary"], width=120):
            rp = right.add_paragraph(line)
            rp.paragraph_format.space_after = Pt(2)
    right.add_paragraph()

    # Experience
    hdr = right.add_paragraph("Work Experience" if lang == "en" else "الخبرات")
    hdr.runs[0].font.size = Pt(12)
    hdr.runs[0].font.bold = True
    for e in exps:
        line = right.add_paragraph()
        r1 = line.add_run(f"{e.get('role','')} — {e.get('company','')}")
        r1.font.bold = True
        r1.font.size = Pt(11)
        if e.get("start_date") or e.get("end_date"):
            line.add_run(f" ({e.get('start_date','')} - {e.get('end_date','')})")
        for b in e.get("bullets", [])[:6]:
            bp = right.add_paragraph(f"• {b}")
            bp.paragraph_format.space_after = Pt(0)
    docx.add_paragraph()

    docx.save(out_path)
    return out_path

def try_convert_to_pdf(docx_path: Path) -> Path | None:
    if not ENABLE_PDF:
        return None
    lo = shutil.which("libreoffice") or shutil.which("soffice")
    if not lo:
        log.warning("LibreOffice not found; skipping PDF convert")
        return None
    out_dir = docx_path.parent
    cmd = [lo, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(docx_path)]
    try:
        import subprocess
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        pdf_path = docx_path.with_suffix(".pdf")
        return pdf_path if pdf_path.exists() else None
    except Exception as e:
        log.exception("PDF convert failed: %s", e)
        return None

# ============ Bot Handlers ============
db = DB(DB_PATH)

async def set_my_commands(app: Application):
    cmds = [
        BotCommand("start", "ابدأ / Start"),
        BotCommand("cv", "إنشاء/تعديل السيرة"),
        BotCommand("upgrade", "الترقية إلى VIP"),
        BotCommand("help", "مساعدة"),
    ]
    await app.bot.set_my_commands(cmds)

# ---------- Helper: previews ----------
def _preview_path(slug: str, lang: str):
    for ext in ("jpg", "jpeg", "png", "webp"):
        p = PREVIEWS_DIR / f"{slug}_{lang}.{ext}"
        if p.exists():
            return p
    return None

def _template_label(slug: str, lang: str) -> str:
    table = dict(TEMPLATES_INDEX.get(lang, []))
    return table.get(slug, slug)

def _template_selection_markup(lang: str, initial: bool = True, pid: int | None = None):
    rows = []
    for slug, name in TEMPLATES_INDEX[lang]:
        if initial:
            choose_cb = f"cv:tpl:{slug}"
        else:
            choose_cb = f"cv:tplset:{slug}:{pid}"
        prev_cb = f"cv:prev:{slug}:{lang}"
        rows.append([
            InlineKeyboardButton(f"👁️ معاينة — {name}", callback_data=prev_cb),
            InlineKeyboardButton(f"اختيار {name}", callback_data=choose_cb),
        ])
    # preview gallery
    rows.append([InlineKeyboardButton("👁️👁️ معاينة جماعية", callback_data=f"cv:prevg:{lang}")])
    if not initial and pid is not None:
        rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data=f"cv:menu:back:{pid}")])
    return InlineKeyboardMarkup(rows)

async def _send_preview_photo(update_or_q, slug: str, lang: str, initial: bool = True, pid: int | None = None):
    p = _preview_path(slug, lang)
    caption = f"معاينة: { _template_label(slug, lang) }"
    if initial:
        choose_cb = f"cv:tpl:{slug}"
    else:
        choose_cb = f"cv:tplset:{slug}:{pid}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("اختيار هذا القالب ✅", callback_data=choose_cb)]])
    if p and p.exists():
        with open(p, "rb") as f:
            if isinstance(update_or_q, Update):
                await update_or_q.effective_message.reply_photo(f, caption=caption, reply_markup=kb)
            else:
                q = update_or_q
                await q.message.reply_photo(f, caption=caption, reply_markup=kb)
    else:
        txt = f"لا تتوفر صورة معاينة لهذا القالب ({slug}). يمكنك اختياره مباشرة أو إضافة صورة إلى assets/previews/{slug}_{lang}.jpg"
        if isinstance(update_or_q, Update):
            await update_or_q.effective_message.reply_text(txt, reply_markup=kb)
        else:
            q = update_or_q
            await q.message.reply_text(txt, reply_markup=kb)

# ---------- /start & basics ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    db.ensure_user(u.id)
    await update.effective_message.reply_text(
        "أهلًا! هذا بوت إنشاء سيرة ذاتية (CV) احترافي.\n"
        "- أنشئ سيرة عربية أو إنجليزية\n"
        "- اختر قالب (ATS/Modern/Minimal/Navy/Elegant) مع معاينة\n"
        "- أضف خبرات/تعليم/مهارات\n"
        "- صدّر DOCX (مجاني) و PDF و Cover Letter (VIP)\n\n"
        "أرسل /cv للبدء."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "/cv للبدء • /upgrade للترقية • للتواصل: @{}".format(OWNER_USERNAME or "admin")
    )

async def upgrade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if PAYLINK_UPGRADE_URL:
        await update.effective_message.reply_text(f"للترقية إلى VIP: {PAYLINK_UPGRADE_URL}")
    else:
        await update.effective_message.reply_text("فعّل PAYLINK_UPGRADE_URL في المتغيرات البيئية لزر ترقية فعّال.")

# --- CV Flow ---
async def cv_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_action(ChatAction.TYPING)
    context.user_data["cv"] = {}
    kb = [[InlineKeyboardButton("عربي", callback_data="cv:lang:ar"),
           InlineKeyboardButton("English", callback_data="cv:lang:en")]]
    await update.effective_message.reply_text("اختر لغة السيرة:", reply_markup=InlineKeyboardMarkup(kb))
    return ASK_LANG

async def cv_set_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    lang = q.data.split(":")[-1]
    context.user_data["cv"]["lang"] = lang
    await q.edit_message_text("اختر القالب أو استعرض المعاينات:",
                              reply_markup=_template_selection_markup(lang, initial=True))
    return ASK_TPL

# initial choose (creation)
async def cv_set_tpl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    tpl_slug = q.data.split(":")[-1]
    context.user_data["cv"]["template"] = tpl_slug
    await q.edit_message_text(f"تم اختيار قالب: {tpl_slug}\nأرسل اسمك الكامل:")
    return ASK_NAME

# preview — initial and in-menu
async def cv_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    _, _, slug, lang = q.data.split(":")
    # determine if we are in initial or menu flow
    pid = context.user_data.get("cv", {}).get("pid")
    initial = pid is None
    await _send_preview_photo(q, slug, lang, initial=initial, pid=pid)
    # keep state
    if initial:
        await q.message.reply_text("اختر القالب أو استعرض المزيد:",
                                   reply_markup=_template_selection_markup(lang, initial=True))
        return ASK_TPL
    else:
        await q.message.reply_text("اختر القالب أو ارجع للقائمة:",
                                   reply_markup=_template_selection_markup(lang, initial=False, pid=pid))
        return MENU

async def cv_preview_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    lang = q.data.split(":")[-1]
    pid = context.user_data.get("cv", {}).get("pid")
    initial = pid is None
    sent_any = False
    for slug, _name in TEMPLATES_INDEX.get(lang, []):
        p = _preview_path(slug, lang)
        if not p: continue
        sent_any = True
        await _send_preview_photo(q, slug, lang, initial=initial, pid=pid)
    if not sent_any:
        await q.message.reply_text("لا توجد صور معاينة مضافة بعد. أضفها إلى assets/previews/")
    # show selection again
    if initial:
        await q.message.reply_text("اختر القالب:", reply_markup=_template_selection_markup(lang, initial=True))
        return ASK_TPL
    else:
        await q.message.reply_text("اختر القالب أو ارجع للقائمة:",
                                   reply_markup=_template_selection_markup(lang, initial=False, pid=pid))
        return MENU

# data collection
async def cv_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["cv"]["full_name"] = update.message.text.strip()
    await update.message.reply_text("اكتب المسمى الوظيفي المستهدف (مثال: محلّل بيانات / Data Analyst):")
    return ASK_TITLE

async def cv_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["cv"]["title"] = update.message.text.strip()
    await update.message.reply_text("رقم الجوال:")
    return ASK_PHONE

async def cv_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["cv"]["phone"] = update.message.text.strip()
    await update.message.reply_text("البريد الإلكتروني:")
    return ASK_EMAIL

async def cv_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["cv"]["email"] = update.message.text.strip()
    await update.message.reply_text("المدينة:")
    return ASK_CITY

async def cv_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["cv"]["city"] = update.message.text.strip()
    await update.message.reply_text("روابطك (LinkedIn/GitHub) إن وجدت، مفصولة بفواصل، أو اكتب - لا يوجد -:")
    return ASK_LINKS

async def cv_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["cv"]["links"] = update.message.text.strip()
    await update.message.reply_text("اكتب ملخّصًا قصيرًا (3-4 أسطر) عن خبرتك ومهاراتك:")
    return ASK_SUMMARY

async def cv_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["cv"]["summary"] = update.message.text.strip()
    u = update.effective_user
    cv = context.user_data["cv"]
    db.ensure_user(u.id, cv.get("lang", "ar"))
    pid = db.new_profile(u.id, cv.get("lang", "ar"), cv.get("template", "ATS"))
    db.update_profile(
        pid,
        full_name=cv.get("full_name"), title=cv.get("title"), phone=cv.get("phone"),
        email=cv.get("email"), city=cv.get("city"), links=cv.get("links"), summary=cv.get("summary"),
    )
    context.user_data["cv"]["pid"] = pid
    await show_menu(update, context, pid)
    return MENU

async def show_menu(update_or_q, context: ContextTypes.DEFAULT_TYPE, pid: int):
    txt = (
        "القائمة الرئيسية للسيرة:\n\n"
        "• أضف خبرات العمل\n"
        "• أضف التعليم\n"
        "• عيّن المهارات\n"
        "• معاينة/تصدير\n"
        "• 🖼️ تغيير القالب"
    )
    kb = [
        [InlineKeyboardButton("➕ إضافة خبرة", callback_data=f"cv:menu:addexp:{pid}")],
        [InlineKeyboardButton("🎓 إضافة تعليم", callback_data=f"cv:menu:addedu:{pid}")],
        [InlineKeyboardButton("🧩 تعيين المهارات", callback_data=f"cv:menu:skills:{pid}")],
        [InlineKeyboardButton("🖼️ تغيير القالب", callback_data=f"cv:menu:tpl:{pid}")],
        [InlineKeyboardButton("📤 معاينة/تصدير", callback_data=f"cv:menu:export:{pid}")],
    ]
    if isinstance(update_or_q, Update):
        await update_or_q.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    else:
        q = update_or_q
        await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))

async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    parts = q.data.split(":")
    action = parts[2]; pid = int(parts[3])
    if action == "addexp":
        context.user_data["exp"] = {"pid": pid}
        await q.edit_message_text("المسمى الوظيفي (Role):")
        return EXP_ROLE
    if action == "addedu":
        context.user_data["edu"] = {"pid": pid}
        await q.edit_message_text("الدرجة العلمية (مثال: بكالوريوس):")
        return EDU_DEGREE
    if action == "skills":
        context.user_data["skills_pid"] = pid
        await q.edit_message_text("أرسل المهارات مفصولة بفواصل (مثال: Excel, SQL, Power BI):")
        return SKILLS_SET
    if action == "export":
        await q.edit_message_text("خيارات التصدير:")
        return await show_export_menu(q, context, pid)
    if action == "tpl":
        # open template selector in menu context
        profile, *_ = db.fetch_full_profile(pid)
        lang = profile.get("lang", "ar")
        context.user_data.setdefault("cv", {})["pid"] = pid
        await q.edit_message_text("غيّر القالب أو استعرض المعاينات:",
                                  reply_markup=_template_selection_markup(lang, initial=False, pid=pid))
        return MENU
    if action == "back":
        await show_menu(q, context, pid)
        return MENU

# Experience flow
async def exp_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["exp"]["role"] = update.message.text.strip()
    await update.message.reply_text("اسم الشركة:")
    return EXP_COMPANY

async def exp_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["exp"]["company"] = update.message.text.strip()
    await update.message.reply_text("تاريخ البدء (مثال: 01/2023):")
    return EXP_START

async def exp_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["exp"]["start_date"] = update.message.text.strip()
    await update.message.reply_text("تاريخ الانتهاء (أو اكتب Present):")
    return EXP_END

async def exp_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["exp"]["end_date"] = update.message.text.strip()
    await update.message.reply_text("أرسل نقاط الإنجاز في هذه الخبرة، كل سطر نقطة (أرسل جميع النقاط برسالة واحدة):")
    return EXP_BULLETS

async def exp_bullets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [l.strip("• ").strip() for l in update.message.text.splitlines() if l.strip()]
    e = context.user_data.get("exp", {})
    db.add_experience(e["pid"], e["company"], e["role"], e["start_date"], e["end_date"], lines)
    await update.message.reply_text("تمت إضافة الخبرة.")
    await show_menu(update, context, e["pid"])
    return MENU

# Education flow
async def edu_degree(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["edu"]["degree"] = update.message.text.strip()
    await update.message.reply_text("التخصص (مثال: علوم الحاسب):")
    return EDU_MAJOR

async def edu_major(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["edu"]["major"] = update.message.text.strip()
    await update.message.reply_text("اسم الجامعة/المعهد:")
    return EDU_SCHOOL

async def edu_school(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["edu"]["school"] = update.message.text.strip()
    await update.message.reply_text("سنة التخرج (مثال: 2024):")
    return EDU_YEAR

async def edu_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["edu"]["year"] = update.message.text.strip()
    ed = context.user_data["edu"]
    db.add_education(ed["pid"], ed["degree"], ed["major"], ed["school"], ed["year"])
    await update.message.reply_text("تمت إضافة التعليم.")
    await show_menu(update, context, ed["pid"])
    return MENU

# Skills
async def skills_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pid = context.user_data.get("skills_pid")
    skills_str = update.message.text.strip()
    db.set_skills(pid, skills_str)
    await update.message.reply_text("تم تعيين المهارات.")
    await show_menu(update, context, pid)
    return MENU

# Export
async def show_export_menu(q, context: ContextTypes.DEFAULT_TYPE, pid: int):
    user_id = q.from_user.id
    buttons = [[InlineKeyboardButton("📄 تصدير DOCX", callback_data=f"cv:export:docx:{pid}")]]
    if db.is_vip(user_id):
        buttons.append([InlineKeyboardButton("🧾 تصدير PDF", callback_data=f"cv:export:pdf:{pid}")])
        buttons.append([InlineKeyboardButton("✉️ Cover Letter", callback_data=f"cv:export:cover:{pid}")])
    else:
        buttons.append([InlineKeyboardButton("⭐ ترقية إلى VIP", url=PAYLINK_UPGRADE_URL or "https://example.com")])
    await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))
    return CONFIRM_EXPORT

async def export_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    _, _, kind, pid = q.data.split(":")
    pid = int(pid)
    user_id = q.from_user.id

    if kind == "docx":
        if not db.is_vip(user_id) and not db.can_export_today(user_id, free_limit=1):
            await q.edit_message_text("وصلت حد التصدير اليومي المجاني. قم بالترقية إلى VIP للمزيد.")
            return ConversationHandler.END
        await q.edit_message_text("جارٍ إنشاء DOCX…")
        docx_path = render_docx_for_profile(pid, db)
        db.bump_export(user_id)
        with open(docx_path, "rb") as f:
            await q.message.reply_document(InputFile(f, filename=docx_path.name), caption="تم إنشاء السيرة ✨")
        await show_menu(q, context, pid)
        return MENU

    if kind == "pdf":
        if not db.is_vip(user_id):
            await q.edit_message_text("ميزة PDF لعملاء VIP فقط.")
            return ConversationHandler.END
        await q.edit_message_text("جارٍ إنشاء PDF…")
        docx_path = render_docx_for_profile(pid, db)
        pdf_path = try_convert_to_pdf(docx_path)
        if not pdf_path:
            await q.message.reply_text("تعذر تحويل PDF على الخادم. تم إرسال DOCX بدلًا منه.")
            with open(docx_path, "rb") as f:
                await q.message.reply_document(InputFile(f, filename=docx_path.name))
        else:
            with open(pdf_path, "rb") as f:
                await q.message.reply_document(InputFile(f, filename=pdf_path.name), caption="PDF جاهز ✅")
        await show_menu(q, context, pid)
        return MENU

    if kind == "cover":
        if not db.is_vip(user_id):
            await q.edit_message_text("Cover Letter لعملاء VIP فقط.")
            return ConversationHandler.END
        profile, exps, edus, skills = db.fetch_full_profile(pid)
        lang = profile.get("lang", "ar")
        body = (
            f"السادة المحترمون،\n\nأود التقدم لوظيفة {profile.get('title','')}،\n"
            f"أمتلك خبرات ذات صلة، وأبرُز إنجازاتي في بيئات عمل ديناميكية.\n"
            f"أرفقت سيرتي الذاتية، وأتطلع لفرصة مقابلة لمناقشة مدى مواءمتي للفريق.\n\n"
            f"تحياتي،\n{profile.get('full_name','')}\n{profile.get('phone','')} • {profile.get('email','')}"
        ) if lang == "ar" else (
            f"Dear Hiring Team,\n\nI am applying for the {profile.get('title','')} role. "
            f"I bring relevant experience and a track record of delivering impact.\n"
            f"Please find my resume attached. I would welcome the opportunity to discuss my fit.\n\n"
            f"Kind regards,\n{profile.get('full_name','')}\n{profile.get('phone','')} • {profile.get('email','')}"
        )
        fn = EXPORTS_DIR / f"cover_{pid}.txt"
        fn.write_text(body, encoding="utf-8")
        with open(fn, "rb") as f:
            await q.message.reply_document(InputFile(f, filename=fn.name), caption="Cover Letter (نصي)")
        await show_menu(q, context, pid)
        return MENU

# In-menu: set template for existing profile
async def cv_tpl_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    _, _, slug, pid_s = q.data.split(":")
    pid = int(pid_s)
    db.update_profile(pid, template=slug)
    profile, *_ = db.fetch_full_profile(pid)
    lang = profile.get("lang", "ar")
    await q.edit_message_text(f"تم تغيير القالب إلى: { _template_label(slug, lang) }")
    await show_menu(q, context, pid)
    return MENU

# ============ AIOHTTP mini server (/health) ============
async def create_app_and_site(app_tg: Application):
    async def root(request):
        return web.Response(text="OK")

    async def health(request):
        return web.json_response({"ok": True, "service": "cvbot", "time": datetime.utcnow().isoformat()})

    app = web.Application()
    app.add_routes([
        web.get("/", root),
        web.get("/health", health),
        web.head("/", root),
        web.head("/health", health),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    log.info("aiohttp listening on :%s", PORT)

# ============ Main ============
async def _post_init(app: Application):
    await set_my_commands(app)
    await create_app_and_site(app)

def main():
    token = os.getenv("BOT_TOKEN", "")
    if not token:
        raise RuntimeError("BOT_TOKEN is missing")

    application = Application.builder().token(token).post_init(_post_init).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("upgrade", upgrade_cmd))

    cv_conv = ConversationHandler(
        entry_points=[CommandHandler("cv", cv_entry)],
        states={
            ASK_LANG: [CallbackQueryHandler(cv_set_lang, pattern=r"^cv:lang:")],
            ASK_TPL: [
                CallbackQueryHandler(cv_set_tpl, pattern=r"^cv:tpl:"),
                CallbackQueryHandler(cv_preview, pattern=r"^cv:prev:"),
                CallbackQueryHandler(cv_preview_all, pattern=r"^cv:prevg:"),
            ],
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, cv_name)],
            ASK_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, cv_title)],
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, cv_phone)],
            ASK_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, cv_email)],
            ASK_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, cv_city)],
            ASK_LINKS: [MessageHandler(filters.TEXT & ~filters.COMMAND, cv_links)],
            ASK_SUMMARY: [MessageHandler(filters.TEXT & ~filters.COMMAND, cv_summary)],

            MENU: [
                CallbackQueryHandler(menu_router, pattern=r"^cv:menu:"),
                CallbackQueryHandler(cv_preview, pattern=r"^cv:prev:"),
                CallbackQueryHandler(cv_preview_all, pattern=r"^cv:prevg:"),
                CallbackQueryHandler(cv_tpl_set, pattern=r"^cv:tplset:"),
            ],

            EXP_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_role)],
            EXP_COMPANY: [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_company)],
            EXP_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_start)],
            EXP_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_end)],
            EXP_BULLETS: [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_bullets)],

            EDU_DEGREE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edu_degree)],
            EDU_MAJOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, edu_major)],
            EDU_SCHOOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, edu_school)],
            EDU_YEAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, edu_year)],

            SKILLS_SET: [MessageHandler(filters.TEXT & ~filters.COMMAND, skills_set)],

            CONFIRM_EXPORT: [CallbackQueryHandler(export_router, pattern=r"^cv:export:")],
        },
        fallbacks=[],
        name="cv_conv",
        persistent=False,
    )

    application.add_handler(cv_conv)

    log.info("Bot starting…")
    application.run_polling()

if __name__ == "__main__":
    main()
