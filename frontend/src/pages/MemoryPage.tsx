import React, { useEffect, useState } from 'react';
import { Database, Search, Sparkles } from 'lucide-react';
import { apiRequest, formatTime } from '../api/client';
import {
  Button,
  Card,
  CardHeader,
  EmptyState,
  ErrorBanner,
  LoadingPanel,
  PaginationBar,
  StatCard,
  StatusBadge,
} from '../components/ui';

export function MemoryPage({ refreshTick }: { refreshTick: number }) {
  const [data, setData] = useState<any>(null);
  const [offset, setOffset] = useState(0);
  const [memoryType, setMemoryType] = useState('');
  const [syncStatus, setSyncStatus] = useState('');
  const [keyword, setKeyword] = useState('');
  const [err, setErr] = useState<string | null>(null);

  // Live Hybrid Semantic Search state
  const [semQuery, setSemQuery] = useState('');
  const [semHits, setSemHits] = useState<any[] | null>(null);
  const [semLoading, setSemLoading] = useState(false);

  useEffect(() => {
    let active = true;
    const params = new URLSearchParams({ limit: '20', offset: String(offset) });
    if (memoryType) params.set('memory_type', memoryType);
    if (syncStatus) params.set('sync_status', syncStatus);
    if (keyword.trim()) params.set('search', keyword.trim());

    apiRequest(`/v2/control-center/memory?${params.toString()}`)
      .then((res) => {
        if (!active) return;
        setData(res);
        setErr(null);
      })
      .catch((e) => active && setErr(e?.detail || e?.message || '加载记忆中心失败'));
    return () => {
      active = false;
    };
  }, [offset, memoryType, syncStatus, keyword, refreshTick]);

  const runSemanticSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!semQuery.trim()) return;
    setSemLoading(true);
    try {
      const res = await apiRequest('/v2/memories/search', {
        method: 'POST',
        body: { query: semQuery.trim(), limit: 8 },
      });
      setSemHits(res.results || res.items || (Array.isArray(res) ? res : []));
    } catch (e: any) {
      setErr(e?.detail || e?.message || '语义检索失败');
    } finally {
      setSemLoading(false);
    }
  };

  if (!data && !err) return <LoadingPanel />;
  if (err && !data) return <ErrorBanner error={err} />;

  const sum = data.summary || {};
  const activeModel = (sum.models || []).find((m: any) => m.is_active) || sum.models?.[0];

  return (
    <div className="space-y-5">
      {err && <ErrorBanner error={err} />}

      {/* Summary KPIs */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="记忆总量 (memory_index)"
          value={data.total}
          subtext={`Episodic ${sum.by_type?.episodic || 0} · Semantic ${sum.by_type?.semantic || 0} · Procedural ${sum.by_type?.procedural || 0} · Identity ${sum.by_type?.identity || 0}`}
          icon={<Database className="h-4 w-4" />}
        />
        <StatCard
          label="真实神经 Embedding"
          value={activeModel ? `${activeModel.dimensions}d` : '—'}
          subtext={activeModel ? `${activeModel.model_key} (${activeModel.provider})` : '未注册'}
          tone="cyan"
        />
        <StatCard
          label="向量存储与扩展"
          value={sum.pgvector_active ? 'pgvector' : sum.embedding_column_type}
          subtext={`已嵌入 ${sum.by_embedding_status?.embedded || 0} · 待嵌入 ${sum.by_embedding_status?.pending || 0}`}
          tone={sum.pgvector_active ? 'emerald' : 'amber'}
        />
        <StatCard
          label="Letta 异步同步"
          value={`${sum.by_sync_status?.synced || 0} synced`}
          subtext={`pending ${sum.by_sync_status?.pending || 0} · failed ${sum.by_sync_status?.failed || 0} · Letta ${sum.letta_available ? '在线' : '离线'}`}
          tone={sum.letta_available ? 'emerald' : 'rose'}
        />
      </div>

      {/* Live Semantic Hybrid Search Panel */}
      <Card>
        <CardHeader
          title="实时混合语义检索（HybridRetrievalEngine）"
          subtitle="向量余弦 + pg_trgm 双语文本 + 时效 + 可靠度 + 重要度五维打分（调用 /v2/memories/search）"
          icon={<Sparkles className="h-4 w-4" />}
        />
        <div className="p-4 space-y-3">
          <form onSubmit={runSemanticSearch} className="flex gap-2">
            <input
              value={semQuery}
              onChange={(e) => setSemQuery(e.target.value)}
              placeholder="输入中文或英文查询，如：pgvector 语义检索 / p1-8 approval security…"
              className="flex-1 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs dark:border-slate-700 dark:bg-slate-950"
            />
            <Button type="submit" variant="primary" disabled={semLoading}>
              <Search className="h-3.5 w-3.5" /> {semLoading ? '检索中…' : '语义检索'}
            </Button>
          </form>

          {semHits && (
            <div className="divide-y divide-slate-200/70 rounded-lg border border-slate-200/80 dark:divide-slate-800 dark:border-slate-800">
              {semHits.length === 0 ? (
                <div className="p-4 text-xs text-slate-400">未命中相关记忆。</div>
              ) : (
                semHits.map((h: any, idx: number) => {
                  const item = h.item || h;
                  return (
                    <div key={item.memory_id || idx} className="p-3 space-y-1">
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-xs font-semibold">{item.title}</span>
                        <div className="flex items-center gap-2">
                          <StatusBadge value="L1" label={item.memory_type} />
                          {h.total_score != null && (
                            <span className="font-mono text-[11px] text-cyan-500">
                              score={Number(h.total_score).toFixed(3)}
                            </span>
                          )}
                        </div>
                      </div>
                      <p className="text-xs text-slate-600 dark:text-slate-300 line-clamp-2">{item.content || item.summary}</p>
                    </div>
                  );
                })
              )}
            </div>
          )}
        </div>
      </Card>

      {/* Memory Index Paginated List */}
      <Card>
        <CardHeader
          title="长期记忆索引（只读保护：禁止删除或篡改 Identity 记忆）"
          subtitle="PostgreSQL 权威事实源"
          action={
            <div className="flex flex-wrap gap-2">
              <input
                value={keyword}
                onChange={(e) => {
                  setKeyword(e.target.value);
                  setOffset(0);
                }}
                placeholder="标题/摘要过滤…"
                className="rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-900"
              />
              <select
                value={memoryType}
                onChange={(e) => {
                  setMemoryType(e.target.value);
                  setOffset(0);
                }}
                className="rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-900"
              >
                <option value="">全部分类</option>
                <option value="episodic">episodic</option>
                <option value="semantic">semantic</option>
                <option value="procedural">procedural</option>
                <option value="identity">identity</option>
              </select>
              <select
                value={syncStatus}
                onChange={(e) => {
                  setSyncStatus(e.target.value);
                  setOffset(0);
                }}
                className="rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-900"
              >
                <option value="">全部同步态</option>
                <option value="synced">synced</option>
                <option value="pending">pending</option>
                <option value="failed">failed</option>
              </select>
            </div>
          }
        />

        <div className="divide-y divide-slate-200/70 dark:divide-slate-800/80">
          {(data.items || []).map((m: any) => (
            <div key={m.memory_id} className="p-4 space-y-1.5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2 min-w-0">
                  <StatusBadge value="L1" label={m.memory_type} />
                  <span className="text-xs font-semibold truncate">{m.title}</span>
                </div>
                <div className="flex items-center gap-1.5 shrink-0">
                  <StatusBadge value={m.sync_status} label={`Letta:${m.sync_status}`} />
                  <StatusBadge value={m.embedding_status} label={`Emb:${m.embedding_status}`} />
                </div>
              </div>
              <p className="text-xs text-slate-600 dark:text-slate-300 line-clamp-3">{m.summary}</p>
              <div className="flex flex-wrap gap-3 text-[11px] font-mono text-slate-400">
                <span>重要度 {Number(m.importance).toFixed(2)}</span>
                <span>可靠度 {Number(m.reliability).toFixed(2)}</span>
                <span>信息增益 {m.information_gain != null ? Number(m.information_gain).toFixed(3) : '未记录'}</span>
                <span>来源 {m.source}</span>
                <span>{formatTime(m.created_at)}</span>
              </div>
            </div>
          ))}
        </div>
        <PaginationBar total={data.total} limit={data.limit} offset={data.offset} onChange={setOffset} />
      </Card>

      {/* Outbox & Self Model Versions */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title="Outbox 异步投递队列状态" subtitle="PostgreSQL Transactional Outbox" />
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-slate-200/70 text-[11px] text-slate-500 dark:border-slate-800">
                <tr>
                  <th className="px-4 py-2">事件类型</th>
                  <th className="px-4 py-2">状态</th>
                  <th className="px-4 py-2">数量</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-200/70 dark:divide-slate-800/80">
                {(sum.outbox || []).map((o: any, i: number) => (
                  <tr key={i}>
                    <td className="px-4 py-2 font-mono">{o.event_type}</td>
                    <td className="px-4 py-2"><StatusBadge value={o.status} /></td>
                    <td className="px-4 py-2 font-mono tabular-nums">{o.count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>

        <Card>
          <CardHeader title="Self Model 历史版本投影" subtitle="同步至 Letta self_model Core Block" />
          <div className="divide-y divide-slate-200/70 max-h-64 overflow-y-auto dark:divide-slate-800/80">
            {(data.self_model_versions || []).map((v: any) => (
              <div key={v.version_id} className="flex items-center justify-between px-4 py-2.5 text-xs">
                <span className="font-mono font-medium">v{v.version_number}</span>
                <span className="text-[11px] text-slate-500 truncate max-w-xs">{v.summary_text}</span>
                <StatusBadge value={v.letta_synced_at ? 'synced' : 'pending'} />
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}
