// dashboard/src/components/CostBreakdown.jsx
// Per-step cost breakdown using /api/runs/{id}/cost-breakdown
import React, { useEffect, useState, useCallback } from 'react';
import { DollarSign, Zap } from 'lucide-react';

const API = 'http://localhost:8000';

const STEP_COLORS = {
  explorer: '#6366f1',
  planner: '#8b5cf6',
  coder: '#06b6d4',
  tester: '#f59e0b',
  reviewer: '#10b981',
  pr_creator: '#ec4899',
};

export default function CostBreakdown({ runId }) {
  const [breakdown, setBreakdown] = useState(null);

  const fetchBreakdown = useCallback(async () => {
    if (!runId) return;
    try {
      const res = await fetch(`${API}/api/runs/${runId}/cost-breakdown`);
      if (res.ok) setBreakdown(await res.json());
    } catch {}
  }, [runId]);

  useEffect(() => {
    fetchBreakdown();
  }, [runId, fetchBreakdown]);

  if (!breakdown || !breakdown.steps?.length) return null;

  const maxCost = Math.max(...breakdown.steps.map(s => s.llm_cost_cents), 0.001);

  return (
    <div style={{ marginTop: '12px' }}>
      <div style={{
        fontSize: '10px',
        color: 'rgba(255,255,255,0.35)',
        textTransform: 'uppercase',
        letterSpacing: '0.08em',
        marginBottom: '8px',
        display: 'flex',
        alignItems: 'center',
        gap: '5px',
      }}>
        <DollarSign size={10} />
        Cost Breakdown
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: '5px' }}>
        {breakdown.steps.map((step, i) => {
          const pct = (step.llm_cost_cents / maxCost) * 100;
          const color = STEP_COLORS[step.step_name] || '#a5b4fc';
          return (
            <div key={i} style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '10px' }}>
                <span style={{ color: 'rgba(255,255,255,0.6)' }}>{step.step_name}</span>
                <div style={{ display: 'flex', gap: '8px', color: 'rgba(255,255,255,0.4)' }}>
                  <span style={{ color }}>
                    ${step.llm_cost_dollars?.toFixed(4) || '0.0000'}
                  </span>
                  {step.total_tokens > 0 && (
                    <span style={{ display: 'flex', alignItems: 'center', gap: '2px' }}>
                      <Zap size={8} />
                      {step.total_tokens.toLocaleString()}
                    </span>
                  )}
                </div>
              </div>
              <div style={{ height: '4px', background: 'rgba(255,255,255,0.06)', borderRadius: '99px', overflow: 'hidden' }}>
                <div
                  style={{
                    width: `${pct}%`,
                    height: '100%',
                    background: color,
                    borderRadius: '99px',
                    opacity: step.llm_cost_cents > 0 ? 1 : 0.2,
                    transition: 'width 0.4s ease',
                  }}
                />
              </div>
            </div>
          );
        })}
      </div>

      <div style={{
        marginTop: '8px',
        paddingTop: '8px',
        borderTop: '1px solid rgba(255,255,255,0.06)',
        display: 'flex',
        justifyContent: 'space-between',
        fontSize: '10px',
        color: 'rgba(255,255,255,0.3)',
      }}>
        <span>Total</span>
        <span style={{ color: '#fbbf24', fontWeight: '600' }}>
          ${(breakdown.total_cost_cents / 100).toFixed(4)}
        </span>
      </div>
    </div>
  );
}
