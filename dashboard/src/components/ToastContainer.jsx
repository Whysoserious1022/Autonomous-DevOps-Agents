// dashboard/src/components/ToastContainer.jsx
import React from 'react';
import { CheckCircle, AlertCircle, Info } from 'lucide-react';

export default function ToastContainer({ toasts = [] }) {
  const icons = {
    success: <CheckCircle size={16} color="var(--color-success)" />,
    error: <AlertCircle size={16} color="var(--color-danger)" />,
    info: <Info size={16} color="var(--accent-primary)" />,
  };

  const colors = {
    success: 'var(--color-success)',
    error: 'var(--color-danger)',
    info: '#a5b4fc',
  };

  if (toasts.length === 0) return null;

  return (
    <div className="toast-container">
      {toasts.map(t => (
        <div key={t.id} className={`toast ${t.type}`}>
          {icons[t.type] || icons.info}
          <span style={{ color: colors[t.type] || '#f1f5f9', fontSize: '13px', lineHeight: 1.4 }}>
            {t.msg}
          </span>
        </div>
      ))}
    </div>
  );
}
