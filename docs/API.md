# Backup Dashboard API Documentation

این سند برای تیم Frontend نوشته شده تا بتوانند APIهای Backup Dashboard را در یک اپلیکیشن دیگر مصرف کنند.

## Base URL

در محیط local یا docker compose:

```text
http://localhost:5000
```

در Postman collection از متغیر زیر استفاده شده است:

```text
{{baseUrl}}
```

## Authentication

به جز `GET /api/health` و `POST /api/login`، همه APIها نیاز به JWT دارند.

Header مشترک:

```http
Authorization: Bearer {{token}}
Content-Type: application/json
```

### Login

```http
POST /api/login
```

Request:

```json
{
  "username": "admin",
  "password": "your-configured-admin-password"
}
```

Response:

```json
{
  "success": true,
  "token": "jwt-token",
  "user": "admin"
}
```

### Validate Token

```http
POST /api/validate-token
```

Response:

```json
{
  "success": true,
  "user": "admin"
}
```

### Logout

```http
POST /api/logout
```

Response:

```json
{
  "success": true,
  "message": "Logged out successfully"
}
```

## Common Response Shape

بیشتر endpointها این الگو را دارند:

```json
{
  "success": true
}
```

در خطا:

```json
{
  "success": false,
  "error": "Human readable error"
}
```

## Profile Types

مقدار `type` یکی از این‌ها است:

```text
postgresql
mongodb
redis
ssh_files
```

`ssh_files` همان Configuration Backup است.

## Security Notes

- فیلدهای `password` و `ssh_password` در storage به شکل encrypted ذخیره می‌شوند.
- فیلدهای secret در responseهای list/get/update profile برنمی‌گردند.
- فیلدهای secret مخزن Restic مثل `password`, `aws_access_key_id`, `aws_secret_access_key` هم encrypted ذخیره می‌شوند و در response حذف می‌شوند.
- دانلود backup به شکل یک repository رمزنگاری‌شده Restic برگردانده می‌شود.

## Profiles

### List Profiles

```http
GET /api/profiles
```

Response:

```json
{
  "success": true,
  "profiles": [
    {
      "id": "profile-id",
      "name": "74 configs",
      "type": "ssh_files",
      "enabled": true,
      "schedule": "0 2 * * *",
      "max_backups": 4,
      "repository_id": "default"
    }
  ]
}
```

### Get Profile

```http
GET /api/profiles/{profile_id}
```

Response:

```json
{
  "success": true,
  "profile": {
    "id": "profile-id",
    "name": "74 configs",
    "type": "ssh_files",
    "max_backups": 4,
    "repository_id": "default"
  }
}
```

### Create PostgreSQL Profile

```http
POST /api/profiles
```

Request:

```json
{
  "name": "Main PostgreSQL",
  "type": "postgresql",
  "description": "Production PostgreSQL backup",
  "enabled": true,
  "schedule": "0 2 * * *",
  "max_backups": 4,
  "repository_id": "default",
  "verify_after_backup": true,
  "host": "192.168.11.74",
  "port": 5432,
  "username": "postgres",
  "password": "secret",
  "databases": ["all"]
}
```

### Create Redis Profile

```http
POST /api/profiles
```

Request:

```json
{
  "name": "Main Redis",
  "type": "redis",
  "description": "Production Redis backup",
  "enabled": true,
  "schedule": "0 3 * * *",
  "max_backups": 4,
  "repository_id": "default",
  "host": "192.168.11.74",
  "port": 6379,
  "password": ""
}
```

### Create MongoDB Profile

```http
POST /api/profiles
```

Request:

```json
{
  "name": "Main MongoDB",
  "type": "mongodb",
  "description": "Production MongoDB backup",
  "enabled": true,
  "schedule": "0 4 * * *",
  "max_backups": 4,
  "repository_id": "default",
  "host": "192.168.11.74",
  "port": 27017,
  "username": "",
  "password": "",
  "databases": ["all"]
}
```

### Create Configuration Backup Profile

```http
POST /api/profiles
```

نکته مهم: هنگام create/update برای `ssh_files`، backend اتصال SSH و نصب بودن `rsync` روی سرور مقصد را validate می‌کند.

Request با کلید برنامه:

```json
{
  "name": "74 configs",
  "type": "ssh_files",
  "description": "Config files from server 74",
  "enabled": true,
  "schedule": "0 2 * * *",
  "max_backups": 4,
  "repository_id": "default",
  "ssh_host": "192.168.11.74",
  "ssh_port": 22,
  "ssh_user": "root",
  "auth_method": "key",
  "ssh_password": "",
  "path_configs": [
    {
      "path": "/root/compose",
      "exclude_patterns": [
        "test/",
        "pg_data_gdb/",
        "raster",
        "gt_routing/",
        "app/",
        "elevation_data/",
        "nominatim-data/"
      ]
    }
  ],
  "paths": ["/root/compose"],
  "exclude_patterns": [],
  "preserve_metadata": true,
  "log_rsync_output": true
}
```

Request با password:

```json
{
  "name": "Config Backup Password Auth",
  "type": "ssh_files",
  "enabled": true,
  "schedule": "0 2 * * *",
  "max_backups": 4,
  "repository_id": "default",
  "ssh_host": "192.168.11.74",
  "ssh_port": 22,
  "ssh_user": "root",
  "auth_method": "password",
  "ssh_password": "secret",
  "path_configs": [
    {
      "path": "/etc",
      "exclude_patterns": ["*.log", "*.tmp"]
    },
    {
      "path": "/opt/app/config",
      "exclude_patterns": ["cache/", "*.pid"]
    }
  ],
  "paths": ["/etc", "/opt/app/config"],
  "exclude_patterns": [],
  "preserve_metadata": false,
  "log_rsync_output": false
}
```

### Update Profile

```http
PUT /api/profiles/{profile_id}
```

می‌توانید کل profile را بفرستید یا فقط فیلدهای تغییر کرده را. Backend داده جدید را با profile قبلی merge می‌کند.

`max_backups` اگر بزرگ‌تر از صفر باشد، بعد از هر backup موفق فقط همان تعداد snapshot آخر آن profile نگه داشته می‌شود و قدیمی‌ترها با `restic forget --prune` حذف می‌شوند. اگر `0` باشد retention سراسری استفاده می‌شود.

Snapshotهای جدید با tag پایدار `profile:{profile_id}` ذخیره می‌شوند. این باعث می‌شود تغییر نام profile باعث خراب شدن retention نشود. برای snapshotهای قدیمی‌تر، backend از مسیر staging مثل `/var/backups/.staging/{profile_id}` هم profile را تشخیص می‌دهد.

Request:

```json
{
  "name": "Updated profile name",
  "enabled": true,
  "schedule": "30 2 * * *",
  "max_backups": 4,
  "repository_id": "default"
}
```

### Delete Profile

```http
DELETE /api/profiles/{profile_id}
```

این endpoint فقط metadata مربوط به profile را حذف می‌کند. snapshotهای موجود برای جلوگیری از حذف ناخواسته داده حفظ می‌شوند و در صورت نیاز باید جداگانه از endpoint حذف backup پاک شوند.

Response:

```json
{
  "success": true,
  "message": "Profile deleted; existing snapshots were preserved",
  "snapshots_preserved": true
}
```

## Configuration Backup Public Key

برای نمایش Public Key برنامه به کاربر:

```http
GET /api/config-backup/public-key
```

Response:

```json
{
  "success": true,
  "public_key": "ssh-ed25519 AAAA..."
}
```

کاربر باید این public key را روی سرور مقصد داخل `authorized_keys` همان user اضافه کند.

## Restic Repositories

هر profile با `repository_id` مشخص می‌کند backup در کدام مخزن Restic ذخیره شود. مقدار پیش‌فرض `default` است.

Restic خودش داده‌ها را encrypt می‌کند. مقدار `password` همان رمز repository است و برای restore/decrypt لازم می‌شود.

### List Repositories

```http
GET /api/restic-repositories
```

Response:

```json
{
  "success": true,
  "repositories": [
    {
      "id": "default",
      "name": "Default Local Repository",
      "type": "local",
      "repository": "/var/backups/restic-repo"
    }
  ]
}
```

### Create Local Repository

```http
POST /api/restic-repositories
```

Request:

```json
{
  "name": "Local Repository",
  "type": "local",
  "repository": "/var/backups/restic-repo",
  "password": "strong-restic-password"
}
```

### Create S3 Repository

```http
POST /api/restic-repositories
```

Request:

```json
{
  "name": "AWS S3 Backup",
  "type": "s3",
  "repository": "s3:s3.amazonaws.com/my-backup-bucket/prod",
  "password": "strong-restic-password",
  "aws_access_key_id": "AKIA...",
  "aws_secret_access_key": "secret",
  "aws_default_region": "us-east-1"
}
```

برای provider داخلی یا هر S3-compatible storage، دو روش قابل استفاده است.

روش اول، ساختن `repository` با فرمت Restic:

```json
{
  "name": "Internal S3 Backup",
  "type": "s3",
  "repository": "s3:https://s3.example.ir/my-bucket/backups/prod",
  "password": "strong-restic-password",
  "aws_access_key_id": "access-key",
  "aws_secret_access_key": "secret-key",
  "aws_default_region": "us-east-1"
}
```

روش دوم، ارسال endpoint و bucket جداگانه؛ backend مقدار `repository` را می‌سازد:

```json
{
  "name": "Internal S3 Backup",
  "type": "s3",
  "endpoint": "https://s3.example.ir",
  "bucket": "my-bucket",
  "prefix": "backups/prod",
  "password": "strong-restic-password",
  "aws_access_key_id": "access-key",
  "aws_secret_access_key": "secret-key",
  "aws_default_region": "us-east-1"
}
```

در UI هم برای provider داخلی:

- `Repository Type`: مقدار `S3`
- `S3 Endpoint`: آدرسی که provider داده، مثل `https://s3.example.ir`
- `Bucket / Prefix`: مثلا `my-bucket/backups/prod`
- `Access Key` و `Secret Key`: مقادیر provider
- `Region`: اگر provider مقدار خاصی نداده، `us-east-1` معمولاً کافی است

### Create MinIO Repository

```http
POST /api/restic-repositories
```

Request:

```json
{
  "name": "MinIO Backup",
  "type": "minio",
  "repository": "s3:http://minio:9000/backups/prod",
  "password": "strong-restic-password",
  "aws_access_key_id": "minio-access-key",
  "aws_secret_access_key": "minio-secret-key",
  "aws_default_region": "us-east-1"
}
```

### Update Repository

```http
PUT /api/restic-repositories/{repository_id}
```

اگر secretها را خالی بفرستید، مقدار قبلی حفظ می‌شود.

```json
{
  "name": "Updated Repository",
  "repository": "s3:s3.amazonaws.com/my-backup-bucket/prod"
}
```

### Check Repository

```http
POST /api/restic-repositories/{repository_id}/check
```

Response:

```json
{
  "success": true,
  "message": "Repository is accessible"
}
```

### Init Repository

```http
POST /api/restic-repositories/{repository_id}/init
```

Response:

```json
{
  "success": true,
  "message": "Repository initialized"
}
```

### Configuration Backup (Disaster Recovery)

هر repository یک کپی از profileهایی که در آن backup می‌گیرند را داخل خودش نگه می‌دارد: یک snapshot کوچک با tag `backupstic-config`، host `backupstic` و فایل `/backupstic-config.json`. این فایل با رمز همان repository رمز شده است (secretهای profile داخلش هستند تا بدون `PROFILE_SECRET_KEY` سرور قبلی هم قابل restore باشد). بعد از هر تغییر profile و بعد از هر backup، فقط اگر محتوا عوض شده باشد نسخه جدید ذخیره می‌شود. این snapshotها در لیست backupها و آمار نمایش داده نمی‌شوند.

```http
GET /api/restic-repositories/{repo_id}/config-backups[?snapshot_id=...]
```

لیست نسخه‌های ذخیره‌شده (جدیدترین اول) و پیش‌نمایش restore یک نسخه (پیش‌فرض: جدیدترین):

```json
{
  "success": true,
  "snapshots": [{"id": "...", "short_id": "c21fd092", "time": "..."}],
  "selected": {"id": "...", "exported_at": "...", "repository_name": "Offsite S3"},
  "profiles": [
    {"id": "8683c668-...", "name": "cache", "type": "redis", "schedule": "15 3 * * *", "backups": 12, "status": "new", "error": ""}
  ]
}
```

`status` هر profile: `new` (روی این سرور نیست)، `exists` (با همین id هست)، `name_conflict` (profile دیگری با همین نام و نوع هست)، `invalid`. `backups` تعداد snapshotهای این profile در repository است. secretها در این response برنمی‌گردند.

```http
POST /api/restic-repositories/{repo_id}/config-backups/restore
Content-Type: application/json

{"snapshot_id": "<optional>", "profile_ids": ["<optional>"], "overwrite": false}
```

profileها با همان id قبلی ساخته می‌شوند و `repository_id` آن‌ها به همین repository اشاره می‌کند؛ بنابراین backupهای قبلی، retention و تاریخچه دوباره به آن‌ها وصل می‌شوند. `exists` فقط با `overwrite: true` جایگزین می‌شود و `name_conflict` همیشه رد می‌شود. Response شامل `restored`, `replaced`, `skipped`, `failed` و `results` است.

```http
POST /api/restic-repositories/{repo_id}/config-backups
```

همین الان کانفیگ را در repository ذخیره می‌کند. در `GET /api/restic-repositories` هر repository فیلد `config_backup` با `synced_at`, `profiles`, `pending` دارد.

### Delete Repository Definition

```http
DELETE /api/restic-repositories/{repository_id}
```

این endpoint فقط تعریف مخزن را حذف می‌کند و فایل‌ها/snapshotهای داخل مخزن Restic را حذف نمی‌کند. مخزن `default` قابل حذف نیست.

## Backup Operations

### Trigger Backup

```http
POST /api/backup
```

Backup پروفایل‌های انتخابی:

```json
{
  "mode": "profile",
  "profile_ids": ["profile-id-1", "profile-id-2"]
}
```

Backup همه پروفایل‌های enabled:

```json
{
  "mode": "profile",
  "profile_ids": []
}
```

Legacy modes:

```json
{
  "mode": "full"
}
```

Legacy modeهای مجاز:

```text
full
db
files
postgres
redis
mongo
elasticsearch
```

Response:

```json
{
  "success": true,
  "message": "Backup started for profiles: 74 configs"
}
```

### Backup Status

```http
GET /api/backup-status
```

Response when idle:

```json
{
  "running": false
}
```

Response when running:

```json
{
  "running": true,
  "mode": "profile:74 configs",
  "started_at": "2026-07-15T10:22:40+03:30",
  "pid": 123
}
```

وقتی یک Health Check در مرحله restore است، همین قفل را نگه می‌دارد تا backupهای زمان‌بندی‌شده و prune با آن تداخل نکنند. در این حالت پاسخ این شکل است (فیلد `kind` جدید و اختیاری است):

```json
{
  "running": true,
  "mode": "verify:1a2b3c4d",
  "kind": "verify",
  "started_at": "2026-10-13T10:22:40+03:30",
  "pid": 123
}
```

## Snapshots And Backups

### List Snapshots

```http
GET /api/snapshots
```

پارامترهای مفید برای داشبورد:

```http
GET /api/snapshots?active_profiles_only=1&respect_retention=1
```

- `active_profiles_only=1`: فقط snapshotهای مربوط به profileهای فعلی سامانه را برمی‌گرداند.
- `respect_retention=1`: خروجی را مطابق `max_backups` هر profile محدود می‌کند.
- `repository_id=default`: فقط snapshotهای یک Restic repository مشخص را برمی‌گرداند.

Response:

```json
{
  "success": true,
  "snapshots": [
    {
      "id": "snapshot-id",
      "short_id": "abcd1234",
      "time": "2026-07-15T10:22:40.000000000+03:30",
      "hostname": "backup-cron",
      "tags": ["74 configs", "ssh_files"],
      "paths": ["/var/backups/.staging/profile-id"],
      "repository_id": "default",
      "repository_name": "Default Local Repository",
      "size": 123456
    }
  ]
}
```

### List Backups

```http
GET /api/backups
```

این endpoint snapshotها را به format ساده‌تر backup تبدیل می‌کند.

Response:

```json
{
  "success": true,
  "backups": [
    {
      "id": "snapshot-id",
      "short_id": "abcd1234",
      "time": "2026-07-15T10:22:40.000000000+03:30",
      "hostname": "backup-cron",
      "tags": ["74 configs", "ssh_files"],
      "paths": ["/var/backups/.staging/profile-id"],
      "repository_id": "default",
      "repository_name": "Default Local Repository",
      "size": 0
    }
  ]
}
```

### Snapshot Files

```http
GET /api/snapshot/{snapshot_id}/files
```

Response:

```json
{
  "success": true,
  "output": "{\"struct_type\":\"snapshot\",...}\n"
}
```

نکته: مقدار `output` یک string است که هر خط آن JSON خروجی `restic ls --json` است.

### Download Backup

```http
GET /api/backup/{backup_id}/download
```

Response:

```text
Binary .restic.tar.gz file
```

Frontend باید این endpoint را با `fetch` و header `Authorization` صدا بزند و response را blob کند. فایل دانلودی شامل یک repository رمزنگاری‌شده Restic است، نه فایل‌های restore شده به صورت plaintext.

### Decrypt / Restore Downloaded File

بعد از دانلود `backup_<id>.restic.tar.gz`:

```bash
mkdir restore-work
tar -xzf backup_<id>.restic.tar.gz -C restore-work
cd restore-work

export RESTIC_REPOSITORY="$PWD/encrypted-restic-repo"
export RESTIC_PASSWORD="<restic repository password>"

restic snapshots
restic restore latest --target ./restore-output
```

رمز مورد نیاز همان `password` مخزن Restic مربوط به snapshot است. این password از API برنمی‌گردد و باید از تنظیمات امن خودتان داشته باشید.

### Download Plain Backup (بدون رمز و بدون پوشه اضافه)

```http
POST /api/backup/{backup_id}/download-plain
Content-Type: application/json

{"password": "<restic repository password>"}
```

snapshot روی سرور restore و decrypt می‌شود و خود فایل‌های backup بدون repository رستیک و بدون مسیرهای `var/backups/.staging/<id>/` برگردانده می‌شوند:

- اگر snapshot فقط یک فایل gzip داشته باشد (مثلاً `app_20260101_020000.sql.gz` یا `mongo_….tar.gz`؛ فایل‌های `*.manifest.json` در نظر گرفته نمی‌شوند)، همان فایل مستقیم دانلود می‌شود.
- در غیر این صورت یک `<profile>_<YYYYMMDD_HHMMSS>_<short_id>.tar.gz` ساخته می‌شود که همه فایل‌ها مستقیماً در ریشه آن هستند.

نام فایل در header `Content-Disposition` است. چون خروجی رمز ندارد، `password` باید همان رمز مخزن Restic همان snapshot باشد (توکن ورود به تنهایی کافی نیست).

خطاها: `400` اگر password خالی یا id نامعتبر باشد، `403` برای password اشتباه (با فیلد `attempts_left`)، `404` اگر snapshot پیدا نشود، `429` بعد از `PLAIN_DOWNLOAD_MAX_FAILURES` (پیش‌فرض `5`) password اشتباه در `PLAIN_DOWNLOAD_FAILURE_WINDOW` ثانیه (پیش‌فرض `300`) با header `Retry-After`، `507` اگر فضای دیسک کافی نباشد.

### One-Time Download Link

فایل روی سرور در background آماده می‌شود و یک لینک یک‌بارمصرف برمی‌گردد که بدون login کار می‌کند (مثلاً با `curl` روی یک سرور دیگر). مرورگر فایل را مستقیم و stream شده دانلود می‌کند (بدون نگه داشتن کل فایل در حافظه).

```http
POST /api/backup/{backup_id}/download-link
Content-Type: application/json

{"format": "plain", "password": "<restic repository password>"}
```

`format` یکی از `plain` (همان خروجی Download Plain Backup؛ `password` الزامی) یا `encrypted` (bundle رمزنگاری‌شده Restic؛ بدون password) است. Response با status `202`:

```json
{
  "success": true,
  "url": "/dl/<token>",
  "ttl_seconds": 3600,
  "link": {"id": "<job id>", "status": "preparing", "format": "plain", "short_id": "1a2b3c4d", "filename": "", "size": 0, "expires_at": null}
}
```

`token` فقط در همین response برمی‌گردد و روی دیسک ذخیره نمی‌شود (`id` همان SHA-256 توکن است).

```http
GET /api/download-links/{id}      # وضعیت: preparing | ready | claimed | failed
DELETE /api/download-links/{id}   # لغو لینک و حذف فایل
```

استفاده از لینک (بدون header احراز هویت):

```http
GET  /dl/{token}   # صفحه دانلود؛ لینک را مصرف نمی‌کند (preview اپ‌های چت لینک را نمی‌سوزاند)
POST /dl/{token}   # دانلود فایل؛ فقط یک بار
```

```bash
curl -fOJ -X POST 'https://backup.example.com/dl/<token>'
```

بعد از اولین `POST` فایل از سرور حذف می‌شود و درخواست‌های بعدی `410` (already used) می‌گیرند. لینک `DOWNLOAD_LINK_TTL_SECONDS` ثانیه (پیش‌فرض `3600`) بعد از آماده شدن منقضی می‌شود. حداکثر `DOWNLOAD_LINK_MAX_PREPARING` (پیش‌فرض `2`) لینک هم‌زمان در حال آماده‌سازی مجاز است (`429`). اگر dashboard وسط آماده‌سازی restart شود، لینک `failed` می‌شود. محدودیت password اشتباه و خطاهای `400/403/404/429/507` مثل Download Plain Backup است.

### Delete Backup Snapshot

```http
DELETE /api/backup/{backup_id}
```

Response:

```json
{
  "success": true,
  "message": "Backup deleted successfully"
}
```

### Verify Backup (Health Check)

سلامت یک backup را end-to-end بررسی می‌کند؛ دقیقاً همان مسیری که کاربر بعد از دانلود طی می‌کند:

1. `restore` — restore کردن snapshot از مخزن Restic
2. `package` — ساخت همان bundle رمزنگاری‌شده‌ای که دانلود برمی‌گرداند
3. `extract` — باز کردن `tar.gz`
4. `decrypt` — باز کردن مخزن با password (و اطمینان از اینکه password اشتباه رد می‌شود) و restore محتوا
5. `compare` — مقایسه SHA-256 همه فایل‌ها با restore اول
6. `archives` — تست کامل همه archiveهای داخل backup (`.gz`, `.tar.gz`, `.tgz`, `.tar`, `.zip`, `.bz2`, `.xz`, `.json`, `.rdb`, `.sql`)؛ برای dump PostgreSQL وجود marker پایان dump چک می‌شود و فایل‌های RDB ردیس با `redis-check-rdb` (ساختار و checksum) بررسی می‌شوند
7. `postgres` — restore واقعی هر dump پستگرس روی یک PostgreSQL موقت و اختصاصی (`VERIFY_POSTGRES_URL`). هر دیتابیس در یک دیتابیس موقت `backupstic_verify_*` restore و بعد حذف می‌شود. اگر تنظیم نشده باشد یا dump پستگرسی وجود نداشته باشد، status این step برابر `skipped` است و روی نتیجه کلی اثری ندارد

کار در background اجرا می‌شود و فقط یک verification هم‌زمان مجاز است. حدود ۳ برابر حجم backup فضای موقت لازم دارد و فایل‌های موقت در پایان پاک می‌شوند.

**Health check خودکار بعد از هر backup:** اگر در profile مقدار `verify_after_backup` برابر `true` باشد، `backup-all.sh` بعد از ساخت snapshot جدید آن را در `verify-queue/<snapshot_id>.json` قرار می‌دهد. dashboard هر `AUTO_VERIFY_POLL_SECONDS` ثانیه (پیش‌فرض `15`؛ `0` = غیرفعال) صف را چک می‌کند و وقتی هیچ backup و verification دیگری در حال اجرا نباشد، همین health check را اجرا می‌کند. در نتیجه این verificationها `trigger` برابر `after_backup` است (برای اجرای دستی `manual`). تا زمانی که در صف است، `GET /api/verifications` برای آن snapshot وضعیت `scheduled` برمی‌گرداند.

```http
POST /api/backup/{backup_id}/verify
```

`backup_id` می‌تواند short id (۸ کاراکتر) یا full id باشد. Response با status `202`:

```json
{
  "success": true,
  "verification": {
    "id": "uuid",
    "snapshot_id": "full-snapshot-id",
    "short_id": "1a2b3c4d",
    "status": "queued",
    "current_step": null,
    "steps": [
      {"key": "restore", "label": "Restore snapshot from Restic", "status": "pending", "detail": "", "duration_ms": null}
    ],
    "archives": [],
    "summary": {}
  }
}
```

خطاها: `400` برای id نامعتبر، `404` اگر snapshot پیدا نشود، `409` اگر verification دیگری در حال اجرا باشد (در این حالت `verification` همان job در حال اجرا است).

```http
GET /api/backup/{backup_id}/verify
```

آخرین نتیجه verification این snapshot (برای polling هر ۱-۲ ثانیه). مقدار `status` یکی از این‌هاست:

```text
queued | running | passed | warning | failed | interrupted
```

(در `GET /api/verifications` مقدار `scheduled` هم ممکن است: یعنی health check خودکار در صف است و هنوز شروع نشده.)

مقدار `status` هر step یکی از `pending | running | passed | warning | failed | skipped` است. `archives` لیست فایل‌های تست‌شده با `path`, `format`, `size`, `status`, `detail` است. `postgres` (در صورت اجرا) لیست dumpها با `path`, `status`, `detail`, `databases`, `tables`, `rows`, `errors` است. `summary` شامل `postgres_version` و `files`, `bytes`, `archives_total`, `archives_passed`, `archives_warning`, `archives_failed` می‌شود. اگر dashboard وسط کار restart شود، status به `interrupted` تغییر می‌کند.

```http
GET /api/verifications
```

آخرین نتیجه (خلاصه) برای همه snapshotهایی که چک شده‌اند، با کلید full snapshot id:

```json
{
  "success": true,
  "verifications": {
    "full-snapshot-id": {"status": "passed", "started_at": "...", "finished_at": "...", "error": "", "summary": {}}
  }
}
```

متغیرهای محیطی اختیاری:

- `VERIFY_LOCK_WAIT_SECONDS` (پیش‌فرض `3600`): مرحله restore حداکثر چقدر منتظر تمام شدن backup در حال اجرا بماند.
- `VERIFY_POSTGRES_URL`: آدرس PostgreSQL موقت و اختصاصی برای step هفتم (با `./init.sh --with-postgres-verify` ساخته می‌شود؛ هرگز سرور دارای داده واقعی را اینجا قرار ندهید).
- `VERIFY_POSTGRES_TIMEOUT` (پیش‌فرض `7200`): حداکثر زمان restore هر dump به ثانیه.

## Logs And Scheduling

### Logs

```http
GET /api/logs
```

Response:

```json
{
  "success": true,
  "logs": "last 100 lines of cron.log"
}
```

### Get Global Schedule

```http
GET /api/schedule
```

Response:

```json
{
  "success": true,
  "schedule": "0 2 * * * /scripts/backup-all.sh full >> /var/backups/cron.log 2>&1"
}
```

### Update Global Schedule

```http
POST /api/schedule
```

Request:

```json
{
  "cron": "0 2 * * *",
  "mode": "full"
}
```

Response:

```json
{
  "success": true,
  "message": "Schedule updated"
}
```

### Update Profile Schedule

```http
POST /api/profile-schedule
```

Request:

```json
{
  "profile_id": "profile-id",
  "cron": "30 2 * * *"
}
```

Response:

```json
{
  "success": true,
  "message": "Profile schedule updated"
}
```

## Retention And Status

### Retention

```http
GET /api/retention
```

Response:

```json
{
  "success": true,
  "retention": {
    "RETENTION_DAYS": "30",
    "RETENTION_WEEKLY": "12",
    "RETENTION_MONTHLY": "12"
  }
}
```

### Dashboard Status

```http
GET /api/status
```

Response:

```json
{
  "success": true,
  "snapshot_count": 12,
  "last_backup": "2026-07-15T10:22:40.000000000+03:30",
  "disk": {
    "used": "10.2 GB",
    "available": "89.8 GB",
    "total": "100.0 GB",
    "percent": "10"
  }
}
```

### Health

```http
GET /api/health
```

نیاز به token ندارد.

Response:

```json
{
  "status": "healthy",
  "timestamp": "2026-07-15T10:22:40.000000"
}
```

## Frontend Integration Notes

### Suggested Initial Load

برای داشبورد دیگر:

1. `POST /api/login`
2. ذخیره token
3. `GET /api/status`
4. `GET /api/restic-repositories`
5. `GET /api/profiles`
6. `GET /api/snapshots`
7. هر 10 تا 30 ثانیه `GET /api/backup-status`

### Secret Fields

برای امنیت، passwordها در response برنمی‌گردند. در فرم edit، فیلد password را empty نشان دهید. اگر کاربر مقدار جدید وارد نکرد، همان مقدار قبلی حفظ می‌شود. برای تغییر password باید مقدار جدید را در `PUT` بفرستید.

### Snapshot Tags

Snapshotهای profile-based با دو tag ذخیره می‌شوند:

```text
profile name
profile type
```

مثال:

```json
["74 configs", "ssh_files"]
```

برای نمایش profile name در UI، tagهایی مثل `postgresql`, `mongodb`, `redis`, `ssh_files` را type حساب کنید و tag دیگر را profile name بدانید.

### Configuration Backup Requirements

برای profile نوع `ssh_files`:

- مسیرها باید absolute باشند.
- `path_configs` حداقل یک مسیر لازم دارد.
- هر مسیر exclude مستقل خودش را دارد.
- اگر `auth_method = key` باشد، public key برنامه باید روی سرور مقصد نصب شده باشد.
- روی سرور مقصد باید `rsync` نصب باشد.
- backend هنگام create/update اتصال SSH و remote rsync را validate می‌کند.

### Download With Fetch

نمونه دانلود:

```js
async function downloadBackup(baseUrl, token, backupId) {
  const res = await fetch(`${baseUrl}/api/backup/${backupId}/download`, {
    headers: { Authorization: `Bearer ${token}` }
  });
  if (!res.ok) throw new Error('Download failed');
  return await res.blob();
}
```
