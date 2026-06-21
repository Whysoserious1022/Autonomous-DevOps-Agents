// dashboard/src/App.jsx  — Cascade DevOps Dashboard v2.0
import React, { useEffect, useState, useCallback, useRef } from 'react';
import {
  Cpu, Github, Database, Zap, Activity, Radio,
  Play, RotateCcw, Eye, RefreshCw, Wifi, WifiOff,
  Square, Layers, BarChart2
} from 'lucide-react';
import DAGViewer from './components/DAGViewer';
import StepCard from './components/StepCard';
import CostChart from './components/CostChart';
import CostBreakdown from './components/CostBreakdown';
import LogStream from './components/LogStream';
import PollerPanel from './components/PollerPanel';
import StatsPanel from './components/StatsPanel';
import KnowledgeGraphPanel from './components/KnowledgeGraphPanel';
import ToastContainer from './components/ToastContainer';

const API = 'http://localhost:8000';

export default function App() {
  // ── State ──────────────────────────────────────────────────────────────────
  const [runs, setRuns] = useState([]);
  const [selectedRunId, setSelectedRunId] = useState(null);
  const [selectedRun, setSelectedRun] = useState(null);
  const [selectedStepName, setSelectedStepName] = useState(null);
  const [isTriggering, setIsTriggering] = useState(false);
  const [isCancelling, setIsCancelling] = useState(false);
  const [sidebarTab, setSidebarTab] = useState('runs'); // 'runs' | 'poller'
  const [detailTab, setDetailTab] = useState('pipeline'); // 'pipeline' | 'knowledge'
  const [toasts, setToasts] = useState([]);
  const [apiConnected, setApiConnected] = useState(false);
  const [watchedRepos, setWatchedRepos] = useState([]);

  // Form state
  const [repoUrl, setRepoUrl] = useState('https://github.com/tiangolo/fastapi');
  const [issueTitle, setIssueTitle] = useState('Add option to disable docs in production');
  const [issueBody, setIssueBody] = useState('Demo triggered from Cascade Dashboard.');
  const [testCommand, setTestCommand] = useState('');

  // Global WS ref
  const globalWsRef = useRef(null);

  // ── Toast Helpers ──────────────────────────────────────────────────────────
  const addToast = useCallback((msg, type = 'info') => {
    const id = Date.now();
    setToasts(prev => [...prev, { id, msg, type }]);
    setTimeout(() => setToasts(prev => prev.filter(t => t.id !== id)), 5000);
  }, []);

  // ── API Helpers ────────────────────────────────────────────────────────────
  const fetchRuns = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/runs`);
      if (res.ok) {
        const data = await res.json();
        setRuns(data);
        setApiConnected(true);
        if (data.length > 0 && !selectedRunId) {
          setSelectedRunId(data[0].id);
        }
      }
    } catch {
      setApiConnected(false);
    }
  }, [selectedRunId]);

  const fetchWatchedRepos = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/poller/watched`);
      if (res.ok) {
        const data = await res.json();
        setWatchedRepos(data.watching || []);
      }
    } catch {}
  }, []);

  // ── Initial load + polling ──────────────────────────────────────────────────
  useEffect(() => {
    fetchRuns();
    fetchWatchedRepos();
    const interval = setInterval(() => { fetchRuns(); fetchWatchedRepos(); }, 10000);
    return () => clearInterval(interval);
  }, [fetchRuns, fetchWatchedRepos]);

  // ── Fetch run details when selection changes ───────────────────────────────
  useEffect(() => {
    if (!selectedRunId) return;
    const fetchRunDetails = async () => {
      try {
        const res = await fetch(`${API}/api/runs/${selectedRunId}`);
        if (res.ok) {
          const data = await res.json();
          setSelectedRun(data);
          if (!selectedStepName || !data.steps?.[selectedStepName]) {
            const first = Object.keys(data.steps || {})[0];
            if (first) setSelectedStepName(first);
          }
        }
      } catch {}
    };
    fetchRunDetails();
  }, [selectedRunId]);

  // ── Per-run WebSocket ──────────────────────────────────────────────────────
  useEffect(() => {
    if (!selectedRunId) return;
    const ws = new WebSocket(`ws://localhost:8000/api/runs/${selectedRunId}/stream`);
    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if (msg.type === 'initial_state') {
        setSelectedRun(prev => ({ ...prev, status: msg.status, total_cost_cents: msg.total_cost_cents, total_tokens: msg.total_tokens, steps: msg.steps }));
      } else if (msg.type === 'run_update') {
        setSelectedRun(prev => prev ? { ...prev, status: msg.status } : null);
        fetchRuns();
      } else if (msg.type === 'step_update') {
        setSelectedRun(prev => {
          if (!prev) return null;
          return { ...prev, steps: { ...prev.steps, [msg.step.name]: msg.step } };
        });
      }
    };
    ws.onerror = () => {};
    return () => ws.close();
  }, [selectedRunId, fetchRuns]);

  // ── Global WebSocket ───────────────────────────────────────────────────────
  useEffect(() => {
    const connectGlobal = () => {
      const ws = new WebSocket(`ws://localhost:8000/api/global/stream`);
      globalWsRef.current = ws;
      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.type === 'poller_triggered') {
          addToast(`🤖 Auto-triggered run for issue #${msg.issue_number}: ${msg.issue_title}`, 'success');
          fetchRuns();
          setWatchedRepos(prev => prev.includes(msg.repo_url) ? prev : [...prev, msg.repo_url]);
        } else if (msg.type === 'webhook_triggered') {
          addToast(`🔔 Webhook: Issue #${msg.issue_number} triggered pipeline`, 'info');
          fetchRuns();
        } else if (msg.type === 'poller_watch_started') {
          fetchWatchedRepos();
        } else if (msg.type === 'poller_watch_stopped') {
          fetchWatchedRepos();
        }
      };
      ws.onclose = () => setTimeout(connectGlobal, 3000);
      ws.onerror = () => {};
    };
    connectGlobal();
    return () => globalWsRef.current?.close();
  }, [addToast, fetchRuns, fetchWatchedRepos]);

  // ── Handlers ───────────────────────────────────────────────────────────────
  const handleTriggerRun = async (e) => {
    e.preventDefault();
    setIsTriggering(true);
    try {
      const res = await fetch(`${API}/api/runs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo_url: repoUrl, issue_title: issueTitle, issue_body: issueBody, test_command: testCommand }),
      });
      if (res.ok) {
        const data = await res.json();
        await fetchRuns();
        setSelectedRunId(data.run_id);
        addToast('Pipeline triggered successfully!', 'success');
      } else {
        addToast('Failed to trigger pipeline', 'error');
      }
    } catch {
      addToast('API connection error', 'error');
    } finally {
      setIsTriggering(false);
    }
  };

  const handleResumeRun = async (stepName) => {
    try {
      const res = await fetch(`${API}/api/runs/${selectedRunId}/resume`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ from_step: stepName }),
      });
      if (res.ok) {
        addToast(`Resuming from step: ${stepName}`, 'info');
        setSelectedRun(prev => prev ? { ...prev, status: 'resumed' } : null);
      }
    } catch {
      addToast('Failed to resume run', 'error');
    }
  };

  const handleWatchRepo = async (url) => {
    try {
      const res = await fetch(`${API}/api/poller/watch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo_url: url }),
      });
      if (res.ok) {
        const data = await res.json();
        setWatchedRepos(data.watching || []);
        addToast(`Now watching: ${url.split('/').slice(-2).join('/')}`, 'success');
      }
    } catch {
      addToast('Failed to start watching', 'error');
    }
  };

  const handleUnwatchRepo = async (url) => {
    try {
      const res = await fetch(`${API}/api/poller/unwatch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo_url: url }),
      });
      if (res.ok) {
        const data = await res.json();
        setWatchedRepos(data.watching || []);
        addToast(`Stopped watching: ${url.split('/').slice(-2).join('/')}`, 'info');
      }
    } catch {}
  };

  const handleCancelRun = async () => {
    if (!selectedRunId) return;
    setIsCancelling(true);
    try {
      const res = await fetch(`${API}/api/runs/${selectedRunId}/cancel`, { method: 'POST' });
      if (res.ok) {
        addToast('Run cancellation requested.', 'info');
        setSelectedRun(prev => prev ? { ...prev, status: 'failed' } : null);
        await fetchRuns();
      } else {
        addToast('Could not cancel run.', 'error');
      }
    } catch {
      addToast('Failed to cancel run.', 'error');
    } finally {
      setIsCancelling(false);
    }
  };

  // ── Stats ──────────────────────────────────────────────────────────────────
  const totalCost = runs.reduce((s, r) => s + (r.total_cost_cents || 0), 0);
  const completedRuns = runs.filter(r => r.status === 'completed').length;
  const activeRuns = runs.filter(r => r.status === 'running' || r.status === 'resumed').length;
  const isActiveRun = selectedRun && ['running', 'pending', 'resumed'].includes(selectedRun.status);

  // ── Render ─────────────────────────────────────────────────────────────────
  return (
    <div className="app-container">
      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <header className="header">
        <div className="logo-group">
          <div className="logo-icon">
            <Cpu size={18} color="white" />
          </div>
          <span className="logo-text">CASCADE</span>
          <span className="logo-version">v1.0 · Autonomous DevOps</span>
        </div>

        <div className="header-actions">
          {/* Connection status */}
          <div className={`header-badge`} style={{ borderColor: apiConnected ? 'rgba(52,211,153,0.3)' : 'rgba(248,113,113,0.3)' }}>
            {apiConnected ? <Wifi size={12} color="var(--color-success)" /> : <WifiOff size={12} color="var(--color-danger)" />}
            <span style={{ color: apiConnected ? 'var(--color-success)' : 'var(--color-danger)' }}>
              {apiConnected ? 'API Connected' : 'API Offline'}
            </span>
          </div>

          {/* Live poller indicator */}
          {watchedRepos.length > 0 && (
            <div className="header-badge" style={{ borderColor: 'rgba(52,211,153,0.25)', color: 'var(--color-success)' }}>
              <div className="live-dot" />
              Watching {watchedRepos.length} repo{watchedRepos.length !== 1 ? 's' : ''}
            </div>
          )}

          <div className="header-badge" style={{ borderColor: 'rgba(99,102,241,0.2)' }}>
            <Activity size={12} color="var(--accent-primary)" />
            <span style={{ color: '#a5b4fc' }}>{completedRuns}/{runs.length} Runs</span>
          </div>

          <a
            href="https://github.com/Whysoserious1022/Autonomous-DevOps-Agents"
            target="_blank" rel="noreferrer"
            className="header-badge"
          >
            <Github size={13} />
            Source
          </a>
        </div>
      </header>

      {/* ── Main ───────────────────────────────────────────────────────────── */}
      <main className="main-content">
        {/* ── Sidebar ─────────────────────────────────────────────────────── */}
        <aside className="sidebar">
          {/* Tabs */}
          <div className="sidebar-tabs">
            <button
              id="tab-runs"
              className={`sidebar-tab ${sidebarTab === 'runs' ? 'active' : ''}`}
              onClick={() => setSidebarTab('runs')}
            >
              <Activity size={12} />
              Runs
              {activeRuns > 0 && (
                <span style={{ background: 'rgba(251,191,36,0.2)', color: 'var(--color-warning)', borderRadius: '99px', padding: '1px 6px', fontSize: '10px' }}>
                  {activeRuns}
                </span>
              )}
            </button>
            <button
              id="tab-poller"
              className={`sidebar-tab ${sidebarTab === 'poller' ? 'active' : ''}`}
              onClick={() => setSidebarTab('poller')}
            >
              <Radio size={12} />
              Watcher
              {watchedRepos.length > 0 && (
                <span style={{ background: 'rgba(52,211,153,0.2)', color: 'var(--color-success)', borderRadius: '99px', padding: '1px 6px', fontSize: '10px' }}>
                  {watchedRepos.length}
                </span>
              )}
            </button>
          </div>

          {/* Tab Content */}
          <div className="sidebar-content">
            {sidebarTab === 'runs' ? (
              <>
                {/* Trigger Form */}
                <form id="trigger-form" className="trigger-form" onSubmit={handleTriggerRun}>
                  <div className="form-label">🚀 New Pipeline Run</div>
                  <div className="form-group">
                    <div className="form-label">Repository URL</div>
                    <input
                      id="input-repo-url"
                      value={repoUrl}
                      onChange={e => setRepoUrl(e.target.value)}
                      placeholder="https://github.com/owner/repo"
                      required
                    />
                  </div>
                  <div className="form-group">
                    <div className="form-label">Issue Title</div>
                    <input
                      id="input-issue-title"
                      value={issueTitle}
                      onChange={e => setIssueTitle(e.target.value)}
                      placeholder="Describe the issue to fix"
                      required
                    />
                  </div>
                  <button
                    id="btn-trigger"
                    type="submit"
                    disabled={isTriggering}
                    className="btn btn-primary"
                    style={{ width: '100%' }}
                  >
                    {isTriggering ? <RefreshCw size={13} className="spinning" /> : <Play size={13} />}
                    {isTriggering ? 'Triggering...' : 'Trigger Pipeline'}
                  </button>
                </form>

                {/* Stats Panel */}
                <StatsPanel />

                {/* Stats Row */}
                <div className="stats-grid" style={{ marginBottom: '14px' }}>
                  <div className="stat-card">
                    <div className="stat-label">Total Cost</div>
                    <div className="stat-value" style={{ fontSize: '18px', color: '#a5b4fc' }}>
                      ${(totalCost / 100).toFixed(4)}
                    </div>
                    <div className="stat-sub">{runs.length} runs total</div>
                  </div>
                  <div className="stat-card">
                    <div className="stat-label">Success Rate</div>
                    <div className="stat-value" style={{ fontSize: '18px', color: 'var(--color-success)' }}>
                      {runs.length > 0 ? Math.round((completedRuns / runs.length) * 100) : 0}%
                    </div>
                    <div className="stat-sub">{completedRuns} completed</div>
                  </div>
                </div>

                {/* Runs List */}
                <div className="section-header">
                  Recent Executions
                  <button
                    id="btn-refresh-runs"
                    className="btn btn-ghost"
                    style={{ padding: '3px 8px', fontSize: '11px' }}
                    onClick={fetchRuns}
                    title="Refresh"
                  >
                    <RefreshCw size={11} />
                  </button>
                </div>
                <div className="runs-list">
                  {runs.length === 0 && (
                    <div className="empty-state" style={{ padding: '30px 10px' }}>
                      <div className="empty-state-icon"><Zap size={28} /></div>
                      <p>No runs yet. Trigger a pipeline above!</p>
                    </div>
                  )}
                  {runs.map(r => (
                    <div
                      key={r.id}
                      id={`run-item-${r.id.substring(0, 8)}`}
                      className={`run-item ${selectedRunId === r.id ? 'active' : ''}`}
                      onClick={() => setSelectedRunId(r.id)}
                    >
                      <div className="run-item-header">
                        <span className="run-id">#{r.id.substring(0, 8)}</span>
                        <span className={`status-badge status-${r.status}`}>
                          {r.status === 'running' && <span style={{ width: '6px', height: '6px', borderRadius: '50%', background: 'currentColor', display: 'inline-block' }} />}
                          {r.status}
                        </span>
                      </div>
                      <div className="run-repo">
                        {r.repo_url ? r.repo_url.replace('https://github.com/', '') : 'Unknown'}
                      </div>
                      <div className="run-meta">
                        <span>${(r.total_cost_cents / 100).toFixed(4)}</span>
                        <span>{r.tags?.source || 'manual'}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </>
            ) : (
              <PollerPanel
                watchedRepos={watchedRepos}
                onWatch={handleWatchRepo}
                onUnwatch={handleUnwatchRepo}
              />
            )}
          </div>
        </aside>

        {/* ── Detail Panel ────────────────────────────────────────────────────── */}
        {selectedRun ? (
          <section className="detail-view fade-in">
            {/* Detail Tab Bar + Cancel */}
            <div style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              padding: '0 2px 12px',
              borderBottom: '1px solid rgba(255,255,255,0.06)',
              marginBottom: '12px',
            }}>
              <div style={{ display: 'flex', gap: '4px' }}>
                <button
                  id="tab-detail-pipeline"
                  className={`sidebar-tab ${detailTab === 'pipeline' ? 'active' : ''}`}
                  onClick={() => setDetailTab('pipeline')}
                  style={{ padding: '5px 12px' }}
                >
                  <Activity size={11} />
                  Pipeline
                </button>
                <button
                  id="tab-detail-knowledge"
                  className={`sidebar-tab ${detailTab === 'knowledge' ? 'active' : ''}`}
                  onClick={() => setDetailTab('knowledge')}
                  style={{ padding: '5px 12px' }}
                >
                  <Layers size={11} />
                  Knowledge Graph
                </button>
              </div>

              {isActiveRun && (
                <button
                  id="btn-cancel-run"
                  className="btn"
                  onClick={handleCancelRun}
                  disabled={isCancelling}
                  style={{
                    background: 'rgba(248,113,113,0.12)',
                    border: '1px solid rgba(248,113,113,0.3)',
                    color: '#f87171',
                    padding: '5px 12px',
                    fontSize: '12px',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '5px',
                  }}
                >
                  <Square size={11} />
                  {isCancelling ? 'Cancelling…' : 'Cancel Run'}
                </button>
              )}
            </div>

            {detailTab === 'pipeline' ? (
              <>
                {/* Top: DAG + Inspector */}
                <div className="canvas-area">
                  <DAGViewer
                    steps={selectedRun.steps}
                    onSelectNode={name => setSelectedStepName(name)}
                    selectedNode={selectedStepName}
                  />
                  <div className="inspector-panel">
                    {/* Run header */}
                    <div>
                      <div className="section-title">Run Overview</div>
                      <div className="step-key-val">
                        <div className="kv-row">
                          <span className="kv-key">Status</span>
                          <span className={`status-badge status-${selectedRun.status}`}>{selectedRun.status}</span>
                        </div>
                        <div className="kv-row">
                          <span className="kv-key">Run ID</span>
                          <span className="kv-val mono">{selectedRun.id?.substring(0, 16)}…</span>
                        </div>
                        <div className="kv-row">
                          <span className="kv-key">Repo</span>
                          <span className="kv-val" style={{ fontSize: '11px' }}>
                            {selectedRun.repo_url?.replace('https://github.com/', '') || '—'}
                          </span>
                        </div>
                        <div className="kv-row">
                          <span className="kv-key">Total Cost</span>
                          <span className="kv-val" style={{ color: '#a5b4fc' }}>
                            ${((selectedRun.total_cost_cents || 0) / 100).toFixed(4)}
                          </span>
                        </div>
                        <div className="kv-row">
                          <span className="kv-key">Tokens</span>
                          <span className="kv-val">{(selectedRun.total_tokens || 0).toLocaleString()}</span>
                        </div>
                      </div>
                    </div>

                    <CostChart run={selectedRun} />
                    <CostBreakdown runId={selectedRunId} />

                    {selectedStepName && (
                      <StepCard
                        stepName={selectedStepName}
                        step={selectedRun.steps?.[selectedStepName]}
                        onResume={handleResumeRun}
                        runStatus={selectedRun.status}
                      />
                    )}
                  </div>
                </div>

                {/* Bottom: Log Terminal */}
                <LogStream runId={selectedRunId} stepName={selectedStepName} />
              </>
            ) : (
              <div style={{ padding: '4px' }}>
                <KnowledgeGraphPanel runId={selectedRunId} />
              </div>
            )}
          </section>
        ) : (
          <div className="empty-state">
            <div className="empty-state-icon"><Cpu size={52} /></div>
            <h3>No Run Selected</h3>
            <p>Select an execution from the sidebar, or trigger a new pipeline to get started.</p>
          </div>
        )}

      </main>

      {/* ── Toast Notifications ──────────────────────────────────────────── */}
      <ToastContainer toasts={toasts} />
    </div>
  );
}
