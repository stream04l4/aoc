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
    </div>
  );
}
