// dashboard/src/components/DAGViewer.jsx
import React, { useEffect, useState } from 'react';
import ReactFlow, { MiniMap, Controls, Background, MarkerType } from 'reactflow';
import 'reactflow/dist/style.css';

const nodePositions = {
  explorer: { x: 50, y: 150 },
  planner: { x: 230, y: 150 },
  coder: { x: 410, y: 150 },
  tester: { x: 590, y: 150 },
  reviewer: { x: 770, y: 150 },
  pr_creator: { x: 950, y: 150 },
};

const getStatusStyles = (status) => {
  switch (status) {
    case 'completed':
      return { background: 'rgba(16, 185, 129, 0.12)', border: '2px solid #10b981', color: '#f8fafc' };
    case 'skipped':
      return { background: 'rgba(6, 182, 212, 0.12)', border: '2px solid #06b6d4', color: '#f8fafc' };
    case 'running':
      return { background: 'rgba(245, 158, 11, 0.12)', border: '2px solid #f59e0b', color: '#f8fafc', boxShadow: '0 0 15px rgba(245, 158, 11, 0.4)' };
    case 'failed':
    case 'permanently_failed':
      return { background: 'rgba(239, 68, 68, 0.12)', border: '2px solid #ef4444', color: '#f8fafc' };
    default:
      return { background: 'rgba(255, 255, 255, 0.02)', border: '1px solid rgba(255, 255, 255, 0.1)', color: '#9ca3af' };
  }
};

export default function DAGViewer({ steps = {}, onSelectNode }) {
  const [nodes, setNodes] = useState([]);
  const [edges, setEdges] = useState([]);

  useEffect(() => {
    // Generate nodes
    const generatedNodes = Object.keys(nodePositions).map((name) => {
      const step = steps[name] || { status: 'pending' };
      const baseStyle = getStatusStyles(step.status);
      const isSelected = step.status === 'running';

      return {
        id: name,
        data: { 
          label: (
            <div style={{ textAlign: 'center', padding: '6px' }}>
              <div style={{ fontWeight: 'bold', fontSize: '13px' }}>{name.replace('_', ' ').toUpperCase()}</div>
              <div style={{ fontSize: '10px', opacity: 0.8, textTransform: 'capitalize', marginTop: '2px' }}>
                {step.status === 'pending' ? 'idle' : step.status}
              </div>
            </div>
          ) 
        },
        position: nodePositions[name],
        style: {
          ...baseStyle,
          borderRadius: '8px',
          width: '140px',
          fontFamily: "'Outfit', sans-serif",
          cursor: 'pointer',
        },
      };
    });

    // Generate edges
    const generatedEdges = [
      { id: 'e-explorer-planner', source: 'explorer', target: 'planner', animated: steps['explorer']?.status === 'running' },
      { id: 'e-planner-coder', source: 'planner', target: 'coder', animated: steps['planner']?.status === 'running' },
      { id: 'e-coder-tester', source: 'coder', target: 'tester', animated: steps['coder']?.status === 'running' },
      
      // Tester to Reviewer
      { 
        id: 'e-tester-reviewer', 
        source: 'tester', 
        target: 'reviewer', 
        animated: steps['tester']?.status === 'completed',
        style: { stroke: steps['tester']?.status === 'completed' ? '#10b981' : '#6b7280' }
      },
      
      // Reviewer to PR Creator
      { 
        id: 'e-reviewer-pr_creator', 
        source: 'reviewer', 
        target: 'pr_creator', 
        animated: steps['reviewer']?.status === 'completed',
        style: { stroke: steps['reviewer']?.status === 'completed' ? '#10b981' : '#6b7280' }
      },
      
      // Loopback edge: Tester -> Coder (Retry feedback loop)
      {
        id: 'e-tester-coder-loop',
        source: 'tester',
        target: 'coder',
        type: 'smoothstep',
        label: steps['tester']?.retry_count > 0 ? `Retry #${steps['tester'].retry_count}` : 'Retry',
        labelStyle: { fill: '#ef4444', fontSize: 9, fontWeight: 600, background: '#111827' },
        style: { 
          stroke: steps['tester']?.status === 'failed' ? '#ef4444' : 'rgba(239, 68, 68, 0.2)',
          strokeWidth: 2, 
          strokeDasharray: '5,5' 
        },
        markerEnd: { type: MarkerType.ArrowClosed, color: '#ef4444' },
      }
    ];

    setNodes(generatedNodes);
    setEdges(generatedEdges);
  }, [steps]);

  const onNodeClick = (event, node) => {
    if (onSelectNode) {
      onSelectNode(node.id);
    }
  };

  return (
    <div className="react-flow-container">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodeClick={onNodeClick}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        proOptions={{ hideAttribution: true }}
      >
        <Controls showInteractive={false} style={{ background: '#1f2937', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '6px' }} />
        <Background color="#374151" gap={16} size={1} />
      </ReactFlow>
    </div>
  );
}
