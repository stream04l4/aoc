import React, { useCallback, useEffect, useState } from 'react';
import {
  Activity,
  Check,
  Flame,
  GitBranch,
  Lock,
  Rocket,
  RotateCcw,
  ShieldCheck,
  Sparkles,
  X,
} from 'lucide-react';
import {
  apiRequest,
  formatPct,
  formatTime,
  hasScope,
  PrincipalInfo,
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

const ENTROPY_LABELS: Record<string, string> = {
  skill_deprecation_ratio: '技能废弃率（DEPRECATED / 总数）',
  skill_non_reuse_ratio: '已验证未复用僵尸技能比',
  hypothesis_backlog_ratio: '悬而未决假设积压比',
  playbook_bloat_score: '外置 Playbook 膨胀度（/60）',
  goal_waste_ratio: '近 50 周期无效目标损耗比',
  ig_token_efficiency_drop: '认知能效（IG/1k Tok）下滑幅度',
};

export function EvolutionPage({
  refreshTick,
  principal,
}: {
  refreshTick: number;
  principal: PrincipalInfo;
}) {
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [confirmRollback, setConfirmRollback] = useState(false);

  const load = useCallback(() => {
    apiRequest('/v2/control-center/evolution')
      .then((res) => {
        setData(res);
        setErr(null);
      })
      .catch((e) => setErr(e?.detail || e?.message || '加载演化谱系数据失败'));
  }, []);

  useEffect(() => {
    load();
  }, [load, refreshTick]);

  const canApprove = hasScope(principal, 'approve');
  const canControl = hasScope(principal, 'control');

  const runProposalAction = async (
    proposalKey: string,
    endpointSuffix: 'approve' | 'reject' | 'arena' | 'promote',
    label: string
  ) => {
    setBusyKey(`${proposalKey}:${endpointSuffix}`);
    setActionMsg(null);
    try {
      const hasBody = endpointSuffix !== 'arena';
      await apiRequest(`/v5/evolution/proposals/${proposalKey}/${endpointSuffix}`, {
        method: 'POST',
        body: hasBody ? { reason: `Control Center ${label} by ${principal.name}` } : undefined,
      });
      setActionMsg(`提案 ${proposalKey} 已执行：${label}`);
      load();
    } catch (e: any) {
      setActionMsg(`操作失败：${e?.detail || e?.message}`);
    } finally {
      setBusyKey(null);
    }
  };

  const handleRollback = async () => {
    setBusyKey('rollback');
    setActionMsg(null);
    try {
      const res = await apiRequest('/v5/evolution/rollback', {
        method: 'POST',
        body: { reason: `Control Center rollback by ${principal.name}` },
      });
      setActionMsg(`已成功回滚至 Gen ${res.generation}（${res.new_primary_key}）`);
      setConfirmRollback(false);
      load();
    } catch (e: any) {
      setActionMsg(`回滚失败：${e?.detail || e?.message}`);
    } finally {
      setBusyKey(null);
    }
  };

  if (!data && !err) return <LoadingPanel />;
  if (err && !data) return <ErrorBanner error={err} onRetry={load} />;

  const summary = data.summary || {};
  const entropy = data.entropy || {};
  const entropyMetrics: Record<string, number> = entropy.metrics || {};
  const constitution = data.constitution || {};
  const instances: any[] = data.instances || [];
  const proposals: any[] = data.proposals || [];
  const lineage: any[] = data.lineage || [];

  const driveEvolvable: string[] = constitution.drive_evolvable_files || [];
  const constCount = constitution.constitutional_file_count ?? Object.keys(constitution.constitutional_files || {}).length;

  return (
    <div className="space-y-5">
      {/* KPI Strip */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="当前主代世代"
          value={`Gen ${summary.active_generation ?? 0}`}
          subtext={`活跃实例：${summary.active_instance_key || 'gen0-001'} · 并发 ${summary.live_instances ?? 1}/${summary.max_concurrent_instances ?? 2}`}
          tone="cyan"
          icon={<GitBranch className="h-4 w-4" />}
        />
        <StatCard
          label="运行期综合熵增指数"
          value={entropy.entropy_index != null ? formatPct(entropy.entropy_index) : '—'}
          subtext="越低越轻盈；过高时建议白板繁衍迭代"
          tone={(entropy.entropy_index ?? 0) >= 0.55 ? 'amber' : 'emerald'}
          icon={<Activity className="h-4 w-4" />}
        />
        <StatCard
          label="进化优胜 / 淘汰墓志铭"
          value={`${summary.promoted_count ?? 0} / ${summary.culled_count ?? 0}`}
          subtext={`已回滚 ${summary.rolled_back_count ?? 0} 次 · 退化自动零残留 GC`}
          tone="default"
          icon={<Sparkles className="h-4 w-4" />}
        />
        <StatCard
          label="极小宪法 vs. 欲望指标进化域"
          value={`${constCount} 锁定 / ${driveEvolvable.length} 欲望可演化`}
          subtext="三元欲望与生存红线锁定，评估指标可跨代演化"
          tone="emerald"
          icon={<Lock className="h-4 w-4" />}
        />
      </div>

      {actionMsg && (
        <div className="rounded-xl border border-cyan-500/30 bg-cyan-500/10 px-4 py-2.5 text-xs text-cyan-800 dark:text-cyan-200">
          {actionMsg}
        </div>
      )}

      {/* Entropy 6D Health & Constitutional Boundary */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="运行期客观熵增 6 维感知（EntropySensor）"
            subtitle="非 LLM 捏造，直接注入 DriveContext 供可进化欲望指标评估"
            icon={<Activity className="h-4 w-4" />}
          />
          <div className="p-4 space-y-2.5">
            {Object.entries(ENTROPY_LABELS).map(([k, label]) => {
              const val = Number(entropyMetrics[k] ?? 0);
              const pct = Math.max(0, Math.min(100, val * 100));
              const barColor =
                val >= 0.7 ? 'bg-rose-500' : val >= 0.4 ? 'bg-amber-500' : 'bg-emerald-500';
              return (
                <div key={k} className="space-y-1">
                  <div className="flex items-center justify-between text-xs">
                    <span className="text-slate-600 dark:text-slate-300">{label}</span>
                    <span className="font-mono font-medium tabular-nums">{formatPct(val)}</span>
                  </div>
                  <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-100 dark:bg-slate-800">
                    <div className={`h-full ${barColor}`} style={{ width: `${pct}%` }} />
                  </div>
                </div>
              );
            })}
          </div>
        </Card>

        <Card>
          <CardHeader
            title="极小化不可变宪法 vs. 欲望指标演化边界"
            subtitle="三元本能（survival / reproduction / curiosity）不可删改，评估公式与权重开放跨代进化"
            icon={<ShieldCheck className="h-4 w-4" />}
          />
          <div className="p-4 space-y-3 text-xs">
            <div>
              <div className="font-medium text-slate-700 dark:text-slate-200 mb-1.5 flex items-center gap-1.5">
                <Lock className="h-3.5 w-3.5 text-rose-500" /> 不可变宪法核心（SHA-256 + AST 锁定 {constCount} 项）
              </div>
              <div className="text-[11px] leading-relaxed text-slate-500 dark:text-slate-400">
                L0–L3 风控边界、SSRF / 提示注入隔离、人类急停权、白板出生验证、适应度仲裁与{' '}
                <code className="font-mono text-cyan-600 dark:text-cyan-400">instinct_guard.py</code>{' '}
                （强制三欲望齐备、<code>[0,1]</code> 闭合与生存危机必接管）。
              </div>
            </div>
            <div>
              <div className="font-medium text-slate-700 dark:text-slate-200 mb-1.5 flex items-center gap-1.5">
                <Flame className="h-3.5 w-3.5 text-amber-500" /> 开放演化的欲望评估模块（{driveEvolvable.length} 项）
              </div>
              <div className="flex flex-wrap gap-1.5">
                {driveEvolvable.map((f) => (
                  <span
                    key={f}
                    className="rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 font-mono text-[11px] text-amber-700 dark:text-amber-300"
                  >
                    {f}
                  </span>
                ))}
              </div>
            </div>
            <div className="pt-1 border-t border-slate-200/70 dark:border-slate-800/80 text-[11px] text-slate-500">
              个体记忆、Self-Model、Affect、旧 Playbook 与 <code className="font-mono">personalitySeed</code> 均严格不继承，子代每代从纯净白板诞生。
            </div>
          </div>
        </Card>
      </div>

      {/* Proposals Funnel Table */}
      <Card>
        <CardHeader
          title="四级演化漏斗与提案审批"
          subtitle="Stage 0 提案 → Stage 1 宪法筛查（0 Token）→ Stage 2 人工审批 → Stage 3 沙箱 A/B → Stage 4 二次确认晋升"
          icon={<Rocket className="h-4 w-4" />}
        />
        {proposals.length === 0 ? (
          <EmptyState
            title="暂无演化提案"
            description="当系统熵增压力升高或由人类发起新假设时，四级演化提案将在此展示。"
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-slate-200/70 text-[11px] text-slate-500 dark:border-slate-800">
                <tr>
                  <th className="px-4 py-2.5">提案编号 / 代数</th>
                  <th className="px-4 py-2.5">状态阶段</th>
                  <th className="px-4 py-2.5">演化假设 &amp; 变异域</th>
                  <th className="px-4 py-2.5">A/B 适应度</th>
                  <th className="px-4 py-2.5">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-200/60 dark:divide-slate-800/70">
                {proposals.map((p) => {
                  const delta = p.genome_delta || {};
                  const cmp = (p.arena_result || {}).comparison;
                  const mutatesDrives = Boolean(delta.mutates_drive_metrics);
                  const files: string[] = delta.files || Object.keys(delta.file_overrides || {});
                  return (
                    <tr key={p.id} className="hover:bg-slate-50/70 dark:hover:bg-slate-800/40">
                      <td className="px-4 py-2.5">
                        <div className="font-mono font-medium">{p.proposal_key}</div>
                        <div className="text-[11px] text-slate-500">
                          Gen {p.target_generation} · {p.trigger_type}
                        </div>
                      </td>
                      <td className="px-4 py-2.5">
                        <StatusBadge value={p.stage} />
                      </td>
                      <td className="px-4 py-2.5 max-w-md">
                        <div className="flex flex-wrap items-center gap-1.5 mb-1">
                          {mutatesDrives && (
                            <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:text-amber-300">
                              欲望评估指标进化
                            </span>
                          )}
                          <span className="text-[11px] font-mono text-slate-500 truncate">
                            {files.join(', ') || '—'}
                          </span>
                        </div>
                        <p className="text-slate-700 dark:text-slate-300 line-clamp-2">{p.hypothesis}</p>
                      </td>
                      <td className="px-4 py-2.5 font-mono tabular-nums">
                        {cmp ? (
                          <span className={cmp.fitness_delta >= 5 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-500'}>
                            {cmp.fitness_delta >= 0 ? `+${cmp.fitness_delta}` : cmp.fitness_delta} 分
                          </span>
                        ) : (
                          '—'
                        )}
                      </td>
                      <td className="px-4 py-2.5">
                        <div className="flex flex-wrap gap-1.5">
                          {p.stage === 'STAGE2_PENDING_APPROVAL' && canApprove && (
                            <>
                              <Button
                                size="xs"
                                variant="primary"
                                disabled={busyKey !== null}
                                onClick={() => runProposalAction(p.proposal_key, 'approve', '批准进入沙箱')}
                              >
                                <Check className="h-3 w-3" /> 同意孵化
                              </Button>
                              <Button
                                size="xs"
                                variant="danger"
                                disabled={busyKey !== null}
                                onClick={() => runProposalAction(p.proposal_key, 'reject', '拒绝提案')}
                              >
                                <X className="h-3 w-3" /> 拒绝
                              </Button>
                            </>
                          )}
                          {p.stage === 'STAGE2_APPROVED' && canControl && (
                            <Button
                              size="xs"
                              variant="secondary"
                              disabled={busyKey !== null}
                              onClick={() => runProposalAction(p.proposal_key, 'arena', '启动沙箱 A/B 竞技')}
                            >
                              启动沙箱 A/B
                            </Button>
                          )}
                          {p.stage === 'STAGE4_PENDING_PROMOTION' && canApprove && (
                            <Button
                              size="xs"
                              variant="primary"
                              disabled={busyKey !== null}
                              onClick={() => runProposalAction(p.proposal_key, 'promote', '确认晋升新一代')}
                            >
                              <Rocket className="h-3 w-3" /> 确认晋升新一代
                            </Button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* Instance Registry & One-Click Rollback */}
      <Card>
        <CardHeader
          title="物种世代实例注册表（隔离 Schema 与一键回滚）"
          subtitle="淘汰实例已自动 DROP SCHEMA CASCADE 与清理 Worktree；已退役亲代保留回滚快照"
          icon={<GitBranch className="h-4 w-4" />}
          action={
            canApprove && summary.can_rollback ? (
              confirmRollback ? (
                <div className="flex items-center gap-2">
                  <Button
                    size="xs"
                    variant="danger"
                    disabled={busyKey !== null}
                    onClick={handleRollback}
                  >
                    确认回滚至上一代
                  </Button>
                  <Button
                    size="xs"
                    variant="ghost"
                    disabled={busyKey !== null}
                    onClick={() => setConfirmRollback(false)}
                  >
                    取消
                  </Button>
                </div>
              ) : (
                <Button size="xs" variant="secondary" onClick={() => setConfirmRollback(true)}>
                  <RotateCcw className="h-3 w-3" /> 一键回滚上一代
                </Button>
              )
            ) : undefined
          }
        />
        {instances.length === 0 ? (
          <EmptyState title="暂无实例记录" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-slate-200/70 text-[11px] text-slate-500 dark:border-slate-800">
                <tr>
                  <th className="px-4 py-2.5">实例 Key / 代数</th>
                  <th className="px-4 py-2.5">角色 / 状态</th>
                  <th className="px-4 py-2.5">隔离 DB Schema</th>
                  <th className="px-4 py-2.5">白板验证</th>
                  <th className="px-4 py-2.5">创建时间</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-200/60 dark:divide-slate-800/70">
                {instances.map((inst) => (
                  <tr key={inst.id} className="hover:bg-slate-50/70 dark:hover:bg-slate-800/40">
                    <td className="px-4 py-2.5 font-mono">
                      <span className="font-medium">{inst.instance_key}</span>
                      <span className="ml-2 text-slate-400">Gen {inst.generation}</span>
                    </td>
                    <td className="px-4 py-2.5">
                      <div className="flex items-center gap-1.5">
                        <StatusBadge value={inst.status} />
                        <span className="text-[11px] font-mono text-slate-500">{inst.role}</span>
                      </div>
                    </td>
                    <td className="px-4 py-2.5 font-mono text-slate-600 dark:text-slate-300">
                      {inst.db_schema}
                    </td>
                    <td className="px-4 py-2.5">
                      {inst.blank_slate_verified ? '✅ 纯净白板' : '未核验'}
                    </td>
                    <td className="px-4 py-2.5 text-slate-500">{formatTime(inst.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* Lineage Records (<5KB Tombstones & Promotions) */}
      <Card>
        <CardHeader
          title="种族演化家谱 &amp; 淘汰教训记录（< 5 KB 结构化精简库）"
          subtitle="仅记录科学假设、适应度差值与反模式标签，绝不堆砌历史情景记忆"
          icon={<Sparkles className="h-4 w-4" />}
        />
        {lineage.length === 0 ? (
          <EmptyState title="暂无谱系记录" description="完成沙箱淘汰或世代晋升后将自动记录家谱条目。" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-slate-200/70 text-[11px] text-slate-500 dark:border-slate-800">
                <tr>
                  <th className="px-4 py-2.5">代数 / 结论</th>
                  <th className="px-4 py-2.5">适应度对比</th>
                  <th className="px-4 py-2.5">欲望指标变更</th>
                  <th className="px-4 py-2.5">精简经验总结（&lt; 5 KB）</th>
                  <th className="px-4 py-2.5">时间</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-200/60 dark:divide-slate-800/70">
                {lineage.map((row) => {
                  const lesson = row.lesson_summary || {};
                  const dmc = row.drive_metric_changes || {};
                  return (
                    <tr key={row.id} className="hover:bg-slate-50/70 dark:hover:bg-slate-800/40">
                      <td className="px-4 py-2.5">
                        <div className="flex items-center gap-1.5">
                          <span className="font-mono font-medium">Gen {row.generation}</span>
                          <StatusBadge value={row.outcome} />
                        </div>
                      </td>
                      <td className="px-4 py-2.5 font-mono tabular-nums">
                        {row.parent_fitness ?? '—'} → {row.offspring_fitness ?? '—'}
                        {row.fitness_delta != null && (
                          <span className="ml-1.5 text-slate-500">
                            ({row.fitness_delta >= 0 ? `+${row.fitness_delta}` : row.fitness_delta})
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-2.5">
                        {dmc.mutates_drive_metrics ? '✅ 已进化欲望指标' : '仅机制/参数'}
                      </td>
                      <td className="px-4 py-2.5 max-w-md">
                        <div className="text-slate-700 dark:text-slate-300 truncate">
                          {lesson.outcome_reason || lesson.hypothesis || '—'}
                        </div>
                      </td>
                      <td className="px-4 py-2.5 text-slate-500">{formatTime(row.created_at)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
