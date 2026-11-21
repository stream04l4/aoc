import React, { useEffect, useState } from 'react';
import { Brain, CheckCircle2, CircleDashed, ArrowUpRight } from 'lucide-react';
import { apiRequest, formatNumber, formatTime, shortId } from '../api/client';
import {
  Button,
  Card,
  CardHeader,
  EmptyState,
  ErrorBanner,
  JsonBlock,
  LoadingPanel,
  PaginationBar,
  StatusBadge,
} from '../components/ui';

const STEP_META: { key: string; num: string; title: string; subtitle: string }[] = [
  { key: '1_perception', num: '01', title: 'Perception（环境与系统感知）', subtitle: 'pre 阶段观测快照' },
  { key: '2_drive_assessment', num: '02', title: 'Drive Assessment（欲望评估）', subtitle: '三大驱动满足度、赤字与维度' },
  { key: '3_arbitration', num: '03', title: 'Arbitration（欲望仲裁）', subtitle: '主导欲望选择与替代备选理由' },
  { key: '4_brain_assessment', num: '04', title: 'Brain Assessment（大脑局势评估）', subtitle: '结构化评估结论（非伪造 CoT）' },
  { key: '5_goal_generation', num: '05', title: 'Goal Generation（目标生成）', subtitle: '目标提案与落库 active_goals' },
  { key: '6_planning', num: '06', title: 'Planning（行动计划）', subtitle: '计划内容、权限申请与 plan_hash' },
  { key: '7_governance', num: '07', title: 'Governance（策略与审批审计）', subtitle: 'L0–L3 判定、规则违规项与审批单' },
  { key: '8_agent_dispatch', num: '08', title: 'Agent Dispatch（路由与派发）', subtitle: '可解释路由评分、任务与 Execution' },
  { key: '9_result_verification', num: '09', title: 'Result Verification（确定性验证）', subtitle: 'ResultVerifier 校验明细与输出摘录' },
  { key: '10_outcome_evaluation', num: '10', title: 'Outcome Evaluation（效果评估与记忆沉淀）', subtitle: '评估结果与新写入记忆' },
  { key: '11_post_perception', num: '11', title: 'Post Perception（行动后重感知）', subtitle: 'post 观测、欲望重算与实测变化' },
];

export function MindPage({
  windowHours,
  refreshTick,
  selectedCycleId,
  onSelectCycle,
  onOpenTask,
}: {
  windowHours: number;
  refreshTick: number;
  selectedCycleId: string | null;
  onSelectCycle: (cycleId: string) => void;
  onOpenTask: (taskId: string) => void;
}) {
  const [listData, setListData] = useState<any>(null);
  const [offset, setOffset] = useState(0);
  const [outcomeFilter, setOutcomeFilter] = useState('');
  const [driveFilter, setDriveFilter] = useState('');
  const [listErr, setListErr] = useState<string | null>(null);

  const [trace, setTrace] = useState<any>(null);
  const [traceLoading, setTraceLoading] = useState(false);
  const [traceErr, setTraceErr] = useState<string | null>(null);
  const [expandedSteps, setExpandedSteps] = useState<Record<string, boolean>>({});

  useEffect(() => {
    let active = true;
    const params = new URLSearchParams({ limit: '20', offset: String(offset) });
    if (windowHours > 0) params.set('hours', String(windowHours));
    if (outcomeFilter) params.set('outcome', outcomeFilter);
    if (driveFilter) params.set('drive', driveFilter);

    apiRequest(`/v2/control-center/cycles?${params.toString()}`)
      .then((res) => {
        if (!active) return;
        setListData(res);
        setListErr(null);
        if (!selectedCycleId && res.items?.length > 0) {
          onSelectCycle(res.items[0].cycle_id);
        }
      })
      .catch((e) => active && setListErr(e?.detail || e?.message || '加载周期列表失败'));
    return () => {
      active = false;
    };
  }, [offset, outcomeFilter, driveFilter, windowHours, refreshTick]);

  useEffect(() => {
    if (!selectedCycleId) return;
    let active = true;
    setTraceLoading(true);
    setTraceErr(null);
    apiRequest(`/v2/control-center/cycles/${selectedCycleId}/trace`)
      .then((res) => active && setTrace(res))
      .catch((e) => active && setTraceErr(e?.detail || e?.message || '加载 Cycle 决策轨迹失败'))
      .finally(() => active && setTraceLoading(false));
    return () => {
      active = false;
    };
  }, [selectedCycleId, refreshTick]);

  const toggleStep = (k: string) =>
    setExpandedSteps((prev) => ({ ...prev, [k]: !prev[k] }));

  return (
    <div className="grid grid-cols-1 gap-5 xl:grid-cols-12">
      {/* Left: Cycle Selector */}
      <Card className="xl:col-span-4 flex flex-col">
        <CardHeader
          title="Cycle 决策时间线"
          subtitle="点击任意周期查看 11 步真实数据库审计轨迹"
          icon={<Brain className="h-4 w-4" />}
        />
        <div className="flex flex-wrap gap-2 border-b border-slate-200/70 p-3 dark:border-slate-800/80">
          <select
            value={outcomeFilter}
            onChange={(e) => {
              setOutcomeFilter(e.target.value);
              setOffset(0);
            }}
            className="flex-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-900"
          >
            <option value="">全部结果</option>
            <option value="SUCCEEDED">SUCCEEDED</option>
            <option value="ACTION_FAILED">ACTION_FAILED</option>
            <option value="IDLE">IDLE</option>
            <option value="POLICY_DENIED">POLICY_DENIED</option>
            <option value="PREFLIGHT_REJECTED">PREFLIGHT_REJECTED</option>
            <option value="GOAL_DEDUP_SKIPPED">GOAL_DEDUP_SKIPPED</option>
            <option value="REQUIRES_APPROVAL">REQUIRES_APPROVAL</option>
            <option value="BUDGET_DEFERRED">BUDGET_DEFERRED</option>
            <option value="BUDGET_EXHAUSTED">BUDGET_EXHAUSTED</option>
            <option value="ERROR">ERROR</option>
          </select>
          <select
            value={driveFilter}
            onChange={(e) => {
              setDriveFilter(e.target.value);
              setOffset(0);
            }}
            className="flex-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-900"
          >
            <option value="">全部欲望</option>
            <option value="survival">survival</option>
            <option value="reproduction">reproduction</option>
            <option value="curiosity">curiosity</option>
          </select>
        </div>

        {listErr && <div className="p-3"><ErrorBanner error={listErr} /></div>}

        <div className="flex-1 divide-y divide-slate-200/70 overflow-y-auto max-h-[680px] dark:divide-slate-800/80">
          {!listData ? (
            <div className="p-6 text-center text-xs text-slate-400">加载周期列表…</div>
          ) : listData.items.length === 0 ? (
            <EmptyState title="该筛选条件下无周期记录" />
          ) : (
            listData.items.map((c: any) => {
              const active = c.cycle_id === selectedCycleId;
              return (
                <button
                  key={c.cycle_id}
                  onClick={() => onSelectCycle(c.cycle_id)}
                  className={`flex w-full flex-col gap-1 px-4 py-3 text-left transition-colors cursor-pointer ${
                    active
                      ? 'bg-cyan-500/10 border-l-2 border-l-cyan-500'
                      : 'hover:bg-slate-50 dark:hover:bg-slate-800/40'
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs font-semibold font-mono text-slate-900 dark:text-slate-100">
                      Cycle #{c.cycle_number}
                    </span>
                    <StatusBadge value={c.outcome || 'RUNNING'} />
                  </div>
                  <div className="text-xs text-slate-600 dark:text-slate-300 line-clamp-1">
                    {c.goal_title || (c.selected_drive ? `主导欲望：${c.selected_drive}` : '无目标提案')}
                  </div>
                  <div className="flex items-center justify-between text-[10px] font-mono text-slate-400">
                    <span>{formatTime(c.started_at)}</span>
                    <span>
                      {c.duration_seconds != null ? `${c.duration_seconds}s` : '—'} · {formatNumber(c.llm_tokens)} tok
                    </span>
                  </div>
                </button>
              );
            })
          )}
        </div>

        {listData && (
          <PaginationBar
            total={listData.total}
            limit={listData.limit}
            offset={listData.offset}
            onChange={setOffset}
          />
        )}
      </Card>

      {/* Right: 11-Step Structured Decision Trace */}
      <div className="xl:col-span-8 space-y-4">
        {traceLoading && !trace && <LoadingPanel label="正在重建 11 步决策审计轨迹…" />}
        {traceErr && <ErrorBanner error={traceErr} />}
        {!trace && !traceLoading && !traceErr && (
          <Card className="p-10">
            <EmptyState title="请从左侧选择一个 Cycle 查看完整认知轨迹" />
          </Card>
        )}

        {trace && (
          <>
            <Card className="p-5">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <div className="flex items-center gap-2.5">
                    <h2 className="text-base font-semibold font-mono">
                      Cycle #{trace.cycle.cycle_number}
                    </h2>
                    <StatusBadge value={trace.cycle.outcome || 'RUNNING'} />
                    <StatusBadge value={trace.cycle.lifecycle_state} />
                  </div>
                  <p className="mt-1 text-xs font-mono text-slate-500">
                    ID: {trace.cycle.cycle_id} · 开始 {formatTime(trace.cycle.started_at)} · 耗时{' '}
                    {trace.cycle.duration_seconds != null ? `${trace.cycle.duration_seconds}s` : '未记录'}
                  </p>
                </div>

                {trace.steps['8_agent_dispatch']?.tasks?.[0]?.task_id && (
                  <Button
                    size="sm"
                    variant="primary"
                    onClick={() => onOpenTask(trace.steps['8_agent_dispatch'].tasks[0].task_id)}
                  >
                    跳转关联任务 {shortId(trace.steps['8_agent_dispatch'].tasks[0].task_id)}{' '}
                    <ArrowUpRight className="h-3.5 w-3.5" />
                  </Button>
                )}
              </div>
              <p className="mt-2.5 rounded-lg bg-slate-100/80 px-3 py-2 text-[11px] text-slate-600 dark:bg-slate-900 dark:text-slate-400">
                诚实审计声明：本页仅展示 PostgreSQL 实际落盘的结构化输入、输出与审计依据，绝不伪造或补写模型隐藏 Chain-of-Thought；未落盘步骤明确标注“未记录”。
              </p>
            </Card>

            <div className="space-y-3">
              {STEP_META.map((step) => {
                const payload = trace.steps?.[step.key];
                const hasData = payload !== null && payload !== undefined;
                const open = expandedSteps[step.key] ?? hasData;

                return (
                  <Card key={step.key} className="overflow-hidden">
                    <button
                      onClick={() => toggleStep(step.key)}
                      className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left hover:bg-slate-50/70 dark:hover:bg-slate-800/40 cursor-pointer"
                    >
                      <div className="flex items-center gap-3 min-w-0">
                        <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-slate-100 text-[11px] font-mono font-semibold text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                          {step.num}
                        </span>
                        <div className="min-w-0">
                          <div className="text-xs font-semibold text-slate-900 dark:text-slate-100">
                            {step.title}
                          </div>
                          <div className="text-[11px] text-slate-500 dark:text-slate-400">
                            {step.subtitle}
                          </div>
                        </div>
                      </div>
                      <div className="flex items-center gap-2 shrink-0">
                        {hasData ? (
                          <span className="inline-flex items-center gap-1 text-[11px] text-emerald-600 dark:text-emerald-400">
                            <CheckCircle2 className="h-3.5 w-3.5" /> 已记录
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1 text-[11px] text-slate-400">
                            <CircleDashed className="h-3.5 w-3.5" /> 未记录
                          </span>
                        )}
                      </div>
                    </button>

                    {open && (
                      <div className="border-t border-slate-200/70 px-4 py-3 dark:border-slate-800/80">
                        {!hasData ? (
                          <p className="text-xs italic text-slate-400">
                            该步骤在此 Cycle 中未执行或数据库中未记录（例如 IDLE / POLICY_DENIED / 跨周期阶段）。
                          </p>
                        ) : (
                          <JsonBlock data={payload} maxHeight="max-h-80" />
                        )}
                      </div>
                    )}
                  </Card>
                );
              })}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
