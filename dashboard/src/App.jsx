// dashboard/src/App.jsx
import React, { useEffect, useState } from 'react';
import { Play, RotateCcw, AlertTriangle, Cpu, DollarSign, Database, Github } from 'lucide-react';
import DAGViewer from './components/DAGViewer';
import StepCard from './components/StepCard';
import CostChart from './components/CostChart';
import LogStream from './components/LogStream';

export default function App() {
  const [runs, setRuns] = useState([]);
  const [selectedRunId, setSelectedRunId] = useState(null);
  const [selectedRun, setSelectedRun] = useState(null);
  const [selectedStepName, setSelectedStepName] = useState(null);
  const [isTriggering, setIsTriggering] = useState(false);

  // Form state for triggering a run
  const [repoUrl, setRepoUrl] = useState('https://github.com/tiangolo/fastapi');
  const [issueTitle, setIssueTitle] = useState('Request: Add a way to disable docs in production without removing routes');
  const [issueBody, setIssueBody] = useState('This is a demo issue triggered via the Cascade Dashboard.');
  const [testCommand, setTestCommand] = useState('pytest tests/');

  const fetchRuns = async () => {
    try {
      const res = await fetch('http://localhost:8000/api/runs');
      if (res.ok) {
        const data = await res.json();
        setRuns(data);
        if (data.length > 0 && !selectedRunId) {
          setSelectedRunId(data[0].id);
        }
      }
    } catch (err) {
      console.error('Error fetching runs:', err);
    }
  };

  // Poll for runs list updates
  useEffect(() => {
    fetchRuns();
    const interval = setInterval(fetchRuns, 8000);
    return () => clearInterval(interval);
  }, []);

  // Fetch initial run details when selected ID changes
  useEffect(() => {
    if (!selectedRunId) return;
    
    const fetchRunDetails = async () => {
      try {
        const res = await fetch(`http://localhost:8000/api/runs/${selectedRunId}`);
        if (res.ok) {
          const data = await res.json();
          setSelectedRun(data);
          // Auto select first step if none selected
          if (!selectedStepName || !data.steps[selectedStepName]) {
            const firstStep = Object.keys(data.steps)[0];
            if (firstStep) setSelectedStepName(firstStep);
          }
        }
      } catch (err) {
        console.error('Error fetching run details:', err);
      }
    };

    fetchRunDetails();
  }, [selectedRunId]);

  // Establish WebSocket stream for real-time updates
  useEffect(() => {
    if (!selectedRunId) return;

    const ws = new WebSocket(`ws://localhost:8000/api/runs/${selectedRunId}/stream`);

    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      if (msg.type === 'initial_state') {
        setSelectedRun((prev) => ({
          ...prev,
          status: msg.status,
          total_cost_cents: msg.total_cost_cents,
          total_tokens: msg.total_tokens,
          steps: msg.steps,
        }));
      } else if (msg.type === 'run_update') {
        setSelectedRun((prev) => {
          if (!prev) return null;
          return { ...prev, status: msg.status };
        });
        fetchRuns(); // Refresh side list
      } else if (msg.type === 'step_update') {
        setSelectedRun((prev) => {
          if (!prev) return null;
          return {
            ...prev,
            steps: {
              ...prev.steps,
              [msg.step.name]: msg.step,
            },
          };
        });
      }
    };

    ws.onerror = (err) => console.error('WebSocket Error:', err);
    return () => ws.close();
  }, [selectedRunId]);

  const handleTriggerRun = async (e) => {
    e.preventDefault();
    setIsTriggering(true);
    try {
      const res = await fetch('http://localhost:8000/api/runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          repo_url: repoUrl,
          issue_title: issueTitle,
          issue_body: issueBody,
          test_command: testCommand,
        }),
      });
      if (res.ok) {
        const data = await res.json();
        await fetchRuns();
        setSelectedRunId(data.run_id);
      }
    } catch (err) {
      console.error('Failed to trigger run:', err);
    } finally {
      setIsTriggering(false);
    }
  };

  const handleResumeRun = async (stepName) => {
    try {
      const res = await fetch(`http://localhost:8000/api/runs/${selectedRunId}/resume`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ from_step: stepName }),
      });
      if (res.ok) {
        // Trigger status refresh
        setSelectedRun((prev) => {
          if (!prev) return null;
          return { ...prev, status: 'resumed' };
        });
      }
    } catch (err) {
      console.error('Failed to resume run:', err);
    }
  };

  return (
    <div className="app-container">
      {/* Header */}
      <header className="header">
        <div className="logo-container">
          <Cpu size={28} color="#6366f1" />
          <span className="logo-text">PROJECT CASCADE</span>
          <span style={{ fontSize: '11px', background: 'rgba(255,255,255,0.08)', padding: '2px 8px', borderRadius: '4px', color: '#9ca3af' }}>
            Stateful Orchestrator v1.0
          </span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
          <span style={{ fontSize: '13px', color: '#9ca3af', display: 'flex', alignItems: 'center', gap: '6px' }}>
            <Database size={14} /> SQLite Metadata Store
          </span>
          <a href="https://github.com/Whysoserious1022/Autonomous-DevOps-Agents" target="_blank" rel="noreferrer" style={{ color: '#f3f4f6', display: 'flex', alignItems: 'center', gap: '6px', fontSize: '13px', textDecoration: 'none' }}>
            <Github size={16} /> Repository
          </a>
        </div>
      </header>

      {/* Main Work Area */}
      <main className="main-content">
        {/* Sidebar */}
        <aside className="sidebar">
          <h3 style={{ margin: 0, fontSize: '16px', fontWeight: 600 }}>Executions</h3>
          
          {/* Quick trigger form */}
          <form onSubmit={handleTriggerRun} style={{ display: 'flex', flexDirection: 'column', gap: '8px', borderBottom: '1px solid rgba(255,255,255,0.08)', paddingBottom: '16px' }}>
            <div style={{ fontSize: '11px', fontWeight: 600, color: '#9ca3af', textTransform: 'uppercase' }}>Target Repository</div>
            <input value={repoUrl} onChange={(e) => setRepoUrl(e.target.value)} placeholder="Repository URL" required />
            <input value={issueTitle} onChange={(e) => setIssueTitle(e.target.value)} placeholder="Issue Title" required />
            <button type="submit" disabled={isTriggering} className="btn btn-primary" style={{ width: '100%', gap: '8px' }}>
              <Play size={14} /> {isTriggering ? 'Triggering...' : 'Trigger Run'}
            </button>
          </form>

          {/* Runs history list */}
          <div className="runs-list">
            {runs.map((r) => (
              <div 
                key={r.id} 
                className={`run-item glass-panel ${selectedRunId === r.id ? 'active' : ''}`}
                onClick={() => setSelectedRunId(r.id)}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
                  <span style={{ fontSize: '13px', fontFamily: 'monospace', color: '#9ca3af' }}>
                    {r.id.substring(0, 8)}
                  </span>
                  <span className={`status-badge status-${r.status}`}>
                    {r.status}
                  </span>
                </div>
                <div style={{ fontSize: '12px', fontWeight: 600, color: '#f3f4f6', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {r.repo_url ? r.repo_url.split('/').pop() : 'Unknown Repo'}
                </div>
                <div style={{ fontSize: '10px', color: '#6b7280', marginTop: '4px' }}>
                  LLM Cost: ${(r.total_cost_cents / 100).toFixed(4)}
                </div>
              </div>
            ))}
          </div>
        </aside>

        {/* Selected Run Details */}
        {selectedRun ? (
          <section className="detail-view">
            {/* Top portion: DAG flow + Inspector panel */}
            <div className="canvas-area">
              <DAGViewer 
                steps={selectedRun.steps} 
                onSelectNode={(name) => setSelectedStepName(name)} 
              />
              
              {/* Step info card / Inspector panel */}
              <StepCard 
                stepName={selectedStepName} 
                step={selectedRun.steps[selectedStepName]} 
                onResume={handleResumeRun}
                runStatus={selectedRun.status}
              />

              {/* Float cost overview panel */}
              <div style={{ position: 'absolute', bottom: '16px', left: '16px', width: '220px', zIndex: 10 }}>
                <CostChart run={selectedRun} />
              </div>
            </div>

            {/* Bottom portion: Terminal logs */}
            <LogStream 
              runId={selectedRunId} 
              stepName={selectedStepName} 
            />
          </section>
        ) : (
          <div style={{ display: 'flex', alignItems: 'center', justifySelf: 'center', flexDirection: 'column', gap: '12px', color: '#6b7280', padding: '40px' }}>
            <AlertTriangle size={48} />
            <h3>No Run Selected</h3>
            <p>Select an execution from the sidebar or trigger a new run to view the dashboard.</p>
          </div>
        )}
      </main>
    </div>
  );
}
