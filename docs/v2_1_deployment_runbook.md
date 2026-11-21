# AOC V2.1 前后端部署、安全访问与恢复手册

> 适用分支：`feature/v2.1`
> 安全基线：**严禁将 AOC Core 或 Control Center 直接暴露到 `0.0.0.0` 公网**；默认仅监听 `127.0.0.1:9200`，通过 SSH Tunnel 或带认证的 HTTPS 反向代理访问。

---

## 1. 前置依赖与构建

### 1.1 后端（Python 3.12 + `.venv`）
```bash
cd /home/ubuntu/aoc
.venv/bin/pytest tests/unit/ -q
```

### 1.2 前端（AOC Control Center SPA）
```bash
cd /home/ubuntu/aoc/frontend
npm ci            # 或 npm install
npm run test      # Vitest 单元测试
npm run build     # 生成 /home/ubuntu/aoc/frontend/dist/
```
构建产物位于 `/home/ubuntu/aoc/frontend/dist/`。FastAPI 启动时自动检测该目录并在 `/` 与 `/assets` 提供故障隔离的 SPA 静态服务；也可通过环境变量 `AOC_FRONTEND_DIST` 指定其他目录（设为不存在路径即可彻底关闭内置静态托管而不影响 API）。

---

## 2. Host `systemd` Runner `0660` 权限切换

V2.1 修复了 uvicorn 将 Runner Unix Socket 强制改写为 `0666` 的缺陷（改为 `bind_uds_socket_0660()` 预绑定）。

切换命令（需 Joel 确认后执行）：
```bash
sudo systemctl restart aoc-agent-runner.service
sudo systemctl status aoc-agent-runner.service --no-pager
# 验证 socket 权限必须严格为 srw-rw---- (0660)，绝不能是 0666
stat -c "%a %U:%G %n" /run/aoc-runner/runner.sock
```
预期输出：`660 ubuntu:ubuntu /run/aoc-runner/runner.sock`。

---

## 3. Letta 512d Embedding 统一迁移（移除 1024d 零填充）

在重启 AOC Core 加载 V2.1 新版 `/v1/embeddings`（原生 512d）时，需同步将 Letta Agent Embedding 配置更新为 512d：
```bash
cd /home/ubuntu/aoc
# 1. 预览将要更新的 Agent 与维度差异
.venv/bin/python scripts/migrate_letta_embedding_512d.py --dry-run
# 2. 经 Joel 确认后执行切换
.venv/bin/python scripts/migrate_letta_embedding_512d.py --execute
```
回滚方式：若需回退到旧版 V2.0 代码，重新切回 `main` 分支后重启 Core 与 Letta 即可恢复旧配置。

---

## 4. 启动方式

### 方式 A：宿主机运行 Core + Docker 运行 Postgres/Letta + Host `systemd` Runner（当前默认生产形态）
```bash
cd /home/ubuntu/aoc
# 仅只读查看 Control Center（不运行自主周期）：
AOC_ENABLE_SCHEDULER=false .venv/bin/uvicorn app.api.app:create_app --factory --host 127.0.0.1 --port 9200

# 正常生产运行（开启自主调度）：
.venv/bin/uvicorn app.api.app:create_app --factory --host 127.0.0.1 --port 9200
```

### 方式 B：标准全栈 Docker Compose 拓扑（Containerized Core UID 1000:1000 -> Host systemd Runner UDS）
已通过容器化实测（`python:3.12-slim --user 1000:1000` 挂载 `/run/aoc-runner` 与 `/home/ubuntu/aoc-workspaces` 成功调用宿主机 Runner `/v1/health` 与 `/v1/executions`）：
```bash
cd /home/ubuntu/aoc
sudo docker compose up -d --build
```
注意：`docker-compose.yml` 中 `aoc-core` 端口绑定严格限制为 `127.0.0.1:9200:9200`，且 `aoc-letta` 直接通过容器内网访问 `http://aoc-core:9200/v1`，不再需要 `172.18.0.1:9200` 临时转发。

---

## 5. 安全访问 AOC Control Center

### 推荐方式 1：SSH 端口转发（零公网暴露，最安全）
在 Joel 本地电脑终端执行：
```bash
ssh -N -L 9200:127.0.0.1:9200 ubuntu@<SERVER_IP>
```
然后在本地浏览器打开：
- **`http://127.0.0.1:9200/`**
- 在登录页输入 `AOC_API__ADMIN_KEY`（管理员权限）或只读审查员 Key（仅 `read` 权限，无法操作暂停/审批）。
- Token 仅存于浏览器 `sessionStorage`，关闭标签页自动销毁，永不写入 URL Query 或 `localStorage`。

### 推荐方式 2：带 TLS 与访问控制的反向代理（Nginx / Caddy）
如需通过内网域名访问，必须在反代层强制开启：
1. HTTPS（TLS 1.2+）
2. IP 白名单或 Basic Auth / Tailscale / ZeroTrust 入口鉴权
3. 转发 `Authorization` 请求头至 `http://127.0.0.1:9200`

示例 Nginx 片段：
```nginx
server {
    listen 443 ssl http2;
    server_name aoc-control.internal;
    # 限制仅内网/Tailscale网段访问
    allow 100.64.0.0/10;
    deny all;

    location / {
        proxy_pass http://127.0.0.1:9200;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Authorization $http_authorization;
    }
}
```

---

## 6. 故障排查与应急恢复

| 故障现象 | 诊断命令 | 恢复动作 |
|---|---|---|
| 前端页面打不开，但 `/health` 返回 200 | `ls -la /home/ubuntu/aoc/frontend/dist/index.html` | 在 `frontend/` 执行 `npm run build` 后刷新页面；Core 自主调度不受任何影响 |
| Control Center 返回 `401 Unauthorized` | 检查登录 Token 是否匹配 `.env` 中 `AOC_API__ADMIN_KEY` 或 `api_credentials` | 点击右上角“退出”重新输入有效 Bearer Token |
| 只读账号无法看到“暂停调度”/审批按钮 | 调用 `/v2/auth/me` 查看 `scopes` | 属预期 RBAC 保护行为；仅 `admin`/`control`/`approve` 权限可见操作按钮 |
| Runner UDS 报 `PermissionError` | `stat -c "%a %U:%G" /run/aoc-runner/runner.sock` | 确认 Core 进程 UID/GID 为 `1000:1000`（`ubuntu`）且重启 `aoc-agent-runner.service` |
| `pgvector` 切换后异常 | 检查 `migrate_pgvector.py --verify` | 按 `docs/v2_1_pgvector_migration_plan.md` 第 3 节 60 秒切回 `aoc-postgres-alpine-backup` |
