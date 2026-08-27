import os
import json
import re
import time
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
# ค่าเหล่านี้ต้องไปตั้งใน Render Dashboard -> Environment
# ห้ามใส่ค่าจริงลงในไฟล์นี้เด็ดขาด (ไฟล์นี้จะถูก push ขึ้น GitHub)
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

# ตั้งค่า Gemini AI
genai.configure(api_key=GEMINI_API_KEY)
GEMINI_MODEL_NAME = "gemini-3.5-flash"
model = genai.GenerativeModel(GEMINI_MODEL_NAME)

# ==========================================
# 2. เชื่อมต่อ Firebase
# ==========================================
# FIREBASE_CREDENTIALS_JSON คือเนื้อหาทั้งหมดของ serviceAccountKey.json
# ที่ถูกใส่เป็น environment variable แบบ string เดียว (ไม่ต้องอัปโหลดไฟล์ทุกครั้งเหมือน Colab)
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
def get_books_context(limit=30):
    try:
        books_ref = db.collection("books").limit(limit)
        docs = books_ref.stream()
        books_data = []
        for doc in docs:
            b = doc.to_dict()
            title = b.get("title", "ไม่ระบุชื่อเรื่อง")
            author = b.get("author", "ไม่ระบุผู้แต่ง")
            edition = b.get("edition", "").strip()
            publisher = b.get("publisher", "ไม่ระบุสำนักพิมพ์")
            isbn = b.get("isbn", "")
            call_number = b.get("call_number", "").strip()
            abstract = b.get("abstract", "") or b.get("content", "")

            # จัดรูปแบบคล้ายบรรณานุกรม APA: ผู้แต่ง. ชื่อเรื่อง (พิมพ์ครั้งที่). สำนักพิมพ์. เลขเรียกหนังสือ.
            citation = f"{author}. {title}"
            if edition:
                citation += f" ({edition})"
            citation += f". {publisher}."
            if call_number:
                citation += f" เลขเรียกหนังสือ: {call_number}."
            if isbn:
                citation += f" ISBN: {isbn}."
            if abstract:
                citation += f" บทคัดย่อ: {abstract}"

            books_data.append(f"- {citation}")
        return "\n".join(books_data) if books_data else "ไม่มีข้อมูลหนังสือในระบบ"
    except Exception as e:
        print(f"❌ Firestore Read Error: {e}")
        traceback.print_exc()
        return "ไม่มีข้อมูลหนังสือในระบบ"


# หมวดหมู่ใหญ่ตามระบบทศนิยมดิวอี้ (DDC) แบ่งตามเลข 3 หลักแรกของเลขเรียกหนังสือ (call_number)
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


def clean_markdown(text):
    """
    ลบสัญลักษณ์ Markdown ที่ Gemini ชอบใส่มา (LINE ไม่รองรับการแสดงผล Markdown)
    เช่น **ตัวหนา**, *ตัวเอียง*, # หัวข้อ, - จุดนำหน้า และจัดการเว้นบรรทัดให้อ่านง่ายขึ้น
    """
    if not text:
        return text

    # ลบตัวหนา/ตัวเอียงแบบ Markdown: **text**, *text*, __text__, _text_
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)

    # ลบสัญลักษณ์หัวข้อ Markdown (#, ##, ###)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)

    # แปลงจุดนำหน้าแบบ Markdown (-, *) ให้เป็นจุดไทยที่อ่านง่ายขึ้น (รองรับกรณีมีการเยื้อง/ย่อหน้าด้วย)
    text = re.sub(r"^[ \t]*[\*\-]\s+", "• ", text, flags=re.MULTILINE)

    # ลบดอกจัน/สัญลักษณ์ที่หลงเหลืออยู่เดี่ยวๆ
    text = text.replace("**", "").replace("##", "").replace("###", "")

    # จัดการบรรทัดว่างซ้ำๆ ให้เหลือแค่ 1 บรรทัดว่างระหว่างย่อหน้า
    text = re.sub(r"\n{3,}", "\n\n", text)

    # ลบช่องว่างท้ายบรรทัดที่ไม่จำเป็น
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines).strip()

    return text


def get_category_summary():
    """
    นับหมวดหมู่หนังสือจริงจาก Firestore ตามระบบทศนิยมดิวอี้ (Dewey Decimal Classification)
    โดยอ่านจาก call_number ของแต่ละเล่ม (ไม่ให้ Gemini เดาเอง)
    คืนค่าเป็นข้อความสรุปจำนวนหมวดหมู่ทั้งหมด พร้อมรายชื่อหมวดหมู่และจำนวนเล่มในแต่ละหมวด
    """
    try:
        books_ref = db.collection("books")
        docs = books_ref.stream()
        category_counts = {}
        for doc in docs:
            b = doc.to_dict()
            call_number = b.get("call_number", "")
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


def call_gemini_with_retry(prompt, max_retries=1):
    """
    เรียก Gemini API พร้อม retry อัตโนมัติเมื่อโดน rate limit (ResourceExhausted / 429)
    Google Gemini free tier มีโควต้าจำกัดต่อนาที ถ้าโดน limit จะบอกเวลาที่ต้องรอ (retry_delay)
    เรารอตามเวลานั้นแล้วลองใหม่อัตโนมัติ 1 ครั้ง ก่อนจะยอมแพ้และคืนค่า None
    """
    for attempt in range(max_retries + 1):
        try:
            response = model.generate_content(prompt)
            return response.text.strip()
        except ResourceExhausted as e:
            print(f"\n⚠️ Gemini Rate Limit (attempt {attempt + 1}/{max_retries + 1}):")
            traceback.print_exc()
            if attempt < max_retries:
                # พยายามอ่านเวลาที่ Google แนะนำให้รอจาก error, ถ้าไม่มีให้รอ default 15 วินาที
                wait_seconds = 15
                try:
                    retry_info = getattr(e, "retry_delay", None)
                    if retry_info and hasattr(retry_info, "seconds"):
                        wait_seconds = retry_info.seconds
                except Exception:
                    pass
                print(f"⏳ รอ {wait_seconds} วินาทีก่อนลองใหม่...")
                time.sleep(min(wait_seconds, 45))  # ไม่รอเกิน 45 วิ กัน LINE reply token หมดอายุ
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
    # Render จะเรียก path นี้เพื่อเช็คว่า service ยังมีชีวิตอยู่
    return "บรรณารักษ์อัจฉริยะ กำลังทำงานอยู่ ✅", 200


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_msg = event.message.text.strip()

    books_context = get_books_context()
    category_summary = get_category_summary()

    prompt = f"""
    คุณคือ 'บรรณารักษ์อัจฉริยะ' ประจำหอสมุดกลาง มหาวิทยาลัยราชภัฏสุราษฎร์ธานี (SRU Library)
    ให้บริการแก่นักศึกษา อาจารย์ บุคลากร และบุคคลภายนอก

    กฎสำคัญในการตอบคำถาม:
    1. ห้ามกล่าวทักทายยาวๆ ซ้ำซ้อน เช่น "สวัสดีค่ะ ยินดีต้อนรับสู่หอสมุดกลาง..." ในทุกๆ คำตอบ
    2. ให้กล่าวทักทาย "สวัสดีค่ะ/ครับ" เฉพาะเมื่อผู้ใช้ทักทายมาก่อนเท่านั้น (เช่น พิมพ์ สวัสดี, หวัดดี, Hello)
    3. หากผู้ใช้ถามข้อมูล ค้นหาหนังสือ หรือสอบถามบริการ ให้ตอบเข้าประเด็นทันที ด้วยคำพูดที่เป็นธรรมชาติ สุภาพ กระชับ เป็นกันเอง ไม่เยิ่นย้อ
    4. อ้างอิงข้อมูลหนังสือจากฐานข้อมูลนี้เมื่อผู้ใช้ถามหาหนังสือ
    5. หากผู้ใช้ถามเรื่องหมวดหมู่หนังสือ ให้ตอบตามข้อมูลหมวดหมู่ระบบดิวอี้ (Dewey) จริงที่ให้ไว้ด้านล่างเท่านั้น ห้ามเดาหรือคาดเดาจำนวนหมวดหมู่เอง
    6. ห้ามใช้สัญลักษณ์ Markdown เด็ดขาด (ห้ามใช้ **ตัวหนา**, *ตัวเอียง*, # หัวข้อ, ``` โค้ด) เพราะแอป LINE ไม่รองรับการแสดงผล Markdown จะเห็นเป็นสัญลักษณ์ดิบๆ ให้เขียนเป็นข้อความธรรมดาล้วนๆ แทน
    7. ถ้าต้องการขึ้นหัวข้อย่อยหรือรายการ ให้ใช้เครื่องหมาย "- " หรือตัวเลข "1. 2. 3." นำหน้าบรรทัดแบบข้อความธรรมดา ไม่ใช้สัญลักษณ์ Markdown อื่น
    8. เว้นบรรทัดว่างระหว่างย่อหน้าหรือหัวข้อเพื่อให้อ่านง่ายบนมือถือ แต่ไม่เว้นบรรทัดถี่เกินไป
    9. เมื่อแนะนำหรือกล่าวถึงหนังสือเล่มใดเล่มหนึ่ง ให้แสดงข้อมูลในรูปแบบคล้ายบรรณานุกรม (เรียงเป็นข้อความต่อเนื่อง ไม่ใช่ตาราง) ประกอบด้วย ผู้แต่ง ชื่อเรื่อง สำนักพิมพ์ เลขเรียกหนังสือ ตามข้อมูลจริงที่ให้ไว้ด้านล่างเท่านั้น ห้ามแต่งข้อมูลที่ไม่มีในฐานข้อมูล
    10. ถ้าผู้ใช้ถามเรื่องย่อ/บทคัดย่อของหนังสือเล่มใด ให้ตอบจากฟิลด์บทคัดย่อที่ให้ไว้ ถ้าไม่มีข้อมูลในระบบให้บอกตรงๆ ว่ายังไม่มีข้อมูลเรื่องย่อเล่มนี้ ห้ามแต่งเนื้อหาขึ้นเอง

    [ฐานข้อมูลหนังสือ หอสมุดกลาง มรส.]
    {books_context}

    [ข้อมูลหมวดหมู่หนังสือจริงในระบบ ณ ปัจจุบัน]
    {category_summary}

    [ข้อมูลบริการหอสมุดกลาง มรส.]
    - ที่ตั้ง: อาคารหอสมุดและศูนย์สารสนเทศเฉลิมพระเกียรติ มรส.
    - ติดต่อ: 077-913-336 (ยืม-คืน ต่อ 5801) | library.sru.ac.th

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
# 5. Local dev entrypoint (Render ใช้ gunicorn แทน ไม่ใช้ส่วนนี้)
# ==========================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
