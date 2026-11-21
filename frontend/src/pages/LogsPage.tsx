import React, { useEffect, useState } from 'react';
import { ScrollText, Search, ArrowUpRight } from 'lucide-react';
import { apiRequest, formatTime, shortId } from '../api/client';
import {
  Button,
  Card,
  CardHeader,
  Drawer,
  EmptyState,
  ErrorBanner,
  JsonBlock,
  LoadingPanel,
  PaginationBar,
  StatusBadge,
} from '../components/ui';

export function LogsPage({
  windowHours,
  refreshTick,
  onOpenCycle,
  onOpenTask,
}: {
  windowHours: number;
  refreshTick: number;
  onOpenCycle: (cycleId: string) => void;
  onOpenTask: (taskId: string) => void;
}) {
  const [data, setData] = useState<any>(null);
  const [kind, setKind] = useState('all');
  const [level, setLevel] = useState('');
  const [search, setSearch] = useState('');
  const [offset, setOffset] = useState(0);
  const [err, setErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<any>(null);

  useEffect(() => {
    let active = true;
    const params = new URLSearchParams({
      kind,
      limit: '40',
      offset: String(offset),
      hours: String(windowHours),
    });
    if (level) params.set('level', level);
    if (search.trim()) params.set('search', search.trim());

    apiRequest(`/v2/control-center/logs?${params.toString()}`)
      .then((res) => {
        if (!active) return;
        setData(res);
        setErr(null);
      })
      .catch((e) => active && setErr(e?.detail || e?.message || '加载日志失败'));
    return () => {
      active = false;
    };
  }, [kind, level, search, offset, windowHours, refreshTick]);

  return (
    <div className="space-y-4">
      {err && <ErrorBanner error={err} />}

      <Card>
        <CardHeader
          title="统一运行日志与审计事件流"
          subtitle="聚合 Cycle / Decision / Event / Governance Policy / Recovery 记录，支持多维筛选与关联跳转"
          icon={<ScrollText className="h-4 w-4" />}
          action={
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative">
                <Search className="pointer-events-none absolute left-2.5 top-2 h-3.5 w-3.5 text-slate-400" />
                <input
                  value={search}
                  onChange={(e) => {
                    setSearch(e.target.value);
                    setOffset(0);
                  }}
                  placeholder="搜索关键词…"
                  className="rounded-lg border border-slate-200 bg-white py-1.5 pl-8 pr-3 text-xs dark:border-slate-700 dark:bg-slate-900"
                />
              </div>
              <select
                value={kind}
                onChange={(e) => {
                  setKind(e.target.value);
                  setOffset(0);
                }}
                className="rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-900"
              >
                <option value="all">全部类别</option>
                <option value="cycles">Cycle 日志</option>
                <option value="decisions">Decision Logs</option>
                <option value="events">Event Logs</option>
                <option value="policy">Governance 审计</option>
                <option value="recovery">异常与恢复记录</option>
              </select>
              <select
                value={level}
                onChange={(e) => {
                  setLevel(e.target.value);
                  setOffset(0);
                }}
                className="rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-900"
              >
                <option value="">全部级别</option>
                <option value="info">info</option>
                <option value="warning">warning</option>
                <option value="error">error</option>
              </select>
            </div>
          }
        />

        {!data ? (
          <LoadingPanel />
        ) : data.items.length === 0 ? (
          <EmptyState title="当前筛选条件下无日志记录" />
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="border-b border-slate-200/70 text-[11px] text-slate-500 dark:border-slate-800">
                  <tr>
                    <th className="px-4 py-2.5 w-36">时间</th>
                    <th className="px-4 py-2.5 w-20">级别</th>
                    <th className="px-4 py-2.5 w-36">模块</th>
                    <th className="px-4 py-2.5">标题与摘要</th>
                    <th className="px-4 py-2.5 w-32">追溯 ID</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-200/70 dark:divide-slate-800/80">
                  {data.items.map((item: any) => (
                    <tr
                      key={`${item.kind}-${item.id}`}
                      onClick={() => setSelected(item)}
                      className="hover:bg-slate-50 dark:hover:bg-slate-800/40 cursor-pointer"
                    >
                      <td className="px-4 py-2.5 font-mono text-[11px] text-slate-500 whitespace-nowrap">
                        {formatTime(item.created_at)}
                      </td>
                      <td className="px-4 py-2.5">
                        <StatusBadge value={item.level} />
                      </td>
                      <td className="px-4 py-2.5 font-mono text-[11px] text-slate-500 truncate max-w-[140px]">
                        {item.module}
                      </td>
                      <td className="px-4 py-2.5 max-w-xl">
                        <div className="font-medium text-slate-900 dark:text-slate-100 truncate">{item.title}</div>
                        <div className="text-[11px] text-slate-500 truncate">{item.message}</div>
                      </td>
                      <td className="px-4 py-2.5 font-mono text-[11px] text-cyan-600 dark:text-cyan-400">
                        {item.cycle_id ? `C:${shortId(item.cycle_id)}` : item.task_id ? `T:${shortId(item.task_id)}` : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <PaginationBar
              total={data.total}
              limit={data.limit}
              offset={data.offset}
              onChange={setOffset}
            />
          </>
        )}
      </Card>

      <Drawer
        open={Boolean(selected)}
        onClose={() => setSelected(null)}
        title={selected?.title || '日志详情'}
        subtitle={selected ? `${selected.kind} · ${selected.id}` : ''}
      >
        {selected && (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge value={selected.level} />
              <span className="font-mono text-xs text-slate-500">{selected.module} · {formatTime(selected.created_at)}</span>
              {selected.cycle_id && (
                <Button
                  size="xs"
                  variant="primary"
                  onClick={() => {
                    const cid = selected.cycle_id;
                    setSelected(null);
                    onOpenCycle(cid);
                  }}
                >
                  跳转 Cycle <ArrowUpRight className="h-3 w-3" />
                </Button>
              )}
              {selected.task_id && (
                <Button
                  size="xs"
                  variant="secondary"
                  onClick={() => {
                    const tid = selected.task_id;
                    setSelected(null);
                    onOpenTask(tid);
                  }}
                >
                  跳转 Task <ArrowUpRight className="h-3 w-3" />
                </Button>
              )}
            </div>
            <Card className="p-4">
              <div className="text-xs font-semibold mb-1.5">完整消息</div>
              <p className="text-xs text-slate-700 dark:text-slate-300 whitespace-pre-wrap">{selected.message || '未记录'}</p>
            </Card>
            <Card className="p-4">
              <div className="text-xs font-semibold mb-1.5">结构化 Payload</div>
              <JsonBlock data={selected.payload} maxHeight="max-h-96" />
            </Card>
          </>
        )}
      </Drawer>
    </div>
  );
}
