import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { App } from '../App';
import { clearStoredToken } from '../api/client';

describe('AOC Control Center UI', () => {
  beforeEach(() => {
    clearStoredToken();
    vi.restoreAllMocks();
  });

  it('shows Bearer Token AuthGate when unauthenticated and logs in via /v2/auth/me', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes('/v2/auth/me')) {
        return new Response(
          JSON.stringify({ name: 'joel-admin', scopes: ['admin', 'read'], source: 'env_admin' }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.includes('/v2/control-center/overview')) {
        return new Response(
          JSON.stringify({
            window_hours: 24,
            organism: {
              name: 'AOC-001',
              lifecycle_state: 'ACTIVE',
              cycle_count: 131,
              scheduler_running: true,
              affect_enabled: false,
            },
            latest_cycle: {
              cycle_id: '00000000-0000-0000-0000-000000000001',
              cycle_number: 131,
              selected_drive: 'curiosity',
              arbitration_reason: 'Open curiosity deficit',
            },
            drives: [
              { drive_type: 'survival', satisfaction: 0.92, priority: 0.12, top_deficits: [] },
              { drive_type: 'reproduction', satisfaction: 0.65, priority: 0.35, top_deficits: [] },
              { drive_type: 'curiosity', satisfaction: 0.58, priority: 0.42, top_deficits: ['open topics'] },
            ],
            drive_trend: [],
            active_goals: [],
            recent_tasks: [],
            recent_decisions: [],
            recent_events: [],
            components: {
              core: { status: 'ok', rss_mb: 224.5 },
              postgres: { status: 'ok', pgvector_enabled: false },
              runner: { enabled: true, runners: [{ runner_id: 'host-runner-1', status: 'online' }] },
              llm: { model: 'Qwen/Qwen3.8-27B-FP8', available: true, circuit_open: false },
              letta: { available: true, memories_synced: 183, memories_total: 183 },
              embedding: { provider: 'local_onnx', dimensions: 512, embedded_count: 179 },
            },
            stats: {
              cycles_total_window: 87,
              cycles_by_outcome: { SUCCEEDED: 53, ERROR: 0, IDLE: 1 },
              tasks_total_window: 72,
              tasks_by_status: { SUCCEEDED: 69, FAILED: 3 },
              usage_by_category: { llm: { calls: 391, total_amount: 9548905, avg_seconds: 38.1 } },
            },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.includes('/v2/control-center/config')) {
        return new Response(
          JSON.stringify({
            read_only: true,
            generated_at: '2026-11-21T09:00:00Z',
            domains: [
              {
                id: 'governance_approval',
                name: 'Governance 与 Approval',
                description: '权限分级',
                items: [
                  {
                    key: 'governance.auto_approve_max_level',
                    label: '自动放行最高权限等级',
                    description: '自动放行等级',
                    effective_value: 'L2',
                    source: 'yaml',
                    overridden: true,
                    requires_restart: true,
                    sensitive: false,
                    risk_note: '当前已放开至 L2',
                  },
                  {
                    key: 'api.admin_key',
                    label: '管理员 API Key',
                    description: '脱敏密钥',
                    effective_value: '***REDACTED***',
                    source: 'env',
                    overridden: true,
                    requires_restart: true,
                    sensitive: true,
                    risk_note: null,
                  },
                ],
              },
            ],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      return new Response(JSON.stringify({ total: 0, limit: 20, offset: 0, items: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    render(<App />);
    expect(await screen.findByText('验证并进入 Control Center')).toBeInTheDocument();

    const input = screen.getByPlaceholderText(/输入 AOC_API__ADMIN_KEY/i);
    fireEvent.change(input, { target: { value: 'test-bearer-tok' } });
    fireEvent.click(screen.getByText('验证并进入 Control Center'));

    await waitFor(() => {
      expect(screen.getByText('AOC-001')).toBeInTheDocument();
      expect(screen.getByText('224.5 MB')).toBeInTheDocument();
      // V3.2 Humanoid Organism & Four Pluggable Organs centerpiece
      expect(screen.getByRole('img', { name: /AOC 拟人数字生命体/ })).toBeInTheDocument();
      expect(screen.getByText('外接大脑 · Brain (LLM)')).toBeInTheDocument();
      expect(screen.getByText(/外接感知 · Perception/)).toBeInTheDocument();
      expect(screen.getByText(/外接记忆 · Memory/)).toBeInTheDocument();
      expect(screen.getByText(/外接行动 · Action/)).toBeInTheDocument();
      expect(screen.getByText(/类人生命内核 · AOC-001/)).toBeInTheDocument();
    });

    // Navigate to Configuration Center and verify redaction & risk note render
    fireEvent.click(screen.getByText('配置中心'));
    await waitFor(() => {
      expect(screen.getByText('***REDACTED***')).toBeInTheDocument();
      expect(screen.getByText('当前已放开至 L2')).toBeInTheDocument();
    });

    expect(fetchMock).toHaveBeenCalled();
  });

  it('enforces read-only UI for SCOPE_READ users and renders explicit 未记录 for null Mind steps', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes('/v2/auth/me')) {
        return new Response(
          JSON.stringify({ name: 'auditor-ro', scopes: ['read'], source: 'db_credential' }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.includes('/v2/control-center/overview')) {
        return new Response(
          JSON.stringify({
            window_hours: 24,
            organism: { name: 'AOC-001', lifecycle_state: 'ACTIVE', cycle_count: 131, scheduler_running: true },
            latest_cycle: { cycle_id: 'cid-54', cycle_number: 54, selected_drive: 'curiosity' },
            drives: [],
            drive_trend: [],
            active_goals: [],
            recent_tasks: [],
            recent_decisions: [],
            recent_events: [],
            components: {
              core: { status: 'ok', rss_mb: 180.2 },
              postgres: { status: 'ok', pgvector_enabled: false },
              runner: { enabled: true, runners: [] },
              llm: { model: 'Qwen/Qwen3.8-27B-FP8', available: true, circuit_open: false },
              letta: { available: true, memories_synced: 10, memories_total: 10 },
              embedding: { provider: 'local_onnx', dimensions: 512, embedded_count: 10 },
            },
            stats: {
              cycles_total_window: 10,
              cycles_by_outcome: { SUCCEEDED: 9 },
              tasks_total_window: 8,
              tasks_by_status: { SUCCEEDED: 8 },
              usage_by_category: {},
            },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.includes('/v2/control-center/cycles/cid-54/trace')) {
        return new Response(
          JSON.stringify({
            cycle: {
              cycle_id: 'cid-54',
              cycle_number: 54,
              lifecycle_state: 'ACTIVE',
              outcome: 'SUCCEEDED',
              started_at: '2026-11-21T03:00:00Z',
              duration_seconds: 21.5,
            },
            steps: {
              '1_perception': { observation_id: 'obs-1', phase: 'pre' },
              '2_drive_assessment': null,
              '3_arbitration': { selected_drive: 'curiosity', reason: 'deficit' },
              '4_brain_assessment': { summary: 'Structured assessment from DB' },
              '5_goal_generation': null,
              '6_planning': null,
              '7_governance': null,
              '8_agent_dispatch': null,
              '9_result_verification': null,
              '10_outcome_evaluation': null,
              '11_post_perception': null,
            },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.includes('/v2/control-center/cycles')) {
        return new Response(
          JSON.stringify({
            total: 1,
            limit: 20,
            offset: 0,
            items: [
              {
                cycle_id: 'cid-54',
                cycle_number: 54,
                outcome: 'SUCCEEDED',
                selected_drive: 'curiosity',
                goal_title: 'Investigate real data',
                started_at: '2026-11-21T03:00:00Z',
                duration_seconds: 21.5,
                llm_tokens: 4200,
              },
            ],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      return new Response(JSON.stringify({ total: 0, limit: 20, offset: 0, items: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    render(<App />);
    fireEvent.change(await screen.findByPlaceholderText(/输入 AOC_API__ADMIN_KEY/i), {
      target: { value: 'aoc_r_readonly_token' },
    });
    fireEvent.click(screen.getByText('验证并进入 Control Center'));

    await waitFor(() => {
      expect(screen.getByText('AOC-001')).toBeInTheDocument();
      expect(screen.getByText('auditor-ro')).toBeInTheDocument();
    });
    // Read-only user must NOT see admin pause/resume control buttons
    expect(screen.queryByText('暂停调度')).not.toBeInTheDocument();
    expect(screen.queryByText('恢复运行')).not.toBeInTheDocument();

    // Open Mind page and confirm unrecorded steps display explicit "未记录"
    fireEvent.click(screen.getByText('认知与决策'));
    await waitFor(() => {
      expect(screen.getByText(/诚实审计声明/)).toBeInTheDocument();
      expect(screen.getAllByText('未记录').length).toBeGreaterThanOrEqual(8);
      expect(screen.getAllByText('已记录').length).toBe(3);
    });
  });

  it('renders V4.0 Skill Reuse, Version Tree, External Playbooks, and External Exploration panels', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes('/v2/auth/me')) {
        return new Response(
          JSON.stringify({ name: 'joel-admin', scopes: ['admin', 'read'], source: 'env_admin' }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.includes('/v2/control-center/overview')) {
        return new Response(
          JSON.stringify({
            window_hours: 24,
            organism: { name: 'AOC-001', lifecycle_state: 'ACTIVE', cycle_count: 700, scheduler_running: true },
            latest_cycle: { cycle_id: 'cid-700', cycle_number: 700, selected_drive: 'curiosity' },
            drives: [],
            drive_trend: [],
            active_goals: [],
            recent_tasks: [],
            recent_decisions: [],
            recent_events: [],
            components: {
              core: { status: 'ok', rss_mb: 210.0 },
              postgres: { status: 'ok', pgvector_enabled: true },
              runner: { enabled: true, runners: [{ runner_id: 'host-runner-1', status: 'online' }] },
              llm: { model: 'Qwen/Qwen3.8-27B-FP8', available: true, circuit_open: false },
              letta: { available: true, memories_synced: 50, memories_total: 50 },
              embedding: { provider: 'local_onnx', dimensions: 512, embedded_count: 50 },
            },
            organs: {
              evolution: {
                capabilities_total: 6,
                capabilities_validated: 4,
                capabilities_acquired: 1,
                skills_total: 12,
                skills_validated: 8,
                skill_cross_task_reuses_window: 14,
                estimated_saved_tokens_window: 168000,
                playbooks_active_count: 3,
                exploration_topics_total: 5,
                exploration_topics_verified: 4,
                verified_ig_per_1m_tokens: 5.2,
                preflight_rejected_window: 0,
                goal_dedup_skipped_window: 2,
              },
            },
            stats: {
              cycles_total_window: 20,
              cycles_by_outcome: { SUCCEEDED: 18 },
              tasks_total_window: 15,
              tasks_by_status: { SUCCEEDED: 15 },
              usage_by_category: {},
            },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.includes('/v2/control-center/self-model')) {
        return new Response(
          JSON.stringify({
            identity: { id: 'org-1', name: 'AOC-001', version: '4.0.0' },
            latest_snapshot: { version_number: 12, summary_text: 'V4 RSI snapshot' },
            capabilities: [],
            recent_evidence: [],
            skills: [],
            skill_reuse_summary: {
              total_invocations: 20,
              cross_task_reuses: 14,
              cross_task_successes: 13,
              reuse_success_rate: 0.9286,
              estimated_saved_tokens: 168000,
              reused_skill_count: 6,
              canonical_families_count: 4,
              recent_invocations: [
                {
                  id: 'inv-1',
                  skill_name: 'data.coding.count_items',
                  skill_version: '1.1.0',
                  invocation_source: 'skill_first',
                  is_cross_task_reuse: true,
                  passed: true,
                  latency_ms: 42.5,
                  estimated_saved_tokens: 12000,
                },
              ],
            },
            skill_version_trees: [
              {
                canonical_name: 'data.coding.count_items',
                version_count: 2,
                total_reuses: 9,
                active_version: '1.1.0',
                versions: [
                  { id: 'v2', version: '1.1.0', parent_version: '1.0.0', status: 'VALIDATED', permission_level: 'L1', invocation_count: 10, reuse_count: 9, reuse_success_count: 9, evolution_meta: {} },
                ],
              },
            ],
            external_playbooks: [
              {
                id: 'pb-1',
                domain: 'curiosity',
                status: 'active',
                active_version: 2,
                version_count: 3,
                rollback_count: 1,
                active_samples: 5,
                active_successes: 5,
                change_summary: 'Verified external research playbook',
              },
            ],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.includes('/v2/control-center/world')) {
        return new Response(
          JSON.stringify({
            poll_on_perceive: true,
            open_public_perception_enabled: true,
            importance_threshold: 0.45,
            allowed_domains: ['arxiv.org', 'blog.pgvector.org'],
            sources: [],
            events: { total: 0, limit: 25, offset: 0, items: [] },
            quarantined_injections: [],
            world_states: [],
            hypotheses: [],
            exploration: {
              total: 2,
              verified_count: 1,
              exploring_count: 1,
              domains_summary: { databases: 1, ai_research: 1 },
              topics: [
                {
                  id: 'et-1',
                  key: 'pgvector_hnsw_tuning',
                  status: 'verified',
                  domain: 'databases',
                  question: 'How does ef_search tune pgvector recall?',
                  attempts: 1,
                  verified_facts: ['pgvector 0.8 iterative_scan prevents empty filtered results'],
                  information_gain: 0.62,
                  source: 'execution:ex-1',
                },
              ],
            },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      return new Response(JSON.stringify({ total: 0, limit: 20, offset: 0, items: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    render(<App />);
    fireEvent.change(await screen.findByPlaceholderText(/输入 AOC_API__ADMIN_KEY/i), {
      target: { value: 'test-admin-tok' },
    });
    fireEvent.click(screen.getByText('验证并进入 Control Center'));

    await waitFor(() => {
      expect(screen.getByText('V4.0 RSI BIO-ARCHITECTURE')).toBeInTheDocument();
      expect(screen.getByText(/复用 14 次/)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText('自我认知'));
    await waitFor(() => {
      expect(screen.getByText('data.coding.count_items')).toBeInTheDocument();
      expect(screen.getByText(/Verified external research playbook/)).toBeInTheDocument();
      expect(screen.getByText('跨任务复用')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText('环境感知'));
    await waitFor(() => {
      expect(screen.getByText('公开 HTTPS 感知扩展 · 强 SSRF 防护')).toBeInTheDocument();
      expect(screen.getByText('pgvector_hnsw_tuning')).toBeInTheDocument();
    });
  });
});
