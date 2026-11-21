# AOC V2.1 PostgreSQL `pgvector` 迁移、验证与回滚方案

> 状态：**隔离演练 100% 通过，等待 Joel 批准生产维护窗口执行**
> 迁移目标：`postgres:16-alpine`（`jsonb` 存储） -> `pgvector/pgvector:0.8.7-pg16-bookworm`（原生 `vector(512)` + HNSW 索引 + `pg_trgm`）

---

## 1. 隔离演练实测报告（零触碰生产库）

演练时间：2026-11-21（在独立临时容器 `pgvector/pgvector:0.8.7-pg16-bookworm` 端口 `5499` 上全流程执行，结束后已销毁容器）。

| 演练阶段 | 验证指标 | 实测结果 | 结论 |
|---|---|---|---|
| 1. 生产逻辑备份 | `pg_dump` 导出规模 | 15 MB，7,973 行 | PASS |
| 2. Dump 清理 | `scripts/migrate_pgvector.py --sanitize-dump` | 自动移除 PG 16 客户端 `\restrict` / `\unrestrict` 2 行 | PASS |
| 3. 隔离库恢复 | 目标版本 / 排序规则 / 扩展 | PG `16.16 (Debian)`，`en_US.utf8`，`vector 0.8.7` + `pg_trgm 1.6` | PASS |
| 4. 数据完整性核对 | 全部 **38 张表** 行数比对 | 100% 逐表完全一致；`alembic_version = 0008_p1_9_affect` | PASS |
| 5. 向量维度校验 | `memory_embeddings` 维度错位检查 | 总计 `179` 行，`dim_mismatch_rows = 0` | PASS |
| 6. 原生类型转换 | `jsonb -> vector(512)` + 建索引 | 成功创建 `ix_mem_emb_hnsw_cosine` (HNSW cosine) 与 `ix_memory_index_trgm` | PASS |
| 7. 原生 `<=>` 性能与召回 | 179 条向量全量自检探针 | `self_recall_at_1 = 1.0`，`self_recall_at_5 = 1.0`，`avg = 1.888 ms`，`p95 = 1.77 ms` | PASS |
| 8. 真实混合检索验证 | `HybridRetrievalEngine`（ONNX + pgvector） | 返回 5 条高相关命中（Top-1 综合分 `0.7198`，`vec=0.5866`，`txt=0.6905`） | PASS |
| 9. V2.1 历史数据修复联动 | `reconcile_v2_1_data.py --execute`（副本） | 回填 15 条被重试替代执行记录，归档 3 条 `AOC-E2E-*`，0 异常 | PASS |

---

## 2. 生产切换标准操作步骤（需 Joel 明确批准后执行）

> **前置保护**：严禁覆盖或删除历史只读快照 `backups/aoc_pg_dump_20261120.sql`。切换前必须新建带时间戳的新备份文件。

### Step 1：确认 AOC Core 与 72h 监控已停止（零写入窗口）
```bash
pgrep -af "uvicorn app.api.app|soak_monitor_72h" || echo "Core stopped cleanly"
```

### Step 2：创建切换前只读备份并清理元命令
```bash
cd /home/ubuntu/aoc
mkdir -p backups
TS=$(date -u +%Y%m%dT%H%M%SZ)
sudo docker exec aoc-postgres pg_dump -U aoc -d aoc --no-owner --no-privileges > "backups/aoc_pre_pgvector_${TS}.sql"
.venv/bin/python scripts/migrate_pgvector.py --sanitize-dump "backups/aoc_pre_pgvector_${TS}.sql" --output "backups/aoc_pre_pgvector_${TS}.clean.sql"
ls -lh "backups/aoc_pre_pgvector_${TS}.sql" "backups/aoc_pre_pgvector_${TS}.clean.sql"
```

### Step 3：保留旧数据卷，启动新的 `pgvector` 数据库容器
> **关键防呆**：不要删除原有 `aoc_pgdata` 卷！改名或使用新数据卷 `aoc_pgdata_pgvector`，确保 30 秒内可无损回滚。

```bash
sudo docker stop aoc-postgres
sudo docker rename aoc-postgres aoc-postgres-alpine-backup
# 使用 docker-compose.yml 中的 pgvector/pgvector:0.8.7-pg16-bookworm（绑定新数据卷）启动 aoc-postgres
sudo docker compose up -d postgres
```

### Step 4：启用扩展、导入数据并执行 `vector(512)` 转换与索引创建
```bash
sudo docker exec -i aoc-postgres psql -U aoc -d aoc -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm;"
sudo docker exec -i aoc-postgres psql -U aoc -d aoc -v ON_ERROR_STOP=1 < "backups/aoc_pre_pgvector_${TS}.clean.sql"

.venv/bin/python scripts/migrate_pgvector.py --convert --verify
```

### Step 5：验证通过后执行 V2.1 幂等历史数据修复
```bash
# 先预览确认
.venv/bin/python scripts/reconcile_v2_1_data.py --dry-run
# 确认无异常后落库
.venv/bin/python scripts/reconcile_v2_1_data.py --execute
```

### Step 6：检查验收指标
确认 `migrate_pgvector.py --verify` 输出满足以下全部条件：
- `pgvector_ready: true`
- `embedding_column_type: "USER-DEFINED"` (`vector(512)`)
- `dim_mismatch_rows: 0`
- `self_recall_at_1: 1.0`, `self_recall_at_5: 1.0`
- `avg_latency_ms < 10.0`
- `alembic_version == "0008_p1_9_affect"`

---

## 3. 紧急回滚方案（RTO < 60 秒）

如果切换过程中任何一步校验失败，或上线后发现异常，**立即执行无损回滚**（保留的原始 `aoc-postgres-alpine-backup` 容器与原数据卷完全未修改）：

```bash
# 1. 停止 AOC Core（若已启动）
pkill -TERM -f "uvicorn app.api.app" || true

# 2. 停掉并移走新 pgvector 容器
sudo docker stop aoc-postgres
sudo docker rm aoc-postgres

# 3. 恢复原 alpine 容器并启动
sudo docker rename aoc-postgres-alpine-backup aoc-postgres
sudo docker start aoc-postgres

# 4. 验证原库健康与 AOC-001 完整
sudo docker exec aoc-postgres psql -U aoc -d aoc -c "SELECT name, version FROM organism_identity WHERE name='AOC-001';"
```

只有在生产环境稳定运行至少 **72 小时** 且 Joel 明确确认后，方可清理 `aoc-postgres-alpine-backup` 容器。
