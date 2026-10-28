# Phase 2: AI & Automation — แผนการพัฒนา

> **สถานะ (2026-10-26):** ขั้น **2A เสร็จแล้ว** (ข้อ 1, 3, 6, 7) พร้อมหน้า Settings → AI Assistant
> ใน Plane — รายละเอียดใน `services/prom-integration-hub/README.md` §"AI assistant (Phase 2A)".
> ทดสอบสดกับ `gpt-oss-120b` 16/16 ประโยค ยังไม่ได้ทำชุด eval 150 ประโยค (§2.7).
> ขั้น **2B เสร็จแล้ว** (ข้อ 4, 5): สรุปงานรายวัน 09:00 + สั่งสรุปเองได้, ตั้งค่าแจ้งเตือนใน Connected
> Accounts และในแชท — README §"Daily summary & notification settings (Phase 2B)".
> ขั้น **2C เสร็จแล้ว** (ข้อ 8): กฎ keyword → label/priority ต่อ workspace + AI เลือกจาก label ที่มีอยู่
> ใช้ทั้ง flow AI และ flow ปุ่ม — README §"Auto-tagging (Phase 2C)".
> ขั้น **2D เสร็จแล้ว** (ข้อ 9): ผูกกลุ่ม LINE / ช่อง Discord แจ้งงานใหม่/มอบหมาย/งานเสร็จ.
> ขั้น **2E เสร็จแล้ว** (ข้อ 10): timeline ต่องานระบุแหล่ง LINE/Discord/Plane + ค้นหา audit log.
> **ครบทั้ง 10 ข้อของ AI MVP แล้ว** (ยังไม่มีชุด eval 150 ประโยค; ทดสอบสด 24/24)

แผนนี้อิงจากสเปก AI MVP 10 ข้อ เทียบกับโค้ด Phase 1 ที่มีอยู่จริง (สถานะ 2026-10-26:
unit test 206 ผ่าน, ruff/mypy/import-linter ผ่าน) การตัดสินใจเชิงสถาปัตยกรรมอยู่ใน
`04-adr-001-ai-intent-layer.md`

ข้อตกลงที่ได้จาก Product Owner:
- LLM: server ขององค์กร เรียกผ่าน API แบบ OpenAI-compatible + API key
- ช่องทาง: LINE และ Discord
- ทำแผนให้อ่านก่อน ยังไม่เขียนโค้ด

## 1. สถานะเทียบสเปก

| # | ฟีเจอร์ | มีแล้วใน Phase 1 | ต้องทำเพิ่ม | ใช้ AI? |
|---|---|---|---|---|
| 1 | สร้างงานจากข้อความ | `CreateWorkItemFlow` แบบปุ่ม + ขั้น REVIEW | แยกชื่อ/วัน/คน/project/สถานะจากประโยคเดียว, วันที่สัมพัทธ์ | ใช่ |
| 2 | Sync LINE ↔ Plane | ครบ: สร้าง, เปลี่ยนสถานะ, เปลี่ยนคน, ปิดงาน, คอมเมนต์ + Plane webhook แจ้งกลับ | — | ไม่ |
| 3 | สั่งงานด้วยภาษาพูด | ทุกคำสั่งทำผ่านปุ่มได้ ยกเว้นเลื่อน deadline | intent detection, "task นี้", use case เลื่อน deadline | ใช่ |
| 4 | สรุปงานประจำวัน | beat 09:00 Asia/Bangkok แจ้งทีละงาน (due_soon/due_today/overdue) | รวมเป็นข้อความเดียว + recently assigned + blocked | เลือกได้ |
| 5 | Smart Notification | trigger ครบ, ตาราง `NotificationPreference` มีแล้ว | API/UI/คำสั่ง LINE ให้ผู้ใช้เปิดปิดเอง | ไม่ |
| 6 | อัปเดตสถานะจากข้อความ | `UpdateWorkItemState`, `MarkComplete` | หางานเป้าหมายจากชื่อในประโยค | ใช่ |
| 7 | ค้นหางาน | `SearchWorkItems` (keyword), `ListMyWorkItems` | แปลงประโยคเป็นตัวกรอง, กรองตาม project/คน | ใช่ |
| 8 | Auto tag / priority | ไม่มี (create ยังส่ง label/priority ไม่ได้) | ส่ง label/priority, rule + LLM, mapping ตั้งค่าได้ | ใช่ |
| 9 | Workflow อัตโนมัติ | assign → แจ้งคนรับ, overdue → เตือน | ปิดงาน → แจ้ง **กลุ่ม** (ยังไม่รองรับกลุ่ม LINE/ช่อง Discord) | ไม่ |
| 10 | Timeline / audit | audit log ของ Hub, activity ของ Plane | timeline ต่องาน ระบุแหล่ง (LINE/Discord/Plane), ค้นได้ | ไม่ |

ข้อ 2 เสร็จแล้ว ข้อ 5, 9 และ 10 ไม่ต้องใช้ AI เป็นงานระบบล้วน

## 2. สถาปัตยกรรมชั้น AI (ใช้ร่วมกันสำหรับข้อ 1, 3, 6, 7)

```
ข้อความ ─▶ คีย์เวิร์ดเดิม? ──ใช่──▶ flow Phase 1 เดิม
              │ไม่
              ▼
        IntentParser (LLM)  ─ล้ม/ไม่มั่นใจ─▶ เมนูเดิม
              │ ParsedCommand (ข้อความดิบ)
              ▼
        CommandResolver (โค้ดล้วน) ─ แปลงชื่อ → ID ผ่าน Plane API, วันที่ → date
              │ กำกวม → ปุ่มเลือก (picker เดิม)
              ▼
   อ่านอย่างเดียว ──▶ ทำทันที (ListMyWorkItems / SearchWorkItems)
   เขียนข้อมูล   ──▶ ข้อความยืนยัน [ยืนยัน] [แก้ไข] [ยกเลิก] ──▶ use case เดิม
```

### 2.1 โมดูลใหม่

| ไฟล์ | หน้าที่ |
|---|---|
| `src/application/ai/ports.py` | `IntentParser` protocol: `async parse(text, ctx) -> ParsedCommand` |
| `src/application/ai/commands.py` | `ParsedCommand` (pydantic) + enum `Intent` |
| `src/application/ai/dates.py` | `resolve_relative_date(expr, today)` แบบ rule-based (ไทย/อังกฤษ) |
| `src/application/ai/resolver.py` | `CommandResolver`: จับคู่ชื่อ project/คน/งาน เป็น ID |
| `src/application/ai/assistant_flow.py` | `AssistantFlow`: state `ai_confirm`, เรียก use case เดิม |
| `src/adapters/ai/openai_compatible.py` | `OpenAICompatibleIntentParser` (httpx → `{AI_BASE_URL}/v1/chat/completions`) |
| `src/adapters/redis/focus_store.py` | จำ "งานที่กำลังคุยถึง" ต่อผู้ใช้ (TTL 30 นาที) สำหรับคำว่า "task นี้" |

จุดเชื่อม: สาขา `else` ท้ายการจัดการข้อความใน `src/channels/line/handler.py` (ตอนนี้ตอบเมนู)
และจุดเดียวกันใน `src/channels/discord/handler.py` ส่วน presenter แยกต่อช่องทางเหมือนเดิม
(LINE ใช้ Quick Reply, Discord ใช้ Button/Select)

### 2.2 Intent schema

```python
class Intent(StrEnum):
    CREATE_WORK_ITEM = "create_work_item"   # ข้อ 1
    LIST_MY_TASKS    = "list_my_tasks"      # ข้อ 3: today | overdue | due_soon | assigned
    SEARCH           = "search"             # ข้อ 7
    CHANGE_STATE     = "change_state"       # ข้อ 3, 6: target_group = started | completed
    REASSIGN         = "reassign"           # ข้อ 3
    RESCHEDULE       = "reschedule"         # ข้อ 3 (ใหม่)
    ADD_COMMENT      = "add_comment"
    SHOW_MENU        = "show_menu"
    UNKNOWN          = "unknown"

class ParsedCommand(BaseModel):
    intent: Intent
    confidence: float                 # < 0.6 → ถามกลับหรือแสดงเมนู
    title: str | None                 # ชื่องานใหม่
    target_ref: str | None            # "API Gateway", "this", "นี้"
    project_ref: str | None           # "AI", "infra"
    assignee_ref: str | None          # "Somchai", "Nok", "me"
    due_expression: str | None        # "วันศุกร์", "สิ้นเดือน", "+2d"
    state_ref: str | None             # "in progress", "เสร็จ"
    filter: Literal["today","overdue","due_soon","assigned"] | None
    keyword: str | None
    comment: str | None
```

ตัวอย่าง: "สร้างงาน Deploy AI Platform ส่งวันศุกร์ Assign ให้ Somchai" จะได้
`{intent: create_work_item, title: "Deploy AI Platform", due_expression: "วันศุกร์", assignee_ref: "Somchai"}`
จากนั้น resolver หา Somchai จาก project members และแปลง "วันศุกร์" เป็นวันที่ ถ้าไม่ระบุ project
ให้ใช้ picker เดิม

### 2.3 Adapter และ Settings

- เรียก `POST {AI_BASE_URL}/v1/chat/completions` ด้วย `Authorization: Bearer {AI_API_KEY}`,
  `temperature=0` และส่ง `response_format` แบบ JSON schema ถ้า server รองรับ (vLLM และ Ollama
  รองรับคนละรูปแบบ ต้องตรวจกับ server จริง) ถ้าไม่รองรับให้ใช้ prompt บังคับ JSON แทน
- validate ผลด้วย pydantic ทุกครั้ง ถ้าผิด schema ให้ถือว่าเป็น `UNKNOWN`
- timeout ตาม `AI_TIMEOUT_SECONDS` (เริ่มที่ 8 วินาที) retry เฉพาะ 5xx/timeout ครั้งเดียว ไม่ retry 4xx
- ใน prompt ใส่วันที่วันนี้ (Asia/Bangkok) และรายชื่อ project ของผู้ใช้ (สูงสุด 10) เพื่อช่วยจับ `project_ref`
- `AI_API_KEY` อยู่ใน `.env` เท่านั้น ห้าม log

### 2.4 วันที่สัมพัทธ์ (ข้อ 1, 3)

ใช้ rule-based (`dates.py`) เป็นหลัก เพราะทดสอบได้แน่นอน: วันนี้, พรุ่งนี้, มะรืน, วันจันทร์–อาทิตย์
(หมายถึงวันนั้นครั้งถัดไป), สัปดาห์หน้า, สิ้นเดือน, สิ้นสัปดาห์, อีก N วัน/สัปดาห์, today, tomorrow,
next Friday, end of month, in N days และรูปแบบ `YYYY-MM-DD`, `DD/MM`, `DD/MM/YYYY`
(ปี พ.ศ. ให้ลบ 543) ถ้า rule แปลไม่ได้ ให้ใช้วันที่ ISO ที่ LLM เสนอ แต่ต้องแสดงในข้อความยืนยันชัดเจน

`parse_due_date` เดิมใน `conversation.py` (รับแค่ `YYYY-MM-DD`) ให้เรียก `dates.py` ด้วย
flow แบบปุ่มก็จะรองรับ "พรุ่งนี้" ไปพร้อมกัน

### 2.5 การหางานเป้าหมาย (ข้อ 3, 6)

1. ถ้า `target_ref` เป็น "นี้/this/มัน" หรือว่าง ให้ใช้งานล่าสุดใน focus store (งานที่เพิ่งแสดง
   สร้าง หรือแก้) ถ้าไม่มี ให้ถามกลับ
2. ถ้าเป็นชื่องาน ให้เรียก `SearchWorkItems(query=target_ref)`: เจอ 1 รายการไปขั้นยืนยัน,
   เจอหลายรายการแสดง picker (สูงสุด 10), ไม่เจอให้แจ้งว่าไม่พบ
3. ถ้าเป็นรหัสงานแบบ `PROJ-123` ให้จับคู่ตรงกับ identifier + sequence_id

### 2.6 การเปลี่ยนสถานะ (ข้อ 6)

ตาม MVP: Todo → In Progress (state group `unstarted` → `started`) และ In Progress → Done
(`started` → `completed`, ใช้ `MarkComplete` เดิม) ให้ map วลีด้วย rule ก่อน ("เริ่มทำ", "กำลังทำ" →
started และ "เสร็จแล้ว", "done" → completed) แล้วจึงใช้ LLM ถ้า project มีหลาย state ในกลุ่มเดียวกัน
ให้เลือกตัวแรกตาม sequence ของ Plane

### 2.7 การวัดผล

- `tests/ai/`: unit test ด้วย `FakeIntentParser` (ไม่เรียก LLM จริง) ครอบคลุม resolver, dates และ flow
- `evals/intent_golden.jsonl`: ประโยคไทย/อังกฤษประมาณ 150 ประโยค พร้อมผลที่คาดหวัง และสคริปต์
  `python -m evals.run_intent_eval` ยิงไปที่ server จริงเพื่อรายงานความแม่นต่อ intent และต่อ field
- **เกณฑ์ก่อนเปิดใช้จริง (เสนอ):** intent ถูกต้อง ≥ 90%, field ที่ใช้เขียนข้อมูลถูกต้อง ≥ 95%,
  p95 latency ≤ 5 วินาที

## 3. ข้อ 4: สรุปงานประจำวัน

- use case ใหม่ `SendDailySummary` แทนการแจ้งเตือนทีละงานตอน 09:00 ใน `CheckDueDateNotifications`
  (ผู้ใช้ที่ปิด summary ยังได้แบบทีละงาน) เพิ่ม event type `DAILY_SUMMARY`
- เนื้อหา:
  - overdue / due today: ใช้ `ListMyWorkItems` เดิม
  - recently assigned: ดึงจาก `OutboundMessageRecord` ประเภท ASSIGNED ใน 24 ชั่วโมงที่ผ่านมา ซึ่ง Hub มีอยู่แล้ว
  - blocked: ใช้ endpoint `work-items/{id}/relations/` ของ Plane เรียกเฉพาะงานที่อยู่ในรายการสรุป (จำกัด 20 งาน)
- AI ใส่ได้แต่ไม่จำเป็น: บรรทัดสรุปสั้นๆ ที่ LLM เขียน ถ้า LLM ล้มให้ใช้ template ปกติ

## 4. ข้อ 5: ตั้งค่าการแจ้งเตือน

- route ใหม่ `GET/PUT /api/v1/identities/{id}/notification-preferences` ใช้ repository ที่มีอยู่แล้ว
  และควรแก้ `set_enabled` ให้ใช้ SAVEPOINT ตามที่ README บันทึกไว้
- patch 0001 เพิ่ม relay ใน `prom_bridge`, patch 0002 เพิ่ม toggle ในหน้า Connected Accounts
- คำสั่งในแชท "ตั้งค่าแจ้งเตือน" แสดงปุ่มเปิด/ปิดต่อประเภท

## 5. ข้อ 8: Auto tag และ priority

- ขยาย `PromApiClient`: `create_work_item(..., priority, label_ids)` และ `list_labels`
  (`workspaces/{slug}/projects/{id}/labels/` มีใน public API แล้ว)
- ตารางใหม่ `classification_rules` (ต่อ workspace connection): `keyword → label_id / priority / state`
- ลำดับการทำงาน: ใช้ rule ก่อน ถ้าไม่ตรงให้ LLM เลือก **จาก label ที่มีอยู่แล้วเท่านั้น** (ไม่สร้าง label ใหม่)
  ผลที่ได้แสดงในข้อความยืนยันและผู้ใช้แก้ได้
- MVP ครอบคลุมเฉพาะงานที่สร้างผ่าน Hub ไม่รวมงานที่สร้างใน Plane โดยตรง

## 6. ข้อ 9: Workflow อัตโนมัติ (แจ้งกลุ่ม)

ส่วนที่ขาดคือรองรับกลุ่ม: ตอนนี้ `src/channels/line/events.py` อ่านแค่ `source.userId`

- ตารางใหม่ `group_bindings`: installation, `group_id` (LINE groupId / Discord channel id),
  workspace connection, project (เลือกได้)
- ผูกกลุ่มด้วยคำสั่งในกลุ่ม "ผูกกลุ่ม" โดยผู้ใช้ที่ link แล้ว จากนั้นเลือก project
- ตาราง `workflow_rules` แบบ predefined เท่านั้น: `on_completed_notify_group`,
  `on_assigned_notify_assignee` (มีอยู่แล้ว), `on_overdue_remind` (มีอยู่แล้ว)
- ต่อจาก `ProcessPromWebhookEvent` เดิม: เมื่อเห็นงานถูกปิด ให้ส่ง push เข้ากลุ่มที่ผูกไว้
- ในกลุ่ม บอทไม่รับคำสั่ง AI ใน MVP เพื่อเลี่ยงการตอบทุกข้อความ

## 7. ข้อ 10: Timeline และ audit log

- route ใหม่ `GET /api/v1/work-items/{id}/timeline` รวมข้อมูล 2 แหล่ง:
  - Plane `work-items/{id}/activities/`: ใครแก้ field อะไร เมื่อไร
  - audit log ของ Hub ที่ `target_id` เป็นงานนั้น: แก้จาก LINE หรือ Discord
- ระบุแหล่งที่มา: Plane บันทึกการแก้จาก Hub เป็นชื่อผู้ใช้คนนั้น (เพราะใช้ token ของเขา) จึงต้องจับคู่
  กับ audit log ของ Hub (งาน + field + เวลาใกล้กันไม่เกินไม่กี่วินาที) ถ้าตรงแสดงเป็น "LINE"/"Discord"
  ถ้าไม่ตรงแสดงเป็น "Plane" วิธีนี้เป็นการประมาณ ต้องระบุไว้ใน README
- ค้นหาได้ตามผู้แก้, แหล่ง, ช่วงเวลา และ field ทั้งผ่าน API และคำสั่ง "ประวัติ <ชื่องาน>"

## 8. ลำดับการทำงาน

| ขั้น | ขอบเขต | ผลลัพธ์ |
|---|---|---|
| **2A** | ADR อนุมัติ, settings, port+adapter, `dates.py`, resolver, `AssistantFlow`, `RescheduleWorkItem`, เชื่อม LINE+Discord, ชุด eval | ข้อ 1, 3, 6, 7 |
| **2B** | Daily summary, preference API/UI/คำสั่ง | ข้อ 4, 5 |
| **2C** | label/priority ใน client, `classification_rules`, LLM classification | ข้อ 8 |
| **2D** | รองรับกลุ่ม, `group_bindings`, `workflow_rules` | ข้อ 9 |
| **2E** | Timeline API + UI ใน Plane + คำสั่งแชท | ข้อ 10 |

ทุกขั้นต้องผ่าน `ruff`, `mypy --strict`, `pytest`, `lint-imports` และมี migration ของ Alembic ถ้าเพิ่มตาราง
ขั้น 2A ต้องผ่านเกณฑ์ eval ใน §2.7 ก่อนเปิด `AI_ENABLED=true` ให้ผู้ใช้จริง

## 9. ผลทดสอบ LLM gateway (2026-10-26)

Gateway คือ NT AI Hub (MATCHA) แบบ OpenAI-compatible:
- API base: `https://aihub.ntaisolution.com/core-api/v1` (`/v1/...` ที่ root ของโดเมนเป็นหน้าเว็บ ตอบ 404)
- Auth: `Authorization: Bearer $NT_AI_HUB_KEY`
- Settings ที่ใช้จริง: `NT_AI_HUB_BASE_URL`, `NT_AI_HUB_KEY`, `NT_AI_HUB_MODEL` (แทนชื่อ `AI_*` ใน §2.3)

ทดสอบ 8 ประโยคตัวอย่างจากสเปก (ไทย 7, อังกฤษ 1), `temperature=0`:

| Model | ถูก | median | max |
|---|---|---|---|
| `gpt-oss-120b` | 7/8 | 1.9 วินาที | 2.6 วินาที |
| `Qwen3.8-27B` | 7/8 | 4.4 วินาที | 6.2 วินาที |
| `openthai2.0` | 7/8 | 7.9 วินาที | 12.6 วินาที |

- ทั้ง 3 ตัวแยกวัน "วันศุกร์" และ "end of month" เป็นวันที่ถูก และจับชื่อคน project และ "นี้" ได้ถูก
- ทุกตัวผิดประโยคเดียวกัน คือ "งานของ project AI" ตอบเป็น `list_my_tasks` แทน `search` สาเหตุคือ
  intent ทับซ้อนกันเอง แก้ด้วยการให้ `list_my_tasks` และ `search` รับ `project_ref` ได้ทั้งคู่ แล้วให้
  resolver ตัดสินว่าไม่มี filter ส่วนตัว (`today`/`overdue`/ฉัน) เท่ากับ search ตาม project
- `gpt-oss-120b` รองรับ `response_format: json_schema` แบบ strict (ทดสอบแล้วได้ JSON ตรง schema)
- **เลือก `gpt-oss-120b` เป็น model หลัก** เพราะเร็วที่สุดและอยู่ในเวลาที่ LINE reply token รับได้
  ส่วน `Qwen3.8-27B` เป็นตัวสำรอง

ผลนี้เป็นการทดสอบเบื้องต้น 8 ประโยคเท่านั้น ยังต้องวัดด้วยชุด eval ประมาณ 150 ประโยคใน §2.7
ก่อนเปิดใช้จริง

## 10. คำถามที่ยังเปิดอยู่

1. เกณฑ์ใน §2.7 ใช้ได้หรือไม่
2. ในกลุ่ม (ข้อ 9) ให้บอทรับคำสั่งด้วยหรือไม่ ใน MVP เสนอว่าไม่รับ
3. โควตาหรือ rate limit ของ key บน NT AI Hub (ใช้กำหนด rate limit ฝั่ง Hub)
