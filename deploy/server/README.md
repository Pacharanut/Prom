# Deploy Prom ขึ้นเครื่อง adminmc (เครื่อง Docker ที่ใช้ร่วมกับคนอื่น)

เครื่อง `praew@118.174.9.188` (hostname `adminmc`) มีงานของคนอื่นรันอยู่ด้วย
**ทุกคำสั่ง docker ของ Prom ต้องสั่งผ่าน `deploy/server/prom.sh` เท่านั้น**
สคริปต์นี้ใส่ชื่อโปรเจ็ค (`-p prom-plane` หรือ `-p prom-hub`) และไฟล์ compose ให้ทุกครั้ง
และจะปฏิเสธ flag ที่ลบข้อมูล

## ภาพรวม

```
อินเทอร์เน็ต ─► nginx บนเครื่อง (80/443, ใบรับรอง *.ntaisolution.com)
                 ├─ prom.ntaisolution.com      ─► 127.0.0.1:8180 ─► prom-plane-proxy (Caddy) ─► Plane
                 └─ prom-hub.ntaisolution.com  ─► 127.0.0.1:8001 ─► prom-hub-api
                      /webhooks/*  เปิดสาธารณะ (LINE, Plane webhook มีลายเซ็น HMAC)
                      /api/*       เฉพาะ 127.0.0.1 และเครือข่าย Docker (172.16.0.0/12)
```

- Postgres, Redis, RabbitMQ และ MinIO ของเราไม่เปิดพอร์ตออกนอกเครือข่าย Docker เลย
- container ที่ต้องเรียกโดเมนของเราเอง (Plane api/worker และ Hub ทุกตัว) ใช้ `extra_hosts` ชี้โดเมนไปที่
  nginx บนเครื่องโดยตรง (`host-gateway`) จึงไม่ต้องพึ่ง NAT hairpin

| ไฟล์ | หน้าที่ |
|---|---|
| `prom.sh` | ตัวครอบ `docker compose` ใช้แทนการพิมพ์ `docker compose` ตรง ๆ |
| `init-env.sh` | สร้างไฟล์ env 4 ไฟล์พร้อมรหัสผ่านสุ่ม ไม่เขียนทับไฟล์ที่มีอยู่แล้ว |
| `plane.override.yml` / `hub.override.yml` | ตั้งชื่อโปรเจ็ค เปลี่ยนชื่อ container เป็น `prom-…` และผูกพอร์ตกับ 127.0.0.1 |
| `nginx/prom.ntaisolution.com.conf` | vhost ใหม่ของ Prom แยกไฟล์จาก taco |
| `server.env.example` | ค่าโดเมนและพอร์ต |

## ห้ามทำบนเครื่องนี้

- `docker system prune`, `docker volume prune`, `docker image prune` และ prune ทุกชนิด
- `docker compose down -v` หรือ `docker volume rm`
- คำสั่งที่ใช้ `$(docker ps -aq)` หรือกระทบทุก container
- `systemctl restart docker` หรือ `systemctl restart nginx` ให้ใช้ `reload` แทน
- แก้ไฟล์ `/etc/nginx/sites-enabled/taco.ntaisolution.com` หรือแตะ container หรือ volume ที่ไม่ได้ขึ้นต้นด้วย `prom-`

## ขั้นตอนครั้งแรก

### 0. สิ่งที่ต้องได้ก่อน
- [ ] DNS ชนิด A: `prom.ntaisolution.com` และ `prom-hub.ntaisolution.com` ชี้ไปที่ `118.174.9.188`
- [ ] ผู้ดูแลเครื่องอนุญาตให้เพิ่มไฟล์ nginx และ reload ได้
- [ ] (ถ้าได้) ให้ผู้ดูแลเพิ่ม `praew` เข้ากลุ่ม `docker` ถ้ายังไม่ได้ `prom.sh` จะใช้ `sudo docker` ให้เอง

### 1. Clone โค้ด
```bash
cd ~
git clone --recurse-submodules https://github.com/Pacharanut/Prom.git
cd ~/Prom
```

### 2. สร้างไฟล์ env
```bash
deploy/server/init-env.sh
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

### 6. ติดตั้ง nginx vhost
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
curl -s -o /dev/null -w '%{http_code}\n' https://prom-hub.ntaisolution.com/api/v1/customers   # 403 (ถูกบล็อกจากภายนอก ถือว่าถูกต้อง)
```
ตรวจว่า container เรียกโดเมนของเราผ่าน nginx ได้:
```bash
deploy/server/prom.sh hub exec api python -c "import urllib.request as u; print(u.urlopen('https://prom.ntaisolution.com').status)"
deploy/server/prom.sh plane exec api python -c "import urllib.request as u; print(u.urlopen('https://prom-hub.ntaisolution.com/healthz').status)"
```
ทั้งสองคำสั่งต้องได้ `200`

### 7. ตั้งค่า Plane ครั้งแรก
1. เปิด `https://prom.ntaisolution.com/god-mode` แล้วสร้างบัญชี instance admin
2. สร้าง workspace และ project

### 8. ผูก Plane กับ Hub
ทำครั้งเดียวต่อฐานข้อมูล Hub
```bash
cd ~/Prom
TOKEN=$(grep -E '^HUB_SERVICE_TOKEN=' services/prom-integration-hub/.env | cut -d= -f2-)

CUSTOMER_ID=$(curl -s -X POST http://127.0.0.1:8001/api/v1/customers \
  -H "X-Hub-Service-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "NT"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

INSTANCE_ID=$(curl -s -X POST http://127.0.0.1:8001/api/v1/prom-instances \
  -H "X-Hub-Service-Token: $TOKEN" -H "Content-Type: application/json" \
  -d "{\"customer_id\": \"$CUSTOMER_ID\", \"name\": \"Prom (adminmc)\", \"base_url\": \"https://prom.ntaisolution.com\"}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

echo "CUSTOMER_ID=$CUSTOMER_ID  INSTANCE_ID=$INSTANCE_ID"
sed -i "s/^PROM_CUSTOMER_ID=.*/PROM_CUSTOMER_ID=$CUSTOMER_ID/; s/^PROM_INSTANCE_ID=.*/PROM_INSTANCE_ID=$INSTANCE_ID/" plane/apps/api/.env

# env เปลี่ยนแล้วต้อง recreate ไม่ใช่แค่ restart
deploy/server/prom.sh plane up -d --force-recreate api worker beat-worker
```

### 9. เชื่อม LINE, Discord และ AI ผ่านหน้าเว็บ
ทำเหมือนตอนทดสอบบนเครื่องตัวเอง ต่างกันแค่ URL:
- **LINE:** สร้าง installation ใน Integrations แล้วนำ Webhook URL ที่ได้ (`https://prom-hub.ntaisolution.com/webhooks/v1/line/…`) ไปวางใน LINE Developers Console
- **Plane webhook:** ตั้งค่า workspace connection แล้วนำ URL `https://prom-hub.ntaisolution.com/webhooks/v1/prom/…` ไปวางใน Plane → Settings → Webhooks
- **Discord:** สร้าง installation แล้วรัน `deploy/server/prom.sh hub up -d discord-bot` ทุกครั้งที่เพิ่มหรือเพิกถอน installation ต้องรัน `deploy/server/prom.sh hub restart discord-bot`
- **AI:** ตั้งค่าที่ Settings → AI Assistant

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
