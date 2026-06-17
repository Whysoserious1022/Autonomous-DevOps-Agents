// dashboard/src/components/CostChart.jsx
import React from 'react';
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Legend } from 'recharts';

export default function CostChart({ run }) {
  if (!run || !run.steps) return null;

  // Calculate Cascade actual cost and baseline Naive cost
  let cascadeTotal = 0;
  let naiveTotal = 0;

  const stepsData = Object.entries(run.steps).map(([name, step]) => {
    const cascadeCost = (step.llm_cost_cents || 0) / 100;
    
    // Naive cost: if the step was skipped, it would have run and cost money (we mock/retrieve its cached cost)
    // If completed/failed, it ran so it costs the same.
    const stepCostHistory = (step.outputs && step.outputs.__cost_cents__) 
      ? step.outputs.__cost_cents__ / 100 
      : 0.15; // default fallback cost per LLM agent execution
      
    const naiveCost = step.status === 'skipped' ? stepCostHistory : cascadeCost;

    cascadeTotal += cascadeCost;
    naiveTotal += naiveCost;

    return {
      name,
      Cascade: parseFloat(cascadeCost.toFixed(4)),
      Naive: parseFloat(naiveCost.toFixed(4)),
    };
  });

  const summaryData = [
    {
      name: 'Total Cost',
      Cascade: parseFloat(cascadeTotal.toFixed(4)),
      Naive: parseFloat(naiveTotal.toFixed(4)),
    }
  ];

  return (
    <div className="glass-panel" style={{ padding: '16px', height: '100%', display: 'flex', flexDirection: 'column', gap: '12px' }}>
      <h3 style={{ margin: 0, fontSize: '15px', fontWeight: 600, color: '#9ca3af' }}>Cost Savings ($)</h3>
      <div style={{ display: 'flex', gap: '16px', fontSize: '12px' }}>
        <div>
          <span style={{ color: '#6366f1', fontWeight: 'bold' }}>Cascade:</span> ${cascadeTotal.toFixed(4)}
        </div>
        <div>
          <span style={{ color: '#ef4444', fontWeight: 'bold' }}>Naive Run:</span> ${naiveTotal.toFixed(4)}
        </div>
        {naiveTotal > 0 && (
          <div style={{ color: '#10b981', fontWeight: 'bold' }}>
            Saved: {(((naiveTotal - cascadeTotal) / naiveTotal) * 100).toFixed(1)}%
          </div>
        )}
      </div>
      <div style={{ flex: 1, minHeight: '120px' }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={summaryData} margin={{ top: 5, right: 5, left: -25, bottom: 5 }}>
            <XAxis dataKey="name" stroke="#6b7280" fontSize={11} tickLine={false} />
            <YAxis stroke="#6b7280" fontSize={11} tickLine={false} />
            <Tooltip
              contentStyle={{ background: '#111827', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '6px' }}
              labelStyle={{ fontWeight: 'bold', color: '#f3f4f6' }}
            />
            <Legend verticalAlign="bottom" height={20} iconSize={10} wrapperStyle={{ fontSize: '11px' }} />
            <Bar dataKey="Cascade" fill="#6366f1" radius={[4, 4, 0, 0]} />
            <Bar dataKey="Naive" fill="#ef4444" radius={[4, 4, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
