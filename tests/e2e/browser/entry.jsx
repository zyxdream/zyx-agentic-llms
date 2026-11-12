import React from 'react';
import { createRoot } from 'react-dom/client';
import Workspace from '../../../frontend/src/workspace/Workspace';
import '../../../frontend/src/workspace/Workspace.css';
createRoot(document.getElementById('root')).render(<Workspace onSignOut={() => {}} />);
