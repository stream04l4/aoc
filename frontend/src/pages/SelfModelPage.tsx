import React, { useEffect, useState } from 'react';
import { Fingerprint, Award, ShieldCheck } from 'lucide-react';
import { apiRequest, formatPct, formatTime, shortId } from '../api/client';
import {
  Card,
  CardHeader,
  EmptyState,
  ErrorBanner,
  LoadingPanel,
  StatusBadge,
} from '../components/ui';

export function SelfModelPage({ refreshTick }: { refreshTick: number }) {
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    apiRequest('/v2/control-center/self-model')
      .then((res) => {
        if (!active) return;
        setData(res);
        setErr(null);
      })
      .catch((e) => active && setErr(e?.detail || e?.message || '加载自我认知数据失败'));
    return () => {
      active = false;
    };
  }, [refreshTick]);

  if (!data && !err) return <LoadingPanel />;
  if (err && !data) return <ErrorBanner error={err} />;

  const id = data.identity;
  const snap = data.latest_snapshot;

  return (
    <div className="space-y-5">
      {/* Identity & Latest Self Model Snapshot */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card className="p-5 space-y-2">
          <div className="flex items-center gap-2 text-cyan-600 dark:text-cyan-400">
            <Fingerprint className="h-4 w-4" />
            <span className="text-xs font-semibold uppercase tracking-wider">Identity（不可变核心身份）</span>
          </div>
          <div className="text-lg font-semibold">{id?.name || '—'}</div>
          <div className="text-xs font-mono text-slate-500">ID: {id?.id || '—'}</div>
          <div className="text-xs font-mono text-slate-500">Version: {id?.version || '—'} · 创建：{formatTime(id?.created_at)}</div>
          <div className="text-xs font-mono text-slate-500 truncate">Letta Agent: {id?.memory_agent_id || '未绑定'}</div>
        </Card>

        <Card className="p-5 lg:col-span-2 space-y-2">
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs font-semibold">最新 Self Model 快照（v{snap?.version_number ?? '—'}）</span>
            <StatusBadge
              value={snap?.letta_synced_at ? 'synced' : 'pending'}
              label={snap?.letta_synced_at ? `Letta 已同步 ${formatTime(snap.letta_synced_at)}` : '待同步'}
            />
          </div>
          <p className="text-xs leading-relaxed text-slate-600 dark:text-slate-300 whitespace-pre-wrap">
            {snap?.summary_text || '未记录'}
          </p>
        </Card>
      </div>

      {/* Capability 5-State Matrix + Beta-Binomial Posteriors */}
      <Card>
        <CardHeader
          title="能力成熟度画像（Beta-Binomial 实证后验）"
          subtitle="严格隔离外部 Agent 静态声明（AVAILABLE）与 AOC 实测验证能力（ACQUIRED / VALIDATED）"
          icon={<Award className="h-4 w-4" />}
        />
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="border-b border-slate-200/70 text-[11px] text-slate-500 dark:border-slate-800">
              <tr>
                <th className="px-4 py-2.5">能力键 (capability_key)</th>
                <th className="px-4 py-2.5">成熟度五态</th>
                <th className="px-4 py-2.5">成功 / 失败样本</th>
                <th className="px-4 py-2.5">Beta(α, β)</th>
                <th className="px-4 py-2.5">估计成功率 p̂</th>
                <th className="px-4 py-2.5">90% 置信区间</th>
                <th className="px-4 py-2.5">更新时间</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-200/70 dark:divide-slate-800/80">
              {(data.capabilities || []).map((c: any) => (
                <tr key={c.id} className="hover:bg-slate-50 dark:hover:bg-slate-800/40">
                  <td className="px-4 py-2.5 font-mono font-medium">{c.capability_key}</td>
                  <td className="px-4 py-2.5"><StatusBadge value={c.maturity_status} /></td>
                  <td className="px-4 py-2.5 font-mono tabular-nums">
                    <span className="text-emerald-600 dark:text-emerald-400">{c.success_samples}</span> /{' '}
                    <span className="text-rose-500">{c.failure_samples}</span> (n={c.sample_count})
                  </td>
                  <td className="px-4 py-2.5 font-mono text-slate-500">({c.alpha}, {c.beta})</td>
                  <td className="px-4 py-2.5 font-mono font-semibold tabular-nums">{formatPct(c.estimated_success_rate)}</td>
                  <td className="px-4 py-2.5 w-48">
                    <div className="flex items-center gap-2">
                      <div className="relative h-2 flex-1 rounded-full bg-slate-200 dark:bg-slate-800">
                        <div
                          className="absolute top-0 h-2 rounded-full bg-cyan-500/70"
                          style={{
                            left: `${c.confidence_low * 100}%`,
                            width: `${Math.max(2, (c.confidence_high - c.confidence_low) * 100)}%`,
                          }}
                        />
                      </div>
                      <span className="text-[10px] font-mono text-slate-400 tabular-nums">
                        [{(c.confidence_low * 100).toFixed(0)}–{(c.confidence_high * 100).toFixed(0)}%]
                      </span>
                    </div>
                  </td>
                  <td className="px-4 py-2.5 font-mono text-[11px] text-slate-500">{formatTime(c.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      {/* Skill Registry + Recent Capability Evidence */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="Skill Registry（可复用技能注册表）"
            subtitle="仅通过隔离断言测试与 SHA-256 校验的技能方可标为 VALIDATED"
            icon={<ShieldCheck className="h-4 w-4" />}
          />
          {(data.skills || []).length === 0 ? (
            <EmptyState
              title="当前尚无已注册技能（0 条）"
              description="V2.1 聚焦可靠性修复与观测面板，自动 Skill 抽取管线留待后续阶段开发。"
            />
          ) : (
            <div className="divide-y divide-slate-200/70 dark:divide-slate-800/80">
              {data.skills.map((sk: any) => (
                <div key={sk.id} className="p-4 space-y-1">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-semibold font-mono">{sk.skill_key}@{sk.version}</span>
                    <StatusBadge value={sk.status} />
                  </div>
                  <p className="text-xs text-slate-500">{sk.description}</p>
                  <div className="text-[10px] font-mono text-slate-400">
                    capability={sk.capability_key} · SHA256={shortId(sk.artifact_checksum)}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card>
          <CardHeader title="最近能力验证证据账本（capability_evidence）" subtitle="按 (profile_id, execution_id) 唯一去重，100% 可追溯" />
          <div className="divide-y divide-slate-200/70 max-h-80 overflow-y-auto dark:divide-slate-800/80">
            {(data.recent_evidence || []).slice(0, 20).map((ev: any) => (
              <div key={ev.id} className="flex items-start justify-between gap-2 px-4 py-2.5">
                <div className="min-w-0">
                  <div className="text-xs font-mono font-medium">{ev.capability_key}</div>
                  <div className="text-[11px] text-slate-500 truncate">{ev.verification_summary || '—'}</div>
                </div>
                <div className="flex flex-col items-end gap-1 shrink-0">
                  <StatusBadge value={ev.outcome} />
                  <span className="text-[10px] font-mono text-slate-400">{formatTime(ev.created_at)}</span>
                </div>
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}
