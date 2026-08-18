import os
import json
import traceback
from flask import Flask, request, abort

import firebase_admin
from firebase_admin import credentials, firestore
import google.generativeai as genai

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
            category = b.get("category", "ทั่วไป")
            summary = b.get("summary", "ไม่มีเรื่องย่อ")
            books_data.append(
                f"- ชื่อเรื่อง: {title} | ผู้แต่ง: {author} | หมวดหมู่: {category} | สาระสำคัญ: {summary}"
            )
        return "\n".join(books_data) if books_data else "ไม่มีข้อมูลหนังสือในระบบ"
    except Exception as e:
        print(f"❌ Firestore Read Error: {e}")
        traceback.print_exc()
        return "ไม่มีข้อมูลหนังสือในระบบ"


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

    prompt = f"""
    คุณคือ 'บรรณารักษ์อัจฉริยะ' ประจำหอสมุดกลาง มหาวิทยาลัยราชภัฏสุราษฎร์ธานี (SRU Library)
    ให้บริการแก่นักศึกษา อาจารย์ บุคลากร และบุคคลภายนอก

    กฎสำคัญในการตอบคำถาม:
    1. **ห้ามกล่าวทักทายยาวๆ ซ้ำซ้อน** เช่น "สวัสดีค่ะ ยินดีต้อนรับสู่หอสมุดกลาง..." ในทุกๆ คำตอบ
    2. ให้กล่าวทักทาย "สวัสดีค่ะ/ครับ" **เฉพาะเมื่อผู้ใช้ทักทายมาก่อนเท่านั้น** (เช่น พิมพ์ สวัสดี, หวัดดี, Hello)
    3. หากผู้ใช้ถามข้อมูล ค้นหาหนังสือ หรือสอบถามบริการ **ให้ตอบเข้าประเด็นทันที** ด้วยคำพูดที่เป็นธรรมชาติ สุภาพ กระชับ ไม่เยิ่นย้อ
    4. อ้างอิงข้อมูลหนังสือจากฐานข้อมูลนี้เมื่อผู้ใช้ถามหาหนังสือ:

    [ฐานข้อมูลหนังสือ หอสมุดกลาง มรส.]
    {books_context}

    [ข้อมูลบริการหอสมุดกลาง มรส.]
    - ที่ตั้ง: อาคารหอสมุดและศูนย์สารสนเทศเฉลิมพระเกียรติ มรส.
    - ติดต่อ: 077-913-336 (ยืม-คืน ต่อ 5801) | library.sru.ac.th

    [คำถามจากผู้ใช้บริการ]
    {user_msg}
    """

    try:
        response = model.generate_content(prompt)
        ai_reply = response.text.strip()
    except Exception as e:
        print("\n❌ Gemini Error Details:")
        traceback.print_exc()
        ai_reply = "ขออภัยค่ะ/ครับ เกิดข้อผิดพลาดในการประมวลผลคำตอบชั่วคราว โปรดลองอีกครั้ง"

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
