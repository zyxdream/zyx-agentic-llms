import React from 'react';
import {createRoot} from 'react-dom/client';
import AuthGate from './auth/AuthGate';
import './workspace/Workspace.css';
createRoot(document.getElementById('root')).render(<AuthGate/>);
