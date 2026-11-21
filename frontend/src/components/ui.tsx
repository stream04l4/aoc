import React from 'react';
import { clsx } from 'clsx';
import { twMerge } from 'tailwind-merge';
import { ChevronLeft, ChevronRight, X } from 'lucide-react';

export function cn(...inputs: (string | undefined | null | false)[]) {
  return twMerge(clsx(inputs));
}

export function Card({
  className,
  children,
}: {
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div
      className={cn(
        'rounded-xl border border-slate-200/80 bg-white shadow-xs dark:border-slate-800/90 dark:bg-slate-900/75 dark:backdrop-blur-sm',
        className
      )}
    >
      {children}
    </div>
  );
}

export function CardHeader({
  title,
  subtitle,
  action,
  icon,
}: {
  title: string;
  subtitle?: string;
  action?: React.ReactNode;
  icon?: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-3 border-b border-slate-200/70 px-5 py-3.5 dark:border-slate-800/80">
      <div className="flex items-center gap-2.5 min-w-0">
        {icon && <span className="text-cyan-600 dark:text-cyan-400 shrink-0">{icon}</span>}
        <div className="min-w-0">
          <h3 className="text-sm font-semibold tracking-tight text-slate-900 dark:text-slate-100 truncate">
            {title}
          </h3>
          {subtitle && (
            <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400 truncate">{subtitle}</p>
          )}
        </div>
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}

export function StatCard({
  label,
  value,
  subtext,
  tone = 'default',
  icon,
}: {
  label: string;
  value: React.ReactNode;
  subtext?: string;
  tone?: 'default' | 'emerald' | 'amber' | 'rose' | 'cyan';
  icon?: React.ReactNode;
}) {
  const accent = {
    default: 'text-slate-900 dark:text-slate-50',
    emerald: 'text-emerald-600 dark:text-emerald-400',
    amber: 'text-amber-600 dark:text-amber-400',
    rose: 'text-rose-600 dark:text-rose-400',
    cyan: 'text-cyan-600 dark:text-cyan-400',
  }[tone];

  return (
    <Card className="p-4">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium text-slate-500 dark:text-slate-400">{label}</span>
        {icon && <span className="text-slate-400 dark:text-slate-500">{icon}</span>}
      </div>
      <div className={cn('mt-2 text-2xl font-semibold tracking-tight tabular-nums', accent)}>
        {value}
      </div>
      {subtext && (
        <div className="mt-1 text-xs text-slate-500 dark:text-slate-400 truncate">{subtext}</div>
      )}
    </Card>
  );
}

const BADGE_STYLES: Record<string, string> = {
  // Healthy / positive
  ACTIVE: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30',
  SUCCEEDED: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30',
  ACHIEVED: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30',
  APPROVED: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30',
  VALIDATED: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30',
  CONSUMED: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30',
  synced: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30',
  embedded: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30',
  dispatched: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30',
  online: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30',
  healthy: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30',
  ok: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30',
  approved: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30',
  verified_success: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30',
  resolved: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30',
  L0: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30',

  // Info / active running
  RUNNING: 'bg-cyan-500/15 text-cyan-700 dark:text-cyan-300 border-cyan-500/30',
  DISPATCHING: 'bg-cyan-500/15 text-cyan-700 dark:text-cyan-300 border-cyan-500/30',
  VERIFYING: 'bg-cyan-500/15 text-cyan-700 dark:text-cyan-300 border-cyan-500/30',
  ACQUIRED: 'bg-cyan-500/15 text-cyan-700 dark:text-cyan-300 border-cyan-500/30',
  COMPLETED: 'bg-cyan-500/15 text-cyan-700 dark:text-cyan-300 border-cyan-500/30',
  queued_topic: 'bg-cyan-500/15 text-cyan-700 dark:text-cyan-300 border-cyan-500/30',
  open: 'bg-cyan-500/15 text-cyan-700 dark:text-cyan-300 border-cyan-500/30',
  L1: 'bg-cyan-500/15 text-cyan-700 dark:text-cyan-300 border-cyan-500/30',
  env: 'bg-violet-500/15 text-violet-700 dark:text-violet-300 border-violet-500/30',
  '.env': 'bg-indigo-500/15 text-indigo-700 dark:text-indigo-300 border-indigo-500/30',
  yaml: 'bg-cyan-500/15 text-cyan-700 dark:text-cyan-300 border-cyan-500/30',

  // Warning / waiting / throttled
  SLEEPING: 'bg-slate-500/15 text-slate-700 dark:text-slate-300 border-slate-500/30',
  IDLE: 'bg-slate-500/15 text-slate-700 dark:text-slate-300 border-slate-500/30',
  AVAILABLE: 'bg-slate-500/15 text-slate-700 dark:text-slate-300 border-slate-500/30',
  DECLARED: 'bg-slate-500/15 text-slate-600 dark:text-slate-400 border-slate-500/30',
  default: 'bg-slate-500/15 text-slate-600 dark:text-slate-400 border-slate-500/30',
  THROTTLED: 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30',
  PAUSED: 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30',
  PENDING: 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30',
  pending: 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30',
  RETRY_WAIT: 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30',
  BUDGET_DEFERRED: 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30',
  REQUIRES_APPROVAL: 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30',
  AWAITING_APPROVAL: 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30',
  requires_approval: 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30',
  TIMEOUT: 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30',
  warning: 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30',
  L2: 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30',

  // Error / denied / critical
  CRITICAL: 'bg-rose-500/15 text-rose-700 dark:text-rose-300 border-rose-500/30',
  FAILED: 'bg-rose-500/15 text-rose-700 dark:text-rose-300 border-rose-500/30',
  ACTION_FAILED: 'bg-rose-500/15 text-rose-700 dark:text-rose-300 border-rose-500/30',
  ERROR: 'bg-rose-500/15 text-rose-700 dark:text-rose-300 border-rose-500/30',
  error: 'bg-rose-500/15 text-rose-700 dark:text-rose-300 border-rose-500/30',
  DENIED: 'bg-rose-500/15 text-rose-700 dark:text-rose-300 border-rose-500/30',
  denied: 'bg-rose-500/15 text-rose-700 dark:text-rose-300 border-rose-500/30',
  POLICY_DENIED: 'bg-rose-500/15 text-rose-700 dark:text-rose-300 border-rose-500/30',
  REJECTED: 'bg-rose-500/15 text-rose-700 dark:text-rose-300 border-rose-500/30',
  dead_letter: 'bg-rose-500/15 text-rose-700 dark:text-rose-300 border-rose-500/30',
  RECOVERY_REQUIRED: 'bg-rose-500/15 text-rose-700 dark:text-rose-300 border-rose-500/30',
  DEPRECATED: 'bg-rose-500/15 text-rose-700 dark:text-rose-300 border-rose-500/30',
  verified_failure: 'bg-rose-500/15 text-rose-700 dark:text-rose-300 border-rose-500/30',
  down: 'bg-rose-500/15 text-rose-700 dark:text-rose-300 border-rose-500/30',
  offline: 'bg-rose-500/15 text-rose-700 dark:text-rose-300 border-rose-500/30',
  L3: 'bg-rose-500/15 text-rose-700 dark:text-rose-300 border-rose-500/30',
};

export function StatusBadge({
  value,
  label,
  className,
}: {
  value: string | null | undefined;
  label?: string;
  className?: string;
}) {
  const key = value || 'default';
  const style = BADGE_STYLES[key] || BADGE_STYLES.default;
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-md border px-2 py-0.5 text-[11px] font-medium tracking-wide whitespace-nowrap',
        style,
        className
      )}
    >
      {label || value || '未记录'}
    </span>
  );
}

export function Button({
  children,
  onClick,
  variant = 'secondary',
  size = 'sm',
  disabled = false,
  className,
  type = 'button',
}: {
  children: React.ReactNode;
  onClick?: () => void;
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost';
  size?: 'xs' | 'sm' | 'md';
  disabled?: boolean;
  className?: string;
  type?: 'button' | 'submit';
}) {
  const v = {
    primary: 'bg-cyan-600 text-white hover:bg-cyan-500 border-transparent shadow-xs',
    secondary:
      'bg-white text-slate-700 border-slate-200 hover:bg-slate-50 dark:bg-slate-800 dark:text-slate-200 dark:border-slate-700 dark:hover:bg-slate-700/80',
    danger: 'bg-rose-600 text-white hover:bg-rose-500 border-transparent',
    ghost: 'bg-transparent border-transparent text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800/80',
  }[variant];
  const s = {
    xs: 'px-2 py-1 text-xs',
    sm: 'px-3 py-1.5 text-xs',
    md: 'px-4 py-2 text-sm',
  }[size];

  return (
    <button
      type={type}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        'inline-flex items-center justify-center gap-1.5 rounded-lg border font-medium transition-colors disabled:opacity-45 disabled:cursor-not-allowed cursor-pointer',
        v,
        s,
        className
      )}
    >
      {children}
    </button>
  );
}

export function EmptyState({
  title,
  description,
}: {
  title: string;
  description?: string;
}) {
  return (
    <div className="flex flex-col items-center justify-center py-12 text-center">
      <p className="text-sm font-medium text-slate-600 dark:text-slate-300">{title}</p>
      {description && <p className="mt-1 text-xs text-slate-400 dark:text-slate-500 max-w-md">{description}</p>}
    </div>
  );
}

export function LoadingPanel({ label = '正在从 PostgreSQL / AOC API 加载实时数据…' }: { label?: string }) {
  return (
    <Card className="p-8">
      <div className="flex items-center justify-center gap-3 text-xs text-slate-500 dark:text-slate-400">
        <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-cyan-500 border-t-transparent" />
        <span>{label}</span>
      </div>
    </Card>
  );
}

export function ErrorBanner({ error, onRetry }: { error: string; onRetry?: () => void }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-xs text-rose-700 dark:text-rose-300">
      <span>请求失败：{error}</span>
      {onRetry && (
        <Button size="xs" variant="secondary" onClick={onRetry}>
          重试
        </Button>
      )}
    </div>
  );
}

export function JsonBlock({ data, maxHeight = 'max-h-72' }: { data: unknown; maxHeight?: string }) {
  if (data === null || data === undefined) {
    return <span className="text-xs italic text-slate-400">未记录</span>;
  }
  return (
    <pre
      className={cn(
        'overflow-auto rounded-lg border border-slate-200/80 bg-slate-50 p-3 text-[11px] leading-relaxed text-slate-800 dark:border-slate-800 dark:bg-slate-950/80 dark:text-slate-200 font-mono',
        maxHeight
      )}
    >
      {typeof data === 'string' ? data : JSON.stringify(data, null, 2)}
    </pre>
  );
}

export function PaginationBar({
  total,
  limit,
  offset,
  onChange,
}: {
  total: number;
  limit: number;
  offset: number;
  onChange: (nextOffset: number) => void;
}) {
  const page = Math.floor(offset / limit) + 1;
  const totalPages = Math.max(1, Math.ceil(total / limit));
  return (
    <div className="flex items-center justify-between border-t border-slate-200/70 px-4 py-2.5 text-xs text-slate-500 dark:border-slate-800/80 dark:text-slate-400">
      <span className="tabular-nums">
        共 {total} 条记录 · 第 {page} / {totalPages} 页
      </span>
      <div className="flex items-center gap-1.5">
        <Button
          size="xs"
          disabled={offset <= 0}
          onClick={() => onChange(Math.max(0, offset - limit))}
        >
          <ChevronLeft className="h-3.5 w-3.5" /> 上一页
        </Button>
        <Button
          size="xs"
          disabled={offset + limit >= total}
          onClick={() => onChange(offset + limit)}
        >
          下一页 <ChevronRight className="h-3.5 w-3.5" />
        </Button>
      </div>
    </div>
  );
}

export function Drawer({
  open,
  onClose,
  title,
  subtitle,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-slate-950/60 backdrop-blur-[2px]">
      <div className="flex h-full w-full max-w-3xl flex-col border-l border-slate-200 bg-white shadow-2xl dark:border-slate-800 dark:bg-slate-950">
        <div className="flex items-start justify-between border-b border-slate-200 px-5 py-4 dark:border-slate-800">
          <div className="min-w-0">
            <h3 className="text-base font-semibold text-slate-900 dark:text-slate-100 truncate">
              {title}
            </h3>
            {subtitle && (
              <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400 font-mono truncate">
                {subtitle}
              </p>
            )}
          </div>
          <button
            onClick={onClose}
            className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700 dark:hover:bg-slate-800 dark:hover:text-slate-200 cursor-pointer"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-5 space-y-4">{children}</div>
      </div>
    </div>
  );
}
