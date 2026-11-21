import React, { useEffect, useMemo, useState } from 'react';
import { Flame, ArrowUpRight } from 'lucide-react';
import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  Legend,
} from 'recharts';
import { apiRequest, formatPct, formatTime } from '../api/client';
import {
  Button,
  Card,
  CardHeader,
  EmptyState,
  ErrorBanner,
  LoadingPanel,
  StatusBadge,
} from '../components/ui';

export function DrivesPage({
  windowHours,
  refreshTick,
  onOpenCycle,
}: {
  windowHours: number;
  refreshTick: number;
  onOpenCycle: (cycleId: string) => void;
}) {
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setLoading(true);
    apiRequest(`/v2/control-center/drives?hours=${windowHours}`)
      .then((res) => {
        if (!active) return;
        setData(res);
        setErr(null);
      })
      .catch((e) => active && setErr(e?.detail || e?.message || '加载欲望数据失败'))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [windowHours, refreshTick]);

  const series = useMemo(() => {
    if (!data?.history?.length) return [];
    const byTime: Record<string, any> = {};
    for (const h of data.history) {
      const k = `${h.timestamp?.slice(0, 19)}_${h.phase}`;
      if (!byTime[k]) {
        byTime[k] = { label: `${formatTime(h.timestamp).slice(6)} (${h.phase})` };
      }
      byTime[k][h.drive_type] = Number((h.satisfaction * 100).toFixed(1));
    }
    return Object.values(byTime).slice(-80);
  }, [data]);

  const affectSeries = useMemo(() => {
    const hist = data?.affect?.history || [];
    return hist.slice(-60).map((h: any) => ({
      label: formatTime(h.timestamp).slice(6),
      valence: Number((Number(h.valence || 0) * 100).toFixed(1)),
      arousal: Number((Number(h.arousal || 0) * 100).toFixed(1)),
      confidence: Number((Number(h.confidence || 0) * 100).toFixed(1)),
      frustration: Number((Number(h.frustration || 0) * 100).toFixed(1)),
      boredom: Number((Number(h.boredom || 0) * 100).toFixed(1)),
    }));
  }, [data]);

  if (loading && !data) return <LoadingPanel />;
  if (err && !data) return <ErrorBanner error={err} />;
  if (!data) return <EmptyState title="暂无欲望状态数据" />;

  return (
    <div className="space-y-5">
      {/* Three independent drive cards */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        {(data.current || []).map((d: any) => (
          <Card key={d.drive_type} className="flex flex-col">
            <CardHeader
              title={`${d.drive_type.toUpperCase()} 欲望评估器`}
              subtitle={`独立确定性评估 · 优先级 ${Number(d.priority || 0).toFixed(3)}`}
              icon={<Flame className="h-4 w-4" />}
              action={
                <span className="text-lg font-semibold tabular-nums text-cyan-600 dark:text-cyan-400">
                  {formatPct(d.satisfaction)}
                </span>
              }
            />
            <div className="p-4 space-y-3 flex-1">
              <div className="space-y-2">
                <div className="text-[11px] font-medium text-slate-500">各维度组成（真实测量）</div>
                {Object.entries(d.dimensions || {}).map(([dim, val]: [string, any]) => {
                  const score = typeof val === 'number' ? val : Number(val?.score ?? 0);
                  return (
                    <div key={dim} className="space-y-1">
                      <div className="flex justify-between text-xs">
                        <span className="font-mono text-slate-600 dark:text-slate-300">{dim}</span>
                        <span className="tabular-nums font-medium">{formatPct(score)}</span>
                      </div>
                      <div className="h-1.5 rounded-full bg-slate-200 dark:bg-slate-800 overflow-hidden">
                        <div
                          className="h-full bg-cyan-500"
                          style={{ width: `${Math.min(100, Math.max(0, score * 100))}%` }}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>

              {d.top_deficits?.length > 0 && (
                <div className="rounded-lg bg-amber-500/10 border border-amber-500/20 p-2.5 text-[11px] text-amber-700 dark:text-amber-300">
                  <div className="font-semibold mb-1">主要赤字因素</div>
                  <ul className="list-disc pl-4 space-y-0.5">
                    {d.top_deficits.map((def: string, idx: number) => (
                      <li key={idx}>{def}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          </Card>
        ))}
      </div>

      {/* V3.0 M2 Affect Kernel Panel (fused into Drives page, zero extra menus) */}
      {data.affect && (
        <Card>
          <CardHeader
            title="情绪内核状态与演化曲线（AffectModulator · 有界调制）"
            subtitle={`受控调制幅度硬上限 ±${Number(data.affect.maximum_influence || 0.15).toFixed(2)} · 永不触碰 L0–L3 Governance 与 Survival 紧急保护`}
            action={<StatusBadge value={data.affect.enabled ? 'ENABLED' : 'DISABLED'} />}
          />
          <div className="p-4 space-y-4">
            {data.affect.current_state ? (
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
                {[
                  { k: 'valence', label: 'Valence（正负效价）', range: '[-1, 1]' },
                  { k: 'arousal', label: 'Arousal（激活度）', range: '[0, 1]' },
                  { k: 'confidence', label: 'Confidence（置信度）', range: '[0, 1]' },
                  { k: 'frustration', label: 'Frustration（挫折度）', range: '[0, 1]' },
                  { k: 'boredom', label: 'Boredom（厌倦度）', range: '[0, 1]' },
                ].map((dim) => (
                  <div key={dim.k} className="rounded-lg bg-slate-100/70 p-2.5 dark:bg-slate-900/70">
                    <div className="text-[11px] text-slate-500">{dim.label}</div>
                    <div className="mt-1 text-base font-semibold font-mono text-cyan-600 dark:text-cyan-400">
                      {Number(data.affect.current_state[dim.k] ?? 0).toFixed(3)}
                    </div>
                    <div className="text-[10px] font-mono text-slate-400">{dim.range}</div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-xs text-slate-500 dark:text-slate-400">
                当前情绪内核开关处于关闭状态（Feature Flag = false），完全按 V2.1 原始确定性公式运行；开启 AOC_KERNEL_V3__AFFECT_ENABLED=true 后将实时记录 5 维情绪状态与调制增量。
              </div>
            )}

            {data.affect.enabled && data.affect.current_modulation && (
              <div className="space-y-2">
                <div className="flex flex-wrap gap-3 text-[11px] font-mono text-slate-500">
                  <span>Curiosity Δ: {Number(data.affect.current_modulation.curiosity_priority_delta || 0).toFixed(4)}</span>
                  <span>·</span>
                  <span>Reproduction Δ: {Number(data.affect.current_modulation.reproduction_priority_delta || 0).toFixed(4)}</span>
                  <span>·</span>
                  <span>Retry Backoff ×{Number(data.affect.current_modulation.retry_backoff_multiplier || 1).toFixed(3)}</span>
                  <span>·</span>
                  <span>Reflection ×{Number(data.affect.current_modulation.reflection_interval_factor || 1).toFixed(3)}</span>
                </div>
                <div className="flex flex-wrap gap-1.5 text-[10px]">
                  {data.affect.brain_read_only_injected && (
                    <StatusBadge value="ok" label="大脑只读感知已接入" />
                  )}
                  {data.affect.confidence_evidence_anchored && (
                    <StatusBadge value="ok" label="Confidence 已锚定能力证据" />
                  )}
                  {data.affect.runtime_hooks?.retry_backoff_active && (
                    <StatusBadge value="ok" label="退避与节律已接线" />
                  )}
                </div>
              </div>
            )}

            {affectSeries.length > 0 && (
              <div className="h-64">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={affectSeries}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.15)" />
                    <XAxis dataKey="label" tick={{ fontSize: 10, fill: '#94a3b8' }} minTickGap={24} />
                    <YAxis domain={[-100, 100]} tick={{ fontSize: 10, fill: '#94a3b8' }} />
                    <Tooltip
                      contentStyle={{
                        backgroundColor: '#0f172a',
                        border: '1px solid #334155',
                        borderRadius: 8,
                        fontSize: 12,
                      }}
                    />
                    <Legend wrapperStyle={{ fontSize: 11 }} />
                    <Line type="monotone" dataKey="valence" name="Valence" stroke="#10b981" strokeWidth={2} dot={false} />
                    <Line type="monotone" dataKey="arousal" name="Arousal" stroke="#f59e0b" strokeWidth={1.8} dot={false} />
                    <Line type="monotone" dataKey="confidence" name="Confidence" stroke="#06b6d4" strokeWidth={1.8} dot={false} />
                    <Line type="monotone" dataKey="frustration" name="Frustration" stroke="#ef4444" strokeWidth={2} dot={false} />
                    <Line type="monotone" dataKey="boredom" name="Boredom" stroke="#a855f7" strokeWidth={1.8} dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            )}
          </div>
        </Card>
      )}

      {/* Historical curves */}
      <Card>
        <CardHeader
          title="三大欲望历史满足度曲线 (%)"
          subtitle={`统计口径：drive_history pre/post 实测采样（时间窗：${windowHours === 0 ? '全部' : `近 ${windowHours}h`}）`}
        />
        <div className="h-80 p-4">
          {series.length > 0 ? (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={series}>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.15)" />
                <XAxis dataKey="label" tick={{ fontSize: 10, fill: '#94a3b8' }} minTickGap={28} />
                <YAxis domain={[0, 100]} tick={{ fontSize: 10, fill: '#94a3b8' }} />
                <Tooltip
                  contentStyle={{
                    backgroundColor: '#0f172a',
                    border: '1px solid #334155',
                    borderRadius: 8,
                    fontSize: 12,
                  }}
                />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                <Line type="monotone" dataKey="survival" name="Survival" stroke="#10b981" strokeWidth={2} dot={false} />
                <Line type="monotone" dataKey="reproduction" name="Reproduction" stroke="#8b5cf6" strokeWidth={2} dot={false} />
                <Line type="monotone" dataKey="curiosity" name="Curiosity" stroke="#06b6d4" strokeWidth={2} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          ) : (
            <EmptyState title="暂无历史曲线数据" />
          )}
        </div>
      </Card>

      {/* Predicted vs Measured & Arbitration History */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="预测改善 vs 实测满足度变化"
            subtitle="严格分离大脑预期 expected_improvement 与 post 重感知实测值"
          />
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-slate-200/70 text-[11px] text-slate-500 dark:border-slate-800">
                <tr>
                  <th className="px-4 py-2.5">Cycle</th>
                  <th className="px-4 py-2.5">主导欲望</th>
                  <th className="px-4 py-2.5">大脑预测</th>
                  <th className="px-4 py-2.5">实测变化</th>
                  <th className="px-4 py-2.5">结果</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-200/70 dark:divide-slate-800/80">
                {(data.predicted_vs_measured || []).slice(0, 15).map((row: any) => (
                  <tr key={row.cycle_id} className="hover:bg-slate-50 dark:hover:bg-slate-800/40">
                    <td className="px-4 py-2 font-mono">
                      <button
                        onClick={() => onOpenCycle(row.cycle_id)}
                        className="text-cyan-600 hover:underline dark:text-cyan-400 cursor-pointer"
                      >
                        #{row.cycle_number}
                      </button>
                    </td>
                    <td className="px-4 py-2 font-mono">{row.selected_drive || '—'}</td>
                    <td className="px-4 py-2 font-mono">
                      {row.expected_improvement != null ? `+${Number(row.expected_improvement).toFixed(3)}` : '未记录'}
                    </td>
                    <td className="px-4 py-2 font-mono">
                      {row.measured_change != null
                        ? `${row.measured_change >= 0 ? '+' : ''}${Number(row.measured_change).toFixed(4)}`
                        : '未记录'}
                    </td>
                    <td className="px-4 py-2">
                      <StatusBadge value={row.outcome} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>

        <Card>
          <CardHeader title="欲望仲裁历史（decision_logs）" subtitle="记录每次仲裁胜出欲望、备选方案与裁决原因" />
          <div className="divide-y divide-slate-200/70 max-h-96 overflow-y-auto dark:divide-slate-800/80">
            {(data.arbitration_history || []).slice(0, 20).map((a: any) => (
              <div key={a.id} className="px-4 py-3 space-y-1">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs font-semibold text-cyan-600 dark:text-cyan-400">
                    胜出：{a.selected}
                  </span>
                  <div className="flex items-center gap-2">
                    <span className="text-[10px] font-mono text-slate-400">{formatTime(a.created_at)}</span>
                    {a.cycle_id && (
                      <Button size="xs" variant="ghost" onClick={() => onOpenCycle(a.cycle_id)}>
                        <ArrowUpRight className="h-3 w-3" />
                      </Button>
                    )}
                  </div>
                </div>
                <p className="text-[11px] text-slate-600 dark:text-slate-300">{a.reason}</p>
              </div>
            ))}
          </div>
        </Card>
      </div>

      {/* V3.1 Expected IG vs Verified IG Prediction-Error Calibration */}
      {data.ig_calibration && (
        <Card>
          <CardHeader
            title="预期 IG vs 实测 IG 校准（Prediction Error · V3.1）"
            subtitle="实测误差（verified_ig - predicted_ig）作为惊喜/落差信号回灌至情绪内核"
          />
          {data.ig_calibration.length === 0 ? (
            <div className="px-5 py-4 text-xs text-slate-500">当前时间窗内尚无带预期/实测 IG 记录的目标。</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="border-b border-slate-200/70 text-[11px] text-slate-500 dark:border-slate-800">
                  <tr>
                    <th className="px-4 py-2.5">目标标题</th>
                    <th className="px-4 py-2.5">欲望</th>
                    <th className="px-4 py-2.5">预期 IG</th>
                    <th className="px-4 py-2.5">实测 IG</th>
                    <th className="px-4 py-2.5">误差 (Surprise)</th>
                    <th className="px-4 py-2.5">状态</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-200/70 dark:divide-slate-800/80">
                  {data.ig_calibration.slice(0, 15).map((g: any) => {
                    const errVal = g.prediction_error;
                    const errColor =
                      errVal == null
                        ? 'text-slate-400'
                        : errVal > 0.05
                          ? 'text-emerald-600 dark:text-emerald-400'
                          : errVal < -0.15
                            ? 'text-rose-500'
                            : 'text-slate-600 dark:text-slate-300';
                    return (
                      <tr key={g.goal_id} className="hover:bg-slate-50 dark:hover:bg-slate-800/40">
                        <td className="px-4 py-2 max-w-xs truncate">
                          {g.cycle_id ? (
                            <button
                              onClick={() => onOpenCycle(g.cycle_id)}
                              className="text-left text-cyan-600 hover:underline dark:text-cyan-400 cursor-pointer truncate max-w-xs block"
                              title={g.title}
                            >
                              {g.title}
                            </button>
                          ) : (
                            <span title={g.title}>{g.title}</span>
                          )}
                        </td>
                        <td className="px-4 py-2 font-mono">{g.drive_type}</td>
                        <td className="px-4 py-2 font-mono">
                          {g.predicted_ig != null ? Number(g.predicted_ig).toFixed(3) : '—'}
                        </td>
                        <td className="px-4 py-2 font-mono">
                          {g.verified_ig != null ? Number(g.verified_ig).toFixed(3) : '—'}
                        </td>
                        <td className={`px-4 py-2 font-mono font-medium ${errColor}`}>
                          {errVal != null ? `${errVal >= 0 ? '+' : ''}${Number(errVal).toFixed(3)}` : '—'}
                        </td>
                        <td className="px-4 py-2">
                          <StatusBadge value={g.status} />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
