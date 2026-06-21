// dashboard/src/components/StatsPanel.jsx
// Aggregate statistics panel using /api/stats endpoint
import React, { useEffect, useState, useCallback } from 'react';
import { TrendingUp, DollarSign, Cpu, CheckCircle, XCircle, Activity } from 'lucide-react';

const API = 'http://localhost:8000';

function StatBox({ icon: Icon, label, value, sub, color = '#a5b4fc' }) {
  return (
    <div style={{
      background: 'rgba(255,255,255,0.03)',
      border: '1px solid rgba(255,255,255,0.07)',
      borderRadius: '10px',
      padding: '12px 14px',
      display: 'flex',
      flexDirection: 'column',
      gap: '4px',
      flex: 1,
      minWidth: 0,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '2px' }}>
        <Icon size={13} color={color} />
        <span style={{ fontSize: '10px', color: 'rgba(255,255,255,0.4)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
          {label}
        </span>
      </div>
      <div style={{ fontSize: '20px', fontWeight: '700', color, lineHeight: 1.1 }}>{value}</div>
      {sub && <div style={{ fontSize: '10px', color: 'rgba(255,255,255,0.3)' }}>{sub}</div>}
    </div>
  );
}

export default function StatsPanel() {
  const [stats, setStats] = useState(null);

  const fetchStats = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/stats`);
      if (res.ok) setStats(await res.json());
    } catch {}
  }, []);

  useEffect(() => {
    fetchStats();
    const interval = setInterval(fetchStats, 15000);
    return () => clearInterval(interval);
  }, [fetchStats]);

  if (!stats) return null;

  const successRate = stats.total_runs > 0
    ? Math.round((stats.by_status.completed / stats.total_runs) * 100)
    : 0;

  return (
    <div style={{
      background: 'rgba(15,15,35,0.85)',
      border: '1px solid rgba(99,102,241,0.15)',
      borderRadius: '12px',
      padding: '14px',
      marginBottom: '14px',
    }}>
      <div style={{
        fontSize: '10px',
        color: 'rgba(255,255,255,0.35)',
        textTransform: 'uppercase',
        letterSpacing: '0.1em',
        marginBottom: '10px',
        display: 'flex',
        alignItems: 'center',
        gap: '6px',
      }}>
        <TrendingUp size={11} />
        Aggregate Stats
      </div>

      <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
        <StatBox
          icon={Activity}
          label="Total Runs"
          value={stats.total_runs}
          sub={`${stats.by_status.running > 0 ? stats.by_status.running + ' live' : 'none live'}`}
          color="#a5b4fc"
        />
        <StatBox
          icon={CheckCircle}
          label="Success"
          value={`${successRate}%`}
          sub={`${stats.by_status.completed} completed`}
          color="var(--color-success)"
        />
        <StatBox
          icon={DollarSign}
          label="Total Cost"
          value={`$${stats.total_cost_dollars?.toFixed(4) || '0.0000'}`}
          sub={`avg $${(stats.avg_cost_cents_per_run / 100).toFixed(4)}/run`}
          color="#fbbf24"
        />
        <StatBox
          icon={Cpu}
          label="Tokens"
          value={stats.total_tokens >= 1000 ? `${(stats.total_tokens / 1000).toFixed(1)}K` : stats.total_tokens}
          sub="all-time"
          color="#34d399"
        />
      </div>
    </div>
  );
}
