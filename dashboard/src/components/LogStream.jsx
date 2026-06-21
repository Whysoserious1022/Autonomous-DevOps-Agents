// dashboard/src/components/LogStream.jsx – Terminal log panel with auto-scroll and refresh
import React, { useEffect, useState, useRef, useCallback } from 'react';
import { RefreshCw, Terminal } from 'lucide-react';

export default function LogStream({ runId, stepName }) {
  const [logs, setLogs] = useState('');
  const [loading, setLoading] = useState(false);
  const logEndRef = useRef(null);

  const fetchLogs = useCallback(async () => {
    if (!runId || !stepName) {
      setLogs('▶  Select a step node in the pipeline diagram to view its execution logs.\n\nAll agent reasoning traces, tool calls, and error tracebacks will appear here.');
      return;
    }
    setLoading(true);
    try {
      const res = await fetch(`http://localhost:8000/api/runs/${runId}/steps/${stepName}/logs`);
      if (res.ok) {
        const data = await res.json();
        setLogs(data.logs || `No logs for step "${stepName}" yet.`);
      } else {
        setLogs(`Failed to retrieve logs (${res.status}).`);
      }
    } catch (err) {
      setLogs(`⚠  Connection error: ${err.message}`);
    } finally {
      setLoading(false);
    }
  }, [runId, stepName]);

  useEffect(() => {
    fetchLogs();
    // Auto-refresh every 5s for running steps
    const interval = setInterval(fetchLogs, 5000);
    return () => clearInterval(interval);
  }, [fetchLogs]);

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logs]);

  return (
    <div className="log-panel">
      <div className="log-header">
        <span style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
          <Terminal size={12} />
          {stepName ? `${stepName.replace('_', ' ')} · execution log` : 'Select a step'}
        </span>
        <button
          id="btn-refresh-logs"
          onClick={fetchLogs}
          title="Refresh logs"
          style={{
            background: 'none',
            border: 'none',
            cursor: 'pointer',
            color: loading ? '#22d3ee' : '#4b5563',
            display: 'flex',
            alignItems: 'center',
            gap: '4px',
            fontSize: '11px',
            padding: '2px 6px',
            borderRadius: '4px',
            transition: 'color 0.2s',
          }}
        >
          <RefreshCw size={11} className={loading ? 'spinning' : ''} />
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </div>
      <pre className="terminal-logs">
        {logs}
        <span ref={logEndRef} />
      </pre>
    </div>
  );
}
