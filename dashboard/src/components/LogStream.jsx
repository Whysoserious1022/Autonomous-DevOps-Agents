// dashboard/src/components/LogStream.jsx
import React, { useEffect, useState, useRef } from 'react';

export default function LogStream({ runId, stepName }) {
  const [logs, setLogs] = useState('');
  const [loading, setLoading] = useState(false);
  const logEndRef = useRef(null);

  useEffect(() => {
    if (!runId || !stepName) {
      setLogs('Select a step on the DAG to view logs.');
      return;
    }

    const fetchLogs = async () => {
      setLoading(true);
      try {
        const res = await fetch(`http://localhost:8000/api/runs/${runId}/steps/${stepName}/logs`);
        if (res.ok) {
          const data = await res.json();
          setLogs(data.logs || 'No logs generated for this step yet.');
        } else {
          setLogs('Failed to retrieve logs for this step.');
        }
      } catch (err) {
        setLogs(`Error connecting to server: ${err.message}`);
      } finally {
        setLoading(false);
      }
    };

    fetchLogs();
  }, [runId, stepName]);

  useEffect(() => {
    if (logEndRef.current) {
      logEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [logs]);

  return (
    <div className="log-panel">
      <div className="log-header">
        <span>Console Log: {stepName || 'No step selected'}</span>
        {loading && <span style={{ color: '#06b6d4' }}>Loading logs...</span>}
      </div>
      <div className="terminal-logs">
        {logs}
        <div ref={logEndRef} />
      </div>
    </div>
  );
}
