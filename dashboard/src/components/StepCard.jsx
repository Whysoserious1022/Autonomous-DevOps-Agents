// dashboard/src/components/StepCard.jsx
import React from 'react';

export default function StepCard({ stepName, step, onResume, runStatus }) {
  if (!stepName || !step) {
    return (
      <div className="inspector-panel">
        <h3 style={{ margin: 0, fontSize: '16px', fontWeight: 600 }}>Step Inspector</h3>
        <p style={{ color: '#6b7280', fontSize: '13px' }}>Select a node in the DAG to inspect its execution parameters, inputs, outputs, and costs.</p>
      </div>
    );
  }

  const isFailed = step.status === 'failed' || step.status === 'permanently_failed';
  const canResume = isFailed && (runStatus === 'failed' || runStatus === 'permanently_failed');

  return (
    <div className="inspector-panel">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h3 style={{ margin: 0, fontSize: '18px', fontWeight: 700, color: '#f3f4f6' }}>
          {step.name}
        </h3>
        <span className={`status-badge status-${step.status}`}>
          {step.status}
        </span>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', fontSize: '13px', borderBottom: '1px solid rgba(255,255,255,0.08)', paddingBottom: '16px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <span style={{ color: '#9ca3af' }}>Duration:</span>
          <span>{step.duration_seconds ? `${step.duration_seconds.toFixed(2)}s` : '—'}</span>
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <span style={{ color: '#9ca3af' }}>LLM Cost:</span>
          <span style={{ color: '#eab308', fontWeight: 500 }}>
            ${((step.llm_cost_cents || 0) / 100).toFixed(4)}
          </span>
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <span style={{ color: '#9ca3af' }}>Tokens:</span>
          <span>{step.total_tokens || 0}</span>
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <span style={{ color: '#9ca3af' }}>Retries:</span>
          <span>{step.retry_count || 0} / {step.max_retries || 3}</span>
        </div>
      </div>

      {canResume && (
        <button 
          className="btn btn-primary" 
          onClick={() => onResume(step.name)}
          style={{ width: '100%' }}
        >
          Resume From This Step
        </button>
      )}

      {step.error_message && (
        <div className="glass-panel" style={{ padding: '12px', borderColor: 'rgba(239, 68, 68, 0.3)', background: 'rgba(239, 68, 68, 0.05)' }}>
          <h4 style={{ margin: '0 0 6px 0', fontSize: '13px', color: '#ef4444', fontWeight: 600 }}>Error Summary</h4>
          <p style={{ margin: 0, fontSize: '12px', fontFamily: 'monospace', color: '#fca5a5', overflowWrap: 'anywhere' }}>
            {step.error_message}
          </p>
        </div>
      )}

      {step.artifact_uris && step.artifact_uris.length > 0 && (
        <div>
          <h4 style={{ margin: '0 0 8px 0', fontSize: '13px', color: '#9ca3af', fontWeight: 600 }}>Generated Artifacts</h4>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
            {step.artifact_uris.map((uri, i) => (
              <div key={i} className="glass-panel" style={{ padding: '6px 10px', fontSize: '12px', fontFamily: 'monospace', color: '#06b6d4', textOverflow: 'ellipsis', overflow: 'hidden', whiteSpace: 'nowrap' }}>
                {uri}
              </div>
            ))}
          </div>
        </div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px', flex: 1 }}>
        <div>
          <h4 style={{ margin: '0 0 6px 0', fontSize: '13px', color: '#9ca3af', fontWeight: 600 }}>Inputs</h4>
          <pre style={{ margin: 0, padding: '8px', background: 'rgba(0,0,0,0.2)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '6px', fontSize: '11px', overflow: 'auto', maxHeight: '180px', fontFamily: 'monospace' }}>
            {JSON.stringify(step.inputs || {}, null, 2)}
          </pre>
        </div>
        <div>
          <h4 style={{ margin: '0 0 6px 0', fontSize: '13px', color: '#9ca3af', fontWeight: 600 }}>Outputs</h4>
          <pre style={{ margin: 0, padding: '8px', background: 'rgba(0,0,0,0.2)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '6px', fontSize: '11px', overflow: 'auto', maxHeight: '180px', fontFamily: 'monospace' }}>
            {JSON.stringify(step.outputs || {}, null, 2)}
          </pre>
        </div>
      </div>
    </div>
  );
}
