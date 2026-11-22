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
});
