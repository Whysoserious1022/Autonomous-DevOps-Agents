// dashboard/src/components/CostChart.jsx – Per-step cost breakdown bar chart
import React from 'react';
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from 'recharts';

const STEP_COLORS = {
  explorer:   '#6366f1',
  planner:    '#8b5cf6',
  coder:      '#22d3ee',
  tester:     '#fbbf24',
  reviewer:   '#f87171',
  pr_creator: '#34d399',
};

const CustomTooltip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null;
  return (
    <div style={{
      background: 'rgba(10,14,32,0.95)',
      border: '1px solid rgba(255,255,255,0.1)',
      borderRadius: '8px',
      padding: '10px 12px',
      fontSize: '12px',
    }}>
      <div style={{ fontWeight: 700, color: '#f1f5f9', marginBottom: '4px', textTransform: 'capitalize' }}>
        {label.replace('_', ' ')}
      </div>
      {payload.map(p => (
        <div key={p.name} style={{ color: p.fill || '#94a3b8' }}>
          ${p.value.toFixed(4)}
        </div>
      ))}
    </div>
  );
};

export default function CostChart({ run }) {
  if (!run?.steps) return null;

  const data = Object.entries(run.steps)
    .filter(([, s]) => (s.llm_cost_cents || 0) > 0 || s.status === 'skipped')
    .map(([name, step]) => ({
      name,
      cost: parseFloat(((step.llm_cost_cents || 0) / 100).toFixed(5)),
      skipped: step.status === 'skipped',
    }));

  const totalCost = data.reduce((s, d) => s + d.cost, 0);
  const skippedCount = data.filter(d => d.skipped).length;

  return (
    <div>
      <div className="section-title">Cost Breakdown</div>

      <div style={{ display: 'flex', gap: '8px', marginBottom: '12px' }}>
        <div style={{
          flex: 1,
          padding: '8px 10px',
          background: 'rgba(99,102,241,0.08)',
          border: '1px solid rgba(99,102,241,0.15)',
          borderRadius: '8px',
          textAlign: 'center',
        }}>
          <div style={{ fontSize: '16px', fontWeight: 700, color: '#a5b4fc' }}>
            ${totalCost.toFixed(4)}
          </div>
          <div style={{ fontSize: '9px', color: '#64748b', marginTop: '2px' }}>TOTAL COST</div>
        </div>
        {skippedCount > 0 && (
          <div style={{
            flex: 1,
            padding: '8px 10px',
            background: 'rgba(34,211,238,0.06)',
            border: '1px solid rgba(34,211,238,0.12)',
            borderRadius: '8px',
            textAlign: 'center',
          }}>
            <div style={{ fontSize: '16px', fontWeight: 700, color: '#22d3ee' }}>{skippedCount}</div>
            <div style={{ fontSize: '9px', color: '#64748b', marginTop: '2px' }}>CACHE HITS</div>
          </div>
        )}
      </div>

      {data.length > 0 ? (
        <div style={{ height: '100px' }}>
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data} margin={{ top: 0, right: 0, left: -28, bottom: 0 }}>
              <XAxis
                dataKey="name"
                stroke="#334155"
                tick={{ fill: '#64748b', fontSize: 9 }}
                tickLine={false}
                axisLine={false}
                tickFormatter={n => n.substring(0, 4)}
              />
              <YAxis
                stroke="#334155"
                tick={{ fill: '#64748b', fontSize: 9 }}
                tickLine={false}
                axisLine={false}
                tickFormatter={v => `$${v.toFixed(2)}`}
              />
              <Tooltip content={<CustomTooltip />} />
              <Bar dataKey="cost" radius={[4, 4, 0, 0]}>
                {data.map((d) => (
                  <Cell
                    key={d.name}
                    fill={d.skipped ? 'rgba(34,211,238,0.4)' : (STEP_COLORS[d.name] || '#6366f1')}
                    opacity={d.skipped ? 0.5 : 1}
                  />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <div style={{ fontSize: '11px', color: '#475569', textAlign: 'center', padding: '12px' }}>
          No cost data yet
        </div>
      )}
    </div>
  );
}
