// dashboard/src/components/KnowledgeGraphPanel.jsx
// Displays the AST knowledge graph from the Explorer agent
import React, { useEffect, useState, useCallback } from 'react';
import { GitBranch, FileCode, Box, ChevronDown, ChevronRight, Layers } from 'lucide-react';

const API = 'http://localhost:8000';

function FileTree({ files = [] }) {
  const [expanded, setExpanded] = useState({});

  const toggle = (path) => setExpanded(prev => ({ ...prev, [path]: !prev[path] }));

  if (!files.length) return (
    <div style={{ color: 'rgba(255,255,255,0.3)', fontSize: '12px', padding: '8px 0' }}>
      No files analyzed.
    </div>
  );

  return (
    <div style={{ fontFamily: 'monospace', fontSize: '11px' }}>
      {files.map((file, i) => {
        const hasDetails = (file.classes?.length > 0 || file.functions?.length > 0);
        const isExpanded = expanded[file.path];
        return (
          <div key={i} style={{ marginBottom: '2px' }}>
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
                padding: '4px 6px',
                borderRadius: '5px',
                cursor: hasDetails ? 'pointer' : 'default',
                background: 'rgba(255,255,255,0.03)',
                border: '1px solid rgba(255,255,255,0.05)',
                transition: 'background 0.15s',
              }}
              onClick={() => hasDetails && toggle(file.path)}
              onMouseEnter={e => e.currentTarget.style.background = 'rgba(255,255,255,0.06)'}
              onMouseLeave={e => e.currentTarget.style.background = 'rgba(255,255,255,0.03)'}
            >
              {hasDetails ? (
                isExpanded ? <ChevronDown size={10} color="rgba(255,255,255,0.4)" /> : <ChevronRight size={10} color="rgba(255,255,255,0.4)" />
              ) : (
                <span style={{ width: 10 }} />
              )}
              <FileCode size={11} color="#a5b4fc" />
              <span style={{ color: 'rgba(255,255,255,0.75)', flex: 1 }}>{file.path}</span>
              <div style={{ display: 'flex', gap: '4px' }}>
                {file.classes?.length > 0 && (
                  <span style={{
                    fontSize: '9px',
                    padding: '1px 5px',
                    borderRadius: '99px',
                    background: 'rgba(99,102,241,0.2)',
                    color: '#a5b4fc',
                  }}>
                    {file.classes.length} cls
                  </span>
                )}
                {file.functions?.length > 0 && (
                  <span style={{
                    fontSize: '9px',
                    padding: '1px 5px',
                    borderRadius: '99px',
                    background: 'rgba(52,211,153,0.15)',
                    color: '#34d399',
                  }}>
                    {file.functions.length} fn
                  </span>
                )}
              </div>
            </div>

            {isExpanded && (
              <div style={{ paddingLeft: '22px', marginTop: '2px', display: 'flex', flexDirection: 'column', gap: '2px' }}>
                {file.classes?.map((cls, j) => (
                  <div key={j} style={{ display: 'flex', alignItems: 'center', gap: '6px', padding: '2px 6px', color: '#a5b4fc' }}>
                    <Box size={9} />
                    <span>{cls.name}</span>
                    {cls.methods?.length > 0 && (
                      <span style={{ fontSize: '9px', color: 'rgba(255,255,255,0.3)' }}>
                        ({cls.methods.length} methods)
                      </span>
                    )}
                  </div>
                ))}
                {file.functions?.map((fn, j) => (
                  <div key={j} style={{ display: 'flex', alignItems: 'center', gap: '6px', padding: '2px 6px', color: '#34d399' }}>
                    <span style={{ fontSize: '10px', opacity: 0.6 }}>ƒ</span>
                    <span>{fn.name}</span>
                    {fn.is_async && (
                      <span style={{ fontSize: '9px', color: 'rgba(255,255,255,0.3)' }}>async</span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

export default function KnowledgeGraphPanel({ runId }) {
  const [graph, setGraph] = useState(null);
  const [loading, setLoading] = useState(true);

  const fetchGraph = useCallback(async () => {
    if (!runId) return;
    try {
      const res = await fetch(`${API}/api/runs/${runId}/knowledge-graph`);
      if (res.ok) {
        const data = await res.json();
        setGraph(data);
      }
    } catch {} finally {
      setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    setLoading(true);
    fetchGraph();
  }, [runId, fetchGraph]);

  if (loading) return (
    <div style={{ padding: '20px', textAlign: 'center', color: 'rgba(255,255,255,0.3)', fontSize: '12px' }}>
      Loading knowledge graph…
    </div>
  );

  if (!graph?.available) return (
    <div style={{
      padding: '24px',
      textAlign: 'center',
      color: 'rgba(255,255,255,0.25)',
      fontSize: '12px',
    }}>
      <Layers size={28} style={{ marginBottom: '8px', opacity: 0.3 }} />
      <div>{graph?.message || 'Explorer step not completed yet.'}</div>
    </div>
  );

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
      {/* Metadata */}
      <div style={{
        background: 'rgba(99,102,241,0.08)',
        border: '1px solid rgba(99,102,241,0.15)',
        borderRadius: '8px',
        padding: '10px 12px',
        fontSize: '11px',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '6px' }}>
          <GitBranch size={12} color="#a5b4fc" />
          <span style={{ color: '#a5b4fc', fontWeight: '600' }}>Repository Graph</span>
        </div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '10px', color: 'rgba(255,255,255,0.5)' }}>
          <span>
            <span style={{ color: 'rgba(255,255,255,0.25)' }}>Repo: </span>
            {graph.repo_url?.replace('https://github.com/', '') || '—'}
          </span>
          <span>
            <span style={{ color: 'rgba(255,255,255,0.25)' }}>SHA: </span>
            <code style={{ fontFamily: 'monospace' }}>{graph.commit_sha?.substring(0, 12) || '—'}</code>
          </span>
          <span>
            <span style={{ color: 'rgba(255,255,255,0.25)' }}>Files: </span>
            {graph.total_files_analyzed}
          </span>
        </div>
        {graph.summary && (
          <div style={{ marginTop: '6px', color: 'rgba(255,255,255,0.4)', fontStyle: 'italic', fontSize: '10px' }}>
            {graph.summary}
          </div>
        )}
      </div>

      {/* File Tree */}
      <div style={{
        background: 'rgba(0,0,0,0.3)',
        border: '1px solid rgba(255,255,255,0.06)',
        borderRadius: '8px',
        padding: '10px',
        maxHeight: '380px',
        overflowY: 'auto',
      }}>
        <FileTree files={graph.files || []} />
      </div>
    </div>
  );
}
