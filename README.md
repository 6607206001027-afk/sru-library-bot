# บรรณารักษ์อัจฉริยะ — Deploy บน Render (ฟรี ไม่ต้องผูกบัตร)

## เตรียมของ 3 อย่างก่อนเริ่ม
1. บัญชี GitHub (ฟรี)
2. บัญชี Render — สมัครที่ https://render.com (สมัครด้วย GitHub ได้เลย)
3. ไฟล์ `serviceAccountKey.json` ของโปรเจกต์ (อันเดิมที่ใช้ใน Colab)

---

## ขั้นตอนที่ 1: อัปโหลดโค้ดขึ้น GitHub
1. ไปที่ github.com สร้าง repository ใหม่ ตั้งชื่อ เช่น `sru-library-bot`
   - ตั้งเป็น **Private** ไว้ก่อน (กันคนนอกเห็นโครงสร้างโค้ด แม้ว่าคีย์จริงจะไม่ได้อยู่ในนี้ก็ตาม)
2. อัปโหลดไฟล์ 3 ไฟล์นี้เข้า repo (ลาก-วางในหน้าเว็บ GitHub ได้เลย ไม่ต้องใช้ terminal):
   - `app.py`
   - `requirements.txt`
   - `Procfile`

## ขั้นตอนที่ 2: สร้าง Web Service บน Render
1. ล็อกอิน Render → กด **New +** → **Web Service**
2. เลือก repo `sru-library-bot` ที่เพิ่งสร้าง
3. ตั้งค่า:
   - **Name**: ตั้งชื่อได้ตามใจ เช่น `sru-library-bot`
   - **Region**: Singapore (ใกล้ไทยที่สุด)
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app`
   - **Instance Type**: Free
4. **ยังไม่ต้องกด Create** — ไปตั้งค่า Environment Variables ก่อน (ขั้นตอนที่ 3)

## ขั้นตอนที่ 3: ใส่ Environment Variables
เลื่อนลงมาหาหัวข้อ **Environment Variables** ในหน้าเดียวกัน แล้วเพิ่มทีละตัว (Key / Value):

| Key | Value |
|---|---|
| `LINE_CHANNEL_ACCESS_TOKEN` | token จาก LINE Developers Console |
| `LINE_CHANNEL_SECRET` | secret จาก LINE Developers Console |
| `GEMINI_API_KEY` | API key จาก Google AI Studio |
| `FIREBASE_CREDENTIALS_JSON` | **เนื้อหาทั้งหมด** ของไฟล์ `serviceAccountKey.json` (ดูวิธีคัดลอกด้านล่าง) |

### วิธีเอาเนื้อหา serviceAccountKey.json มาใส่ FIREBASE_CREDENTIALS_JSON
เปิดไฟล์ `serviceAccountKey.json` ด้วย Notepad/text editor แล้ว **copy ทั้งหมด** (ตั้งแต่ `{` ตัวแรกถึง `}` ตัวสุดท้าย) วางลงในช่อง Value ของ `FIREBASE_CREDENTIALS_JSON` ได้เลยแบบไม่ต้องแก้ format ใดๆ — Render รับ multi-line value ได้

⚠️ **สำคัญ:** ต้อง revoke คีย์ชุดเดิมที่เคยหลุดไปในสกรีนช็อตก่อนหน้า แล้วสร้างคีย์ใหม่มาใส่ในนี้ ไม่ใช่ชุดเดิม

## ขั้นตอนที่ 4: Deploy
1. กด **Create Web Service**
2. รอ build (~2-5 นาที) ดู log ว่าขึ้น `Your service is live 🎉`
3. จะได้ URL ถาวรประมาณ `https://sru-library-bot.onrender.com`

## ขั้นตอนที่ 5: อัปเดต LINE Webhook URL
1. ไปที่ LINE Developers Console → Channel ของโปรเจกต์ → Messaging API
2. ใส่ Webhook URL เป็น: `https://sru-library-bot.onrender.com/callback`
3. กด **Verify** ให้ขึ้นเครื่องหมายถูกสีเขียว
4. เปิด **Use webhook** เป็น ON

เสร็จแล้ว! ทดสอบทักบอทใน LINE ได้เลย ไม่ต้องเปิด Colab ทิ้งไว้อีกต่อไป

---

## หมายเหตุเรื่อง Free tier ของ Render
- ถ้าไม่มีคนทักบอทเกิน ~15 นาที service จะ "sleep" อัตโนมัติ
- พอมีข้อความเข้าอีกครั้ง Render จะปลุก service ขึ้นมา ซึ่ง**ข้อความแรกอาจตอบช้า 30-50 วินาที** (รอบถัดไปจะเร็วปกติ)
- ถ้าอยากให้ตอบเร็วตลอดเวลาแบบไม่ sleep เลย ต้องอัปเกรดเป็น instance แบบเสียเงิน — สำหรับโปรเจกต์นักศึกษา/สาธิต free tier เพียงพอแล้ว

## ถ้า deploy แล้ว error
ดู log ได้ที่แท็บ **Logs** ในหน้า Render service — error ที่พบบ่อยคือ:
- ลืมใส่ Environment Variable ตัวใดตัวหนึ่ง → แอปจะไม่ start และบอก error ชัดเจนว่าขาดตัวไหน
- `FIREBASE_CREDENTIALS_JSON` วางไม่ครบ/ตัด format ผิด → จะขึ้น error "ไม่ใช่ JSON ที่ถูกต้อง"
