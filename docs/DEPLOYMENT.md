# Production Deployment Guide

**Project**: Task Management REST API
**Version**: 1.0
**Date**: 2025-10-23
**Status**: Production Ready

---

## Table of Contents

1. [Pre-Deployment Checklist](#pre-deployment-checklist)
2. [Environment Setup](#environment-setup)
3. [Database Configuration](#database-configuration)
4. [Application Configuration](#application-configuration)
5. [Deployment Steps](#deployment-steps)
6. [Post-Deployment Verification](#post-deployment-verification)
7. [Monitoring & Logging](#monitoring--logging)
8. [Performance Tuning](#performance-tuning)
9. [Security Hardening](#security-hardening)
10. [Troubleshooting](#troubleshooting)
11. [Rollback Procedures](#rollback-procedures)
12. [Maintenance](#maintenance)

---

## Pre-Deployment Checklist

Before deploying to production, ensure all items are completed:

### Code Quality
- [ ] All tests passing (65/65 tests)
- [ ] Test coverage ≥ 85% (current: 88%)
- [ ] Code review completed and approved
- [ ] No critical security vulnerabilities
- [ ] Static analysis clean (ruff, pylint)
- [ ] Documentation updated

### Infrastructure
- [ ] Production server provisioned
- [ ] Database server provisioned (PostgreSQL)
- [ ] SSL/TLS certificates obtained
- [ ] Domain name configured
- [ ] Firewall rules configured
- [ ] Backup system configured

### Configuration
- [ ] Production environment variables configured
- [ ] Database connection string ready
- [ ] CORS origins configured
- [ ] Logging system configured
- [ ] Monitoring tools configured

### Security
- [ ] Secrets rotated (no dev secrets in prod)
- [ ] Database credentials secured
- [ ] API access controls reviewed
- [ ] SSL/TLS enabled
- [ ] Security headers configured

### Dependencies
- [ ] Python 3.11+ installed
- [ ] All dependencies installed from requirements.txt
- [ ] Database migrations tested
- [ ] Backup/restore procedures tested

---

## Environment Setup

### Server Requirements

**Minimum Requirements**:
- OS: Ubuntu 20.04 LTS or later / Red Hat 8+ / macOS
- CPU: 2 cores
- RAM: 2 GB
- Disk: 20 GB
- Network: 100 Mbps

**Recommended Requirements**:
- OS: Ubuntu 22.04 LTS
- CPU: 4 cores
- RAM: 4 GB
- Disk: 50 GB SSD
- Network: 1 Gbps

### Software Prerequisites

1. **Python 3.11+**
```bash
# Ubuntu
sudo apt update
sudo apt install python3.11 python3.11-venv python3-pip

# Verify installation
python3.11 --version
```

2. **PostgreSQL 14+**
```bash
# Ubuntu
sudo apt install postgresql-14 postgresql-contrib

# Start PostgreSQL
sudo systemctl start postgresql
sudo systemctl enable postgresql

# Verify installation
psql --version
```

3. **Nginx (Web Server/Reverse Proxy)**
```bash
# Ubuntu
sudo apt install nginx

# Start Nginx
sudo systemctl start nginx
sudo systemctl enable nginx
```

4. **Supervisor (Process Manager)**
```bash
# Ubuntu
sudo apt install supervisor

# Start Supervisor
sudo systemctl start supervisor
sudo systemctl enable supervisor
```

5. **Git**
```bash
sudo apt install git
```

---

## Database Configuration

### PostgreSQL Setup

#### 1. Create Database User

```bash
sudo -u postgres psql
```

```sql
-- Create user
CREATE USER taskapi WITH PASSWORD 'your_secure_password_here';

-- Create database
CREATE DATABASE tasks_db OWNER taskapi;

-- Grant privileges
GRANT ALL PRIVILEGES ON DATABASE tasks_db TO taskapi;

-- Exit
\q
```

#### 2. Configure PostgreSQL

Edit `/etc/postgresql/14/main/postgresql.conf`:

```ini
# Connection settings
listen_addresses = 'localhost'  # Only local connections
max_connections = 100

# Memory settings
shared_buffers = 256MB
effective_cache_size = 1GB
work_mem = 4MB

# Logging
log_destination = 'stderr'
logging_collector = on
log_directory = 'log'
log_filename = 'postgresql-%Y-%m-%d.log'
log_rotation_age = 1d
log_line_prefix = '%m [%p] %u@%d '
```

Edit `/etc/postgresql/14/main/pg_hba.conf`:

```
# TYPE  DATABASE        USER            ADDRESS                 METHOD
local   all             postgres                                peer
local   all             taskapi                                 md5
host    tasks_db        taskapi         127.0.0.1/32            md5
host    tasks_db        taskapi         ::1/128                 md5
```

Restart PostgreSQL:
```bash
sudo systemctl restart postgresql
```

#### 3. Test Database Connection

```bash
# Install PostgreSQL client
sudo apt install postgresql-client

# Test connection
psql -h localhost -U taskapi -d tasks_db -W
```

---

## Application Configuration

### 1. Deploy Application Code

```bash
# Create application directory
sudo mkdir -p /opt/taskapi
sudo chown $USER:$USER /opt/taskapi

# Clone repository
cd /opt/taskapi
git clone https://github.com/sergeychernyakov/blank_python_project.git .

# Checkout specific version/tag
git checkout v1.0.0  # Or specific commit
```

### 2. Create Virtual Environment

```bash
cd /opt/taskapi
python3.11 -m venv venv
source venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Configure Environment Variables

Create `/opt/taskapi/.env`:

```env
# Application
APP_ENV=production
LOG_LEVEL=INFO

# Database
DATABASE_URL=postgresql+asyncpg://taskapi:your_secure_password_here@localhost:5432/tasks_db

# API
API_V1_PREFIX=/api/v1
CORS_ORIGINS=https://yourdomain.com,https://www.yourdomain.com

# Server
HOST=127.0.0.1
PORT=8000
```

**Important**:
- Use strong, unique passwords
- Never commit .env file to version control
- Secure file permissions: `chmod 600 /opt/taskapi/.env`

### 5. Run Database Migrations

```bash
cd /opt/taskapi
source venv/bin/activate
alembic upgrade head
```

Verify migrations:
```bash
alembic current
```

Expected output: `70e6d45fd912 (head)`

---

## Deployment Steps

### Option 1: Deployment with Supervisor + Nginx

#### 1. Configure Supervisor

Create `/etc/supervisor/conf.d/taskapi.conf`:

```ini
[program:taskapi]
command=/opt/taskapi/venv/bin/uvicorn src.api.main:app --host 127.0.0.1 --port 8000 --workers 4
directory=/opt/taskapi
user=www-data
autostart=true
autorestart=true
redirect_stderr=true
stdout_logfile=/var/log/taskapi/app.log
stdout_logfile_maxbytes=10MB
stdout_logfile_backups=5
environment=PATH="/opt/taskapi/venv/bin"
```

Create log directory:
```bash
sudo mkdir -p /var/log/taskapi
sudo chown www-data:www-data /var/log/taskapi
```

Update supervisor:
```bash
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start taskapi
```

Check status:
```bash
sudo supervisorctl status taskapi
```

#### 2. Configure Nginx

Create `/etc/nginx/sites-available/taskapi`:

```nginx
upstream taskapi_backend {
    server 127.0.0.1:8000;
}

server {
    listen 80;
    server_name yourdomain.com www.yourdomain.com;

    # Redirect HTTP to HTTPS
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name yourdomain.com www.yourdomain.com;

    # SSL certificates (use Let's Encrypt or your provider)
    ssl_certificate /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    # Security headers
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    # Logging
    access_log /var/log/nginx/taskapi_access.log;
    error_log /var/log/nginx/taskapi_error.log;

    # API endpoints
    location / {
        proxy_pass http://taskapi_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Timeouts
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }

    # Health check endpoint
    location /api/v1/health {
        proxy_pass http://taskapi_backend;
        access_log off;  # Don't log health checks
    }
}
```

Enable site:
```bash
sudo ln -s /etc/nginx/sites-available/taskapi /etc/nginx/sites-enabled/
sudo nginx -t  # Test configuration
sudo systemctl reload nginx
```

#### 3. Configure SSL with Let's Encrypt

```bash
# Install certbot
sudo apt install certbot python3-certbot-nginx

# Obtain certificate
sudo certbot --nginx -d yourdomain.com -d www.yourdomain.com

# Test auto-renewal
sudo certbot renew --dry-run
```

### Option 2: Docker Deployment

Create `Dockerfile`:

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create non-root user
RUN useradd -m -u 1000 taskapi && chown -R taskapi:taskapi /app
USER taskapi

# Expose port
EXPOSE 8000

# Run application
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Create `docker-compose.yml`:

```yaml
version: '3.8'

services:
  db:
    image: postgres:14
    environment:
      POSTGRES_DB: tasks_db
      POSTGRES_USER: taskapi
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    ports:
      - "5432:5432"
    restart: unless-stopped

  api:
    build: .
    environment:
      APP_ENV: production
      DATABASE_URL: postgresql+asyncpg://taskapi:${DB_PASSWORD}@db:5432/tasks_db
      CORS_ORIGINS: https://yourdomain.com
    ports:
      - "8000:8000"
    depends_on:
      - db
    restart: unless-stopped

volumes:
  postgres_data:
```

Deploy with Docker:
```bash
# Set database password
export DB_PASSWORD="your_secure_password"

# Build and start
docker-compose up -d

# Run migrations
docker-compose exec api alembic upgrade head

# Check logs
docker-compose logs -f api
```

---

## Post-Deployment Verification

### 1. Health Check

```bash
# Check API health
curl https://yourdomain.com/api/v1/health
```

Expected response:
```json
{
  "status": "ok",
  "timestamp": "2025-10-23T12:00:00Z",
  "database": "connected"
}
```

### 2. Smoke Tests

**Create task**:
```bash
curl -X POST https://yourdomain.com/api/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{"title": "Test task", "priority": "HIGH"}'
```

**List tasks**:
```bash
curl https://yourdomain.com/api/v1/tasks
```

**Get documentation**:
```bash
curl https://yourdomain.com/docs
```

### 3. Verify Logs

```bash
# Supervisor logs
sudo tail -f /var/log/taskapi/app.log

# Nginx logs
sudo tail -f /var/log/nginx/taskapi_access.log
sudo tail -f /var/log/nginx/taskapi_error.log

# Application logs
tail -f /opt/taskapi/tmp/logs/__main__.log
```

### 4. Verify Database

```bash
psql -h localhost -U taskapi -d tasks_db -c "SELECT COUNT(*) FROM tasks;"
```

---

## Monitoring & Logging

### Application Logging

**Log Locations**:
- Application logs: `/opt/taskapi/tmp/logs/`
- Supervisor logs: `/var/log/taskapi/`
- Nginx logs: `/var/log/nginx/`

**Log Rotation**:
Application logs automatically rotate at 10MB with 5 backups.

For system logs, configure logrotate in `/etc/logrotate.d/taskapi`:

```
/var/log/taskapi/*.log {
    daily
    missingok
    rotate 14
    compress
    delaycompress
    notifempty
    create 0640 www-data www-data
    sharedscripts
    postrotate
        supervisorctl restart taskapi > /dev/null 2>&1 || true
    endscript
}
```

### Health Check Monitoring

Set up monitoring to check `/api/v1/health` endpoint:

**Using curl in cron**:
```bash
# /etc/cron.d/taskapi-health
*/5 * * * * root curl -sf https://yourdomain.com/api/v1/health || echo "API health check failed" | mail -s "TaskAPI Alert" admin@example.com
```

**Using external services**:
- UptimeRobot
- Pingdom
- StatusCake
- AWS CloudWatch
- Datadog

### Performance Monitoring

**Recommended Tools**:
- **Application Performance Monitoring (APM)**:
  - New Relic
  - Datadog
  - Elastic APM

- **Infrastructure Monitoring**:
  - Prometheus + Grafana
  - CloudWatch (AWS)
  - Azure Monitor

**Key Metrics to Monitor**:
- Request latency (p50, p95, p99)
- Error rate
- Requests per second
- Database connection pool usage
- Memory usage
- CPU usage
- Disk I/O

### Database Monitoring

```sql
-- Active connections
SELECT count(*) FROM pg_stat_activity;

-- Slow queries
SELECT pid, now() - pg_stat_activity.query_start AS duration, query
FROM pg_stat_activity
WHERE state = 'active' AND now() - pg_stat_activity.query_start > interval '5 seconds';

-- Database size
SELECT pg_size_pretty(pg_database_size('tasks_db'));

-- Table sizes
SELECT schemaname, tablename, pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename))
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;
```

---

## Performance Tuning

### Application Performance

#### 1. Worker Processes

Adjust number of Uvicorn workers in Supervisor config:

```ini
# Formula: (2 * CPU cores) + 1
# For 4 CPU cores: (2 * 4) + 1 = 9 workers
command=/opt/taskapi/venv/bin/uvicorn src.api.main:app --host 127.0.0.1 --port 8000 --workers 9
```

#### 2. Database Connection Pool

Edit `src/database/session.py` if needed:

```python
engine = create_async_engine(
    config.DATABASE_URL,
    echo=config.DATABASE_ECHO,
    pool_size=20,  # Increase from default 5
    max_overflow=40,  # Increase from default 10
    pool_pre_ping=True,
    pool_recycle=3600  # Recycle connections after 1 hour
)
```

#### 3. PostgreSQL Tuning

Edit `/etc/postgresql/14/main/postgresql.conf`:

```ini
# Memory settings for 4GB RAM server
shared_buffers = 1GB
effective_cache_size = 3GB
maintenance_work_mem = 256MB
work_mem = 8MB

# Query planner
random_page_cost = 1.1  # For SSD
effective_io_concurrency = 200  # For SSD

# Write-Ahead Log
wal_buffers = 16MB
checkpoint_completion_target = 0.9
```

Restart PostgreSQL:
```bash
sudo systemctl restart postgresql
```

### Nginx Performance

Edit `/etc/nginx/nginx.conf`:

```nginx
user www-data;
worker_processes auto;  # Auto-detect CPU cores
worker_rlimit_nofile 65535;

events {
    worker_connections 4096;
    use epoll;
    multi_accept on;
}

http {
    # Basic settings
    sendfile on;
    tcp_nopush on;
    tcp_nodelay on;
    keepalive_timeout 65;
    types_hash_max_size 2048;
    client_max_body_size 10M;

    # Gzip compression
    gzip on;
    gzip_vary on;
    gzip_proxied any;
    gzip_comp_level 6;
    gzip_types text/plain text/css text/xml text/javascript
               application/json application/javascript application/xml+rss;

    # Connection pooling to backend
    upstream taskapi_backend {
        server 127.0.0.1:8000;
        keepalive 32;
    }

    # ... rest of config
}
```

---

## Security Hardening

### 1. Firewall Configuration

```bash
# UFW (Ubuntu)
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow ssh
sudo ufw allow http
sudo ufw allow https
sudo ufw enable
```

### 2. Fail2ban

```bash
# Install fail2ban
sudo apt install fail2ban

# Configure for Nginx
sudo nano /etc/fail2ban/jail.local
```

Add:
```ini
[nginx-http-auth]
enabled = true

[nginx-limit-req]
enabled = true
filter = nginx-limit-req
logpath = /var/log/nginx/taskapi_error.log
```

Restart fail2ban:
```bash
sudo systemctl restart fail2ban
```

### 3. Database Security

```bash
# Secure PostgreSQL
sudo -u postgres psql

-- Remove test database
DROP DATABASE IF EXISTS test;

-- Revoke public schema privileges
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
```

### 4. File Permissions

```bash
# Application directory
sudo chown -R www-data:www-data /opt/taskapi
sudo chmod -R 755 /opt/taskapi

# Environment file
sudo chmod 600 /opt/taskapi/.env

# Log directory
sudo chown -R www-data:www-data /var/log/taskapi
sudo chmod 755 /var/log/taskapi
```

### 5. Regular Updates

```bash
# System packages
sudo apt update && sudo apt upgrade -y

# Python dependencies
cd /opt/taskapi
source venv/bin/activate
pip list --outdated

# Security audit
pip install pip-audit
pip-audit
```

---

## Troubleshooting

### Application Won't Start

**Check Supervisor status**:
```bash
sudo supervisorctl status taskapi
```

**Check logs**:
```bash
sudo tail -100 /var/log/taskapi/app.log
```

**Common issues**:
1. Database connection failed - Check DATABASE_URL
2. Port already in use - Check if another process is using port 8000
3. Permission denied - Check file permissions and user

### Database Connection Issues

**Test connection**:
```bash
psql -h localhost -U taskapi -d tasks_db -W
```

**Check PostgreSQL status**:
```bash
sudo systemctl status postgresql
```

**Check PostgreSQL logs**:
```bash
sudo tail -100 /var/log/postgresql/postgresql-14-main.log
```

### High Response Times

**Check database**:
```sql
-- Find slow queries
SELECT * FROM pg_stat_activity WHERE state = 'active';
```

**Check application logs**:
```bash
grep -i "error\|warning" /opt/taskapi/tmp/logs/__main__.log
```

**Check system resources**:
```bash
top
htop
iostat
```

### 502 Bad Gateway

**Check application is running**:
```bash
sudo supervisorctl status taskapi
curl http://127.0.0.1:8000/api/v1/health
```

**Check Nginx config**:
```bash
sudo nginx -t
sudo systemctl status nginx
```

### SSL Certificate Issues

**Check certificate validity**:
```bash
sudo certbot certificates
```

**Renew certificate**:
```bash
sudo certbot renew
```

---

## Rollback Procedures

### Application Rollback

```bash
# Stop application
sudo supervisorctl stop taskapi

# Checkout previous version
cd /opt/taskapi
git fetch --all --tags
git checkout v0.9.0  # Previous version

# Reinstall dependencies (if changed)
source venv/bin/activate
pip install -r requirements.txt

# Rollback database migrations (if needed)
alembic downgrade -1

# Restart application
sudo supervisorctl start taskapi

# Verify
curl https://yourdomain.com/api/v1/health
```

### Database Rollback

```bash
# Restore from backup
sudo -u postgres pg_restore -d tasks_db /backups/tasks_db_20251023.dump

# Or use point-in-time recovery if configured
```

---

## Maintenance

### Database Backups

**Automated daily backup script** (`/opt/taskapi/scripts/backup-db.sh`):

```bash
#!/bin/bash
BACKUP_DIR="/backups/taskapi"
DATE=$(date +%Y%m%d_%H%M%S)
DB_NAME="tasks_db"
DB_USER="taskapi"

mkdir -p $BACKUP_DIR
pg_dump -U $DB_USER -d $DB_NAME | gzip > $BACKUP_DIR/${DB_NAME}_${DATE}.sql.gz

# Keep only last 30 days
find $BACKUP_DIR -name "${DB_NAME}_*.sql.gz" -mtime +30 -delete

echo "Backup completed: ${DB_NAME}_${DATE}.sql.gz"
```

**Add to cron**:
```bash
# /etc/cron.d/taskapi-backup
0 2 * * * postgres /opt/taskapi/scripts/backup-db.sh >> /var/log/taskapi/backup.log 2>&1
```

### Database Maintenance

**Weekly vacuum** (`/etc/cron.weekly/postgres-vacuum`):
```bash
#!/bin/bash
sudo -u postgres vacuumdb --all --analyze --verbose
```

### Update Dependencies

```bash
# Check for updates
cd /opt/taskapi
source venv/bin/activate
pip list --outdated

# Update (test in staging first!)
pip install --upgrade package_name

# Update requirements.txt
pip freeze > requirements.txt

# Test application
pytest
```

### Log Monitoring

Set up log monitoring alerts for:
- Application errors (ERROR level)
- Database connection failures
- High response times (> 1s)
- High memory usage (> 80%)

---

## Production Checklist

### Before Going Live

- [ ] All tests passing
- [ ] SSL/TLS configured
- [ ] CORS origins restricted
- [ ] Database backed up
- [ ] Monitoring configured
- [ ] Logs configured
- [ ] Health checks working
- [ ] Load testing completed
- [ ] Security audit completed
- [ ] Documentation updated
- [ ] Rollback procedure tested

### After Going Live

- [ ] Monitor logs for errors
- [ ] Monitor performance metrics
- [ ] Verify backups working
- [ ] Test health check endpoint
- [ ] Review security logs
- [ ] Update documentation with any changes

---

## Support Contacts

**Technical Issues**:
- GitHub: [sergeychernyakov/blank_python_project](https://github.com/sergeychernyakov/blank_python_project)
- Telegram: [@AIBotsTech](https://t.me/AIBotsTech)

**Emergency Contacts**:
- On-call engineer: [Your contact]
- Database administrator: [Your contact]
- DevOps team: [Your contact]

---

**Deployment Guide Version**: 1.0
**Last Updated**: 2025-10-23
**Status**: Production Ready
**Next Review**: 2026-01-23
