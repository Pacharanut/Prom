# ADR-001: เพิ่มชั้น AI (Intent Layer) ใน Prom Integration Hub

- **สถานะ:** Accepted (Product Owner สั่งเริ่มขั้น 2A, 2026-10-26)
- **วันที่:** 2026-10-26
- **เกี่ยวข้อง:** `04-phase2-ai-plan.md`, `services/prom-integration-hub/AGENTS.md` (กฎ "No AI/LLM in Phase 1")

## บริบท

Phase 1 ปิดแล้ว: LINE และ Discord สร้าง/อัปเดต/ค้นหางาน และรับแจ้งเตือนได้ แต่ทั้งหมดขับด้วย
คีย์เวิร์ดตายตัว (`สร้างงาน`/`งานของฉัน`/`อัปเดต`) และปุ่ม ข้อความอิสระใดๆ ตกไปที่เมนู
(`src/channels/line/handler.py`, สาขา `else` ท้าย `_handle_text`). AI MVP ต้องการให้ผู้ใช้พิมพ์
ประโยคธรรมดา (ไทย/อังกฤษ) แล้วระบบเข้าใจ

Phase 1 ตั้งใจห้าม AI ไว้ (`AGENTS.md`, DoD ข้อ 25) และเก็บ "MCP confirm-guard pattern" ไว้เป็น
แนวทางสำหรับ Phase 2 (`00-gate0-1-discovery-and-plan.md` §Reuse matrix)

LLM ที่จะใช้คือ **model ที่องค์กรโฮสต์เอง เรียกผ่าน API แบบ OpenAI-compatible พร้อม API key**
ข้อมูลงานจึงไม่ออกนอกองค์กร

## การตัดสินใจ

1. **AI เป็นตัวแปลภาษาเท่านั้น ไม่มีสิทธิ์เขียนข้อมูลเอง.** LLM แปลงข้อความเป็น `ParsedCommand`
   (โครงสร้างตาม schema ที่กำหนด) เท่านั้น การทำงานจริงทุกอย่างผ่าน use case เดิมใน
   `src/application/work_items/use_cases.py` ด้วย delegated token ของผู้ใช้คนนั้น จึงได้สิทธิ์,
   audit log, idempotency และ tenant isolation ที่มีอยู่ต่อทันที
2. **ทุกคำสั่งที่เขียนข้อมูลต้องกดยืนยันก่อน** (confirm-guard) คำสั่งอ่านอย่างเดียว (ดูงาน/ค้นหา)
   ทำทันทีได้
3. **LLM ห้ามสร้าง ID เอง.** LLM คืนค่าเป็นข้อความดิบ (ชื่อ project, ชื่อคน, ชื่องาน, วลีวันที่)
   แล้ว Hub แปลงเป็น ID ด้วยโค้ดที่ตรวจสอบได้ (จับคู่กับรายการจาก Plane API) ถ้ากำกวม
   ให้แสดงตัวเลือกแบบปุ่มเดิม
4. **ล้มแล้วต้องกลับไปใช้แบบเดิมได้.** AI ปิด, timeout, ตอบผิด schema หรือความมั่นใจต่ำ
   ให้ตอบเมนูหรือปุ่มเหมือน Phase 1 บอทต้องไม่พังเพราะ AI
5. **ตามกฎ Hexagonal.** port `IntentParser` อยู่ใน `src/application/ai/`
   (ห้าม import httpx หรือ SDK ใดๆ) และ adapter `OpenAICompatibleIntentParser` อยู่ใน
   `src/adapters/ai/` โดยใช้ `httpx` ที่เป็น dependency อยู่แล้ว ไม่เพิ่ม SDK ใหม่
6. **ใช้ร่วมกันทั้ง LINE และ Discord** วางตัวแปลและ flow ยืนยันไว้ใน application layer
   ให้ทั้งสองช่องทางเรียกใช้ เหมือนที่ `CreateWorkItemFlow` ใช้ร่วมกันอยู่แล้ว
7. **ไม่เก็บเนื้อหาข้อความ** audit log บันทึกแค่ intent, ผลลัพธ์ และ latency ไม่บันทึกข้อความผู้ใช้หรือ
   prompt (คงกฎเดิมของ `src/domain/audit/entities.py`)

## สิ่งที่ต้องแก้ตามมา

- `AGENTS.md`: เปลี่ยนกฎ "No AI/LLM dependencies or code in this service during Phase 1" เป็น
  "AI code อยู่ได้เฉพาะ `src/application/ai/` (port และ logic ล้วน) กับ `src/adapters/ai/` (HTTP)
  และต้องผ่าน confirm-guard ก่อนเขียนข้อมูลเสมอ"
- import-linter: เพิ่ม `openai`, `anthropic`, `langchain` ใน forbidden modules ของ domain/application
  กันไม่ให้ SDK หลุดเข้าไป
- Settings ใหม่: `AI_ENABLED`, `NT_AI_HUB_BASE_URL` (`https://aihub.ntaisolution.com/core-api/v1`),
  `NT_AI_HUB_KEY`, `NT_AI_HUB_MODEL` (เริ่มที่ `gpt-oss-120b`), `AI_TIMEOUT_SECONDS`
  ผลเลือก model ดู `04-phase2-ai-plan.md` §9

## ทางเลือกที่ไม่เลือก

- **ให้ LLM เรียก tool และเขียนข้อมูลเอง (agent loop):** คาดเดายาก ทดสอบยาก และเสี่ยง
  prompt injection จากชื่อหรือคำอธิบายงานใน Plane
- **ใช้ rule/regex อย่างเดียว:** รองรับประโยคไทยที่หลากหลายไม่ไหว แต่ยังใช้ rule กับส่วนที่แน่นอน
  เช่น วันที่สัมพัทธ์และคีย์เวิร์ดเดิม
- **ใช้ LLM ภายนอก (Claude/OpenAI):** ไม่เลือกเพราะองค์กรมี server ของตัวเอง แต่ port
  ออกแบบให้เปลี่ยนไปใช้ภายนอกภายหลังได้โดยเขียน adapter ใหม่ตัวเดียว

## ความเสี่ยง

- **คุณภาพภาษาไทยของ model ที่โฮสต์เอง:** ต้องวัดด้วยชุดทดสอบก่อนเปิดใช้จริง (ดูแผน §การวัดผล)
- **Latency:** LINE reply token มีอายุจำกัด ถ้า model ช้าต้องเปลี่ยนเป็น push message ซึ่งนับโควตา LINE OA
- **Prompt injection:** จำกัดผลกระทบด้วย schema ที่ตายตัว, การยืนยันก่อนเขียน และสิทธิ์ของผู้ใช้เอง
