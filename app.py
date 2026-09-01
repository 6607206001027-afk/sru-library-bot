import os
import json
import re
import time
import random
import traceback
from flask import Flask, request, abort

import firebase_admin
from firebase_admin import credentials, firestore
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FlexSendMessage

# ==========================================
# 1. ตั้งค่า Keys จาก Environment Variables
# ==========================================
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
FIREBASE_CREDENTIALS_JSON = os.environ.get("FIREBASE_CREDENTIALS_JSON")

_missing = [name for name, val in [
    ("LINE_CHANNEL_ACCESS_TOKEN", LINE_CHANNEL_ACCESS_TOKEN),
    ("LINE_CHANNEL_SECRET", LINE_CHANNEL_SECRET),
    ("GEMINI_API_KEY", GEMINI_API_KEY),
    ("FIREBASE_CREDENTIALS_JSON", FIREBASE_CREDENTIALS_JSON),
] if not val]
if _missing:
    raise RuntimeError(
        "❌ ขาด Environment Variables: " + ", ".join(_missing) +
        " กรุณาตั้งค่าใน Render Dashboard -> Environment ก่อน deploy"
    )

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

genai.configure(api_key=GEMINI_API_KEY)
GEMINI_MODEL_NAME = "gemini-3.5-flash"
model = genai.GenerativeModel(GEMINI_MODEL_NAME)

# ==========================================
# 2. เชื่อมต่อ Firebase
# ==========================================
if not firebase_admin._apps:
    try:
        cred_dict = json.loads(FIREBASE_CREDENTIALS_JSON)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            "❌ FIREBASE_CREDENTIALS_JSON ไม่ใช่ JSON ที่ถูกต้อง "
            "ตรวจสอบว่าคัดลอกเนื้อหาไฟล์ serviceAccountKey.json มาทั้งหมดแบบไม่ตัดตอน"
        ) from e
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred)

db = firestore.client()
app = Flask(__name__)


# ==========================================
# 3. Helper Functions
# ==========================================
def parse_publisher_year(publisher_raw):
    """
    แยกปีที่พิมพ์ (พ.ศ. 4 หลัก) ที่มักซ่อนอยู่ท้าย field publisher ออกมา
    เช่น 'กรุงเทพฯ : แอร์โรว์, 2566' -> ('กรุงเทพฯ : แอร์โรว์', '2566')
    """
    if not publisher_raw:
        return publisher_raw, None
    match = re.match(r"^(.*?),?\s*(\d{4})\s*\.?$", publisher_raw.strip())
    if match:
        year = match.group(2)
        if 2400 <= int(year) <= 2600:
            publisher_main = match.group(1).strip().rstrip(",")
            return publisher_main, year
    return publisher_raw.strip(), None


def parse_edition(edition_raw):
    """คืนเลขครั้งที่พิมพ์ เฉพาะกรณีเป็นครั้งที่ 2 ขึ้นไปเท่านั้น"""
    if not edition_raw:
        return None
    match = re.search(r"(\d+)", str(edition_raw))
    if match:
        num = int(match.group(1))
        if num >= 2:
            return num
    return None


DDC_MAIN_CLASSES = [
    (0, 99, "000 - คอมพิวเตอร์ สารสนเทศ และความรู้ทั่วไป"),
    (100, 199, "100 - ปรัชญาและจิตวิทยา"),
    (200, 299, "200 - ศาสนา"),
    (300, 399, "300 - สังคมศาสตร์"),
    (400, 499, "400 - ภาษา"),
    (500, 599, "500 - วิทยาศาสตร์"),
    (600, 699, "600 - เทคโนโลยี"),
    (700, 799, "700 - ศิลปะและนันทนาการ"),
    (800, 899, "800 - วรรณกรรม"),
    (900, 999, "900 - ประวัติศาสตร์และภูมิศาสตร์"),
]


def classify_ddc(call_number):
    """แปลงเลขเรียกหนังสือ (เช่น '495.9225') ให้เป็นชื่อหมวดดิวอี้ใหญ่ (เช่น '400 - ภาษา')"""
    try:
        number = float(str(call_number).strip().split()[0])
        for low, high, label in DDC_MAIN_CLASSES:
            if low <= number <= high:
                return label
    except (ValueError, IndexError):
        pass
    return "ไม่ระบุหมวดหมู่ (ไม่มีเลขเรียกหนังสือ)"


def _first(d, keys, default=""):
    """ลองไล่หาค่าจากหลายชื่อ key เผื่อเอกสารใน Firestore ตั้งชื่อ field ไม่ตรงกันทุกเล่ม"""
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return default


def _tokenize(text):
    """
    ตัดข้อความเป็นชุดคำ (set) สำหรับคำ/วลีภาษาอังกฤษหรือตัวเลขที่มีการเว้นวรรคชัดเจน
    ใช้ regex จับกลุ่มตัวอักษรไทย+อังกฤษ+ตัวเลขติดกัน แล้วแปลงเป็นตัวพิมพ์เล็กทั้งหมด
    หมายเหตุ: ใช้ไม่ได้ดีกับภาษาไทยที่พิมพ์ติดกันไม่เว้นวรรค (ซึ่งเป็นเรื่องปกติของภาษาไทย)
    เพราะจะเห็นทั้งประโยคเป็น "คำเดียว" — สำหรับค้นหาหนังสือให้ใช้ _extract_search_terms() แทน
    """
    if not text:
        return set()
    return set(re.findall(r"[a-zA-Z0-9\u0E00-\u0E7F]+", str(text).lower()))


# วลีทั่วไปที่ไม่ควรเอามาช่วยกรองหนังสือ (พบบ่อยในทุกประโยคแต่ไม่ได้บ่งบอกเนื้อหาหนังสือ)
# เรียงจากวลียาวไปสั้น เพื่อตัดวลีที่ยาวกว่าก่อน กันตัดคำผิดจากคำที่เป็นคำย่อยของกันและกัน
_FILLER_PHRASES = [
    "แนะนำหนังสือน่าอ่าน", "หนังสือน่าอ่าน", "อยากอ่านหนังสือ", "อยากได้หนังสือ",
    "ขอหนังสือ", "หาหนังสือ", "ค้นหนังสือ", "มีหนังสือ", "แนะนำหนังสือ",
    "เกี่ยวกับ", "หนังสือ", "แนะนำ", "น่าอ่าน", "ไหม", "ไหน", "หา", "ค้น",
    "ขอ", "อยาก", "อ่าน", "ครับ", "ค่ะ", "คะ", "หน่อย", "บ้าง", "เรื่อง",
    "เล่ม", "ที่", "จะ", "ให้", "ได้", "และ", "หรือ", "ใน", "ของ", "กับ",
    "ยืม", "สนใจ", "มี",
]

# ข้อความที่ปุ่มเมนูริชเมนู (Rich Menu) ส่งมาแบบตรงตัว เมื่อผู้ใช้กดปุ่ม "แนะนำหนังสือน่าอ่าน"
# เก็บไว้แยกจาก _FILLER_PHRASES เพื่อให้เช็คแบบ "ข้อความทั้งหมดตรงกับปุ่มเมนูนี้พอดี" ได้ตรงไปตรงมา
# ไม่ต้องพึ่งผลลัพธ์จากการตัดคำฟุ่มเฟือยทีละวลี ซึ่งอาจพลาดได้ถ้ามีอักขระแฝง (เช่น zero-width space)
# ปนมากับข้อความที่ปุ่มส่ง ทำให้ตัดคำไม่หมดและเข้าใจผิดว่าเป็นคำค้นเจาะจง
MENU_RANDOM_RECOMMEND_TEXTS = {
    "แนะนำหนังสือน่าอ่าน",
}
# เทียบแบบ normalize ทั้งสองฝั่งด้วยฟังก์ชันเดียวกัน กันกรณีวลีอ้างอิงเองมีช่องว่างแฝงอยู่ด้วย
_MENU_RANDOM_RECOMMEND_NORMALIZED = set()  # เติมค่าไว้ด้านล่างหลังนิยาม _normalize_menu_text


def _normalize_menu_text(text):
    """
    ล้างอักขระที่มองไม่เห็น (zero-width space, BOM ฯลฯ) และช่องว่างทุกชนิดออกจากข้อความทั้งหมด
    (ไม่ใช่แค่หัวท้าย เผื่อปุ่มเมนูแอบมีช่องว่างหรือ newline แทรกอยู่ตรงกลาง)
    ใช้เทียบกับปุ่มเมนูเท่านั้น เพื่อกันปัญหาปุ่มเมนูส่งอักขระแฝงมาปนแล้วเทียบไม่ตรง
    """
    if not text:
        return ""
    cleaned = re.sub(r"[\u200b-\u200f\ufeff]", "", str(text))
    cleaned = re.sub(r"\s+", "", cleaned)
    return cleaned


_MENU_RANDOM_RECOMMEND_NORMALIZED = {_normalize_menu_text(t) for t in MENU_RANDOM_RECOMMEND_TEXTS}


def _extract_search_terms(user_msg):
    """
    เตรียมคำค้นสำหรับจับคู่กับหนังสือ โดยลบวลี/คำฟุ่มเฟือยออกจากข้อความก่อนด้วย string replace
    (ไม่ใช่แยกเป็นชุดคำแล้วเทียบทีหลัง เพราะภาษาไทยไม่เว้นวรรคระหว่างคำ การแยกคำแบบ regex ทั่วไป
    จะเห็นทั้งประโยคเป็นคำเดียว) เหลือแต่ส่วนที่น่าจะเป็นคำค้นจริงๆ ไว้ใช้เทียบแบบ substring ต่อ
    คืนค่าเป็น list ของวลีที่เหลือ (อาจมีมากกว่า 1 ท่อนถ้าแยกด้วยช่องว่าง/เครื่องหมายวรรคตอน)
    """
    if not user_msg:
        return []
    cleaned = str(user_msg).lower()
    for phrase in _FILLER_PHRASES:
        cleaned = cleaned.replace(phrase, " ")
    # แยกด้วยช่องว่าง/เครื่องหมายวรรคตอนที่เหลืออยู่ เก็บเฉพาะท่อนที่ยาวพอจะมีความหมาย
    parts = re.split(r"[^a-zA-Z0-9\u0E00-\u0E7F]+", cleaned)
    return [p for p in parts if len(p) >= 2]


def fetch_all_books():
    """
    ดึงหนังสือทั้งหมดจาก Firestore เพียงครั้งเดียวต่อข้อความ แล้วส่งต่อให้ทั้งฟังก์ชันค้นหา
    และฟังก์ชันนับหมวดหมู่ใช้ข้อมูลชุดเดียวกัน (เดิมแต่ละฟังก์ชันอ่าน Firestore แยกกันคนละรอบ
    ทำให้ช้าโดยไม่จำเป็น) ไม่ใส่ limit เพื่อให้การนับหมวดหมู่ยังแม่นยำเท่าของเดิมทุกประการ
    """
    try:
        return list(db.collection("books").stream())
    except Exception as e:
        print(f"❌ Firestore Read Error: {e}")
        traceback.print_exc()
        return []


def get_books_context(all_docs, user_msg="", max_results=15, random_pick_count=5):
    """
    ค้นหาหนังสือที่เกี่ยวข้องกับคำถามของผู้ใช้จริง แทนการหยิบเล่มแรกๆ ในฐานข้อมูลแบบสุ่ม
    รับ all_docs ที่ดึงมาแล้วจาก fetch_all_books() เพื่อไม่ต้องอ่าน Firestore ซ้ำ
    วิธีทำงาน:
    0. ถ้าข้อความทั้งหมดตรงกับปุ่มเมนู "แนะนำหนังสือน่าอ่าน" พอดี (เทียบหลังล้างอักขระแฝงแล้ว)
       ให้ถือว่าเป็นการขอคำแนะนำทั่วไปทันที ข้ามการตัดคำฟุ่มเฟือยไปเลย เพื่อกันปัญหาตัดคำไม่หมด
    1. ไม่งั้นตัดคำฟุ่มเฟือยออกจากข้อความผู้ใช้ด้วย _extract_search_terms() (รองรับภาษาไทยไม่เว้นวรรค)
       แล้วเทียบแบบ substring กับ ชื่อเรื่อง (คะแนนสูงสุด) / ผู้แต่ง+คำสำคัญ+บทคัดย่อ (คะแนนรอง)
    2. เรียงตามคะแนนมากไปน้อย เลือกเฉพาะเล่มที่มีคะแนน > 0 มาส่งให้ Gemini
    3. ถ้าตัดคำฟุ่มเฟือยออกแล้วไม่เหลือคำค้นที่มีความหมายเลย (เช่น "แนะนำหนังสือน่าอ่าน" ที่กดจากเมนู
       หรือทักทายทั่วไป) ถือว่าเป็นการขอคำแนะนำทั่วไป — สุ่มหยิบ random_pick_count เล่มจากทั้งฐานข้อมูล
       มาแนะนำ (สุ่มใหม่ทุกครั้งที่ถาม ไม่ใช่เล่มเดิมซ้ำๆ)
    คืนค่า: (books_text, search_attempted)
      - search_attempted = True หมายถึงผู้ใช้พิมพ์คำค้นเจาะจงแล้วแต่ไม่เจอเล่มที่ตรงเลย
    """
    if not all_docs:
        return "ไม่มีข้อมูลหนังสือในระบบ", False

    normalized_msg = _normalize_menu_text(user_msg)
    if normalized_msg in _MENU_RANDOM_RECOMMEND_NORMALIZED:
        search_terms = []
    else:
        search_terms = _extract_search_terms(user_msg)
        # ชั้นป้องกันสุดท้าย: ถ้าตัดคำฟุ่มเฟือยตามปกติแล้วยังเหลือคำค้น แต่ข้อความดิบ (ตัดช่องว่าง/
        # อักขระแฝงแล้ว) กลับสั้นมากและเป็นส่วนหนึ่งของวลีปุ่มเมนูที่รู้จัก ก็ให้ถือว่าเป็นการกดเมนูอยู่ดี
        # กันไว้เผื่อมีสัญลักษณ์แปลกปลอมที่ _extract_search_terms ไม่ได้ถูกออกแบบมาให้ตัด
        if search_terms and any(
            normalized_msg and normalized_msg in known
            for known in _MENU_RANDOM_RECOMMEND_NORMALIZED
        ):
            search_terms = []

    search_attempted = bool(search_terms)

    if search_attempted:
        scored = []
        for doc in all_docs:
            b = doc.to_dict()
            title = str(_first(b, ["title", "Title", "book_name", "BookName"], "")).lower()
            other_text = " ".join(str(_first(b, k, "")) for k in [
                ["author", "Author", "book_author"],
                ["keywords", "Keyword", "subject", "Subject"],
                ["abstract", "Abstract", "content", "Description"],
            ]).lower()

            score = 0
            for term in search_terms:
                if term in title:
                    score += 3
                elif term in other_text:
                    score += 1
                elif title and title in term:
                    # กรณีคำค้นยาวกว่าชื่อเรื่องจริง (พิมพ์ชื่อเต็มปนคำอื่นมา)
                    score += 2
            if score > 0:
                scored.append((score, doc))

        scored.sort(key=lambda x: x[0], reverse=True)
        selected_docs = [d for _, d in scored[:max_results]]

        if not selected_docs:
            return "ไม่พบหนังสือที่ตรงกับคำค้นนี้ในฐานข้อมูล", True
    else:
        # ไม่มีคำค้นเจาะจง (เช่น กดปุ่มเมนู "แนะนำหนังสือน่าอ่าน" หรือทักทายทั่วไป)
        # สุ่มหยิบมาแนะนำใหม่ทุกครั้ง แทนการหยิบเล่มแรกๆ ซ้ำเดิมทุกครั้ง
        pool = all_docs
        selected_docs = random.sample(pool, min(random_pick_count, len(pool)))

    books_data = []
    for doc in selected_docs:
        b = doc.to_dict()

        title = _first(b, ["title", "Title", "book_name", "BookName"], "ไม่ระบุชื่อเรื่อง")
        author = _first(b, ["author", "Author", "book_author"], "ไม่ระบุผู้แต่ง")
        publisher_raw = _first(b, ["publisher", "Publisher", "PublicationName"], "")
        publisher, year_from_publisher = parse_publisher_year(publisher_raw)
        year = _first(b, ["year", "published_year", "publish_year"]) or year_from_publisher
        edition_num = parse_edition(_first(b, ["edition", "Edition"], ""))
        isbn = _first(b, ["isbn", "ISBN", "Isbn"], "")
        call_number = str(_first(b, ["call_number", "CallNumber", "callno", "CallNo"], "")).strip()
        abstract = _first(b, ["abstract", "Abstract", "content", "Description"], "")
        ddc_label = classify_ddc(call_number) if call_number else ""

        entry_lines = [f"ชื่อเรื่อง: {title}", f"ผู้แต่ง: {author}"]
        if edition_num:
            entry_lines.append(f"พิมพ์ครั้งที่: {edition_num}")
        if publisher:
            if year:
                entry_lines.append(f"สำนักพิมพ์: {publisher} (ปีที่พิมพ์: {year})")
            else:
                entry_lines.append(f"สำนักพิมพ์: {publisher}")
        if isbn:
            entry_lines.append(f"ISBN: {isbn}")
        if call_number:
            entry_lines.append(f"เลขเรียกหนังสือ: {call_number}")
        if ddc_label:
            entry_lines.append(f"หมวดหมู่ดิวอี้: {ddc_label}")
        if abstract:
            entry_lines.append(f"บทคัดย่อ: {abstract}")

        books_data.append("\n".join(entry_lines))

    result_text = "\n\n---\n\n".join(books_data) if books_data else "ไม่มีข้อมูลหนังสือในระบบ"
    return result_text, search_attempted


def get_category_summary(all_docs):
    try:
        category_counts = {}
        for doc in all_docs:
            b = doc.to_dict()
            call_number = _first(b, ["call_number", "CallNumber", "callno", "CallNo"], "")
            ddc_label = classify_ddc(call_number)
            category_counts[ddc_label] = category_counts.get(ddc_label, 0) + 1

        if not category_counts:
            return "ยังไม่มีข้อมูลหมวดหมู่หนังสือในระบบ"

        total_categories = len(category_counts)
        lines = [f"ห้องสมุดจัดหมวดหมู่ตามระบบทศนิยมดิวอี้ (Dewey Decimal Classification) มีทั้งหมด {total_categories} หมวดที่มีหนังสืออยู่จริง ได้แก่:"]
        for cat, count in sorted(category_counts.items()):
            lines.append(f"- {cat} ({count} เล่ม)")
        return "\n".join(lines)
    except Exception as e:
        print(f"❌ Firestore Category Count Error: {e}")
        traceback.print_exc()
        return "ไม่สามารถดึงข้อมูลหมวดหมู่ได้ในขณะนี้"


def clean_markdown(text):
    """
    ลบสัญลักษณ์ Markdown ทั้งหมดแบบเด็ดขาด (LINE ไม่รองรับการแสดงผล Markdown)
    ขั้นแรกลบแบบมีคู่ (**text**, *text*) ก่อน เพื่อดึงเนื้อความข้างในออกมาให้ครบ
    จากนั้น "กวาด" ดอกจัน/สัญลักษณ์เดี่ยวๆ ที่หลุดรอดออกมาทั้งหมดทิ้งไปเลย ไม่ว่าจะจับคู่ครบหรือไม่
    """
    if not text:
        return text

    # ลบตัวหนา/ตัวเอียงแบบมีคู่ก่อน เพื่อเก็บเนื้อหาข้างในไว้
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)

    # ลบสัญลักษณ์หัวข้อ Markdown (#, ##, ###)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)

    # แปลงจุดนำหน้าแบบ Markdown (-, *) ให้เป็นจุดไทยที่อ่านง่ายขึ้น
    text = re.sub(r"^[ \t]*[\*\-]\s+", "• ", text, flags=re.MULTILINE)

    # กวาดดอกจัน/สัญลักษณ์ Markdown ที่เหลือค้างทั้งหมดทิ้ง ไม่ว่าจะจับคู่ครบหรือไม่ก็ตาม
    text = re.sub(r"\*+", "", text)
    text = text.replace("##", "").replace("###", "")

    # จัดการบรรทัดว่างซ้ำๆ ให้เหลือแค่ 1 บรรทัดว่างระหว่างย่อหน้า
    text = re.sub(r"\n{3,}", "\n\n", text)

    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines).strip()

    return text


def call_gemini_with_retry(prompt, max_retries=1):
    for attempt in range(max_retries + 1):
        try:
            response = model.generate_content(prompt)
            return response.text.strip()
        except ResourceExhausted as e:
            print(f"\n⚠️ Gemini Rate Limit (attempt {attempt + 1}/{max_retries + 1}):")
            traceback.print_exc()
            if attempt < max_retries:
                wait_seconds = 15
                try:
                    retry_info = getattr(e, "retry_delay", None)
                    if retry_info and hasattr(retry_info, "seconds"):
                        wait_seconds = retry_info.seconds
                except Exception:
                    pass
                print(f"⏳ รอ {wait_seconds} วินาทีก่อนลองใหม่...")
                time.sleep(min(wait_seconds, 45))
            else:
                return None
        except Exception as e:
            print("\n❌ Gemini Error Details:")
            traceback.print_exc()
            return None
    return None


def save_user_interaction(user_id, user_message, ai_response):
    try:
        interaction_ref = db.collection("user_interactions")
        interaction_ref.add({
            "user_id": user_id,
            "user_message": user_message,
            "ai_response": ai_response,
            "timestamp": firestore.SERVER_TIMESTAMP
        })
    except Exception as e:
        print(f"❌ Firestore Write Error: {e}")


def create_flex_message(reply_text):
    safe_text = reply_text if len(reply_text) <= 1800 else reply_text[:1800] + " ..."

    flex_contents = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#006699",
            "contents": [
                {
                    "type": "text",
                    "text": "📚 หอสมุดกลาง ม.ราชภัฏสุราษฎร์ฯ",
                    "weight": "bold",
                    "color": "#FFFFFF",
                    "size": "md"
                }
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": safe_text,
                    "wrap": True,
                    "size": "sm",
                    "color": "#333333"
                }
            ]
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": "อาคารหอสมุดและศูนย์สารสนเทศเฉลิมพระเกียรติ มรส.",
                    "size": "xs",
                    "color": "#AAAAAA",
                    "align": "center"
                }
            ]
        }
    }
    return FlexSendMessage(alt_text="คำตอบจากบรรณารักษ์อัจฉริยะ", contents=flex_contents)


# ==========================================
# 4. Routes
# ==========================================
@app.route("/", methods=["GET"])
def health_check():
    return "บรรณารักษ์อัจฉริยะ กำลังทำงานอยู่ ✅", 200


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception:
        # กัน error อื่นๆ ที่ไม่คาดคิดทำให้ Flask ตอบ 500 ดิบๆ กลับ LINE
        # (LINE จะเห็นแค่ 500 เฉยๆ ไม่มีรายละเอียด แต่เราจะเห็น traceback เต็มใน log ของ Render)
        print("❌ Callback Error:", traceback.format_exc())
        return "OK", 200
    return "OK", 200


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_msg = event.message.text.strip()

    # DEBUG: พิมพ์ค่าดิบของข้อความที่ได้รับ (แบบ repr เห็นอักขระแฝง/ช่องว่างที่มองไม่เห็นด้วย)
    # ไว้ตรวจสอบชั่วคราวว่าปุ่มเมนูส่งอะไรมาจริงๆ ลบบรรทัดนี้ทิ้งได้เมื่อแก้ปัญหาเสร็จแล้ว
    print(f"DEBUG user_msg repr: {user_msg!r}")

    all_docs = fetch_all_books()
    books_context, search_attempted = get_books_context(all_docs, user_msg)
    category_summary = get_category_summary(all_docs)

    prompt = f"""
    คุณคือ 'บรรณารักษ์อัจฉริยะ' ประจำหอสมุดกลาง มหาวิทยาลัยราชภัฏสุราษฎร์ธานี (SRU Library)
    ให้บริการแก่นักศึกษา อาจารย์ บุคลากร และบุคคลภายนอก

    กฎสำคัญในการตอบคำถาม:
    1. ห้ามกล่าวทักทายยาวๆ ซ้ำซ้อนในทุกๆ คำตอบ
    2. ให้กล่าวทักทาย "สวัสดีค่ะ/ครับ" เฉพาะเมื่อผู้ใช้ทักทายมาก่อนเท่านั้น
    3. ตอบเข้าประเด็นทันที ด้วยคำพูดที่เป็นธรรมชาติ สุภาพ กระชับ เป็นกันเอง ไม่เยิ่นย้อ
    4. อ้างอิงข้อมูลหนังสือจากฐานข้อมูลนี้เมื่อผู้ใช้ถามหาหนังสือ
    5. เรื่องหมวดหมู่หนังสือ ให้ตอบตามข้อมูลหมวดหมู่ระบบดิวอี้จริงที่ให้ไว้ด้านล่างเท่านั้น ห้ามเดา
    6. ห้ามใช้สัญลักษณ์ Markdown เด็ดขาดทุกกรณี ห้ามใส่ * หรือ ** ล้อมรอบคำใดๆ ทั้งสิ้น ไม่ว่าจะเพื่อเน้นคำ ทำตัวหนา หรือทำเป็นหัวข้อย่อยก็ตาม เพราะ LINE ไม่รองรับการแสดงผล Markdown จะเห็นเป็นดอกจันดิบๆ ให้ใช้อิโมจิที่เหมาะสมนำหน้าแทนการเน้นคำหรือทำตัวหนาแทน
    7. ถ้าต้องการขึ้นหัวข้อย่อยหรือแยกหมวดหมู่ ให้ใช้ตัวเลข "1. 2. 3." หรืออิโมจิ (เช่น 📌 🔹) นำหน้าแบบข้อความธรรมดา ห้ามใช้ "-" หรือ "*" นำหน้าบรรทัดเด็ดขาด
    8. เว้นบรรทัดว่างระหว่างย่อหน้าพอประมาณ ไม่ถี่เกินไป
    9. ทุกครั้งที่พูดถึงหรือแนะนำหนังสือเล่มใดก็ตาม ไม่ว่าจะเป็นการแนะนำเล่มเดียวหรือหลายเล่มพร้อมกัน (เช่น ตอบคำถาม "มีหนังสือน่าอ่านไหม" หรือ "แนะนำหนังสือหมวด...") ให้แสดงข้อมูล "ทุกฟิลด์ที่มีข้อมูลอยู่จริงในฐานข้อมูลด้านล่าง" แยกรายบรรทัดตามลำดับนี้เป๊ะๆ ใช้อิโมจินำหน้าแต่ละบรรทัดตามนี้ (ห้ามข้ามฟิลด์ที่มีข้อมูลอยู่จริง):
       📖 ชื่อเรื่อง: [ชื่อหนังสือ]
       ✍️ ผู้แต่ง: [ชื่อผู้แต่ง]
       🖨️ พิมพ์ครั้งที่: [เลขครั้งที่พิมพ์ เฉพาะกรณีมีข้อมูลและเป็นครั้งที่ 2 ขึ้นไป]
       🏢 สำนักพิมพ์: [ชื่อสำนักพิมพ์] (ปีที่พิมพ์: [ปี ถ้ามีข้อมูล])
       🔖 ISBN: [เลข ISBN ถ้ามีข้อมูล]
       📚 เลขเรียกหนังสือ: [เลขเรียก ถ้ามีข้อมูล]
       🗂️ หมวดหมู่ดิวอี้: [หมวดดิวอี้ ถ้ามีข้อมูล]
       💡 จุดเด่น: [สรุปสั้นๆ ว่าทำไมน่าอ่าน อิงจากบทคัดย่อถ้ามี ถ้าไม่มีข้อมูลบทคัดย่อให้เขียนสรุปสั้นๆ จากชื่อเรื่อง/หมวดหมู่แทน]
       ข้อมูลแต่ละส่วน (ยกเว้น "จุดเด่น") ต้องมาจากฐานข้อมูลด้านล่างเท่านั้น ห้ามแต่งขึ้นเอง ถ้าฟิลด์ไหนไม่มีข้อมูลจริงๆ ให้ข้ามบรรทัดนั้นไปเลย ไม่ต้องเขียนว่า "ไม่ระบุ"
       ถ้าแนะนำหลายเล่ม ให้เว้นบรรทัดว่าง 1 บรรทัดคั่นระหว่างแต่ละเล่ม ไม่ต้องแบ่งเป็นหมวด "แนว..." ก่อน ให้แสดงรายการหนังสือแบบนี้เรียงต่อกันไปเลย
    10. ถ้าถามเรื่องย่อ/บทคัดย่อ ให้ตอบจากฟิลด์บทคัดย่อที่ให้ไว้ ถ้าไม่มีข้อมูลให้บอกตรงๆ ห้ามแต่งเนื้อหาขึ้นเอง
    11. ถ้าผู้ใช้ถามเรื่องสิทธิ์การยืม/ระยะเวลายืม/จำนวนที่ยืมได้ ให้ตอบตาม [ข้อมูลสิทธิ์การยืมทรัพยากรสารสนเทศ] ด้านล่างเท่านั้น ห้ามเดาหรือแต่งตัวเลขขึ้นเอง ถ้าผู้ใช้ถามถึงกลุ่ม "บุคคลภายนอก" ให้ตอบตรงๆ ว่าเว็บไซต์ห้องสมุดไม่ได้ระบุสิทธิ์การยืมของบุคคลภายนอกไว้ชัดเจน และแนะนำให้ติดต่อเจ้าหน้าที่หอสมุดโดยตรงตามเบอร์ที่ให้ไว้เพื่อสอบถามเงื่อนไข ห้ามสรุปเองว่าบุคคลภายนอกยืมได้หรือไม่ได้
    12. เมื่อตอบเรื่องสิทธิ์การยืม ให้ใช้อิโมจิ 🎓 นำหน้าแต่ละกลุ่มผู้ใช้ และจัดรูปแบบเป็นบรรทัดสั้นๆ อ่านง่ายบนมือถือ ไม่ต้องทำเป็นตารางเพราะ LINE แสดงตารางไม่ได้
    13. หนังสือใน [ผลการค้นหาหนังสือที่เกี่ยวข้องกับคำถาม] ด้านล่างนี้ ถูกกรองมาจากคำที่ผู้ใช้พิมพ์ถามแล้ว ไม่ใช่ตัวอย่างสุ่มจากทั้งระบบ ถ้าข้อความด้านล่างระบุว่า "ไม่พบหนังสือที่ตรงกับคำค้นนี้ในฐานข้อมูล" ให้บอกตรงๆ ตามนั้นว่าไม่พบในระบบ ห้ามหยิบหนังสือเล่มอื่นที่ไม่เกี่ยวข้องมาแนะนำแทนโดยไม่บอกผู้ใช้ว่าไม่ตรงกับที่ถาม

    [ผลการค้นหาหนังสือที่เกี่ยวข้องกับคำถาม]
    {books_context}

    [ข้อมูลหมวดหมู่หนังสือจริงในระบบ ณ ปัจจุบัน]
    {category_summary}

    [ข้อมูลสิทธิ์การยืมทรัพยากรสารสนเทศ อ้างอิงจาก library.sru.ac.th/circulation-service-sru-library ณ ปัจจุบัน]
    ผู้มีสิทธิ์ยืมตามที่เว็บไซต์ระบุไว้มี 6 กลุ่ม: นักศึกษาภาคปกติ, นักศึกษาภาค กศ.บท., นักศึกษาระดับบัณฑิตศึกษา (ป.โท), นักศึกษาระดับดุษฎีบัณฑิต (ป.เอก), อาจารย์, เจ้าหน้าที่/ผู้เกษียณอายุราชการ
    (เว็บไซต์ไม่ได้ระบุสิทธิ์การยืมของ "บุคคลภายนอก" ไว้อย่างชัดเจน)

    นักศึกษาภาคปกติ: หนังสือทั่วไป ยืมได้ 10 วัน | วิจัย/วิทยานิพนธ์ 7 รายการ ยืมได้ 7 วัน | ซีดีคู่หนังสือ 7 วัน | หนังสืออ้างอิง 7 วัน

    นักศึกษาภาค กศ.บท.: หนังสือทั่วไป ยืมได้ 14 วัน | วิจัย/วิทยานิพนธ์ 7 รายการ ยืมได้ 7 วัน | ซีดีคู่หนังสือ 7 วัน | หนังสืออ้างอิง 7 วัน

    นักศึกษาระดับบัณฑิตศึกษา (ป.โท): หนังสือทั่วไป ยืมได้ 14 วัน | วิจัย/วิทยานิพนธ์ 10 รายการ ยืมได้ 7 วัน | ซีดีคู่หนังสือ 7 วัน | หนังสืออ้างอิง 7 วัน

    นักศึกษาระดับดุษฎีบัณฑิต (ป.เอก): หนังสือทั่วไป ยืมได้ 14 วัน | วิจัย/วิทยานิพนธ์ 15 รายการ ยืมได้ 7 วัน | ซีดีคู่หนังสือ 7 วัน | หนังสืออ้างอิง 7 วัน

    อาจารย์: หนังสือทั่วไป ยืมได้ 120 วัน | วิจัย/วิทยานิพนธ์ 20 รายการ ยืมได้ 7 วัน | ซีดีคู่หนังสือ 7 วัน | หนังสืออ้างอิง 7 วัน

    เจ้าหน้าที่/ผู้เกษียณอายุราชการ: หนังสือทั่วไป ยืมได้ 30 วัน | วิจัย/วิทยานิพนธ์ 10 รายการ ยืมได้ 7 วัน | ซีดีคู่หนังสือ 7 วัน | หนังสืออ้างอิง 7 วัน

    ทุกกลุ่ม (สมาชิกทุกประเภท): วารสาร/นิตยสาร (ฉบับล่วงเวลา) ยืมได้ 3 ฉบับ / 7 วัน

    วิธียืม: ยืมด้วยตนเองที่หอสมุดโดยแสดงบัตรประจำตัว (ห้ามใช้บัตรผู้อื่นยืมแทน) หรือสืบค้นและยืมผ่านระบบยืมหนังสือออนไลน์ที่ https://checkoutsystem.sru.ac.th/home

    [ข้อมูลบริการหอสมุดกลาง มรส.]
    - ที่ตั้ง: อาคารหอสมุดและศูนย์สารสนเทศเฉลิมพระเกียรติ มรส.
    - ติดต่อ: 077-913-336 (ยืม-คืน ต่อ 5801) หรือ 081-891-7337 | library.sru.ac.th

    [คำถามจากผู้ใช้บริการ]
    {user_msg}
    """

    ai_reply = call_gemini_with_retry(prompt)
    if ai_reply is None:
        ai_reply = (
            "ขออภัยค่ะ/ครับ ขณะนี้มีผู้ใช้งานพร้อมกันจำนวนมาก "
            "ระบบประมวลผลไม่ทัน กรุณารอสักครู่แล้วลองพิมพ์ใหม่อีกครั้งนะคะ 🙏"
        )
    else:
        ai_reply = clean_markdown(ai_reply)

    save_user_interaction(user_id, user_msg, ai_reply)

    try:
        flex_msg = create_flex_message(ai_reply)
        line_bot_api.reply_message(event.reply_token, flex_msg)
    except Exception as e:
        print(f"❌ Line Reply Error: {e}")
        traceback.print_exc()
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=ai_reply))
        except Exception as e2:
            print(f"❌ Line Fallback Reply Error: {e2}")
            traceback.print_exc()


# ==========================================
# 5. Local dev entrypoint
# ==========================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
