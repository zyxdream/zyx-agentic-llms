import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import axios from 'axios';
import Workspace from './Workspace';
jest.mock('axios', () => Object.assign(jest.fn(), {
  get: jest.fn(),
  post: jest.fn()
}));
jest.mock('../auth/useSetHeaders', () => ({
  ensureHeaders: jest.fn().mockResolvedValue()
}));
jest.mock('../upload/useDocumentPreparation', () => ({
  useDocumentPreparation: () => ({
    documents: [],
    excludedIds: new Set(),
    blocked: false,
    transferring: 0,
    clearExclusions: jest.fn(),
    refresh: async () => ({
      documents: []
    })
  })
}));
jest.mock('./AgentProgress', () => () => null);
jest.mock('./documentMarkdown', () => ({
  renderDocumentMarkdown: text => text
}));
const state = {
  id: 'chat',
  name: 'Review',
  document_id: 'authored',
  document_version: 1,
  document: {
    title: 'Notes',
    format_version: 2,
    source_format: 'markdown',
    markdown: '# Original'
  },
  history: [],
  active_run: null
};
beforeEach(() => {
  jest.clearAllMocks();
  localStorage.clear();
  axios.mockImplementation(async ({
    url,
    method,
    data
  }) => {
    if (url.endsWith('/projects')) return {
      data: {
        projects: [{
          id: 1,
          name: 'Research'
        }]
      }
    };
    if (url.endsWith('/projects/1/conversations')) return {
      data: {
        conversations: [{
          id: 'chat',
          name: 'Review'
        }]
      }
    };
    if (url.endsWith('/conversations/chat')) return {
      data: state
    };
    throw new Error(`Unexpected ${method} ${url}`);
  });
});
test('restores a conversation and preserves unsaved edits after a version conflict', async () => {
  render(<Workspace />);
  await screen.findByRole('heading', {
    name: 'Review'
  });
  fireEvent.click(screen.getByText('Edit Markdown'));
  const editor = screen.getByLabelText('Document Markdown');
  fireEvent.change(editor, {
    target: {
      value: '# My unsaved edits'
    }
  });
  axios.mockImplementationOnce(async () => {
    throw {
      response: {
        data: {
          msg: 'The document changed elsewhere. Your edits have been kept.'
        }
      }
    };
  });
  fireEvent.click(screen.getByText('Save document'));
  await screen.findByRole('alert');
  expect(editor.value).toBe('# My unsaved edits');
  const call = axios.mock.calls.at(-1)[0];
  expect(call.url).toContain('/documents/authored');
  expect(call.data.expected_version).toBe(1);
});
test('restores a saved clarification and sends the answer to that run', async () => {
  axios.mockImplementation(async ({
    url
  }) => {
    if (url.endsWith('/projects')) return {
      data: {
        projects: [{
          id: 1,
          name: 'Research'
        }]
      }
    };
    if (url.endsWith('/projects/1/conversations')) return {
      data: {
        conversations: [{
          id: 'chat',
          name: 'Review'
        }]
      }
    };
    if (url.endsWith('/conversations/chat')) return {
      data: {
        ...state,
        active_run: {
          run_id: 'wf_question',
          status: 'awaiting_user_confirmation',
          info: {
            status: 'Waiting'
          },
          value: {
            confirmation: {
              kind: 'agent_question',
              message: 'Which period?',
              options: []
            }
          }
        }
      }
    };
    if (url.endsWith('/runs/wf_question/confirm')) return {
      data: {
        run_id: 'wf_question'
      }
    };
    throw new Error(url);
  });
  render(<Workspace />);
  await screen.findByText('Which period?');
  fireEvent.change(screen.getByLabelText('Your answer'), {
    target: {
      value: 'Last quarter'
    }
  });
  fireEvent.click(screen.getByText('Continue'));
  await waitFor(() => expect(axios).toHaveBeenCalledWith(expect.objectContaining({
    method: 'post',
    url: expect.stringContaining('runs/wf_question/confirm'),
    data: {
      decision: {
        answer: 'Last quarter'
      }
    }
  })));
});
test('reuses a submission identity when the HTTP response is lost', async () => {
  let sent = 0;
  const normal = axios.getMockImplementation();
  axios.mockImplementation(async request => {
    if (request.url.endsWith('/runs')) {
      sent++;
      if (sent === 1) throw new Error('Connection lost');
      return {
        data: {
          run_id: request.data.run_id,
          status: 'running',
          info: {
            status: 'Working'
          }
        }
      };
    }
    return normal(request);
  });
  Object.defineProperty(window, 'crypto', {
    configurable: true,
    value: {randomUUID: jest.fn().mockReturnValueOnce('first-id').mockReturnValueOnce('second-id')}
  });
  render(<Workspace />);
  await screen.findByRole('heading', {
    name: 'Review'
  });
  fireEvent.change(screen.getByLabelText('Assignment'), {
    target: {
      value: 'Draft a summary'
    }
  });
  fireEvent.click(screen.getByText('Send'));
  await screen.findByRole('alert');
  fireEvent.click(screen.getByText('Send'));
  await waitFor(() => expect(sent).toBe(2));
  const calls = axios.mock.calls.map(c => c[0]).filter(c => c.url.endsWith('/runs'));
  expect(calls[0].data.run_id).toBe(calls[1].data.run_id);
});
