import React from 'react';
import ReactDOM from 'react-dom/client';
import '@xterm/xterm/css/xterm.css';
import App from './App';
import './monacoSetup';
import './styles.css';
import './agentTypography.css';
import './workflow.css';
import './planReview.css';
import './agentSuggestions.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
