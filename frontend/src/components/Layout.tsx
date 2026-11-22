import React, { useState } from 'react';
import {
  Activity,
  Brain,
  Flame,
  ListChecks,
  Database,
  Globe,
  Fingerprint,
  Shield,
  SlidersHorizontal,
  ScrollText,
  RefreshCw,
  Sun,
  Moon,
  LogOut,
  Menu,
  X,
} from 'lucide-react';
import { PrincipalInfo } from '../api/client';
import { Button, cn, StatusBadge } from './ui';

export type NavPage =
  | 'overview'
  | 'mind'
  | 'drives'
  | 'tasks'
  | 'memory'
  | 'world'
  | 'self-model'
  | 'governance'
  | 'config'
  | 'logs';

export type NavGroup = 'hub' | 'kernel' | 'organs' | 'ops';

export const NAV_GROUPS: { id: NavGroup; title: string; subtitle: string }[] = [
  { id: 'hub', title: '生命体总控', subtitle: 'ORGANISM' },
  { id: 'kernel', title: '类人内核', subtitle: 'KERNEL' },
  { id: 'organs', title: '四大外接器官', subtitle: 'ORGANS' },
  { id: 'ops', title: '系统运维', subtitle: 'OPS' },
];

export const NAV_ITEMS: {
  id: NavPage;
  group: NavGroup;
  label: string;
  en: string;
  badge?: string;
  icon: React.ComponentType<{ className?: string }>;
}[] = [
  { id: 'overview', group: 'hub', label: '生命体总览', en: 'Overview', icon: Activity },
  { id: 'drives', group: 'kernel', label: '欲望系统', en: 'Drives · Affect', icon: Flame },
  { id: 'self-model', group: 'kernel', label: '自我认知', en: 'Self · RSI', icon: Fingerprint },
  { id: 'governance', group: 'kernel', label: '安全与审批', en: 'Governance', icon: Shield },
  { id: 'mind', group: 'organs', label: '认知与决策', en: 'Brain · LLM', icon: Brain },
  { id: 'world', group: 'organs', label: '环境感知', en: 'Perception · MCP', icon: Globe },
  { id: 'memory', group: 'organs', label: '记忆中心', en: 'Memory · Letta', icon: Database },
  { id: 'tasks', group: 'organs', label: '任务与执行', en: 'Action · Agents', icon: ListChecks },
  { id: 'config', group: 'ops', label: '配置中心', en: 'Config', icon: SlidersHorizontal },
  { id: 'logs', group: 'ops', label: '运行日志', en: 'Logs', icon: ScrollText },
];

export function Layout({
  activePage,
  onNavigate,
  principal,
  lifecycleState,
  organismName,
  windowHours,
  onWindowHoursChange,
  pollSeconds,
  onPollSecondsChange,
  onManualRefresh,
  lastUpdated,
  darkMode,
  onToggleDark,
  onLogout,
  children,
}: {
  activePage: NavPage;
  onNavigate: (page: NavPage) => void;
  principal: PrincipalInfo;
  lifecycleState?: string;
  organismName?: string;
  windowHours: number;
  onWindowHoursChange: (h: number) => void;
  pollSeconds: number;
  onPollSecondsChange: (s: number) => void;
  onManualRefresh: () => void;
  lastUpdated: string;
  darkMode: boolean;
  onToggleDark: () => void;
  onLogout: () => void;
  children: React.ReactNode;
}) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const currentMeta = NAV_ITEMS.find((n) => n.id === activePage) || NAV_ITEMS[0];

  const sidebarContent = (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-slate-200/80 px-4 py-4 dark:border-slate-800/80">
        <div className="flex items-center gap-2.5 min-w-0">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-cyan-500 to-indigo-600 text-white font-bold text-xs shadow-sm">
            AOC
          </div>
          <div className="min-w-0">
            <div className="text-sm font-semibold tracking-tight text-slate-900 dark:text-slate-100 truncate">
              Control Center
            </div>
            <div className="text-[11px] text-slate-500 dark:text-slate-400 truncate">
              {organismName || 'AOC-001'} · V3.2 类人内核
            </div>
          </div>
        </div>
        <button
          onClick={() => setMobileOpen(false)}
          className="rounded-lg p-1 text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800 lg:hidden cursor-pointer"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <nav className="flex-1 overflow-y-auto px-2.5 py-3 space-y-3.5">
        {NAV_GROUPS.map((grp) => {
          const items = NAV_ITEMS.filter((i) => i.group === grp.id);
          return (
            <div key={grp.id} className="space-y-0.5">
              <div className="flex items-center justify-between px-3 pb-1">
                <span className="text-[10px] font-semibold tracking-wider text-slate-400 dark:text-slate-500">
                  {grp.title}
                </span>
                <span className="text-[9px] font-mono text-slate-300 dark:text-slate-600">
                  {grp.subtitle}
                </span>
              </div>
              {items.map((item) => {
                const Icon = item.icon;
                const active = item.id === activePage;
                return (
                  <button
                    key={item.id}
                    onClick={() => {
                      onNavigate(item.id);
                      setMobileOpen(false);
                    }}
                    className={cn(
                      'flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-xs font-medium transition-colors cursor-pointer',
                      active
                        ? 'bg-cyan-500/12 text-cyan-700 dark:text-cyan-300 border border-cyan-500/25'
                        : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-slate-800/70 dark:hover:text-slate-100 border border-transparent'
                    )}
                  >
                    <Icon className="h-4 w-4 shrink-0" />
                    <span className="truncate">{item.label}</span>
                    <span className="ml-auto text-[10px] text-slate-400 dark:text-slate-500 font-mono truncate">
                      {item.en}
                    </span>
                  </button>
                );
              })}
            </div>
          );
        })}
      </nav>

      <div className="border-t border-slate-200/80 p-3 dark:border-slate-800/80">
        <div className="rounded-lg bg-slate-100/80 p-2.5 dark:bg-slate-900/90">
          <div className="flex items-center justify-between gap-2">
            <span className="text-[11px] font-medium text-slate-700 dark:text-slate-300 truncate">
              {principal.name}
            </span>
            <StatusBadge value={lifecycleState || 'UNKNOWN'} />
          </div>
          <div className="mt-1.5 flex flex-wrap gap-1">
            {principal.scopes.map((sc) => (
              <span
                key={sc}
                className="rounded bg-slate-200/80 px-1.5 py-0.5 text-[10px] font-mono text-slate-600 dark:bg-slate-800 dark:text-slate-400"
              >
                {sc}
              </span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );

  return (
    <div className="flex min-h-screen bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      {/* Desktop sidebar */}
      <aside className="hidden w-64 shrink-0 border-r border-slate-200/80 bg-white dark:border-slate-800/80 dark:bg-slate-950/90 lg:block">
        <div className="sticky top-0 h-screen">{sidebarContent}</div>
      </aside>

      {/* Mobile drawer sidebar */}
      {mobileOpen && (
        <div className="fixed inset-0 z-50 flex lg:hidden">
          <div className="w-64 bg-white dark:bg-slate-950 border-r border-slate-200 dark:border-slate-800 shadow-xl">
            {sidebarContent}
          </div>
          <div className="flex-1 bg-slate-950/60" onClick={() => setMobileOpen(false)} />
        </div>
      )}

      {/* Main workspace */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex flex-wrap items-center justify-between gap-3 border-b border-slate-200/80 bg-white/85 px-4 py-3 backdrop-blur-md dark:border-slate-800/80 dark:bg-slate-950/80 lg:px-6">
          <div className="flex items-center gap-3 min-w-0">
            <button
              onClick={() => setMobileOpen(true)}
              className="rounded-lg p-1.5 text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800 lg:hidden cursor-pointer"
            >
              <Menu className="h-5 w-5" />
            </button>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-base font-semibold tracking-tight text-slate-900 dark:text-slate-100">
                  {currentMeta.label}
                </h1>
                <span className="text-xs font-mono text-slate-400 dark:text-slate-500">
                  {currentMeta.en}
                </span>
              </div>
              <p className="text-[11px] text-slate-500 dark:text-slate-400">
                实时数据源：PostgreSQL + AOC API · 上次刷新：{lastUpdated || '—'}
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {/* Time window selector */}
            <select
              aria-label="时间范围"
              value={windowHours}
              onChange={(e) => onWindowHoursChange(Number(e.target.value))}
              className="rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-700 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200"
            >
              <option value={1}>近 1 小时</option>
              <option value={6}>近 6 小时</option>
              <option value={24}>近 24 小时</option>
              <option value={168}>近 7 天</option>
              <option value={0}>全部历史</option>
            </select>

            {/* Auto-poll selector */}
            <select
              aria-label="自动轮询"
              value={pollSeconds}
              onChange={(e) => onPollSecondsChange(Number(e.target.value))}
              className="rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-700 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200"
            >
              <option value={10}>自动 10s</option>
              <option value={30}>自动 30s</option>
              <option value={0}>手动刷新</option>
            </select>

            <Button size="sm" onClick={onManualRefresh}>
              <RefreshCw className="h-3.5 w-3.5" /> 刷新
            </Button>

            <Button size="sm" variant="ghost" onClick={onToggleDark}>
              {darkMode ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
            </Button>

            <Button size="sm" variant="ghost" onClick={onLogout}>
              <LogOut className="h-3.5 w-3.5" /> 退出
            </Button>
          </div>
        </header>

        <main className="mx-auto w-full max-w-[1440px] flex-1 p-4 lg:p-6 space-y-5">
          {children}
        </main>
      </div>
    </div>
  );
}
