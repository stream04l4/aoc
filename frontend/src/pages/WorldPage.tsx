import React, { useEffect, useState } from 'react';
import { Globe, ShieldAlert, Radio } from 'lucide-react';
import { apiRequest, formatTime } from '../api/client';
import {
  Card,
  CardHeader,
  EmptyState,
  ErrorBanner,
  LoadingPanel,
  PaginationBar,
  StatusBadge,
} from '../components/ui';

export function WorldPage({ refreshTick }: { refreshTick: number }) {
  const [data, setData] = useState<any>(null);
  const [offset, setOffset] = useState(0);
  const [sourceId, setSourceId] = useState('');
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const params = new URLSearchParams({ limit: '25', offset: String(offset) });
    if (sourceId) params.set('source_id', sourceId);
    apiRequest(`/v2/control-center/world?${params.toString()}`)
      .then((res) => {
        if (!active) return;
        setData(res);
        setErr(null);
      })
      .catch((e) => active && setErr(e?.detail || e?.message || '加载环境感知数据失败'));
    return () => {
      active = false;
    };
  }, [offset, sourceId, refreshTick]);

  if (!data && !err) return <LoadingPanel />;
  if (err && !data) return <ErrorBanner error={err} />;

  return (
    <div className="space-y-5">
      {/* Sensors Status */}
      <Card>
        <CardHeader
          title="数字环境传感器状态（RSS / Git / Web）"
          subtitle={`poll_on_perceive = ${String(data.poll_on_perceive)} · 重要性入库阈值 = ${data.importance_threshold}`}
          icon={<Radio className="h-4 w-4" />}
        />
        <div className="grid grid-cols-1 gap-3 p-4 md:grid-cols-3">
          {(data.sources || []).map((s: any) => (
            <div key={s.source_id} className="rounded-xl border border-slate-200/80 p-4 space-y-2 dark:border-slate-800">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-semibold truncate">{s.name || s.source_id}</span>
                <StatusBadge value={s.enabled ? 'healthy' : 'IDLE'} label={s.enabled ? '已启用' : '已禁用'} />
              </div>
              <div className="flex items-center gap-2 text-[11px] font-mono text-slate-500">
                <StatusBadge value="L0" label={s.sensor_type.toUpperCase()} />
                <span>周期 {s.poll_interval_seconds}s</span>
                <span>可信度 {s.reliability_score}</span>
              </div>
              <div className="text-[11px] font-mono text-slate-500">
                最近抓取：{formatTime(s.checkpoint?.last_polled_at)} · 连续失败：{s.checkpoint?.consecutive_failures ?? 0}
              </div>
              {s.last_error && (
                <div className="text-[11px] text-rose-500 truncate">错误：{s.last_error}</div>
              )}
            </div>
          ))}
        </div>
      </Card>

      {/* Prompt Injection Quarantine Alert Box */}
      <Card>
        <CardHeader
          title="Prompt Injection 隔离事件"
          subtitle="外部不可信文本中检测到的指令注入尝试均被自动隔离，禁止升格为目标"
          icon={<ShieldAlert className="h-4 w-4" />}
          action={<StatusBadge value={data.quarantined_injections?.length ? 'warning' : 'ok'} label={`${data.quarantined_injections?.length || 0} 条隔离`} />}
        />
        {data.quarantined_injections?.length ? (
          <div className="divide-y divide-slate-200/70 dark:divide-slate-800/80">
            {data.quarantined_injections.map((q: any) => (
              <div key={q.event_id} className="px-4 py-3 text-xs">
                <div className="flex justify-between">
                  <span className="font-semibold text-rose-500">{q.title}</span>
                  <span className="font-mono text-[11px] text-slate-400">{formatTime(q.observed_at)}</span>
                </div>
                <div className="text-[11px] text-slate-500">来源：{q.source_id}</div>
              </div>
            ))}
          </div>
        ) : (
          <div className="px-5 py-4 text-xs text-slate-500">当前无被隔离的外部 Prompt Injection 事件。</div>
        )}
      </Card>

      {/* World Events + World States */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        <Card className="lg:col-span-7">
          <CardHeader
            title="外部 World Events（world_events）"
            subtitle="按 (source_id, content_hash) 去重与可信度评分"
            icon={<Globe className="h-4 w-4" />}
            action={
              <select
                value={sourceId}
                onChange={(e) => {
                  setSourceId(e.target.value);
                  setOffset(0);
                }}
                className="rounded-lg border border-slate-200 bg-white px-2.5 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
              >
                <option value="">全部来源</option>
                {(data.sources || []).map((s: any) => (
                  <option key={s.source_id} value={s.source_id}>{s.source_id}</option>
                ))}
              </select>
            }
          />
          {data.events.items.length === 0 ? (
            <EmptyState title="暂无 World Events" />
          ) : (
            <>
              <div className="divide-y divide-slate-200/70 dark:divide-slate-800/80">
                {data.events.items.map((ev: any) => (
                  <div key={ev.event_id} className="p-4 space-y-1.5">
                    <div className="flex items-start justify-between gap-2">
                      <span className="text-xs font-semibold text-slate-900 dark:text-slate-100">{ev.title}</span>
                      <StatusBadge value={ev.status} />
                    </div>
                    <p className="text-xs text-slate-600 dark:text-slate-300 line-clamp-2">{ev.content}</p>
                    <div className="flex flex-wrap gap-3 text-[11px] font-mono text-slate-400">
                      <span>源：{ev.source_id}</span>
                      <span>可信度：{ev.source_reliability}</span>
                      <span>重要性：{Number(ev.importance_score || 0).toFixed(2)}</span>
                      <span>{formatTime(ev.observed_at)}</span>
                    </div>
                  </div>
                ))}
              </div>
              <PaginationBar
                total={data.events.total}
                limit={data.events.limit}
                offset={data.events.offset}
                onChange={setOffset}
              />
            </>
          )}
        </Card>

        <Card className="lg:col-span-5">
          <CardHeader title="工作环境态（world_states 知识话题）" subtitle="直接驱动 Curiosity 欲望的开放与已解决主题" />
          <div className="divide-y divide-slate-200/70 max-h-[540px] overflow-y-auto dark:divide-slate-800/80">
            {(data.world_states || []).map((w: any) => (
              <div key={w.id} className="flex items-start justify-between gap-3 px-4 py-3">
                <div className="min-w-0">
                  <div className="text-xs font-medium font-mono truncate">{w.key}</div>
                  <div className="text-[11px] text-slate-500">
                    {w.kind} · source={w.source} {w.is_simulated ? '· [模拟]' : ''} · {formatTime(w.updated_at)}
                  </div>
                </div>
                <StatusBadge value={w.status} />
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}
