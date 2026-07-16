# HestiaCP → aaPanel Migration Tool

Migrate websites, databases, SSL certificates, DNS records, mail accounts, and cron jobs từ **HestiaCP** sang **aaPanel Community Edition**.

## Yêu cầu

- Python 3.8+
- SSH access vào cả 2 server (root hoặc sudo)
- aaPanel đã cài đặt + bật API (Settings → API)

## Cài đặt

```bash
cd hestia-to-aapanel
pip install -r requirements.txt
```

## Cấu hình

1. Copy và sửa file `config.yaml`:

```bash
cp config.yaml config.mine.yaml
# Sửa config.mine.yaml với thông tin server của bạn
```

2. Các thông tin cần điền:
   - **Hestia server**: IP, SSH key/password
   - **aaPanel server**: IP, SSH key/password, panel URL, API key
   - **Migration options**: Chọn thành phần cần migrate (web/db/ssl/dns/mail/cron)

3. Lấy aaPanel API key:
   - Đăng nhập aaPanel → Settings → API → Enable
   - Thêm IP của máy chạy script vào whitelist
   - Copy API secret key

## Sử dụng

### Dry-run (xem trước, không thay đổi gì)

```bash
python migrate.py --config config.mine.yaml --dry-run
```

### Migrate thật

```bash
# Sửa dry_run: false trong config.yaml HOẶC
python migrate.py --config config.mine.yaml
```

### Resume sau khi bị gián đoạn

```bash
python migrate.py --config config.mine.yaml --resume
```

### Rollback (xóa sites đã migrate)

```bash
python migrate.py --config config.mine.yaml --rollback
```

## Quy trình migrate mỗi site

1. **Extract** (SSH → HestiaCP):
   - Đọc config websites, databases, DNS, mail, cron qua `v-*` CLI commands (JSON)
   - Dump MySQL databases
   - Archive web files

2. **Transfer** (SCP/RSYNC):
   - Chuyển archives + database dumps về máy local

3. **Import** (API + SSH → aaPanel):
   - Tạo `/www/wwwroot/{domain}/` và giải nén web files
   - Import database dumps
   - Tạo site qua API `AddSite` với PHP version + aliases
   - Deploy SSL certificate (SetSSL + SetSSLConf + HttpToHttps)
   - Tạo mail accounts (nếu có Mail plugin)
   - Import cron jobs
   - Verify HTTP/SSL sau migrate

## Cấu trúc project

```
hestia-to-aapanel/
├── migrate.py              # Main orchestrator
├── config.yaml             # Configuration template
├── state.json              # Runtime state (auto-generated, for resume)
├── modules/
│   ├── hestia.py           # HestiaCP SSH extraction
│   ├── aapanel_api.py      # aaPanel REST API client
│   ├── aapanel_ssh.py      # aaPanel SSH operations
│   ├── transfer.py         # SCP/RSYNC file transfer
│   ├── transformers.py     # Hestia → aaPanel data mapping
│   └── utils.py            # Logging, state, progress bars
├── logs/                   # Migration logs
├── requirements.txt
└── README.md
```

## Lưu ý quan trọng

### DNS
aaPanel **không có built-in DNS server**. Options:
- Giữ nguyên DNS trên HestiaCP server (recommended)
- Cài DNS Manager plugin trên aaPanel
- Dùng Cloudflare hoặc DNS provider khác

### Mail
aaPanel **không có built-in mail server**. Cần cài **Mail Server plugin** từ aaPanel App Store. Set `migration.mail_accounts: true` trong config nếu đã cài plugin.

### PHP Versions
Script tự động detect PHP version từ HestiaCP config. Nếu không detect được, dùng default (81 = PHP 8.1). Có thể override trong `php_mapping`.

### Idempotent
Script an toàn để chạy lại — check state.json để skip sites đã migrate.

### File Permissions
aaPanel dùng user `www` (hoặc `www-data`). Script tự động set permissions sau khi giải nén files.

## Troubleshooting

| Lỗi | Fix |
|-----|-----|
| API authentication failed | Kiểm tra API key + IP whitelist trong aaPanel Settings → API |
| SSH connection refused | Kiểm tra firewall + SSH port |
| AddSite returns error | Check domain format, path exists, port not in use |
| Database import fails | Kiểm tra MySQL root password, DB name conflicts |
| SSL deploy fails | Đảm bảo certificate format PEM hợp lệ |
