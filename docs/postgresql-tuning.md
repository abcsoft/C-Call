# C-Call IVR — PostgreSQL Production Tuning

## Connection Budget

| Service | Instances | Pool max | Total |
|---------|-----------|----------|-------|
| web (Gunicorn gthread) | 1 × 4 workers | 5 | 20 |
| scheduler | 1 | 3 | 3 |
| call_worker | 2 | 3 | 6 |
| migrate (one-off) | 1 | 1 | 1 |
| backup / admin reserve | — | — | 10 |
| **Total** | | | **40** |

Set `max_connections = 60` in postgresql.conf (40 app + 20 superuser/monitoring reserve).

### Pool settings per service (set in app/extensions.py)

```python
# web process
ConnectionPool(min_size=1, max_size=5, ...)
# scheduler
ConnectionPool(min_size=1, max_size=3, ...)
# call_worker
ConnectionPool(min_size=1, max_size=3, ...)
```

**Why reduced from earlier 20-per-service design:**
Each connection on PostgreSQL consumes ~5–8 MB RAM. 20 connections × multiple services = 100+ connections = 500–800 MB of server RAM consumed by idle connections alone, before any query runs. With psycopg_pool, connections are reused across threads. Start at these conservative values and increase only under measured load.

If `max_connections` must exceed 60, add **PgBouncer** in transaction pooling mode first.

---

## postgresql.conf Tuning Fragment

Apply via a `conf.d/ccall.conf` include or directly in `postgresql.conf`.

```conf
# ── Connections ───────────────────────────────────────────────────────────────
max_connections = 60
superuser_reserved_connections = 5

# ── Timeouts ──────────────────────────────────────────────────────────────────
# Kill queries running more than 30 s (protects against runaway analytics queries)
statement_timeout = '30s'
# Kill any lock wait that blocks longer than 5 s (surfaces deadlocks fast)
lock_timeout = '5s'
# Kill idle transactions — prevents connection starvation in the pool
idle_in_transaction_session_timeout = '10s'
# Log any lock wait exceeding 250 ms
log_lock_waits = on
deadlock_timeout = '500ms'

# ── Memory ────────────────────────────────────────────────────────────────────
# Start values for a 2 GB server. Tune based on EXPLAIN BUFFERS output.
shared_buffers = 512MB                  # 25% of RAM
work_mem = 4MB                          # per sort/hash — 60 connections × 4 MB = 240 MB peak
maintenance_work_mem = 128MB            # VACUUM, CREATE INDEX
effective_cache_size = 1536MB           # total OS + PG cache estimate (75% of RAM)

# ── WAL / Checkpointing ───────────────────────────────────────────────────────
checkpoint_timeout = '10min'
max_wal_size = '1GB'
min_wal_size = '256MB'
wal_compression = on                    # reduce WAL size (safe from PostgreSQL 9.5+)

# ── WAL Archiving (for PITR — enable when archive storage is ready) ───────────
# Uncomment these to enable point-in-time recovery.
# Until enabled, RPO = last daily backup (up to 24 h data loss).
# archive_mode = on
# archive_command = 'aws s3 cp %p s3://${BACKUP_S3_BUCKET}/wal/%f'
# archive_timeout = 300       # archive WAL segment every 5 min even if not full

# ── Observability ─────────────────────────────────────────────────────────────
shared_preload_libraries = 'pg_stat_statements'
pg_stat_statements.max = 5000
pg_stat_statements.track = all
log_min_duration_statement = 500ms       # log slow queries (>500 ms)
log_checkpoints = on
log_connections = off                    # verbose — enable only for debugging
log_disconnections = off

# ── Locale / Timezone ─────────────────────────────────────────────────────────
timezone = 'UTC'
lc_messages = 'C'

# ── Application names ─────────────────────────────────────────────────────────
# Each service sets application_name in its DATABASE_URL or pool config.
# Example (set in pool options):
#   options=-c application_name=ccall_web
#   options=-c application_name=ccall_scheduler
#   options=-c application_name=ccall_worker
# This lets pg_stat_activity show which service holds each connection.
```

---

## pg_hba.conf

```conf
# TYPE  DATABASE    USER        ADDRESS       METHOD
local   all         postgres                  peer
local   all         ccall_app                 md5
host    ccall_prod  ccall_app   10.0.0.0/8   scram-sha-256
host    ccall_prod  ccall_mig   10.0.0.0/8   scram-sha-256
# Reject all other connections
host    all         all         0.0.0.0/0    reject
```

`ccall_app` — runtime role (SELECT, INSERT, UPDATE, DELETE on all tables)
`ccall_mig` — migration role (CONNECT + DDL, used only by migrate service)

---

## Monitoring Queries

```sql
-- Active connections per application
SELECT application_name, state, COUNT(*)
FROM pg_stat_activity
WHERE datname = 'ccall_prod'
GROUP BY application_name, state;

-- Top slow queries (requires pg_stat_statements)
SELECT query, calls, total_exec_time/calls AS avg_ms, rows/calls AS avg_rows
FROM pg_stat_statements
ORDER BY total_exec_time DESC LIMIT 20;

-- Bloat / lock check
SELECT pid, usename, application_name, state, wait_event_type, wait_event, query
FROM pg_stat_activity
WHERE wait_event_type = 'Lock';

-- Check WAL archive lag (when archive_mode=on)
SELECT * FROM pg_stat_archiver;
```

---

## Remaining Risks

| Risk | Mitigation |
|------|-----------|
| WAL archiving not configured | Enable `archive_mode=on` + `archive_command` to reduce RPO to < 5 min |
| PgBouncer not deployed | Add when tenant count × services approaches `max_connections` |
| `statement_timeout` may cancel long reports | Raise for admin role: `ALTER ROLE ccall_admin SET statement_timeout='300s'` |
| pg_stat_statements not yet in `shared_preload_libraries` on dev box | Add to prod postgresql.conf before first production deploy |
