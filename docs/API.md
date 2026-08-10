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
