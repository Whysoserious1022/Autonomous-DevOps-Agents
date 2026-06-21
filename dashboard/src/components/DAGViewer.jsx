// dashboard/src/components/DAGViewer.jsx – Enhanced pipeline flow visualization
import React, { useEffect, useState } from 'react';
import ReactFlow, { Controls, Background, MarkerType, Handle, Position } from 'reactflow';
import 'reactflow/dist/style.css';

const PIPELINE = ['explorer', 'planner', 'coder', 'tester', 'reviewer', 'pr_creator'];

const NODE_META = {
  explorer:   { label: 'Explorer',    icon: '🔍', desc: 'Repo analysis' },
  planner:    { label: 'Planner',     icon: '🌳', desc: 'ToT planning' },
  coder:      { label: 'Coder',       icon: '⚙️', desc: 'ReAct coding' },
  tester:     { label: 'Tester',      icon: '🧪', desc: 'Docker sandbox' },
  reviewer:   { label: 'Reviewer',    icon: '🔒', desc: 'Security gate' },
  pr_creator: { label: 'PR Creator',  icon: '🚀', desc: 'GitHub PR' },
};

const STATUS_STYLES = {
  completed:          { bg: 'rgba(52,211,153,0.10)',  border: '#34d399', dot: '#34d399' },
  skipped:            { bg: 'rgba(34,211,238,0.10)',  border: '#22d3ee', dot: '#22d3ee' },
  running:            { bg: 'rgba(251,191,36,0.12)',  border: '#fbbf24', dot: '#fbbf24', glow: '0 0 20px rgba(251,191,36,0.5)' },
  failed:             { bg: 'rgba(248,113,113,0.10)', border: '#f87171', dot: '#f87171' },
  permanently_failed: { bg: 'rgba(248,113,113,0.10)', border: '#f87171', dot: '#f87171' },
  pending:            { bg: 'rgba(255,255,255,0.02)', border: 'rgba(255,255,255,0.10)', dot: '#4b5563' },
};

function PipelineNode({ data }) {
  const { step, meta, selected } = data;
  const status = step?.status || 'pending';
  const style = STATUS_STYLES[status] || STATUS_STYLES.pending;

  return (
    <div style={{
      background: style.bg,
      border: `2px solid ${selected ? '#818cf8' : style.border}`,
      borderRadius: '12px',
      padding: '12px 14px',
      textAlign: 'center',
      cursor: 'pointer',
      minWidth: '120px',
      boxShadow: selected
        ? '0 0 0 3px rgba(129,140,248,0.3)'
        : style.glow || 'none',
      transition: 'all 0.25s',
      backdropFilter: 'blur(10px)',
      position: 'relative',
    }}>
      <Handle
        type="target"
        position={Position.Left}
        style={{
          background: style.border,
          width: '6px',
          height: '6px',
          border: 'none',
          boxShadow: `0 0 4px ${style.border}`,
        }}
      />
      <div style={{ fontSize: '18px', marginBottom: '3px' }}>{meta.icon}</div>
      <div style={{ fontWeight: 700, fontSize: '12px', color: '#f1f5f9', letterSpacing: '0.02em' }}>
        {meta.label}
      </div>
      <div style={{ fontSize: '9px', color: '#64748b', marginTop: '2px' }}>{meta.desc}</div>
      <div style={{ marginTop: '6px', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '4px' }}>
        <div style={{ width: '5px', height: '5px', borderRadius: '50%', background: style.dot,
          boxShadow: status === 'running' ? `0 0 6px ${style.dot}` : 'none' }} />
        <span style={{ fontSize: '9px', color: style.dot, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.04em' }}>
          {status === 'permanently_failed' ? 'failed' : status}
        </span>
      </div>
      {step?.retry_count > 0 && (
        <div style={{ fontSize: '9px', color: '#fbbf24', marginTop: '3px' }}>
          retry {step.retry_count}×
        </div>
      )}
      <Handle
        type="source"
        position={Position.Right}
        style={{
          background: style.border,
          width: '6px',
          height: '6px',
          border: 'none',
          boxShadow: `0 0 4px ${style.border}`,
        }}
      />
    </div>
  );
}

const nodeTypes = { pipeline: PipelineNode };

export default function DAGViewer({ steps = {}, onSelectNode, selectedNode }) {
  const [nodes, setNodes] = useState([]);
  const [edges, setEdges] = useState([]);

  useEffect(() => {
    const spacing = 170;
    const startX = 60;
    const y = 130;

    const generatedNodes = PIPELINE.map((name, idx) => ({
      id: name,
      type: 'pipeline',
      position: { x: startX + idx * spacing, y },
      data: {
        step: steps[name] || { status: 'pending', retry_count: 0 },
        meta: NODE_META[name],
        selected: selectedNode === name,
      },
    }));

    const getEdgeColor = (fromName) => {
      const s = steps[fromName]?.status;
      if (s === 'completed' || s === 'skipped') return '#34d399';
      if (s === 'running') return '#fbbf24';
      return 'rgba(100,116,139,0.4)';
    };

    const generatedEdges = PIPELINE.slice(0, -1).map((name, idx) => ({
      id: `e-${name}-${PIPELINE[idx + 1]}`,
      source: name,
      target: PIPELINE[idx + 1],
      animated: steps[name]?.status === 'running',
      style: { stroke: getEdgeColor(name), strokeWidth: 2 },
      markerEnd: { type: MarkerType.ArrowClosed, color: getEdgeColor(name) },
    }));

    // Retry loop: tester → coder
    const retryCount = steps['tester']?.retry_count || 0;
    const testerFailed = steps['tester']?.status === 'failed';
    generatedEdges.push({
      id: 'e-retry-loop',
      source: 'tester',
      target: 'coder',
      type: 'smoothstep',
      label: retryCount > 0 ? `↩ Retry #${retryCount}` : '↩ Retry',
      labelStyle: { fill: '#f87171', fontSize: 9, fontWeight: 700 },
      labelBgStyle: { fill: '#0f1728', fillOpacity: 0.9 },
      style: {
        stroke: testerFailed ? '#f87171' : 'rgba(248,113,113,0.2)',
        strokeWidth: 2,
        strokeDasharray: '6,4',
      },
      markerEnd: { type: MarkerType.ArrowClosed, color: '#f87171' },
    });

    setNodes(generatedNodes);
    setEdges(generatedEdges);
  }, [steps, selectedNode]);

  return (
    <div style={{ width: '100%', height: '100%', background: 'radial-gradient(circle at 50% 50%, rgba(99,102,241,0.04) 0%, transparent 70%)' }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodeClick={(_, node) => onSelectNode?.(node.id)}
        fitView
        fitViewOptions={{ padding: 0.25 }}
        proOptions={{ hideAttribution: true }}
        zoomOnScroll={false}
      >
        <Controls
          showInteractive={false}
          style={{
            background: 'rgba(15,20,40,0.9)',
            border: '1px solid rgba(255,255,255,0.08)',
            borderRadius: '8px',
          }}
        />
        <Background color="rgba(255,255,255,0.04)" gap={24} size={1.5} />
      </ReactFlow>
    </div>
  );
}
