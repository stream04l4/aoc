import React, { useMemo } from 'react';
import {
  Activity,
  Cpu,
  Database,
  Flame,
  Play,
  Pause,
  Server,
  Sparkles,
  Zap,
  ArrowUpRight,
} from 'lucide-react';
import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
} from 'recharts';
import {
  apiRequest,
  formatNumber,
  formatPct,
  formatTime,
  hasScope,
  PrincipalInfo,
  shortId,
} from '../api/client';
import {
  Button,
  Card,
  CardHeader,
  EmptyState,
  ErrorBanner,
  LoadingPanel,
  StatCard,
  StatusBadge,
} from '../components/ui';
import { NavPage } from '../components/Layout';
import { OrganismAnatomy } from '../components/OrganismAnatomy';

export function OverviewPage({
  data,
  loading,
  error,
  onRefresh,
  principal,
  onOpenCycle,
  onOpenTask,
  onNavigate,
}: {
  data: any;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
  principal: PrincipalInfo;
  onOpenCycle: (cycleId: string) => void;
  onOpenTask: (taskId: string) => void;
  onNavigate?: (page: NavPage) => void;
}) {
  const [ctrlBusy, setCtrlBusy] = React.useState(false);
  const [ctrlMsg, setCtrlMsg] = React.useState<string | null>(null);

  const chartData = useMemo(() => {
    if (!data?.drive_trend?.length) return [];
    const grouped: Record<string, any> = {};
    for (const pt of data.drive_trend) {
      const k = pt.timestamp?.slice(0, 19) || '';
      if (!grouped[k]) {
        grouped[k] = { time: formatTime(pt.timestamp).slice(6) };
      }
      grouped[k][pt.drive_type] = Number((pt.satisfaction * 100).toFixed(1));
    }
    return Object.values(grouped).slice(-50);
  }, [data]);

  if (loading && !data) return <LoadingPanel />;
  if (error && !data) return <ErrorBanner error={error} onRetry={onRefresh} />;
  if (!data) return <EmptyState title="暂无总览数据" />;

  const org = data.organism || {};
  const latest = data.latest_cycle;
  const comps = data.components || {};
  const llmStats = data.stats?.usage_by_category?.llm || { calls: 0, total_amount: 0, avg_seconds: 0 };
  const canControl = hasScope(principal, 'control');

  const triggerControl = async (action: 'pause' | 'resume' | 'wake') => {
    setCtrlBusy(true);
    setCtrlMsg(null);
    try {
      await apiRequest(`/control/${action}`, {
        method: 'POST',
        body: { reason: `Control Center operator (${principal.name})` },
      });
      setCtrlMsg(`已执行 /control/${action}`);
      onRefresh();
    } catch (err: any) {
      setCtrlMsg(`操作失败：${err?.detail || err?.message}`);
    } finally {
      setCtrlBusy(false);
    }
  };

  return (
    <div className="space-y-5">
      {error && <ErrorBanner error={error} onRetry={onRefresh} />}

      {/* Hero status banner */}
      <Card className="p-5">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="space-y-1">
            <div className="flex flex-wrap items-center gap-2.5">
              <span className="relative flex h-2.5 w-2.5">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-cyan-400 opacity-60" />
                <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-cyan-500" />
              </span>
              <h2 className="text-lg font-semibold tracking-tight text-slate-900 dark:text-slate-50">
                {org.name}
              </h2>
              <StatusBadge value={org.lifecycle_state} />
              <span className="text-xs font-mono text-slate-500 dark:text-slate-400">
                Cycle #{org.cycle_count}
              </span>
              {org.affect_enabled ? (
                <StatusBadge value="ACQUIRED" label="Affect ON" />
              ) : (
                <StatusBadge value="IDLE" label="Affect OFF" />
              )}
            </div>
            <p className="text-xs text-slate-500 dark:text-slate-400">
              最近活动：{formatTime(org.last_cycle_at)} · 调度器：{org.scheduler_running ? '运行中' : '未启用'} ·
              {latest?.selected_drive ? ` 最近主导欲望：${latest.selected_drive}` : ' 暂无主导欲望'}
            </p>
            {latest?.arbitration_reason && (
              <p className="text-xs text-slate-600 dark:text-slate-300 max-w-3xl">
                仲裁依据：{latest.arbitration_reason}
              </p>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {canControl && (
              <>
                <Button size="sm" disabled={ctrlBusy} onClick={() => triggerControl('pause')}>
                  <Pause className="h-3.5 w-3.5" /> 暂停
                </Button>
                <Button size="sm" disabled={ctrlBusy} onClick={() => triggerControl('resume')}>
                  <Play className="h-3.5 w-3.5" /> 恢复
                </Button>
                <Button size="sm" variant="primary" disabled={ctrlBusy} onClick={() => triggerControl('wake')}>
                  <Zap className="h-3.5 w-3.5" /> 唤醒
                </Button>
              </>
            )}
            {latest?.cycle_id && (
              <Button size="sm" variant="secondary" onClick={() => onOpenCycle(latest.cycle_id)}>
                追踪最新 Cycle <ArrowUpRight className="h-3.5 w-3.5" />
              </Button>
            )}
          </div>
        </div>
        {ctrlMsg && (
          <div className="mt-3 text-xs text-cyan-600 dark:text-cyan-400">{ctrlMsg}</div>
        )}
      </Card>

      {/* V3.2 Humanoid Digital Organism & Four Pluggable Organs Centerpiece */}
      <OrganismAnatomy data={data} onNavigate={onNavigate} />

      {/* Top KPI Grid */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label={`窗口期周期数 (${data.window_hours === 0 ? '全部' : `${data.window_hours}h`})`}
          value={formatNumber(data.stats?.cycles_total_window)}
          subtext={`SUCCEEDED ${data.stats?.cycles_by_outcome?.SUCCEEDED || 0} · ERROR ${data.stats?.cycles_by_outcome?.ERROR || 0} · IDLE ${data.stats?.cycles_by_outcome?.IDLE || 0}`}
          tone={(data.stats?.cycles_by_outcome?.ERROR || 0) > 0 ? 'rose' : 'emerald'}
          icon={<Activity className="h-4 w-4" />}
        />
        <StatCard
          label="窗口期 Token 消耗"
          value={formatNumber(Math.round(llmStats.total_amount || 0))}
          subtext={`LLM 调用 ${formatNumber(llmStats.calls)} 次 · 平均 ${llmStats.avg_seconds}s`}
          tone="cyan"
          icon={<Sparkles className="h-4 w-4" />}
        />
        <StatCard
          label="窗口期任务执行"
          value={formatNumber(data.stats?.tasks_total_window)}
          subtext={`成功 ${data.stats?.tasks_by_status?.SUCCEEDED || 0} · 失败 ${data.stats?.tasks_by_status?.FAILED || 0} · 待审 ${data.stats?.tasks_by_status?.REQUIRES_APPROVAL || 0}`}
          icon={<Server className="h-4 w-4" />}
        />
        <StatCard
          label="Core 常驻内存 (RSS)"
          value={comps.core?.rss_mb != null ? `${comps.core.rss_mb} MB` : '—'}
          subtext={`红线 ≤ 512 MB · 宿主机可用内存 ${comps.core?.mem_available_pct != null ? `${comps.core.mem_available_pct}%` : '未记录'}`}
          tone={(comps.core?.rss_mb || 0) > 512 ? 'rose' : 'emerald'}
          icon={<Cpu className="h-4 w-4" />}
        />
      </div>

      {/* Drives Snapshot + Trend Chart */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader title="三大核心欲望实时状态" subtitle="基于真实感知重算，禁止 LLM 篡改" icon={<Flame className="h-4 w-4" />} />
          <div className="divide-y divide-slate-200/70 dark:divide-slate-800/80">
            {(data.drives || []).map((d: any) => (
              <div key={d.drive_type} className="p-4 space-y-2">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-semibold uppercase tracking-wider text-slate-700 dark:text-slate-200">
                    {d.drive_type}
                  </span>
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-semibold tabular-nums text-slate-900 dark:text-slate-50">
                      {formatPct(d.satisfaction)}
                    </span>
                    <span className="text-[11px] font-mono text-slate-400">
                      P={Number(d.priority || 0).toFixed(3)}
                    </span>
                  </div>
                </div>
                <div className="h-2 w-full overflow-hidden rounded-full bg-slate-200 dark:bg-slate-800">
                  <div
                    className={
                      d.drive_type === 'survival'
                        ? 'h-full bg-emerald-500'
                        : d.drive_type === 'reproduction'
                        ? 'h-full bg-violet-500'
                        : 'h-full bg-cyan-500'
                    }
                    style={{ width: `${Math.min(100, Math.max(0, (d.satisfaction || 0) * 100))}%` }}
                  />
                </div>
                {d.top_deficits?.length > 0 && (
                  <p className="text-[11px] text-slate-500 dark:text-slate-400 truncate">
                    主要赤字：{d.top_deficits.slice(0, 2).join('；')}
                  </p>
                )}
              </div>
            ))}
          </div>
        </Card>

        <Card className="lg:col-span-2">
          <CardHeader
            title="欲望满足度变化趋势 (%)"
            subtitle={`统计口径：drive_history（窗口 ${data.window_hours === 0 ? '全部' : `近 ${data.window_hours}h`}，最近 50 采样点）`}
          />
          <div className="h-72 p-4">
            {chartData.length > 0 ? (
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.15)" />
                  <XAxis dataKey="time" tick={{ fontSize: 10, fill: '#94a3b8' }} minTickGap={24} />
                  <YAxis domain={[0, 100]} tick={{ fontSize: 10, fill: '#94a3b8' }} />
                  <Tooltip
                    contentStyle={{
                      backgroundColor: '#0f172a',
                      border: '1px solid #334155',
                      borderRadius: 8,
                      fontSize: 12,
                    }}
                  />
                  <Line type="monotone" dataKey="survival" name="Survival" stroke="#10b981" strokeWidth={2} dot={false} />
                  <Line type="monotone" dataKey="reproduction" name="Reproduction" stroke="#8b5cf6" strokeWidth={2} dot={false} />
                  <Line type="monotone" dataKey="curiosity" name="Curiosity" stroke="#06b6d4" strokeWidth={2} dot={false} />
                </LineChart>
              </ResponsiveContainer>
            ) : (
              <EmptyState title="该时间窗口暂无欲望历史采样" />
            )}
          </div>
        </Card>
      </div>

      {/* Component Health Matrix */}
      <Card>
        <CardHeader title="核心子系统与基础设施健康矩阵" subtitle="Core / Runner / PostgreSQL / LLM / Letta / Embedding" icon={<Database className="h-4 w-4" />} />
        <div className="grid grid-cols-1 gap-3 p-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
          <div className="rounded-lg border border-slate-200/80 p-3 dark:border-slate-800">
            <div className="flex items-center justify-between">
              <span className="text-xs font-medium">AOC Core</span>
              <StatusBadge value={comps.core?.status} />
            </div>
            <p className="mt-2 text-[11px] font-mono text-slate-500">RSS: {comps.core?.rss_mb ?? '—'} MB</p>
          </div>
          <div className="rounded-lg border border-slate-200/80 p-3 dark:border-slate-800">
            <div className="flex items-center justify-between">
              <span className="text-xs font-medium">PostgreSQL</span>
              <StatusBadge value={comps.postgres?.status} />
            </div>
            <p className="mt-2 text-[11px] font-mono text-slate-500">
              pgvector: {comps.postgres?.pgvector_enabled ? '已启用' : 'JSONB 兼容'}
            </p>
          </div>
          <div className="rounded-lg border border-slate-200/80 p-3 dark:border-slate-800">
            <div className="flex items-center justify-between">
              <span className="text-xs font-medium">Host Runner</span>
              <StatusBadge value={comps.runner?.runners?.[0]?.status || (comps.runner?.enabled ? 'offline' : 'IDLE')} />
            </div>
            <p className="mt-2 text-[11px] font-mono text-slate-500 truncate">
              {comps.runner?.runners?.[0]?.runner_id || 'local-fallback'}
            </p>
          </div>
          <div className="rounded-lg border border-slate-200/80 p-3 dark:border-slate-800">
            <div className="flex items-center justify-between">
              <span className="text-xs font-medium">LLM Gateway</span>
              <StatusBadge value={comps.llm?.circuit_open ? 'ERROR' : comps.llm?.available === false ? 'down' : 'ok'} />
            </div>
            <p className="mt-2 text-[11px] font-mono text-slate-500 truncate">{comps.llm?.model || '—'}</p>
          </div>
          <div className="rounded-lg border border-slate-200/80 p-3 dark:border-slate-800">
            <div className="flex items-center justify-between">
              <span className="text-xs font-medium">Letta 记忆</span>
              <StatusBadge value={comps.letta?.available ? 'ok' : 'down'} />
            </div>
            <p className="mt-2 text-[11px] font-mono text-slate-500">
              已同步 {comps.letta?.memories_synced}/{comps.letta?.memories_total}
            </p>
          </div>
          <div className="rounded-lg border border-slate-200/80 p-3 dark:border-slate-800">
            <div className="flex items-center justify-between">
              <span className="text-xs font-medium">Embedding</span>
              <StatusBadge value="ok" label={comps.embedding?.provider} />
            </div>
            <p className="mt-2 text-[11px] font-mono text-slate-500 truncate">
              {comps.embedding?.dimensions}d · {comps.embedding?.embedded_count} 条
            </p>
          </div>
        </div>
      </Card>

      {/* Active Goals / Recent Tasks + Recent Decisions */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title="最近任务执行" subtitle="点击任务可跳转至完整状态机与执行详情" />
          <div className="divide-y divide-slate-200/70 dark:divide-slate-800/80">
            {(data.recent_tasks || []).length === 0 ? (
              <EmptyState title="暂无任务记录" />
            ) : (
              data.recent_tasks.map((t: any) => (
                <button
                  key={t.task_id}
                  onClick={() => onOpenTask(t.task_id)}
                  className="flex w-full items-start justify-between gap-3 px-5 py-3 text-left hover:bg-slate-50 dark:hover:bg-slate-800/50 transition-colors cursor-pointer"
                >
                  <div className="min-w-0">
                    <div className="text-xs font-medium text-slate-900 dark:text-slate-100 line-clamp-1">
                      {t.objective}
                    </div>
                    <div className="mt-1 flex items-center gap-2 text-[11px] font-mono text-slate-500">
                      <span>{shortId(t.task_id)}</span>
                      <span>· {t.assigned_agent}</span>
                      <span>· {t.permission_level}</span>
                      <span>· {formatTime(t.created_at)}</span>
                    </div>
                  </div>
                  <StatusBadge value={t.status} />
                </button>
              ))
            )}
          </div>
        </Card>

        <Card>
          <CardHeader title="最近决策与系统事件" subtitle="来自 decision_logs 与 event_logs 实时流" />
          <div className="divide-y divide-slate-200/70 dark:divide-slate-800/80 max-h-80 overflow-y-auto">
            {(data.recent_decisions || []).map((d: any) => (
              <div key={d.id} className="px-5 py-2.5">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs font-medium text-slate-800 dark:text-slate-200">
                    {d.decision_type} → <span className="text-cyan-600 dark:text-cyan-400">{d.selected}</span>
                  </span>
                  <span className="text-[10px] font-mono text-slate-400">{formatTime(d.created_at)}</span>
                </div>
                <p className="mt-0.5 text-[11px] text-slate-500 dark:text-slate-400 line-clamp-2">{d.reason}</p>
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}
