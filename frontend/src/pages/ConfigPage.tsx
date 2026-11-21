import React, { useEffect, useState } from 'react';
import { SlidersHorizontal, Lock, AlertTriangle, RotateCw, Search } from 'lucide-react';
import { apiRequest, formatTime } from '../api/client';
import {
  Card,
  CardHeader,
  ErrorBanner,
  LoadingPanel,
  StatusBadge,
} from '../components/ui';

function formatCfgValue(val: unknown): string {
  if (val === null || val === undefined) return '未设置 (null)';
  if (typeof val === 'boolean') return val ? 'true' : 'false';
  if (typeof val === 'object') return JSON.stringify(val);
  return String(val);
}

export function ConfigPage({ refreshTick }: { refreshTick: number }) {
  const [data, setData] = useState<any>(null);
  const [search, setSearch] = useState('');
  const [sourceFilter, setSourceFilter] = useState('');
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    apiRequest('/v2/control-center/config')
      .then((res) => {
        if (!active) return;
        setData(res);
        setErr(null);
      })
      .catch((e) => active && setErr(e?.detail || e?.message || '加载生效配置失败'));
    return () => {
      active = false;
    };
  }, [refreshTick]);

  if (!data && !err) return <LoadingPanel />;
  if (err && !data) return <ErrorBanner error={err} />;

  const q = search.trim().toLowerCase();

  return (
    <div className="space-y-5">
      {/* Read-Only & Security Banner */}
      <Card className="p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <Lock className="h-4 w-4 text-cyan-500" />
            <div>
              <div className="text-xs font-semibold">
                只读配置中心（12 大运行配置域 · 敏感凭证已强制脱敏）
              </div>
              <p className="text-[11px] text-slate-500 dark:text-slate-400">
                实时反射当前生效值与覆盖来源（env &gt; .env &gt; yaml &gt; default）· 生成时间：{formatTime(data.generated_at)}
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-2 h-3.5 w-3.5 text-slate-400" />
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="搜索配置键 / 说明…"
                className="rounded-lg border border-slate-200 bg-white py-1.5 pl-8 pr-3 text-xs dark:border-slate-700 dark:bg-slate-900"
              />
            </div>
            <select
              value={sourceFilter}
              onChange={(e) => setSourceFilter(e.target.value)}
              className="rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-900"
            >
              <option value="">全部来源</option>
              <option value="env">env (环境变量)</option>
              <option value=".env">.env</option>
              <option value="yaml">yaml</option>
              <option value="default">default</option>
            </select>
          </div>
        </div>
      </Card>

      {/* 12 Configuration Domain Cards */}
      <div className="space-y-4">
        {(data.domains || []).map((dom: any) => {
          const items = (dom.items || []).filter((it: any) => {
            if (sourceFilter && it.source !== sourceFilter) return false;
            if (!q) return true;
            return (
              it.key.toLowerCase().includes(q) ||
              it.label.toLowerCase().includes(q) ||
              it.description.toLowerCase().includes(q)
            );
          });
          if (items.length === 0) return null;

          return (
            <Card key={dom.id}>
              <CardHeader
                title={dom.name}
                subtitle={dom.description}
                icon={<SlidersHorizontal className="h-4 w-4" />}
                action={<span className="font-mono text-[11px] text-slate-400">{items.length} 项</span>}
              />
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead className="border-b border-slate-200/70 text-[11px] text-slate-500 dark:border-slate-800">
                    <tr>
                      <th className="px-4 py-2.5 w-64">配置项与说明</th>
                      <th className="px-4 py-2.5">当前生效值</th>
                      <th className="px-4 py-2.5 w-28">来源</th>
                      <th className="px-4 py-2.5 w-44">属性标记</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-200/70 dark:divide-slate-800/80">
                    {items.map((it: any) => (
                      <tr key={it.key} className="hover:bg-slate-50/70 dark:hover:bg-slate-800/40">
                        <td className="px-4 py-3 align-top">
                          <div className="font-medium text-slate-900 dark:text-slate-100">{it.label}</div>
                          <div className="font-mono text-[11px] text-cyan-600 dark:text-cyan-400">{it.key}</div>
                          <div className="mt-0.5 text-[11px] text-slate-500">{it.description}</div>
                        </td>
                        <td className="px-4 py-3 align-top">
                          <code className="break-all rounded bg-slate-100 px-2 py-1 text-[11px] text-slate-800 dark:bg-slate-950 dark:text-slate-200 inline-block max-w-md">
                            {formatCfgValue(it.effective_value)}
                          </code>
                          {it.risk_note && (
                            <div className="mt-1.5 flex items-center gap-1.5 text-[11px] text-amber-600 dark:text-amber-300">
                              <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                              <span>{it.risk_note}</span>
                            </div>
                          )}
                        </td>
                        <td className="px-4 py-3 align-top">
                          <StatusBadge value={it.source} />
                          {it.overridden && (
                            <div className="mt-1 text-[10px] text-violet-500 font-medium">已覆盖默认/低优先级</div>
                          )}
                        </td>
                        <td className="px-4 py-3 align-top">
                          <div className="flex flex-wrap gap-1.5">
                            {it.sensitive && (
                              <span className="inline-flex items-center gap-1 rounded bg-rose-500/15 px-1.5 py-0.5 text-[10px] text-rose-500">
                                <Lock className="h-2.5 w-2.5" /> 敏感脱敏
                              </span>
                            )}
                            {it.requires_restart && (
                              <span className="inline-flex items-center gap-1 rounded bg-slate-500/15 px-1.5 py-0.5 text-[10px] text-slate-500 dark:text-slate-400">
                                <RotateCw className="h-2.5 w-2.5" /> 需重启生效
                              </span>
                            )}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
