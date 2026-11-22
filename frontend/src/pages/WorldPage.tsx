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
          subtitle={`poll_on_perceive = ${String(data.poll_on_perceive)} · 重要性入库阈值 = ${data.importance_threshold} · 已放行域名 ${(data.allowed_domains || []).length} 个`}
          icon={<Radio className="h-4 w-4" />}
          action={
            <StatusBadge
              value={data.open_public_perception_enabled ? 'ENABLED' : 'IDLE'}
              label={data.open_public_perception_enabled ? '公开 HTTPS 感知扩展 · 强 SSRF 防护' : '严格域名白名单'}
            />
          }
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

      {/* V3.0 M3 External MCP ToolProvider Card (fused into World page, 0 new menus) */}
      {data.mcp_tools && (
        <Card>
          <CardHeader
            title="外接 MCP 只读工具口与调用审计（ToolProvider）"
            subtitle={`主循环接入：${data.mcp_tools.in_loop_enabled ? '已启用' : '已关闭'} · 单周期上限 ${data.mcp_tools.max_calls_per_cycle} 次 · 累计治理审计 ${data.mcp_tools.policy_evals_count} 次`}
            action={<StatusBadge value={data.mcp_tools.in_loop_enabled ? 'ENABLED' : 'DISABLED'} />}
          />
          <div className="p-4 space-y-3">
            <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
              {(data.mcp_tools.registered || []).map((t: any) => (
                <div key={t.tool_name} className="rounded-xl border border-slate-200/80 p-3 space-y-1.5 dark:border-slate-800">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs font-semibold font-mono truncate">{t.tool_name}</span>
                    <StatusBadge value={t.permission_level || 'L0'} />
                  </div>
                  <div className="text-[11px] font-mono text-slate-400">{t.server_name}</div>
                  <p className="text-[11px] text-slate-600 dark:text-slate-300 line-clamp-2">{t.description}</p>
                </div>
              ))}
            </div>
            {(data.mcp_tools.recent_invocations || []).length > 0 && (
              <div className="divide-y divide-slate-200/70 rounded-lg border border-slate-200/70 dark:divide-slate-800 dark:border-slate-800">
                {data.mcp_tools.recent_invocations.slice(0, 8).map((inv: any) => (
                  <div key={inv.id} className="flex items-center justify-between gap-2 px-3 py-2 text-[11px] font-mono">
                    <span className="truncate">{inv.tool_name} ({inv.server_name})</span>
                    <div className="flex items-center gap-2 shrink-0">
                      <span className="text-slate-400">{inv.seconds}s</span>
                      <StatusBadge value={inv.ok ? 'SUCCEEDED' : 'FAILED'} />
                      <span className="text-slate-400">{formatTime(inv.created_at)}</span>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </Card>
      )}

      {/* V4.0 Curiosity External Exploration Topics (anti-internal-loop multi-domain tracking) */}
      {data.exploration && (
        <Card>
          <CardHeader
            title="好奇心外部探索主题追踪（Exploration Topics · V4.0）"
            subtitle={`复用 digital_world_state(kind=exploration_topic)，限制内部自查死循环并推动跨领域外部探索 · 已验证 ${data.exploration.verified_count || 0} / 总计 ${data.exploration.total || 0}`}
            icon={<Globe className="h-4 w-4" />}
            action={
              <div className="flex flex-wrap gap-1.5">
                {Object.entries(data.exploration.domains_summary || {}).map(([dom, cnt]) => (
                  <span
                    key={dom}
                    className="rounded-full bg-cyan-500/10 px-2 py-0.5 text-[10px] font-mono text-cyan-700 dark:text-cyan-300"
                  >
                    {dom}: {String(cnt)}
                  </span>
                ))}
              </div>
            }
          />
          {(data.exploration.topics || []).length === 0 ? (
            <div className="px-5 py-4 text-xs text-slate-500">
              当前暂无外部探索主题；好奇心驱动向外探索时将自动记录主题领域、验证事实与信息增益。
            </div>
          ) : (
            <div className="divide-y divide-slate-200/70 dark:divide-slate-800/80">
              {data.exploration.topics.slice(0, 15).map((t: any) => (
                <div key={t.id} className="px-4 py-3 space-y-1">
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="text-xs font-semibold font-mono truncate">{t.key}</span>
                      <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-mono text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                        {t.domain}
                      </span>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      {t.information_gain != null && (
                        <span className="text-[11px] font-mono text-emerald-600 dark:text-emerald-400">
                          IG {Number(t.information_gain).toFixed(2)}
                        </span>
                      )}
                      <StatusBadge value={t.status || 'exploring'} />
                    </div>
                  </div>
                  {t.question && <p className="text-xs text-slate-600 dark:text-slate-300">{t.question}</p>}
                  {(t.verified_facts || []).length > 0 && (
                    <p className="text-[11px] text-cyan-600 dark:text-cyan-400">
                      已验证事实：{t.verified_facts[t.verified_facts.length - 1]}
                    </p>
                  )}
                  <div className="text-[10px] font-mono text-slate-400">
                    尝试 {t.attempts ?? 0} 次 · 来源：{t.source} · 更新：{formatTime(t.updated_at)}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
      )}

      {/* V3.1 Lightweight World Hypotheses (Strict separation of unverified hypotheses vs verified facts) */}
      {Array.isArray(data.hypotheses) && (
        <Card>
          <CardHeader
            title="轻量世界假设追踪（World Hypotheses · V3.1）"
            subtitle="复用 digital_world_state(kind=hypothesis)，严格区分未验证假设与经确定性验证的观察事实（0 新增重型图谱表）"
            action={<StatusBadge value="ok" label={`${data.hypotheses.length} 条假设`} />}
          />
          {data.hypotheses.length === 0 ? (
            <div className="px-5 py-4 text-xs text-slate-500">
              当前暂无待验证或已验证的世界假设；当大脑在探索中提出 hypothesis_to_test 时将自动记录并由确定性验收更新结论。
            </div>
          ) : (
            <div className="divide-y divide-slate-200/70 dark:divide-slate-800/80">
              {data.hypotheses.slice(0, 15).map((h: any) => (
                <div key={h.id} className="px-4 py-3 space-y-1">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs font-semibold font-mono text-slate-900 dark:text-slate-100 truncate">
                      {h.key}
                    </span>
                    <div className="flex items-center gap-2 shrink-0">
                      <span className="text-[11px] font-mono text-slate-400">
                        置信度 {Number(h.confidence ?? 0.5).toFixed(2)} · 证据 {h.evidence_count ?? 0}
                      </span>
                      <StatusBadge value={h.status || 'unverified'} />
                    </div>
                  </div>
                  {h.claim && (
                    <p className="text-xs text-slate-600 dark:text-slate-300">{h.claim}</p>
                  )}
                  {h.last_verdict_summary && (
                    <p className="text-[11px] text-cyan-600 dark:text-cyan-400">
                      最近验证：{h.last_verdict_summary}
                    </p>
                  )}
                  <div className="text-[10px] font-mono text-slate-400">
                    来源：{h.source} · 更新：{formatTime(h.updated_at)}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
      )}

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
            {(data.world_states || [])
              .filter((w: any) => !['hypothesis', 'exploration_topic', 'external_playbook'].includes(w.kind))
              .map((w: any) => (
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
