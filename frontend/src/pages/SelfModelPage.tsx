import React, { useEffect, useState } from 'react';
import { Fingerprint, Award, ShieldCheck, GitBranch, BookOpen, Zap, RotateCcw } from 'lucide-react';
import { apiRequest, formatNumber, formatPct, formatTime, shortId } from '../api/client';
import {
  Card,
  CardHeader,
  EmptyState,
  ErrorBanner,
  LoadingPanel,
  StatCard,
  StatusBadge,
} from '../components/ui';

export function SelfModelPage({ refreshTick }: { refreshTick: number }) {
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    apiRequest('/v2/control-center/self-model')
      .then((res) => {
        if (!active) return;
        setData(res);
        setErr(null);
      })
      .catch((e) => active && setErr(e?.detail || e?.message || '加载自我认知数据失败'));
    return () => {
      active = false;
    };
  }, [refreshTick]);

  if (!data && !err) return <LoadingPanel />;
  if (err && !data) return <ErrorBanner error={err} />;

  const id = data.identity;
  const snap = data.latest_snapshot;
  const reuse = data.skill_reuse_summary || {};
  const trees: any[] = data.skill_version_trees || [];
  const playbooks: any[] = data.external_playbooks || [];
  const recentInvs: any[] = reuse.recent_invocations || [];

  return (
    <div className="space-y-5">
      {/* Identity & Latest Self Model Snapshot */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card className="p-5 space-y-2">
          <div className="flex items-center gap-2 text-cyan-600 dark:text-cyan-400">
            <Fingerprint className="h-4 w-4" />
            <span className="text-xs font-semibold uppercase tracking-wider">Identity（不可变核心身份）</span>
          </div>
          <div className="text-lg font-semibold">{id?.name || '—'}</div>
          <div className="text-xs font-mono text-slate-500">ID: {id?.id || '—'}</div>
          <div className="text-xs font-mono text-slate-500">Version: {id?.version || '—'} · 创建：{formatTime(id?.created_at)}</div>
          <div className="text-xs font-mono text-slate-500 truncate">Letta Agent: {id?.memory_agent_id || '未绑定'}</div>
        </Card>

        <Card className="p-5 lg:col-span-2 space-y-2">
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs font-semibold">最新 Self Model 快照（v{snap?.version_number ?? '—'}）</span>
            <StatusBadge
              value={snap?.letta_synced_at ? 'synced' : 'pending'}
              label={snap?.letta_synced_at ? `Letta 已同步 ${formatTime(snap.letta_synced_at)}` : '待同步'}
            />
          </div>
          <p className="text-xs leading-relaxed text-slate-600 dark:text-slate-300 whitespace-pre-wrap">
            {snap?.summary_text || '未记录'}
          </p>
        </Card>
      </div>

      {/* V4.0 RSI Skill Reuse & Token Savings KPI Strip */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="跨任务真实复用次数"
          value={formatNumber(reuse.cross_task_reuses || 0)}
          subtext={`总调用 ${formatNumber(reuse.total_invocations || 0)} 次 · 已复用技能 ${reuse.reused_skill_count || 0} 个`}
          tone="cyan"
          icon={<Zap className="h-4 w-4" />}
        />
        <StatCard
          label="跨任务复用成功率"
          value={reuse.reuse_success_rate != null ? formatPct(reuse.reuse_success_rate) : '—'}
          subtext={`成功 ${reuse.cross_task_successes || 0} / ${reuse.cross_task_reuses || 0}（严格排除同任务自调用）`}
          tone={(reuse.reuse_success_rate ?? 1) >= 0.85 ? 'emerald' : 'amber'}
          icon={<ShieldCheck className="h-4 w-4" />}
        />
        <StatCard
          label="Skill-First 累计节省 Token"
          value={formatNumber(reuse.estimated_saved_tokens || 0)}
          subtext="通过隔离 SkillRuntime 直接执行跳过 Codex 重写估算"
          tone="emerald"
          icon={<Award className="h-4 w-4" />}
        />
        <StatCard
          label="规范技能家族 / 外置 Playbook"
          value={`${reuse.canonical_families_count ?? trees.length} / ${playbooks.length}`}
          subtext="规范命名 <domain>.<capability>.<op> · 内核外自主迭代"
          tone="default"
          icon={<GitBranch className="h-4 w-4" />}
        />
      </div>

      {/* Capability 5-State Matrix + Beta-Binomial Posteriors */}
      <Card>
        <CardHeader
          title="能力成熟度画像（Beta-Binomial 实证后验）"
          subtitle="严格隔离外部 Agent 静态声明（AVAILABLE）与 AOC 实测验证能力（ACQUIRED / VALIDATED）"
          icon={<Award className="h-4 w-4" />}
        />
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="border-b border-slate-200/70 text-[11px] text-slate-500 dark:border-slate-800">
              <tr>
                <th className="px-4 py-2.5">能力键 (capability_key)</th>
                <th className="px-4 py-2.5">成熟度五态</th>
                <th className="px-4 py-2.5">成功 / 失败样本</th>
                <th className="px-4 py-2.5">Beta(α, β)</th>
                <th className="px-4 py-2.5">估计成功率 p̂</th>
                <th className="px-4 py-2.5">90% 置信区间</th>
                <th className="px-4 py-2.5">更新时间</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-200/70 dark:divide-slate-800/80">
              {(data.capabilities || []).map((c: any) => (
                <tr key={c.id} className="hover:bg-slate-50 dark:hover:bg-slate-800/40">
                  <td className="px-4 py-2.5 font-mono font-medium">{c.capability_key}</td>
                  <td className="px-4 py-2.5"><StatusBadge value={c.maturity_status} /></td>
                  <td className="px-4 py-2.5 font-mono tabular-nums">
                    <span className="text-emerald-600 dark:text-emerald-400">{c.success_samples}</span> /{' '}
                    <span className="text-rose-500">{c.failure_samples}</span> (n={c.sample_count})
                  </td>
                  <td className="px-4 py-2.5 font-mono text-slate-500">({c.alpha}, {c.beta})</td>
                  <td className="px-4 py-2.5 font-mono font-semibold tabular-nums">{formatPct(c.estimated_success_rate)}</td>
                  <td className="px-4 py-2.5 w-48">
                    <div className="flex items-center gap-2">
                      <div className="relative h-2 flex-1 rounded-full bg-slate-200 dark:bg-slate-800">
                        <div
                          className="absolute top-0 h-2 rounded-full bg-cyan-500/70"
                          style={{
                            left: `${c.confidence_low * 100}%`,
                            width: `${Math.max(2, (c.confidence_high - c.confidence_low) * 100)}%`,
                          }}
                        />
                      </div>
                      <span className="text-[10px] font-mono text-slate-400 tabular-nums">
                        [{(c.confidence_low * 100).toFixed(0)}–{(c.confidence_high * 100).toFixed(0)}%]
                      </span>
                    </div>
                  </td>
                  <td className="px-4 py-2.5 font-mono text-[11px] text-slate-500">{formatTime(c.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      {/* V4.0 Skill Version Evolution Trees & External Playbook Evolution */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        <Card className="lg:col-span-7">
          <CardHeader
            title="RSI 技能版本演化树与跨任务复用（Skill Version Tree · V4.0）"
            subtitle="按 canonical_name 聚合 v1 → vNext 演进血缘、回归验收与真实跨任务复用统计"
            icon={<GitBranch className="h-4 w-4" />}
          />
          {trees.length === 0 ? (
            <EmptyState
              title="当前尚无已注册技能（0 条）"
              description="当 AOC 在隔离工作区完成可验证脚本结晶后，将自动规范命名并展示版本演进树。"
            />
          ) : (
            <div className="divide-y divide-slate-200/70 max-h-[420px] overflow-y-auto dark:divide-slate-800/80">
              {trees.map((tree: any) => (
                <div key={tree.canonical_name} className="p-4 space-y-2">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="text-xs font-semibold font-mono text-cyan-700 dark:text-cyan-300 truncate">
                        {tree.canonical_name}
                      </span>
                      <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-mono text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                        v{tree.active_version} · {tree.version_count} 版本
                      </span>
                    </div>
                    <span className="text-[11px] font-mono text-emerald-600 dark:text-emerald-400">
                      跨任务复用 {tree.total_reuses} 次
                    </span>
                  </div>
                  <div className="space-y-1.5 pl-3 border-l-2 border-cyan-500/30">
                    {(tree.versions || []).map((v: any) => (
                      <div key={v.id} className="flex flex-wrap items-center justify-between gap-2 text-[11px] font-mono">
                        <div className="flex items-center gap-2 min-w-0">
                          <span className="font-semibold text-slate-800 dark:text-slate-200">v{v.version}</span>
                          {v.parent_version && (
                            <span className="text-[10px] text-slate-400">← v{v.parent_version}</span>
                          )}
                          <StatusBadge value={v.status} />
                          <span className="text-[10px] text-slate-400">{v.permission_level}</span>
                        </div>
                        <div className="flex items-center gap-2 text-[10px] text-slate-500">
                          <span>调用 {v.invocation_count}</span>
                          <span>复用 {v.reuse_success_count}/{v.reuse_count}</span>
                          {v.evolution_meta?.rolled_back_from && (
                            <span className="text-amber-500">已回滚自 v{v.evolution_meta.rolled_back_from}</span>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card className="lg:col-span-5">
          <CardHeader
            title="外置认知策略手册自进化（External Playbooks · V4.0）"
            subtitle="内核外版本化经验手册，SHA-256 校验 + 实证成功率退化自动回滚"
            icon={<BookOpen className="h-4 w-4" />}
            action={<StatusBadge value="ok" label={`${playbooks.length} 领域`} />}
          />
          {playbooks.length === 0 ? (
            <div className="px-5 py-6 text-xs text-slate-500">
              当前暂无外置策略手册；当任务经确定性验收沉淀出可复用策略（playbook_update）时将自动版本化保存并追踪表现。
            </div>
          ) : (
            <div className="divide-y divide-slate-200/70 max-h-[420px] overflow-y-auto dark:divide-slate-800/80">
              {playbooks.map((pb: any) => (
                <div key={pb.id} className="p-4 space-y-2">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs font-semibold font-mono text-violet-700 dark:text-violet-300">
                      {pb.domain} · v{pb.active_version}
                    </span>
                    <div className="flex items-center gap-1.5">
                      {pb.rollback_count > 0 && (
                        <span className="inline-flex items-center gap-1 rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-mono text-amber-600 dark:text-amber-400">
                          <RotateCcw className="h-3 w-3" /> 回滚 {pb.rollback_count}
                        </span>
                      )}
                      <StatusBadge value={pb.status || 'active'} />
                    </div>
                  </div>
                  {pb.change_summary && (
                    <p className="text-xs text-slate-600 dark:text-slate-300">{pb.change_summary}</p>
                  )}
                  <div className="flex flex-wrap gap-3 text-[10px] font-mono text-slate-400">
                    <span>版本数：{pb.version_count}</span>
                    <span>实证成功：{pb.active_successes}/{pb.active_samples}</span>
                    <span>更新：{formatTime(pb.updated_at)}</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>

      {/* Recent Skill Invocations Audit + Recent Capability Evidence */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="最近技能真实调用审计（skill_invocations）"
            subtitle="记录 Skill-First / MCP 直接调用的跨任务复用标记、延迟与节省 Token"
            icon={<ShieldCheck className="h-4 w-4" />}
          />
          {recentInvs.length === 0 ? (
            <div className="px-5 py-6 text-xs text-slate-500">
              暂无 Skill Runtime 调用记录；当规划器命中已有 VALIDATED 技能或通过 MCP 调用时将实时审计。
            </div>
          ) : (
            <div className="divide-y divide-slate-200/70 max-h-80 overflow-y-auto dark:divide-slate-800/80">
              {recentInvs.map((inv: any) => (
                <div key={inv.id} className="flex items-center justify-between gap-2 px-4 py-2.5 text-[11px] font-mono">
                  <div className="min-w-0">
                    <div className="font-semibold text-slate-800 dark:text-slate-200 truncate">
                      {inv.skill_name}@v{inv.skill_version}
                    </div>
                    <div className="text-[10px] text-slate-400">
                      via {inv.invocation_source} · {inv.latency_ms}ms · 省 {formatNumber(inv.estimated_saved_tokens)} Tok
                    </div>
                  </div>
                  <div className="flex items-center gap-1.5 shrink-0">
                    {inv.is_cross_task_reuse && (
                      <span className="rounded bg-cyan-500/15 px-1.5 py-0.5 text-[10px] text-cyan-600 dark:text-cyan-400">
                        跨任务复用
                      </span>
                    )}
                    <StatusBadge value={inv.passed ? 'SUCCEEDED' : 'FAILED'} />
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card>
          <CardHeader title="最近能力验证证据账本（capability_evidence）" subtitle="按 (profile_id, execution_id) 唯一去重，100% 可追溯" />
          <div className="divide-y divide-slate-200/70 max-h-80 overflow-y-auto dark:divide-slate-800/80">
            {(data.recent_evidence || []).slice(0, 20).map((ev: any) => (
              <div key={ev.id} className="flex items-start justify-between gap-2 px-4 py-2.5">
                <div className="min-w-0">
                  <div className="text-xs font-mono font-medium">{ev.capability_key}</div>
                  <div className="text-[11px] text-slate-500 truncate">{ev.verification_summary || '—'}</div>
                </div>
                <div className="flex flex-col items-end gap-1 shrink-0">
                  <StatusBadge value={ev.outcome} />
                  <span className="text-[10px] font-mono text-slate-400">{formatTime(ev.created_at)}</span>
                </div>
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}
