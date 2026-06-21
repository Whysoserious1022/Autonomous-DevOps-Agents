// dashboard/src/components/StepCard.jsx – Step inspector panel
import React from 'react';
import { RotateCcw, AlertTriangle, CheckCircle, Package } from 'lucide-react';

const STEP_DESCRIPTIONS = {
  explorer:   'Clones the repository, builds AST knowledge graph, and ranks relevant files using LLM analysis.',
  planner:    'Generates Tree-of-Thoughts solution branches, evaluates each with a Critic model, and selects the best plan.',
  coder:      'Runs a ReAct coding loop to produce a git-compatible unified diff patch. Retries on test failures.',
  tester:     'Executes the test suite inside an isolated Docker sandbox. Extracts JUnit XML results and failure summaries.',
  reviewer:   'Scans the patch for secrets, complexity violations, and security issues. LLM-reviews code quality.',
  pr_creator: 'Pushes a new branch, applies the patch, commits, and opens a pull request with a full execution summary.',
};

export default function StepCard({ stepName, step, onResume, runStatus }) {
  if (!stepName || !step) {
    return (
      <div>
        <div className="section-title">Step Inspector</div>
        <p style={{ color: '#475569', fontSize: '13px', lineHeight: 1.6, margin: 0 }}>
          Click a node in the pipeline diagram to inspect its execution details, inputs, outputs, and costs.
        </p>
      </div>
    );
  }

  const isFailed = step.status === 'failed' || step.status === 'permanently_failed';
  const canResume = isFailed && (runStatus === 'failed' || runStatus === 'permanently_failed');

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
      {/* Header */}
      <div>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
          <div style={{ fontWeight: 700, fontSize: '15px', color: '#f1f5f9', textTransform: 'capitalize' }}>
            {stepName.replace('_', ' ')}
          </div>
          <span className={`status-badge status-${step.status}`}>{step.status}</span>
        </div>
        {STEP_DESCRIPTIONS[stepName] && (
          <p style={{ margin: 0, fontSize: '11px', color: '#64748b', lineHeight: 1.6 }}>
            {STEP_DESCRIPTIONS[stepName]}
          </p>
        )}
      </div>

      {/* Metrics */}
      <div className="section-title">Execution Metrics</div>
      <div className="step-key-val">
        <div className="kv-row">
          <span className="kv-key">Duration</span>
          <span className="kv-val">{step.duration_seconds ? `${step.duration_seconds.toFixed(2)}s` : '—'}</span>
        </div>
        <div className="kv-row">
          <span className="kv-key">LLM Cost</span>
          <span className="kv-val" style={{ color: '#a5b4fc' }}>
            ${((step.llm_cost_cents || 0) / 100).toFixed(4)}
          </span>
        </div>
        <div className="kv-row">
          <span className="kv-key">Tokens</span>
          <span className="kv-val">{(step.total_tokens || 0).toLocaleString()}</span>
        </div>
        <div className="kv-row">
          <span className="kv-key">Retries</span>
          <span className="kv-val">{step.retry_count || 0}/{step.max_retries || 3}</span>
        </div>
      </div>

      {/* Error */}
      {step.error_message && (
        <div style={{
          padding: '10px 12px',
          background: 'rgba(248,113,113,0.07)',
          border: '1px solid rgba(248,113,113,0.2)',
          borderRadius: '8px',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '6px', color: 'var(--color-danger)', fontWeight: 700, fontSize: '12px' }}>
            <AlertTriangle size={12} /> Error
          </div>
          <pre style={{ margin: 0, fontSize: '11px', fontFamily: 'var(--font-mono)', color: '#fca5a5', overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' }}>
            {step.error_message}
          </pre>
        </div>
      )}

      {/* Resume button */}
      {canResume && (
        <button
          id={`btn-resume-${stepName}`}
          className="btn btn-primary"
          onClick={() => onResume(step.name || stepName)}
          style={{ width: '100%' }}
        >
          <RotateCcw size={13} />
          Resume from {stepName.replace('_', ' ')}
        </button>
      )}

      {/* Artifacts */}
      {step.artifact_uris?.length > 0 && (
        <div>
          <div className="section-title" style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
            <Package size={11} /> Artifacts ({step.artifact_uris.length})
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
            {step.artifact_uris.slice(0, 4).map((uri, i) => (
              <div key={i} style={{
                padding: '5px 9px',
                background: 'rgba(34,211,238,0.06)',
                border: '1px solid rgba(34,211,238,0.12)',
                borderRadius: '6px',
                fontSize: '10px',
                fontFamily: 'var(--font-mono)',
                color: '#67e8f9',
                textOverflow: 'ellipsis',
                overflow: 'hidden',
                whiteSpace: 'nowrap',
              }}>
                {uri}
              </div>
            ))}
            {step.artifact_uris.length > 4 && (
              <div style={{ fontSize: '10px', color: '#475569', paddingLeft: '4px' }}>
                +{step.artifact_uris.length - 4} more…
              </div>
            )}
          </div>
        </div>
      )}

      {/* Outputs summary */}
      {step.outputs && Object.keys(step.outputs).length > 0 && (
        <div>
          <div className="section-title">Key Outputs</div>
          <div className="step-key-val">
            {Object.entries(step.outputs)
              .filter(([k]) => !k.startsWith('__') && !k.endsWith('_uri'))
              .slice(0, 5)
              .map(([k, v]) => (
                <div className="kv-row" key={k}>
                  <span className="kv-key" style={{ fontSize: '11px' }}>{k}</span>
                  <span className="kv-val mono" style={{ maxWidth: '160px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: '10px' }}>
                    {typeof v === 'boolean' ? (v ? '✓ true' : '✗ false') : String(v).substring(0, 40)}
                  </span>
                </div>
              ))}
          </div>
        </div>
      )}
    </div>
  );
}
