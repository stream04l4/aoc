import React, { useState } from 'react';
import {
  ArrowUpRight,
  Brain,
  Cpu,
  Database,
  Dna,
  Flame,
  Globe,
  ShieldCheck,
  Wrench,
} from 'lucide-react';
import { formatNumber, formatPct } from '../api/client';
import { NavPage } from './Layout';
import { Card, cn, StatusBadge } from './ui';

type OrganKey = 'kernel' | 'brain' | 'perception' | 'memory' | 'action' | 'evolution';

function deriveMoodSummary(
  affect: any,
  lifecycleState?: string,
): { label: string; accent: string; auraFrom: string; auraTo: string; pulseSec: number } {
  const lc = String(lifecycleState || '').toUpperCase();
  if (lc === 'CRITICAL' || lc === 'ERROR') {
    return {
      label: '应急守护中',
      accent: '#f43f5e',
      auraFrom: '#f43f5e',
      auraTo: '#f59e0b',
      pulseSec: 1.4,
    };
  }
  if (!affect?.enabled || !affect?.current_state) {
    return {
      label: lc === 'SLEEPING' ? '静息休眠' : lc === 'IDLE' ? '平衡待机' : '确定性运转',
      accent: '#06b6d4',
      auraFrom: '#06b6d4',
      auraTo: '#6366f1',
      pulseSec: 3.6,
    };
  }
  const st = affect.current_state;
  const valence = Number(st.valence ?? 0);
  const arousal = Number(st.arousal ?? 0.2);
  const frustration = Number(st.frustration ?? 0);
  const boredom = Number(st.boredom ?? 0);
  const pulseSec = Math.max(1.6, Math.min(4.6, 4.2 - arousal * 2.2));

  if (frustration >= 0.45) {
    return {
      label: '审慎承压（挫折升高）',
      accent: '#f59e0b',
      auraFrom: '#f59e0b',
      auraTo: '#f43f5e',
      pulseSec,
    };
  }
  if (boredom >= 0.45) {
    return {
      label: '求新躁动（厌倦触发探索）',
      accent: '#a855f7',
      auraFrom: '#a855f7',
      auraTo: '#06b6d4',
      pulseSec,
    };
  }
  if (valence >= 0.25) {
    return {
      label: '积极高涨（正向增益）',
      accent: '#10b981',
      auraFrom: '#10b981',
      auraTo: '#06b6d4',
      pulseSec,
    };
  }
  return {
    label: lc === 'SLEEPING' ? '平稳消化休眠' : '专注自驱平衡',
    accent: '#06b6d4',
    auraFrom: '#06b6d4',
    auraTo: '#8b5cf6',
    pulseSec,
  };
}

function ringProps(radius: number, ratio: number) {
  const clamped = Math.max(0, Math.min(1, Number.isFinite(ratio) ? ratio : 0));
  const c = 2 * Math.PI * radius;
  return {
    strokeDasharray: `${c.toFixed(2)}`,
    strokeDashoffset: `${(c * (1 - clamped)).toFixed(2)}`,
  };
}

export function OrganismAnatomy({
  data,
  onNavigate,
}: {
  data: any;
  onNavigate?: (page: NavPage) => void;
}) {
  const [focused, setFocused] = useState<OrganKey>('kernel');

  const org = data?.organism || {};
  const comps = data?.components || {};
  const affect = data?.affect || null;
  const organs = data?.organs || {};
  const drivesList: any[] = data?.drives || [];

  const findDrive = (key: string) =>
    drivesList.find((d) => d.drive_type === key) || { drive_type: key, satisfaction: 0, priority: 0 };

  const surv = findDrive('survival');
  const repro = findDrive('reproduction');
  const cur = findDrive('curiosity');

  const brainInfo = organs.brain || {
    model: comps.llm?.model,
    available: comps.llm?.available,
    latency_ms: comps.llm?.latency_ms,
    circuit_open: comps.llm?.circuit_open,
    calls_window: data?.stats?.usage_by_category?.llm?.calls ?? 0,
    tokens_window: Math.round(data?.stats?.usage_by_category?.llm?.total_amount ?? 0),
    avg_seconds: data?.stats?.usage_by_category?.llm?.avg_seconds ?? 0,
  };

  const perceptionInfo = organs.perception || {
    poll_on_perceive: false,
    sources_total: 0,
    sources_enabled: 0,
    events_total: 0,
    hypotheses_total: 0,
    hypotheses_confirmed: 0,
    mcp_enabled: false,
    mcp_tools_count: 0,
    mcp_tools: [],
  };

  const memoryInfo = organs.memory || {
    provider: comps.letta?.provider || 'letta',
    letta_available: comps.letta?.available,
    memories_synced: comps.letta?.memories_synced ?? 0,
    memories_total: comps.letta?.memories_total ?? 0,
    pgvector_enabled: comps.postgres?.pgvector_enabled ?? false,
    embedding_provider: comps.embedding?.provider || 'local_onnx',
    embedding_dim: comps.embedding?.dimensions ?? 512,
    embedded_count: comps.embedding?.embedded_count ?? 0,
    outbox_pending: (comps.letta?.outbox?.pending || 0) + (comps.letta?.outbox?.failed || 0),
  };

  const actionInfo = organs.action || {
    runner_enabled: comps.runner?.enabled ?? false,
    runner_online: comps.runner?.runners?.[0]?.status === 'online',
    runner_id: comps.runner?.runners?.[0]?.runner_id || 'local-fallback',
    agents: comps.agents || [],
    agents_healthy: (comps.agents || []).filter((a: any) => a.healthy).length,
    agents_total: (comps.agents || []).length,
    tasks_window: data?.stats?.tasks_total_window ?? 0,
    tasks_succeeded: data?.stats?.tasks_by_status?.SUCCEEDED ?? 0,
    tasks_failed: data?.stats?.tasks_by_status?.FAILED ?? 0,
  };

  const evoInfo = organs.evolution || {
    capabilities_total: 0,
    capabilities_validated: 0,
    capabilities_acquired: 0,
    skills_total: 0,
    skills_validated: 0,
    skill_invocations_window: 0,
    skill_cross_task_reuses_window: 0,
    skill_reuse_success_rate: null,
    estimated_saved_tokens_window: 0,
    playbooks_active_count: 0,
    exploration_topics_total: 0,
    exploration_topics_verified: 0,
    open_public_perception_enabled: false,
    verified_ig_per_1m_tokens: 0,
    preflight_rejected_window: 0,
    goal_dedup_skipped_window: 0,
  };

  const mood = deriveMoodSummary(affect, org.lifecycle_state);
  const brainHealthy = !brainInfo.circuit_open && brainInfo.available !== false;
  const memoryHealthy = Boolean(memoryInfo.letta_available);
  const actionHealthy = Boolean(actionInfo.runner_online || actionInfo.agents_healthy > 0);
  const perceptionHealthy = Boolean(perceptionInfo.sources_enabled > 0 || perceptionInfo.mcp_tools_count > 0);

  const selectedDrive = data?.latest_cycle?.selected_drive || '—';
  const nav = (page: NavPage) => onNavigate && onNavigate(page);

  const cardRing = (key: OrganKey) =>
    cn(
      'group relative rounded-2xl border p-4 transition-all duration-200 cursor-pointer text-left w-full',
      focused === key
        ? 'border-cyan-500/60 bg-cyan-500/[0.06] shadow-md shadow-cyan-500/10 dark:border-cyan-400/60 dark:bg-cyan-500/10'
        : 'border-slate-200/80 bg-white/90 hover:border-slate-300 dark:border-slate-800/90 dark:bg-slate-900/75 dark:hover:border-slate-700',
    );

  return (
    <Card className="overflow-hidden border-slate-200/90 bg-gradient-to-br from-white via-slate-50/70 to-cyan-50/40 dark:border-slate-800 dark:from-slate-950 dark:via-slate-900/95 dark:to-slate-950">
      {/* Section Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200/70 px-5 py-3.5 dark:border-slate-800/80">
        <div className="flex items-center gap-2.5">
          <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-gradient-to-br from-cyan-500 to-violet-600 text-white shadow-sm">
            <Dna className="h-4 w-4" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                数字生命体全息总览 · 类人内核与四大外接器官
              </h3>
              <span className="rounded-full bg-cyan-500/10 px-2 py-0.5 text-[10px] font-mono font-medium text-cyan-700 dark:text-cyan-300">
                V4.0 RSI BIO-ARCHITECTURE
              </span>
            </div>
            <p className="text-[11px] text-slate-500 dark:text-slate-400">
              三内驱力（生存 / 繁衍 / 好奇心）+ 有界情绪构成类人内核，外接大脑、感知、记忆、行动驱动知识沉淀与 RSI 自进化 · 点击任意器官直达详情
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2 text-[11px] font-mono text-slate-500 dark:text-slate-400">
          <span>情绪态：</span>
          <span
            className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[11px] font-medium"
            style={{ backgroundColor: `${mood.accent}1a`, color: mood.accent }}
          >
            <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: mood.accent }} />
            {mood.label}
          </span>
        </div>
      </div>

      {/* Main 3-column Humanoid Anatomy Stage */}
      <div className="grid grid-cols-1 gap-4 p-4 lg:p-5 xl:grid-cols-12 items-stretch">
        {/* LEFT COLUMN: Brain + Perception */}
        <div className="xl:col-span-4 flex flex-col justify-between gap-4">
          {/* 1. External Brain (LLM) */}
          <button
            type="button"
            onMouseEnter={() => setFocused('brain')}
            onClick={() => nav('mind')}
            className={cardRing('brain')}
          >
            <div className="flex items-start justify-between gap-2">
              <div className="flex items-center gap-2">
                <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-sky-500/15 text-sky-600 dark:text-sky-400">
                  <Brain className="h-4 w-4" />
                </span>
                <div>
                  <div className="text-xs font-semibold text-slate-900 dark:text-slate-100">
                    外接大脑 · Brain (LLM)
                  </div>
                  <div className="text-[10px] font-mono text-slate-400">HEAD / CORTEX PORT</div>
                </div>
              </div>
              <div className="flex items-center gap-1.5">
                <StatusBadge value={brainHealthy ? 'ok' : 'down'} label={brainHealthy ? '在线' : '异常'} />
                <ArrowUpRight className="h-3.5 w-3.5 text-slate-400 group-hover:text-cyan-500 transition-colors" />
              </div>
            </div>
            <div className="mt-3 rounded-lg bg-slate-100/80 px-2.5 py-1.5 text-[11px] font-mono text-slate-700 dark:bg-slate-950/70 dark:text-slate-300 truncate">
              {brainInfo.model || '未配置模型'}
            </div>
            <div className="mt-2.5 grid grid-cols-3 gap-2 text-[11px]">
              <div>
                <div className="text-[10px] text-slate-400">窗口调用</div>
                <div className="font-mono font-semibold text-slate-800 dark:text-slate-200">
                  {formatNumber(brainInfo.calls_window)}
                </div>
              </div>
              <div>
                <div className="text-[10px] text-slate-400">Token 消耗</div>
                <div className="font-mono font-semibold text-slate-800 dark:text-slate-200">
                  {formatNumber(brainInfo.tokens_window)}
                </div>
              </div>
              <div>
                <div className="text-[10px] text-slate-400">探测 / 均耗时</div>
                <div className="font-mono font-semibold text-slate-800 dark:text-slate-200">
                  {brainInfo.latency_ms != null ? `${Math.round(brainInfo.latency_ms)}ms` : `${brainInfo.avg_seconds ?? 0}s`}
                </div>
              </div>
            </div>
          </button>

          {/* 2. External Perception (Sensors + MCP) */}
          <button
            type="button"
            onMouseEnter={() => setFocused('perception')}
            onClick={() => nav('world')}
            className={cardRing('perception')}
          >
            <div className="flex items-start justify-between gap-2">
              <div className="flex items-center gap-2">
                <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-teal-500/15 text-teal-600 dark:text-teal-400">
                  <Globe className="h-4 w-4" />
                </span>
                <div>
                  <div className="text-xs font-semibold text-slate-900 dark:text-slate-100">
                    外接感知 · Perception (插件 / MCP)
                  </div>
                  <div className="text-[10px] font-mono text-slate-400">VISOR / SENSORY PORT</div>
                </div>
              </div>
              <div className="flex items-center gap-1.5">
                <StatusBadge
                  value={perceptionHealthy ? 'ok' : 'IDLE'}
                  label={perceptionInfo.poll_on_perceive ? '主动轮询' : '按需感知'}
                />
                <ArrowUpRight className="h-3.5 w-3.5 text-slate-400 group-hover:text-cyan-500 transition-colors" />
              </div>
            </div>
            <div className="mt-3 grid grid-cols-3 gap-2 text-[11px]">
              <div className="rounded-lg bg-slate-100/70 p-2 dark:bg-slate-950/60">
                <div className="text-[10px] text-slate-400">活跃传感器</div>
                <div className="font-mono font-semibold text-teal-600 dark:text-teal-400">
                  {perceptionInfo.sources_enabled}/{perceptionInfo.sources_total}
                </div>
              </div>
              <div className="rounded-lg bg-slate-100/70 p-2 dark:bg-slate-950/60">
                <div className="text-[10px] text-slate-400">世界事件</div>
                <div className="font-mono font-semibold text-slate-800 dark:text-slate-200">
                  {formatNumber(perceptionInfo.events_total)}
                </div>
              </div>
              <div className="rounded-lg bg-slate-100/70 p-2 dark:bg-slate-950/60">
                <div className="text-[10px] text-slate-400">MCP / 假设</div>
                <div className="font-mono font-semibold text-slate-800 dark:text-slate-200">
                  {perceptionInfo.mcp_tools_count} · {perceptionInfo.hypotheses_confirmed}/{perceptionInfo.hypotheses_total}
                </div>
              </div>
            </div>
            {Array.isArray(perceptionInfo.active_source_names) && perceptionInfo.active_source_names.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1">
                {perceptionInfo.active_source_names.slice(0, 4).map((sn: string) => (
                  <span
                    key={sn}
                    className="rounded bg-teal-500/10 px-1.5 py-0.5 text-[10px] font-mono text-teal-700 dark:text-teal-300"
                  >
                    {sn}
                  </span>
                ))}
              </div>
            )}
          </button>
        </div>

        {/* CENTER COLUMN: Humanoid Bio-Cybernetic Figure & Tri-Drive Core */}
        <div
          onMouseEnter={() => setFocused('kernel')}
          className="xl:col-span-4 relative flex flex-col items-center justify-between rounded-2xl border border-slate-200/80 bg-gradient-to-b from-slate-900/[0.03] via-cyan-500/[0.04] to-violet-500/[0.05] p-4 dark:border-slate-800/90 dark:from-slate-900/90 dark:via-slate-950 dark:to-slate-900/90"
        >
          {/* Top Kernel Badge */}
          <div className="flex w-full items-center justify-between text-[11px]">
            <span className="inline-flex items-center gap-1.5 font-semibold text-slate-700 dark:text-slate-200">
              <Flame className="h-3.5 w-3.5 text-cyan-500" />
              类人生命内核 · {org.name || 'AOC-001'}
            </span>
            <button
              type="button"
              onClick={() => nav('drives')}
              className="inline-flex items-center gap-1 font-mono text-[10px] text-cyan-600 hover:underline dark:text-cyan-400 cursor-pointer"
            >
              主导：{selectedDrive} <ArrowUpRight className="h-3 w-3" />
            </button>
          </div>

          {/* Interactive SVG Humanoid Figure */}
          <div className="relative my-1 w-full max-w-[300px]">
            <svg
              viewBox="0 0 320 355"
              className="w-full h-auto select-none"
              role="img"
              aria-label="AOC 拟人数字生命体与四大外接器官状态图"
            >
              <defs>
                <radialGradient id="aoc-core-aura" cx="50%" cy="46%" r="50%">
                  <stop offset="0%" stopColor={mood.auraFrom} stopOpacity="0.32" />
                  <stop offset="60%" stopColor={mood.auraTo} stopOpacity="0.10" />
                  <stop offset="100%" stopColor={mood.auraTo} stopOpacity="0" />
                </radialGradient>
                <linearGradient id="aoc-body-grad" x1="0%" y1="0%" x2="0%" y2="100%">
                  <stop offset="0%" stopColor="#38bdf8" stopOpacity="0.22" />
                  <stop offset="50%" stopColor="#6366f1" stopOpacity="0.16" />
                  <stop offset="100%" stopColor="#0f172a" stopOpacity="0.28" />
                </linearGradient>
                <linearGradient id="aoc-outline-grad" x1="0%" y1="0%" x2="100%" y2="100%">
                  <stop offset="0%" stopColor="#22d3ee" stopOpacity="0.85" />
                  <stop offset="50%" stopColor="#818cf8" stopOpacity="0.65" />
                  <stop offset="100%" stopColor="#10b981" stopOpacity="0.8" />
                </linearGradient>
              </defs>

              {/* Ambient Bio-Aura */}
              <circle cx="160" cy="175" r="145" fill="url(#aoc-core-aura)" />
              <circle
                cx="160"
                cy="175"
                r="128"
                fill="none"
                stroke="currentColor"
                className="text-slate-300/40 dark:text-slate-700/40 aoc-spin-slow"
                strokeWidth="1"
                strokeDasharray="3 9"
              />

              {/* Neural Connection Lines to 4 Pluggable Organs */}
              {/* To Brain (top-left) */}
              <path
                d="M 160 44 C 130 22, 65 26, 14 36"
                fill="none"
                stroke={brainHealthy ? '#38bdf8' : '#f43f5e'}
                strokeWidth={focused === 'brain' ? 2.6 : 1.5}
                className="aoc-flow-line"
                opacity={focused === 'brain' ? 1 : 0.65}
              />
              {/* To Perception (mid-left) */}
              <path
                d="M 126 78 C 88 86, 52 135, 14 176"
                fill="none"
                stroke={perceptionHealthy ? '#14b8a6' : '#94a3b8'}
                strokeWidth={focused === 'perception' ? 2.6 : 1.5}
                className="aoc-flow-line"
                opacity={focused === 'perception' ? 1 : 0.65}
              />
              {/* To Memory (top-right) */}
              <path
                d="M 192 78 C 232 68, 266 56, 306 54"
                fill="none"
                stroke={memoryHealthy ? '#a855f7' : '#f43f5e'}
                strokeWidth={focused === 'memory' ? 2.6 : 1.5}
                className="aoc-flow-line"
                opacity={focused === 'memory' ? 1 : 0.65}
              />
              {/* To Action (mid-right from hand/arm) */}
              <path
                d="M 244 236 C 272 232, 286 218, 306 208"
                fill="none"
                stroke={actionHealthy ? '#10b981' : '#f59e0b'}
                strokeWidth={focused === 'action' ? 2.6 : 1.5}
                className="aoc-flow-line"
                opacity={focused === 'action' ? 1 : 0.65}
              />

              {/* Stylized Cyber-Organic Humanoid Silhouette */}
              {/* Head */}
              <g
                className="cursor-pointer"
                onMouseEnter={() => setFocused('brain')}
                onClick={() => nav('mind')}
              >
                <path
                  d="M 160 32 C 182 32, 194 48, 193 70 C 192 92, 179 108, 160 110 C 141 108, 128 92, 127 70 C 126 48, 138 32, 160 32 Z"
                  fill="url(#aoc-body-grad)"
                  stroke="url(#aoc-outline-grad)"
                  strokeWidth="1.8"
                />
                {/* Neural Crown */}
                <path
                  d="M 142 44 Q 160 35 178 44"
                  fill="none"
                  stroke="#38bdf8"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                />
                <circle cx="160" cy="39" r="3" fill={brainHealthy ? '#38bdf8' : '#f43f5e'} />
              </g>

              {/* Sensory Visor / Eyes (Perception hotspot) */}
              <g
                className="cursor-pointer"
                onMouseEnter={() => setFocused('perception')}
                onClick={() => nav('world')}
              >
                <rect
                  x="136"
                  y="64"
                  width="48"
                  height="14"
                  rx="7"
                  fill="#0f172a"
                  fillOpacity="0.72"
                  stroke={perceptionHealthy ? '#2dd4bf' : '#64748b'}
                  strokeWidth="1.3"
                />
                <circle cx="150" cy="71" r="3" fill={mood.accent} />
                <circle cx="170" cy="71" r="3" fill={mood.accent} />
                <line x1="154" y1="71" x2="166" y2="71" stroke={mood.accent} strokeWidth="1" opacity="0.6" />
              </g>

              {/* Neck & Spine Memory Bus */}
              <path
                d="M 149 110 L 149 124 L 171 124 L 171 110"
                fill="url(#aoc-body-grad)"
                stroke="url(#aoc-outline-grad)"
                strokeWidth="1.5"
              />
              <g
                className="cursor-pointer"
                onMouseEnter={() => setFocused('memory')}
                onClick={() => nav('memory')}
              >
                <line x1="160" y1="112" x2="160" y2="140" stroke="#a855f7" strokeWidth="2" strokeDasharray="3 3" />
                <circle cx="187" cy="80" r="3.5" fill={memoryHealthy ? '#a855f7' : '#f43f5e'} />
              </g>

              {/* Torso & Shoulders */}
              <path
                d="M 149 124 L 106 136 C 94 140, 88 152, 86 168 L 76 234 C 75 244, 82 250, 90 248 L 102 244 L 111 182 L 117 268 C 118 286, 134 298, 160 300 C 186 298, 202 286, 203 268 L 209 182 L 218 244 L 230 248 C 238 250, 245 244, 244 234 L 234 168 C 232 152, 226 140, 214 136 L 171 124 Z"
                fill="url(#aoc-body-grad)"
                stroke="url(#aoc-outline-grad)"
                strokeWidth="1.8"
                strokeLinejoin="round"
              />

              {/* Actuator Hand Hotspots (Action / External Agents) */}
              <g
                className="cursor-pointer"
                onMouseEnter={() => setFocused('action')}
                onClick={() => nav('tasks')}
              >
                <circle cx="85" cy="240" r="6" fill={actionHealthy ? '#10b981' : '#f59e0b'} fillOpacity="0.25" stroke={actionHealthy ? '#10b981' : '#f59e0b'} strokeWidth="1.5" />
                <circle cx="235" cy="240" r="6" fill={actionHealthy ? '#10b981' : '#f59e0b'} fillOpacity="0.25" stroke={actionHealthy ? '#10b981' : '#f59e0b'} strokeWidth="1.5" />
                <circle cx="85" cy="240" r="2.3" fill={actionHealthy ? '#10b981' : '#f59e0b'} />
                <circle cx="235" cy="240" r="2.3" fill={actionHealthy ? '#10b981' : '#f59e0b'} />
              </g>

              {/* Chest Tri-Drive Bio-Core (Survival + Reproduction + Curiosity real SVG gauges!) */}
              <g
                className="cursor-pointer"
                onMouseEnter={() => setFocused('kernel')}
                onClick={() => nav('drives')}
              >
                {/* Background dark core cavity */}
                <circle cx="160" cy="202" r="46" fill="#0f172a" fillOpacity="0.72" stroke="rgba(148,163,184,0.25)" strokeWidth="1" />

                {/* Track rings */}
                <circle cx="160" cy="202" r="39" fill="none" stroke="rgba(16,185,129,0.18)" strokeWidth="4.5" />
                <circle cx="160" cy="202" r="31" fill="none" stroke="rgba(139,92,246,0.18)" strokeWidth="4.5" />
                <circle cx="160" cy="202" r="23" fill="none" stroke="rgba(6,182,212,0.18)" strokeWidth="4.5" />

                {/* Live Drive Arcs (rotated -90deg so 12 o'clock is start) */}
                <g transform="rotate(-90 160 202)">
                  <circle
                    cx="160"
                    cy="202"
                    r="39"
                    fill="none"
                    stroke="#10b981"
                    strokeWidth="4.5"
                    strokeLinecap="round"
                    {...ringProps(39, surv.satisfaction)}
                  />
                  <circle
                    cx="160"
                    cy="202"
                    r="31"
                    fill="none"
                    stroke="#8b5cf6"
                    strokeWidth="4.5"
                    strokeLinecap="round"
                    {...ringProps(31, repro.satisfaction)}
                  />
                  <circle
                    cx="160"
                    cy="202"
                    r="23"
                    fill="none"
                    stroke="#06b6d4"
                    strokeWidth="4.5"
                    strokeLinecap="round"
                    {...ringProps(23, cur.satisfaction)}
                  />
                </g>

                {/* Breathing Emotional Heart Nucleus */}
                <circle
                  cx="160"
                  cy="202"
                  r="13"
                  fill={mood.accent}
                  fillOpacity="0.28"
                  className="aoc-breathe"
                  style={{ animationDuration: `${mood.pulseSec}s` }}
                />
                <circle
                  cx="160"
                  cy="202"
                  r="6.5"
                  fill={mood.accent}
                  className="aoc-breathe"
                  style={{ animationDuration: `${mood.pulseSec}s` }}
                />
              </g>

              {/* Lower Evolution / RSI DNA Node */}
              <g
                className="cursor-pointer"
                onMouseEnter={() => setFocused('evolution')}
                onClick={() => nav('self-model')}
              >
                <line x1="160" y1="250" x2="160" y2="316" stroke="#8b5cf6" strokeWidth="1.6" strokeDasharray="3 4" />
                <rect
                  x="104"
                  y="316"
                  width="112"
                  height="26"
                  rx="13"
                  fill="#0f172a"
                  fillOpacity="0.78"
                  stroke="#8b5cf6"
                  strokeWidth="1.2"
                />
                <text
                  x="160"
                  y="333"
                  textAnchor="middle"
                  fill="#e2e8f0"
                  fontSize="10"
                  fontFamily="var(--font-mono)"
                >
                  RSI · {evoInfo.skills_validated} Skills
                </text>
              </g>

              {/* Minimal Labels near endpoints */}
              <text x="16" y="27" fill="#38bdf8" fontSize="9.5" fontFamily="var(--font-mono)">BRAIN</text>
              <text x="16" y="168" fill="#14b8a6" fontSize="9.5" fontFamily="var(--font-mono)">SENSE</text>
              <text x="262" y="45" fill="#a855f7" fontSize="9.5" fontFamily="var(--font-mono)">MEMORY</text>
              <text x="264" y="199" fill="#10b981" fontSize="9.5" fontFamily="var(--font-mono)">ACTION</text>
            </svg>
          </div>

          {/* Tri-Drive Legend + Live 5-Dim Affect Strip */}
          <div className="w-full space-y-2.5">
            <div className="grid grid-cols-3 gap-1.5 text-center text-[10px] font-mono">
              <div className="rounded-lg bg-emerald-500/10 px-1.5 py-1 text-emerald-700 dark:text-emerald-300">
                生存 {formatPct(surv.satisfaction)}
              </div>
              <div className="rounded-lg bg-violet-500/10 px-1.5 py-1 text-violet-700 dark:text-violet-300">
                繁衍 {formatPct(repro.satisfaction)}
              </div>
              <div className="rounded-lg bg-cyan-500/10 px-1.5 py-1 text-cyan-700 dark:text-cyan-300">
                好奇 {formatPct(cur.satisfaction)}
              </div>
            </div>

            {affect?.enabled && affect?.current_state && (
              <div className="flex flex-wrap items-center justify-center gap-x-2.5 gap-y-1 rounded-lg bg-slate-100/80 px-2.5 py-1.5 text-[10px] font-mono text-slate-600 dark:bg-slate-950/70 dark:text-slate-400">
                <span>V {Number(affect.current_state.valence ?? 0).toFixed(2)}</span>
                <span>A {Number(affect.current_state.arousal ?? 0).toFixed(2)}</span>
                <span>C {Number(affect.current_state.confidence ?? 0).toFixed(2)}</span>
                <span>F {Number(affect.current_state.frustration ?? 0).toFixed(2)}</span>
                <span>B {Number(affect.current_state.boredom ?? 0).toFixed(2)}</span>
              </div>
            )}
          </div>
        </div>

        {/* RIGHT COLUMN: Memory + Action */}
        <div className="xl:col-span-4 flex flex-col justify-between gap-4">
          {/* 3. External Memory (Letta + pgvector) */}
          <button
            type="button"
            onMouseEnter={() => setFocused('memory')}
            onClick={() => nav('memory')}
            className={cardRing('memory')}
          >
            <div className="flex items-start justify-between gap-2">
              <div className="flex items-center gap-2">
                <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-purple-500/15 text-purple-600 dark:text-purple-400">
                  <Database className="h-4 w-4" />
                </span>
                <div>
                  <div className="text-xs font-semibold text-slate-900 dark:text-slate-100">
                    外接记忆 · Memory (Letta + pgvector)
                  </div>
                  <div className="text-[10px] font-mono text-slate-400">HIPPOCAMPUS / LONG-TERM PORT</div>
                </div>
              </div>
              <div className="flex items-center gap-1.5">
                <StatusBadge value={memoryHealthy ? 'ok' : 'down'} label={memoryHealthy ? '已连接' : '降级'} />
                <ArrowUpRight className="h-3.5 w-3.5 text-slate-400 group-hover:text-cyan-500 transition-colors" />
              </div>
            </div>
            <div className="mt-3 grid grid-cols-3 gap-2 text-[11px]">
              <div className="rounded-lg bg-slate-100/70 p-2 dark:bg-slate-950/60">
                <div className="text-[10px] text-slate-400">Letta 同步</div>
                <div className="font-mono font-semibold text-purple-600 dark:text-purple-400">
                  {memoryInfo.memories_synced}/{memoryInfo.memories_total}
                </div>
              </div>
              <div className="rounded-lg bg-slate-100/70 p-2 dark:bg-slate-950/60">
                <div className="text-[10px] text-slate-400">向量索引</div>
                <div className="font-mono font-semibold text-slate-800 dark:text-slate-200">
                  {memoryInfo.embedding_dim}d · {memoryInfo.embedded_count}
                </div>
              </div>
              <div className="rounded-lg bg-slate-100/70 p-2 dark:bg-slate-950/60">
                <div className="text-[10px] text-slate-400">Outbox 积压</div>
                <div
                  className={cn(
                    'font-mono font-semibold',
                    memoryInfo.outbox_pending > 0 ? 'text-amber-500' : 'text-emerald-600 dark:text-emerald-400',
                  )}
                >
                  {memoryInfo.outbox_pending}
                </div>
              </div>
            </div>
            <div className="mt-2 flex items-center gap-2 text-[10px] font-mono text-slate-500">
              <span>pgvector: {memoryInfo.pgvector_enabled ? 'HNSW 已启用' : 'JSONB'}</span>
              <span>·</span>
              <span>Embed: {memoryInfo.embedding_provider}</span>
            </div>
          </button>

          {/* 4. External Action (Host Runner + CLI Agents) */}
          <button
            type="button"
            onMouseEnter={() => setFocused('action')}
            onClick={() => nav('tasks')}
            className={cardRing('action')}
          >
            <div className="flex items-start justify-between gap-2">
              <div className="flex items-center gap-2">
                <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-emerald-500/15 text-emerald-600 dark:text-emerald-400">
                  <Wrench className="h-4 w-4" />
                </span>
                <div>
                  <div className="text-xs font-semibold text-slate-900 dark:text-slate-100">
                    外接行动 · Action (Runner / Agents)
                  </div>
                  <div className="text-[10px] font-mono text-slate-400">LIMBS / EXECUTION PORT</div>
                </div>
              </div>
              <div className="flex items-center gap-1.5">
                <StatusBadge value={actionHealthy ? 'online' : 'offline'} label={actionHealthy ? '就绪' : '离线'} />
                <ArrowUpRight className="h-3.5 w-3.5 text-slate-400 group-hover:text-cyan-500 transition-colors" />
              </div>
            </div>
            <div className="mt-3 grid grid-cols-3 gap-2 text-[11px]">
              <div className="rounded-lg bg-slate-100/70 p-2 dark:bg-slate-950/60">
                <div className="text-[10px] text-slate-400">健康 Agent</div>
                <div className="font-mono font-semibold text-emerald-600 dark:text-emerald-400">
                  {actionInfo.agents_healthy}/{actionInfo.agents_total}
                </div>
              </div>
              <div className="rounded-lg bg-slate-100/70 p-2 dark:bg-slate-950/60">
                <div className="text-[10px] text-slate-400">窗口任务</div>
                <div className="font-mono font-semibold text-slate-800 dark:text-slate-200">
                  {formatNumber(actionInfo.tasks_window)}
                </div>
              </div>
              <div className="rounded-lg bg-slate-100/70 p-2 dark:bg-slate-950/60">
                <div className="text-[10px] text-slate-400">成功 / 失败</div>
                <div className="font-mono font-semibold text-slate-800 dark:text-slate-200">
                  {actionInfo.tasks_succeeded}/{actionInfo.tasks_failed}
                </div>
              </div>
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[10px] font-mono">
              <span className="rounded bg-slate-200/70 px-1.5 py-0.5 text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                Runner: {actionInfo.runner_id}
              </span>
              {(actionInfo.agents || [])
                .filter((a: any) => a.enabled)
                .map((a: any) => (
                  <span
                    key={a.agent_id}
                    className={cn(
                      'rounded px-1.5 py-0.5',
                      a.healthy
                        ? 'bg-emerald-500/12 text-emerald-700 dark:text-emerald-300'
                        : 'bg-rose-500/12 text-rose-600 dark:text-rose-300',
                    )}
                  >
                    {a.agent_id} {a.version ? `(${a.version})` : ''}
                  </span>
                ))}
            </div>
          </button>
        </div>
      </div>

      {/* Bottom Evolution & Governance Guard Bar */}
      <div className="grid grid-cols-1 gap-3 border-t border-slate-200/70 bg-slate-50/70 px-5 py-3.5 sm:grid-cols-2 lg:grid-cols-4 dark:border-slate-800/80 dark:bg-slate-950/60">
        <button
          type="button"
          onMouseEnter={() => setFocused('evolution')}
          onClick={() => nav('self-model')}
          className="flex items-center justify-between rounded-xl border border-slate-200/80 bg-white/80 px-3.5 py-2.5 text-left hover:border-violet-400/60 transition-colors cursor-pointer dark:border-slate-800 dark:bg-slate-900/70"
        >
          <div>
            <div className="text-[10px] text-slate-500">实证能力画像（Self Model）</div>
            <div className="mt-0.5 text-sm font-semibold font-mono text-violet-600 dark:text-violet-400">
              {evoInfo.capabilities_validated} 已验证 · {evoInfo.capabilities_acquired} 已习得
            </div>
          </div>
          <Dna className="h-4 w-4 text-violet-500 shrink-0" />
        </button>

        <button
          type="button"
          onMouseEnter={() => setFocused('evolution')}
          onClick={() => nav('self-model')}
          className="flex items-center justify-between rounded-xl border border-slate-200/80 bg-white/80 px-3.5 py-2.5 text-left hover:border-cyan-400/60 transition-colors cursor-pointer dark:border-slate-800 dark:bg-slate-900/70"
        >
          <div>
            <div className="text-[10px] text-slate-500">RSI 技能进化与复用（Skill Runtime）</div>
            <div className="mt-0.5 text-sm font-semibold font-mono text-cyan-600 dark:text-cyan-400">
              {evoInfo.skills_validated} 有效 / {evoInfo.skills_total} 总 · 复用 {evoInfo.skill_cross_task_reuses_window || 0} 次
            </div>
            <div className="mt-0.5 text-[10px] font-mono text-slate-400">
              省 {formatNumber(evoInfo.estimated_saved_tokens_window || 0)} Tok · Playbook {evoInfo.playbooks_active_count || 0}
            </div>
          </div>
          <Cpu className="h-4 w-4 text-cyan-500 shrink-0" />
        </button>

        <button
          type="button"
          onClick={() => nav('world')}
          className="flex items-center justify-between rounded-xl border border-slate-200/80 bg-white/80 px-3.5 py-2.5 text-left hover:border-emerald-400/60 transition-colors cursor-pointer dark:border-slate-800 dark:bg-slate-900/70"
        >
          <div>
            <div className="text-[10px] text-slate-500">认知效率与外部探索（IG / 1M Tok）</div>
            <div className="mt-0.5 text-sm font-semibold font-mono text-emerald-600 dark:text-emerald-400">
              {Number(evoInfo.verified_ig_per_1m_tokens || 0).toFixed(2)}
            </div>
            <div className="mt-0.5 text-[10px] font-mono text-slate-400">
              外部探索 {evoInfo.exploration_topics_verified || 0}/{evoInfo.exploration_topics_total || 0} 已验证
            </div>
          </div>
          <ArrowUpRight className="h-4 w-4 text-emerald-500 shrink-0" />
        </button>

        <button
          type="button"
          onClick={() => nav('governance')}
          className="flex items-center justify-between rounded-xl border border-slate-200/80 bg-white/80 px-3.5 py-2.5 text-left hover:border-amber-400/60 transition-colors cursor-pointer dark:border-slate-800 dark:bg-slate-900/70"
        >
          <div>
            <div className="text-[10px] text-slate-500">内核护栏拦截（预检 / 去重）</div>
            <div className="mt-0.5 text-sm font-semibold font-mono text-slate-800 dark:text-slate-200">
              {evoInfo.preflight_rejected_window} · {evoInfo.goal_dedup_skipped_window}
            </div>
          </div>
          <ShieldCheck className="h-4 w-4 text-amber-500 shrink-0" />
        </button>
      </div>
    </Card>
  );
}
