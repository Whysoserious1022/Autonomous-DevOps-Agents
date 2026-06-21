// dashboard/src/components/PollerPanel.jsx
// Autonomous GitHub Issue Watcher management panel
import React, { useState } from 'react';
import { Radio, Plus, X, ExternalLink, Eye, Clock, AlertCircle } from 'lucide-react';

export default function PollerPanel({ watchedRepos = [], onWatch, onUnwatch }) {
  const [newRepo, setNewRepo] = useState('');
  const [isAdding, setIsAdding] = useState(false);
  const [error, setError] = useState('');

  const handleAdd = async (e) => {
    e.preventDefault();
    setError('');

    // Basic URL validation
    const trimmed = newRepo.trim();
    if (!trimmed) return;
    if (!trimmed.includes('github.com/')) {
      setError('Please enter a valid GitHub URL (e.g. https://github.com/owner/repo)');
      return;
    }

    setIsAdding(true);
    try {
      await onWatch(trimmed);
      setNewRepo('');
    } catch {
      setError('Failed to start watching. Check API connection.');
    } finally {
      setIsAdding(false);
    }
  };

  return (
    <div className="poller-panel">
      {/* Status Bar */}
      <div className={`poller-status-bar ${watchedRepos.length === 0 ? 'inactive' : ''}`}>
        <Radio size={14} />
        {watchedRepos.length > 0
          ? `Autonomous mode: watching ${watchedRepos.length} repo${watchedRepos.length > 1 ? 's' : ''}`
          : 'Autonomous watcher inactive'}
      </div>

      {/* How it works */}
      <div style={{ padding: '12px', background: 'rgba(99,102,241,0.06)', border: '1px solid rgba(99,102,241,0.12)', borderRadius: '10px', fontSize: '12px', color: '#94a3b8', lineHeight: 1.6 }}>
        <div style={{ fontWeight: 700, color: '#c7d2fe', marginBottom: '6px', display: 'flex', alignItems: 'center', gap: '6px' }}>
          <Eye size={12} /> How it works
        </div>
        Add any GitHub repo below. Cascade will poll every <strong style={{ color: '#e2e8f0' }}>60 seconds</strong> for issues labeled <code style={{ background: 'rgba(99,102,241,0.15)', padding: '1px 5px', borderRadius: '4px', color: '#a5b4fc' }}>agent-task</code> and automatically trigger the full DevOps pipeline — zero human intervention.
      </div>

      {/* Add repo form */}
      <div>
        <div className="form-label" style={{ marginBottom: '8px' }}>Watch a Repository</div>
        <form onSubmit={handleAdd} style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          <input
            id="input-watch-repo"
            value={newRepo}
            onChange={e => { setNewRepo(e.target.value); setError(''); }}
            placeholder="https://github.com/owner/repo"
          />
          {error && (
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '11px', color: 'var(--color-danger)' }}>
              <AlertCircle size={11} />
              {error}
            </div>
          )}
          <button
            id="btn-add-watch"
            type="submit"
            disabled={isAdding || !newRepo.trim()}
            className="btn btn-success"
            style={{ width: '100%' }}
          >
            <Plus size={13} />
            {isAdding ? 'Adding...' : 'Start Watching'}
          </button>
        </form>
      </div>

      {/* Watched repos list */}
      <div>
        <div className="section-header">
          Active Watchers
          <span style={{ background: 'rgba(52,211,153,0.1)', color: 'var(--color-success)', borderRadius: '99px', padding: '1px 8px', fontSize: '10px', fontWeight: 700 }}>
            {watchedRepos.length}
          </span>
        </div>

        {watchedRepos.length === 0 ? (
          <div style={{ textAlign: 'center', padding: '24px 12px', color: '#475569', fontSize: '12px' }}>
            <Radio size={24} style={{ opacity: 0.3, marginBottom: '8px', display: 'block', margin: '0 auto 8px' }} />
            No repos being watched yet.
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {watchedRepos.map(url => (
              <div key={url} className="watched-repo-item">
                <div style={{ display: 'flex', flexDirection: 'column', flex: 1, gap: '2px', overflow: 'hidden' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
                    <div style={{ width: '6px', height: '6px', borderRadius: '50%', background: 'var(--color-success)', boxShadow: '0 0 6px var(--color-success)', flexShrink: 0 }} />
                    <span className="watched-repo-url">
                      {url.replace('https://github.com/', '')}
                    </span>
                  </div>
                  <div style={{ fontSize: '10px', color: '#475569', paddingLeft: '11px', display: 'flex', alignItems: 'center', gap: '4px' }}>
                    <Clock size={9} />
                    polling every 60s · label: agent-task
                  </div>
                </div>
                <div style={{ display: 'flex', gap: '4px', flexShrink: 0, marginLeft: '8px' }}>
                  <a href={url} target="_blank" rel="noreferrer" className="btn-icon" title="Open on GitHub">
                    <ExternalLink size={12} />
                  </a>
                  <button
                    id={`btn-unwatch-${url.split('/').slice(-2).join('-')}`}
                    className="btn-icon"
                    onClick={() => onUnwatch(url)}
                    title="Stop watching"
                  >
                    <X size={12} />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Webhook section */}
      <div style={{ padding: '12px', background: 'rgba(34,211,238,0.05)', border: '1px solid rgba(34,211,238,0.12)', borderRadius: '10px', fontSize: '12px', color: '#94a3b8', lineHeight: 1.6 }}>
        <div style={{ fontWeight: 700, color: '#67e8f9', marginBottom: '6px' }}>⚡ Webhook Mode (Instant)</div>
        For zero-latency triggering, configure a GitHub Webhook:
        <div style={{ fontFamily: 'var(--font-mono)', fontSize: '11px', background: 'rgba(0,0,0,0.3)', padding: '8px', borderRadius: '6px', marginTop: '8px', color: '#7dd3fc', wordBreak: 'break-all' }}>
          http://localhost:8000/api/webhook/github
        </div>
        <div style={{ marginTop: '6px', fontSize: '11px' }}>
          Events: <code style={{ color: '#a5b4fc' }}>Issues (labeled)</code>
        </div>
      </div>
    </div>
  );
}
