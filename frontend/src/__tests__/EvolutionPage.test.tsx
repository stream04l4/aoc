import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { EvolutionPage } from '../pages/EvolutionPage';
import { clearStoredToken, setStoredToken } from '../api/client';

describe('V5.0 EvolutionPage UI', () => {
  beforeEach(() => {
    clearStoredToken();
    setStoredToken('tok-evo-admin');
    vi.restoreAllMocks();
  });

  it('renders entropy, minimal constitution boundary, promotes candidate & confirms rollback', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.includes('/v2/control-center/evolution')) {
        return new Response(
          JSON.stringify({
            summary: {
              active_generation: 0,
              active_instance_key: 'gen0-001',
              live_instances: 2,
              max_concurrent_instances: 2,
              promoted_count: 0,
              culled_count: 1,
              rolled_back_count: 0,
              can_rollback: true,
            },
            entropy: {
              entropy_index: 0.62,
              metrics: {
                skill_deprecation_ratio: 0.93,
                skill_non_reuse_ratio: 0.5,
                hypothesis_backlog_ratio: 0.47,
                playbook_bloat_score: 0.86,
                goal_waste_ratio: 0.25,
                ig_token_efficiency_drop: 0.1,
              },
            },
            constitution: {
              constitutional_file_count: 19,
              constitutional_files: { 'app/drives/instinct_guard.py': 'abc123' },
              drive_evolvable_files: ['app/drives/reproduction.py', 'config/drives.yaml'],
            },
            instances: [
              {
                id: 'inst-0',
                instance_key: 'gen0-001',
                generation: 0,
                role: 'RETIRED_ARCHIVE',
                status: 'RETIRED',
                db_schema: 'public',
                blank_slate_verified: true,
                created_at: '2026-11-24T09:00:00Z',
              },
              {
                id: 'inst-1',
                instance_key: 'gen1-win',
                generation: 1,
                role: 'PRIMARY',
                status: 'ACTIVE',
                db_schema: 'aoc_inst_win01',
                blank_slate_verified: true,
                created_at: '2026-11-24T13:00:00Z',
              },
            ],
            proposals: [
              {
                id: 'prop-1',
                proposal_key: 'prop-gen1-ready',
                target_generation: 1,
                trigger_type: 'ENTROPY_PRESSURE',
                hypothesis: 'Incorporate skill deprecation ratio into reproduction metric',
                stage: 'STAGE4_PENDING_PROMOTION',
                genome_delta: {
                  files: ['app/drives/reproduction.py'],
                  mutates_drive_metrics: true,
                },
                arena_result: {
                  comparison: { fitness_delta: 12.5, promotion_eligible: true },
                },
              },
            ],
            lineage: [
              {
                id: 'lin-1',
                generation: 1,
                outcome: 'CULLED',
                parent_fitness: 72.0,
                offspring_fitness: 64.0,
                fitness_delta: -8.0,
                drive_metric_changes: { mutates_drive_metrics: true },
                lesson_summary: { outcome_reason: 'Mediocre gain below +5 threshold' },
                created_at: '2026-11-24T12:00:00Z',
              },
            ],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.includes('/v5/evolution/proposals/prop-gen1-ready/promote')) {
        expect(init?.method).toBe('POST');
        return new Response(
          JSON.stringify({ success: true, action: 'PROMOTED', generation: 1, new_primary_key: 'gen1-win' }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.includes('/v5/evolution/rollback')) {
        return new Response(
          JSON.stringify({ success: true, action: 'ROLLED_BACK', generation: 0, new_primary_key: 'gen0-001' }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } });
    });

    render(
      <EvolutionPage
        refreshTick={0}
        principal={{ name: 'joel-admin', scopes: ['admin', 'read', 'approve'], source: 'env' }}
      />
    );

    expect(await screen.findByText('运行期客观熵增 6 维感知（EntropySensor）')).toBeInTheDocument();
    expect(screen.getByText('极小化不可变宪法 vs. 欲望指标演化边界')).toBeInTheDocument();
    expect(screen.getByText('欲望评估指标进化')).toBeInTheDocument();
    expect(screen.getByText('Mediocre gain below +5 threshold')).toBeInTheDocument();

    // Promote button dispatches POST /v5/evolution/proposals/{key}/promote
    fireEvent.click(screen.getByText('确认晋升新一代'));
    await waitFor(() => {
      expect(screen.getByText(/已执行：确认晋升新一代/)).toBeInTheDocument();
    });

    // Two-step rollback confirmation button dispatches POST /v5/evolution/rollback
    fireEvent.click(screen.getByText('一键回滚上一代'));
    fireEvent.click(await screen.findByText('确认回滚至上一代'));
    await waitFor(() => {
      expect(screen.getByText(/已成功回滚至 Gen 0/)).toBeInTheDocument();
    });
    expect(fetchMock).toHaveBeenCalled();
  });
});
