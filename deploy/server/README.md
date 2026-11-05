# Deploy Prom ขึ้นเครื่อง adminmc (เครื่อง Docker ที่ใช้ร่วมกับคนอื่น)

เครื่อง `praew@118.174.9.188` (hostname `adminmc`) มีงานของคนอื่นรันอยู่ด้วย
**ทุกคำสั่ง docker ของ Prom ต้องสั่งผ่าน `deploy/server/prom.sh` เท่านั้น**
สคริปต์นี้ใส่ชื่อโปรเจ็ค (`-p prom-plane` หรือ `-p prom-hub`) และไฟล์ compose ให้ทุกครั้ง
และจะปฏิเสธ flag ที่ลบข้อมูล

## ภาพรวม

มี 2 โหมด เลือกได้ด้วย `PROM_MODE` ใน `server.env`

**โหมด `ngrok` (ใช้ทดสอบตอนนี้)** ใช้ URL ngrok ชื่อเดียวรับทั้ง Plane และ Hub ไม่ต้องมี DNS และไม่แตะ nginx ของเครื่อง
```
อินเทอร์เน็ต ─► ngrok ─► prom-edge-ngrok ─► prom-edge-nginx (127.0.0.1:8280)
                                              ├─ /webhooks/*  ─► 127.0.0.1:8001 ─► Hub
                                              └─ /*           ─► 127.0.0.1:8180 ─► Plane
```

**โหมด `domain` (ใช้งานจริง)** ใช้ subdomain จริงผ่าน nginx ของเครื่อง
```
อินเทอร์เน็ต ─► nginx บนเครื่อง (80/443, ใบรับรอง *.ntaisolution.com)
                 ├─ prom.ntaisolution.com      ─► 127.0.0.1:8180 ─► Plane
                 └─ prom-hub.ntaisolution.com  ─► 127.0.0.1:8001 ─► Hub (เปิดสาธารณะเฉพาะ /webhooks/)
```

ทั้งสองโหมดมีส่วนที่เหมือนกันดังนี้
- Plane เรียก API ของ Hub ผ่านเครือข่าย Docker ภายในชื่อ `prom-net` (`http://prom-hub-api:8000`) ไม่ผ่านอินเทอร์เน็ต
- Postgres, Redis, RabbitMQ และ MinIO ไม่เปิดพอร์ตออกนอกเครือข่าย Docker เลย

| ไฟล์ | หน้าที่ |
|---|---|
| `prom.sh` | ตัวครอบ `docker compose` ใช้แทนการพิมพ์ `docker compose` ตรง ๆ (`plane`, `hub`, `edge`) |
| `init-env.sh` | สร้างไฟล์ env ทั้ง 4 ไฟล์พร้อมรหัสผ่านสุ่ม ไม่เขียนทับไฟล์ที่มีอยู่แล้ว |
| `plane.override.yml` / `hub.override.yml` | ตั้งชื่อโปรเจ็ค เปลี่ยนชื่อ container เป็น `prom-…` และผูกพอร์ตกับ 127.0.0.1 |
| `plane.domain.yml` / `hub.domain.yml` | ใช้เฉพาะโหมด domain: ชี้โดเมนของเราไปที่ nginx บนเครื่องโดยตรง |
| `edge/` | ใช้เฉพาะโหมด ngrok: nginx ตัวเล็กกับ ngrok agent |
| `nginx/prom.ntaisolution.com.conf` | ใช้เฉพาะโหมด domain: vhost ใหม่ของ Prom แยกไฟล์จาก taco |

## ห้ามทำบนเครื่องนี้

- `docker system prune`, `docker volume prune`, `docker image prune` และ prune ทุกชนิด
- `docker compose down -v` หรือ `docker volume rm`
- คำสั่งที่ใช้ `$(docker ps -aq)` หรือกระทบทุก container
- `systemctl restart docker` หรือ `systemctl restart nginx` ให้ใช้ `reload` แทน
- แก้ไฟล์ `/etc/nginx/sites-enabled/taco.ntaisolution.com` หรือแตะ container หรือ volume ที่ไม่ได้ขึ้นต้นด้วย `prom-`

## ขั้นตอนครั้งแรก

### 0. สิ่งที่ต้องได้ก่อน (โหมด ngrok)
- [ ] บัญชี ngrok ที่ https://dashboard.ngrok.com ใช้ 2 อย่าง
  - **Authtoken** อยู่ที่เมนู Your Authtoken
  - **โดเมนฟรีแบบคงที่** อยู่ที่เมนู Domains เช่น `xxxx.ngrok-free.app` ต้องเป็นชื่อที่ไม่เปลี่ยน เพราะจะนำไปวางใน LINE
- [ ] (ถ้าได้) ให้ผู้ดูแลเพิ่ม `praew` เข้ากลุ่ม `docker` ถ้ายังไม่ได้ `prom.sh` จะใช้ `sudo docker` ให้เอง

### 1. Clone โค้ด
```bash
cd ~
git clone --recurse-submodules https://github.com/Pacharanut/Prom.git
cd ~/Prom
```

### 2. สร้างไฟล์ env
```bash
deploy/server/init-env.sh            # รอบแรก: สร้างแค่ deploy/server/server.env แล้วหยุด
nano deploy/server/server.env        # ใส่ NGROK_AUTHTOKEN, NGROK_DOMAIN และตั้ง PROM_DOMAIN = PROM_HUB_DOMAIN = NGROK_DOMAIN
deploy/server/init-env.sh            # รอบสอง: สร้างอีก 3 ไฟล์จากค่าที่ใส่
```
จากนั้นเก็บสำรองไฟล์ทั้ง 4 ไว้ในที่ปลอดภัย:
- `deploy/server/server.env`
- `plane/.env`
- `plane/apps/api/.env`
- `services/prom-integration-hub/.env`

**ถ้าทำ `SECRET_ENCRYPTION_KEY` หาย ข้อมูลรับรอง LINE/Discord ที่บันทึกไว้จะถอดรหัสไม่ได้อีก**

ถ้าจะใส่ AI key ใน `.env` ของ Hub (`NT_AI_HUB_KEY`) ให้เปิดไฟล์แก้เอง หรือจะไปตั้งในหน้า Settings → AI Assistant ทีหลังก็ได้

### 3. Build image
```bash
deploy/server/prom.sh hub build api worker beat discord-bot
deploy/server/prom.sh plane build          # ใช้เวลานาน (Node build) รอได้
```

### 4. เปิด Hub
```bash
deploy/server/prom.sh hub up -d postgres redis
deploy/server/prom.sh hub run --rm --entrypoint alembic api upgrade head
deploy/server/prom.sh hub up -d api worker beat
curl -s http://127.0.0.1:8001/healthz            # ต้องได้ {"status":"ok"} หรือคล้ายกัน
```
ยังไม่ต้องเปิด `discord-bot` ถ้ายังไม่มี Discord installation ตัวบอทจะปิดตัวเองแล้วรีสตาร์ทวนไปเรื่อย ๆ

### 5. เปิด Plane
```bash
deploy/server/prom.sh plane up -d
deploy/server/prom.sh plane ps
curl -sI http://127.0.0.1:8180 | head -1         # ต้องได้ HTTP 200 หรือ 30x
```

### 6. เปิด ngrok
```bash
deploy/server/prom.sh edge up -d
deploy/server/prom.sh edge logs --tail=30 ngrok     # ต้องเห็น "started tunnel" และ URL ของเรา
```
ตรวจผลจากเครื่องตัวเอง (Mac):
```bash
curl -sI https://<NGROK_DOMAIN> | head -1                                   # 200 หรือ 30x
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<NGROK_DOMAIN>/webhooks/v1/line/x   # 4xx จาก Hub ถือว่าถูกต้อง
```
ngrok แบบฟรีจะแสดงหน้าเตือนตอนเปิดผ่านเบราว์เซอร์ครั้งแรก ให้กด Visit Site ได้เลย หน้านี้ไม่มีผลกับ webhook

### 7. ตั้งค่า Plane ครั้งแรก
1. เปิด `https://<PROM_DOMAIN>/god-mode` แล้วสร้างบัญชี instance admin
2. สร้าง workspace และ project

### 8. ผูก Plane กับ Hub
ทำครั้งเดียวต่อฐานข้อมูล Hub
```bash
cd ~/Prom
TOKEN=$(grep -E '^HUB_SERVICE_TOKEN=' services/prom-integration-hub/.env | cut -d= -f2-)
PROM_DOMAIN=$(grep -E '^PROM_DOMAIN=' deploy/server/server.env | cut -d= -f2-)

CUSTOMER_ID=$(curl -s -X POST http://127.0.0.1:8001/api/v1/customers \
  -H "X-Hub-Service-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "NT"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

INSTANCE_ID=$(curl -s -X POST http://127.0.0.1:8001/api/v1/prom-instances \
  -H "X-Hub-Service-Token: $TOKEN" -H "Content-Type: application/json" \
  -d "{\"customer_id\": \"$CUSTOMER_ID\", \"name\": \"Prom (adminmc)\", \"base_url\": \"https://$PROM_DOMAIN\"}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

echo "CUSTOMER_ID=$CUSTOMER_ID  INSTANCE_ID=$INSTANCE_ID"
sed -i "s/^PROM_CUSTOMER_ID=.*/PROM_CUSTOMER_ID=$CUSTOMER_ID/; s/^PROM_INSTANCE_ID=.*/PROM_INSTANCE_ID=$INSTANCE_ID/" plane/apps/api/.env

# env เปลี่ยนแล้วต้อง recreate ไม่ใช่แค่ restart
deploy/server/prom.sh plane up -d --force-recreate api worker beat-worker
```

### 9. เชื่อม LINE, Discord และ AI ผ่านหน้าเว็บ
ทำเหมือนตอนทดสอบบนเครื่องตัวเอง ต่างกันแค่ URL:
- **LINE:** สร้าง installation ใน Integrations แล้วนำ Webhook URL ที่ได้ (`https://<PROM_HUB_DOMAIN>/webhooks/v1/line/…`) ไปวางใน LINE Developers Console
- **Plane webhook:** ตั้งค่า workspace connection แล้วนำ URL `https://<PROM_HUB_DOMAIN>/webhooks/v1/prom/…` ไปวางใน Plane → Settings → Webhooks
- **Discord:** สร้าง installation แล้วรัน `deploy/server/prom.sh hub up -d discord-bot` ทุกครั้งที่เพิ่มหรือเพิกถอน installation ต้องรัน `deploy/server/prom.sh hub restart discord-bot`
- **AI:** ตั้งค่าที่ Settings → AI Assistant

## เปลี่ยนจาก ngrok เป็นโดเมนจริง
ทำเมื่อ DNS ของ `prom.ntaisolution.com` และ `prom-hub.ntaisolution.com` ใช้งานได้แล้ว (เช็คด้วย `dig +short` ต้องได้ `118.174.9.188`)

1. ติดตั้ง nginx vhost (ขั้นด้านล่าง)
2. แก้ `deploy/server/server.env` ให้เป็น `PROM_MODE=domain`, `PROM_DOMAIN=prom.ntaisolution.com` และ `PROM_HUB_DOMAIN=prom-hub.ntaisolution.com`
3. แก้ URL ของ ngrok เป็นโดเมนจริงในไฟล์ต่อไปนี้
   - `plane/apps/api/.env`: แก้ `CORS_ALLOWED_ORIGINS`, `WEB_URL`, `APP_BASE_URL`, `ADMIN_BASE_URL`, `SPACE_BASE_URL`, `LIVE_BASE_URL` และเพิ่ม 2 บรรทัดนี้:
     ```
     WEBHOOK_ALLOWED_HOSTS=prom-hub.ntaisolution.com
     WEBHOOK_ALLOWED_IPS=172.16.0.0/12
     ```
   - `services/prom-integration-hub/.env`: แก้ `PROM_API_BASE_URL` และ `HUB_PUBLIC_BASE_URL`
4. แก้ `base_url` ที่บันทึกไว้ใน Hub (ยังไม่มี API สำหรับแก้ค่านี้ จึงต้องแก้ในฐานข้อมูลตรง):
   ```bash
   deploy/server/prom.sh hub exec postgres psql -U prom prom_hub -c \
     "UPDATE prom_instances SET base_url='https://prom.ntaisolution.com' WHERE base_url LIKE '%ngrok%';"
   ```
5. recreate ทุก service แล้วปิด ngrok:
   ```bash
   deploy/server/prom.sh hub up -d --force-recreate api worker beat discord-bot
   deploy/server/prom.sh plane up -d --force-recreate api worker beat-worker
   deploy/server/prom.sh edge down
   ```
6. แก้ Webhook URL ใน LINE Developers Console และใน Plane → Settings → Webhooks ให้เป็น `https://prom-hub.ntaisolution.com/...`

### ติดตั้ง nginx vhost
ทำหลังจาก DNS ใช้งานได้และผู้ดูแลเครื่องอนุญาตแล้ว
```bash
sudo cp deploy/server/nginx/prom.ntaisolution.com.conf /etc/nginx/sites-available/prom.ntaisolution.com
sudo ln -s /etc/nginx/sites-available/prom.ntaisolution.com /etc/nginx/sites-enabled/prom.ntaisolution.com
sudo nginx -t                    # ต้องขึ้น "syntax is ok" และ "test is successful" ถ้าไม่ผ่าน ห้าม reload
sudo systemctl reload nginx      # reload ไม่ตัดการเชื่อมต่อของ taco
```
ตรวจผลจากเครื่องตัวเอง (Mac):
```bash
curl -sI https://prom.ntaisolution.com | head -1                  # 200
curl -s -o /dev/null -w '%{http_code}\n' https://prom-hub.ntaisolution.com/healthz   # 403 (ถูกบล็อกจากภายนอก ถือว่าถูกต้อง)
```
ตรวจว่า container เรียกโดเมนของเราผ่าน nginx ได้:
```bash
deploy/server/prom.sh hub exec api python -c "import urllib.request as u; print(u.urlopen('https://prom.ntaisolution.com').status)"
deploy/server/prom.sh plane exec api python -c "import urllib.request as u; print(u.urlopen('https://prom-hub.ntaisolution.com/healthz').status)"   # ต้อง recreate ในโหมด domain ก่อน
```
ทั้งสองคำสั่งต้องได้ `200`

## อัปเดตเวอร์ชันใหม่
```bash
cd ~/Prom
git pull && git submodule update --init --recursive
deploy/server/prom.sh hub build api worker beat discord-bot
deploy/server/prom.sh hub run --rm --entrypoint alembic api upgrade head
deploy/server/prom.sh hub up -d api worker beat discord-bot
deploy/server/prom.sh plane build
deploy/server/prom.sh plane up -d
```

## ดูสถานะ, log และสำรองข้อมูล
```bash
deploy/server/prom.sh hub ps
deploy/server/prom.sh hub logs -f --tail=100 api worker
deploy/server/prom.sh plane logs -f --tail=100 api

# สำรองฐานข้อมูล
mkdir -p ~/prom-backups
deploy/server/prom.sh hub exec -T postgres pg_dump -U prom prom_hub | gzip > ~/prom-backups/hub-$(date +%F).sql.gz
deploy/server/prom.sh plane exec -T plane-db pg_dump -U plane plane | gzip > ~/prom-backups/plane-$(date +%F).sql.gz
```

## Rollback (Hub)
ต้องย้อน migration ก่อน แล้วค่อยย้อนโค้ด:
```bash
deploy/server/prom.sh hub run --rm --entrypoint alembic api downgrade <revision>
git checkout <commit เดิม>
deploy/server/prom.sh hub build api worker beat discord-bot
deploy/server/prom.sh hub up -d api worker beat discord-bot
```

## หมายเหตุ
- ใบรับรอง `*.ntaisolution.com` หมดอายุ **27 ธ.ค. 2026** เจ้าของใบรับรองเป็นคนต่ออายุ
  เมื่อไฟล์ใบใหม่ถูกวางทับที่เดิม ให้รัน `sudo nginx -t && sudo systemctl reload nginx`
- ถ้าพอร์ต 8180 หรือ 8001 ถูกใช้ไปแล้วในวันที่ deploy ให้แก้ค่าใน `server.env` และใน `nginx/…conf` ให้ตรงกัน
