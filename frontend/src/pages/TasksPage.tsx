import React, { useEffect, useState } from 'react';
import { ListChecks, Cpu, ArrowUpRight, Search } from 'lucide-react';
import { apiRequest, formatTime, shortId } from '../api/client';
import {
  Button,
  Card,
  CardHeader,
  Drawer,
  EmptyState,
  ErrorBanner,
  JsonBlock,
  LoadingPanel,
  PaginationBar,
  StatusBadge,
} from '../components/ui';

export function TasksPage({
  refreshTick,
  selectedTaskId,
  onSelectTask,
  onOpenCycle,
}: {
  refreshTick: number;
  selectedTaskId: string | null;
  onSelectTask: (taskId: string | null) => void;
  onOpenCycle: (cycleId: string) => void;
}) {
  const [data, setData] = useState<any>(null);
  const [offset, setOffset] = useState(0);
  const [status, setStatus] = useState('');
  const [search, setSearch] = useState('');
  const [err, setErr] = useState<string | null>(null);

  const [detail, setDetail] = useState<any>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  useEffect(() => {
    let active = true;
    const params = new URLSearchParams({ limit: '20', offset: String(offset) });
    if (status) params.set('status', status);
    if (search.trim()) params.set('search', search.trim());
    apiRequest(`/v2/control-center/tasks?${params.toString()}`)
      .then((res) => {
        if (!active) return;
        setData(res);
        setErr(null);
      })
      .catch((e) => active && setErr(e?.detail || e?.message || '加载任务列表失败'));
    return () => {
      active = false;
    };
  }, [offset, status, search, refreshTick]);

  useEffect(() => {
    if (!selectedTaskId) {
      setDetail(null);
      return;
    }
    let active = true;
    setDetailLoading(true);
    apiRequest(`/v2/control-center/tasks/${selectedTaskId}/detail`)
      .then((res) => active && setDetail(res))
      .catch(() => active && setDetail(null))
      .finally(() => active && setDetailLoading(false));
    return () => {
      active = false;
    };
  }, [selectedTaskId, refreshTick]);

  return (
    <div className="space-y-5">
      {err && <ErrorBanner error={err} />}

      {/* Agents & Host Runners Health Row */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title="AgentProvider 注册与实测健康状态" subtitle="严格区分 AgentProvider（Codex）与 MCP ToolProvider" icon={<Cpu className="h-4 w-4" />} />
          <div className="divide-y divide-slate-200/70 dark:divide-slate-800/80">
            {(data?.agents || []).map((a: any) => (
              <div key={a.agent_id} className="flex items-center justify-between gap-3 px-4 py-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-semibold">{a.name}</span>
                    <span className="font-mono text-[11px] text-slate-400">({a.agent_id})</span>
                    {a.experimental && <StatusBadge value="IDLE" label="experimental" />}
                  </div>
                  <p className="mt-0.5 text-[11px] font-mono text-slate-500">
                    版本：{a.version || '未安装'} · 模式：{(a.supported_modes || []).join(', ')}
                  </p>
                </div>
                <StatusBadge value={a.enabled && a.healthy ? 'healthy' : 'offline'} label={a.enabled ? (a.healthy ? '可用' : '异常') : '已禁用'} />
              </div>
            ))}
          </div>
        </Card>

        <Card>
          <CardHeader title="宿主机 systemd Agent Runner" subtitle="非特权 User=ubuntu + cgroup v2 + UDS (0660)" />
          <div className="divide-y divide-slate-200/70 dark:divide-slate-800/80">
            {(data?.runners || []).length === 0 ? (
              <EmptyState title="未注册外部 Runner（当前使用本地降级模式）" />
            ) : (
              data.runners.map((r: any) => (
                <div key={r.runner_id} className="px-4 py-3 space-y-1">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-semibold font-mono">{r.runner_id}</span>
                    <StatusBadge value={r.status} />
                  </div>
                  <div className="text-[11px] font-mono text-slate-500">
                    Unit: {r.systemd_unit || '—'} · Socket: {r.socket_path}
                  </div>
                  <div className="text-[11px] font-mono text-slate-500">
                    boot_id: {shortId(r.host_boot_id)} · 心跳：{formatTime(r.last_heartbeat_at)} · cgroup_delegated:{' '}
                    {String(r.capabilities?.cgroup_delegated ?? '—')}
                  </div>
                </div>
              ))
            )}
          </div>
        </Card>
      </div>

      {/* Tasks Table */}
      <Card>
        <CardHeader
          title="任务执行列表（Durable Task State Machine）"
          subtitle="点击任意行展开状态迁移时间线、Execution、ProcessIdentity 与结果验证详情"
          icon={<ListChecks className="h-4 w-4" />}
          action={
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative">
                <Search className="pointer-events-none absolute left-2.5 top-2 h-3.5 w-3.5 text-slate-400" />
                <input
                  value={search}
                  onChange={(e) => {
                    setSearch(e.target.value);
                    setOffset(0);
                  }}
                  placeholder="搜索任务目标…"
                  className="rounded-lg border border-slate-200 bg-white py-1.5 pl-8 pr-3 text-xs dark:border-slate-700 dark:bg-slate-900"
                />
              </div>
              <select
                value={status}
                onChange={(e) => {
                  setStatus(e.target.value);
                  setOffset(0);
                }}
                className="rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-900"
              >
                <option value="">全部状态</option>
                <option value="SUCCEEDED">SUCCEEDED</option>
                <option value="FAILED">FAILED</option>
                <option value="RUNNING">RUNNING</option>
                <option value="REQUIRES_APPROVAL">REQUIRES_APPROVAL</option>
                <option value="REJECTED">REJECTED</option>
                <option value="RECOVERY_REQUIRED">RECOVERY_REQUIRED</option>
              </select>
            </div>
          }
        />

        {!data ? (
          <LoadingPanel />
        ) : data.items.length === 0 ? (
          <EmptyState title="暂无任务记录" />
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="border-b border-slate-200/70 text-[11px] text-slate-500 dark:border-slate-800">
                  <tr>
                    <th className="px-4 py-2.5">Task ID</th>
                    <th className="px-4 py-2.5">目标摘要</th>
                    <th className="px-4 py-2.5">状态</th>
                    <th className="px-4 py-2.5">权限</th>
                    <th className="px-4 py-2.5">执行体 / 尝试</th>
                    <th className="px-4 py-2.5">PID / Thread</th>
                    <th className="px-4 py-2.5">耗时</th>
                    <th className="px-4 py-2.5">创建时间</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-200/70 dark:divide-slate-800/80">
                  {data.items.map((t: any) => (
                    <tr
                      key={t.task_id}
                      onClick={() => onSelectTask(t.task_id)}
                      className="hover:bg-slate-50 dark:hover:bg-slate-800/50 cursor-pointer transition-colors"
                    >
                      <td className="px-4 py-2.5 font-mono text-cyan-600 dark:text-cyan-400">
                        {shortId(t.task_id)}
                      </td>
                      <td className="px-4 py-2.5 max-w-md truncate">{t.objective}</td>
                      <td className="px-4 py-2.5">
                        <StatusBadge value={t.status} />
                      </td>
                      <td className="px-4 py-2.5">
                        <StatusBadge value={t.permission_level} />
                      </td>
                      <td className="px-4 py-2.5 font-mono">
                        {t.assigned_agent} ({t.executions_count}/{t.max_attempts})
                      </td>
                      <td className="px-4 py-2.5 font-mono text-[11px] text-slate-500">
                        {t.latest_execution?.pid ? `PID ${t.latest_execution.pid}` : '—'}
                        {t.latest_execution?.thread_id ? ` · ${shortId(t.latest_execution.thread_id)}` : ''}
                      </td>
                      <td className="px-4 py-2.5 font-mono tabular-nums">
                        {t.duration_seconds != null ? `${t.duration_seconds}s` : '—'}
                      </td>
                      <td className="px-4 py-2.5 font-mono text-[11px] text-slate-500">
                        {formatTime(t.created_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <PaginationBar
              total={data.total}
              limit={data.limit}
              offset={data.offset}
              onChange={setOffset}
            />
          </>
        )}
      </Card>

      {/* Task Detail Drawer */}
      <Drawer
        open={Boolean(selectedTaskId)}
        onClose={() => onSelectTask(null)}
        title={detail?.task?.objective?.slice(0, 80) || '任务详情'}
        subtitle={selectedTaskId || ''}
      >
        {detailLoading && <LoadingPanel label="加载任务生命周期与执行明细…" />}
        {detail && (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge value={detail.task.status} />
              <StatusBadge value={detail.task.permission_level} />
              <span className="text-xs font-mono text-slate-500">
                Agent: {detail.task.assigned_agent} · state_version={detail.task.state_version}
              </span>
              {detail.task.cycle_id && (
                <Button
                  size="xs"
                  variant="primary"
                  onClick={() => {
                    const cid = detail.task.cycle_id;
                    onSelectTask(null);
                    onOpenCycle(cid);
                  }}
                >
                  跳转至关联 Cycle <ArrowUpRight className="h-3 w-3" />
                </Button>
              )}
            </div>

            <Card className="p-4 space-y-2">
              <div className="text-xs font-semibold">任务目标与约束</div>
              <p className="text-xs text-slate-700 dark:text-slate-300 whitespace-pre-wrap">{detail.task.objective}</p>
              <div className="text-[11px] font-mono text-slate-500">
                Workspace: {detail.task.workspace} · Finalized: {formatTime(detail.task.finalized_at)}
              </div>
            </Card>

            <Card className="p-4 space-y-2">
              <div className="text-xs font-semibold">状态迁移时间线（task_lifecycle_events）</div>
              <div className="space-y-1.5">
                {detail.lifecycle_events.map((ev: any) => (
                  <div key={ev.id} className="flex items-center justify-between gap-2 text-xs font-mono">
                    <span>
                      v{ev.state_version}: {ev.from_status || '∅'} →{' '}
                      <strong className="text-cyan-600 dark:text-cyan-400">{ev.to_status}</strong> ({ev.actor})
                    </span>
                    <span className="text-[10px] text-slate-400">{formatTime(ev.created_at)}</span>
                  </div>
                ))}
              </div>
            </Card>

            <div className="space-y-3">
              <div className="text-xs font-semibold">执行尝试明细（agent_executions & ProcessIdentity）</div>
              {detail.executions.map((ex: any) => (
                <Card key={ex.execution_id} className="p-4 space-y-2.5">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="text-xs font-semibold font-mono">
                      Attempt #{ex.attempt_number} · {shortId(ex.execution_id)}
                    </span>
                    <div className="flex items-center gap-2">
                      <StatusBadge value={ex.status} />
                      <StatusBadge
                        value={ex.finalized_at ? 'SUCCEEDED' : 'PENDING'}
                        label={ex.finalized_at ? '已结项' : '未结项'}
                      />
                    </div>
                  </div>
                  <div className="grid grid-cols-2 gap-2 text-[11px] font-mono text-slate-500">
                    <div>Runner: {ex.runner_id || 'local'}</div>
                    <div>PID: {ex.pid ?? '未记录'}</div>
                    <div>Thread ID: {ex.thread_id || '未记录'}</div>
                    <div>耗时: {ex.duration_seconds != null ? `${ex.duration_seconds}s` : '—'}</div>
                  </div>
                  {ex.error && (
                    <div className="rounded bg-rose-500/10 px-2.5 py-1.5 text-xs text-rose-500">
                      错误：{ex.error}
                    </div>
                  )}
                  <details>
                    <summary className="cursor-pointer text-xs text-cyan-600 dark:text-cyan-400">
                      展开 ProcessIdentity / Verification / Output
                    </summary>
                    <div className="mt-2 space-y-2">
                      <JsonBlock
                        data={{
                          process_identity: ex.process_identity,
                          verification: ex.verification,
                          resource_usage: ex.resource_usage,
                        }}
                      />
                      {ex.stdout_excerpt && <JsonBlock data={ex.stdout_excerpt} maxHeight="max-h-48" />}
                    </div>
                  </details>
                </Card>
              ))}
            </div>
          </>
        )}
      </Drawer>
    </div>
  );
}
