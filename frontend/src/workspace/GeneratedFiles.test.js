import React from 'react';
import {act, fireEvent, render, screen, waitFor} from '@testing-library/react';
import axios from 'axios';
import {ensureHeaders} from '../auth/useSetHeaders';
import GeneratedFiles from './GeneratedFiles';
import ConversationView from './ConversationView';

jest.mock('axios');
jest.mock('marked', () => jest.requireActual('../../node_modules/marked/lib/marked.umd.js'));
jest.mock('../auth/useSetHeaders', () => ({ensureHeaders:jest.fn().mockResolvedValue()}));
jest.mock('./AgentProgress', () => () => <div>Saved results</div>);

const file={file_id:'recalculated-file',execution_id:'execution-final',filename:'payroll.xlsx',file_size:12000};
let click;
beforeEach(() => {
  jest.clearAllMocks();
  ensureHeaders.mockResolvedValue();
  Object.defineProperty(window.URL,'createObjectURL',{configurable:true,value:jest.fn().mockReturnValue('blob:output')});
  Object.defineProperty(window.URL,'revokeObjectURL',{configurable:true,value:jest.fn()});
  click=jest.spyOn(HTMLAnchorElement.prototype,'click').mockImplementation(() => {});
});
afterEach(() => click.mockRestore());

test('downloads the explicitly selected version rather than inferring from its filename or supplied URL',async () => {
  axios.get.mockResolvedValue({data:new Blob(['recalculated workbook'])});
  render(<GeneratedFiles runId="run-final" files={[{...file,download_url:'https://untrusted.invalid/payroll.xlsx',storage_key:'/private/old/payroll.xlsx'}]}/>);
  fireEvent.click(screen.getByRole('button',{name:'Download payroll.xlsx',exact:true}));
  await waitFor(() => expect(click).toHaveBeenCalledTimes(1));
  expect(ensureHeaders).toHaveBeenCalledTimes(1);
  expect(axios.get).toHaveBeenCalledWith(expect.stringContaining('runs/run-final/artifacts/execution-final/files/recalculated-file'),{responseType:'blob'});
  expect(window.URL.revokeObjectURL).toHaveBeenCalledWith('blob:output');
  expect(document.querySelector('a[download]')).toBeNull();
});

test('two same-name selections remain distinct files and use their individual saved IDs',async () => {
  axios.get.mockResolvedValue({data:new Blob(['workbook'])});
  render(<GeneratedFiles runId="run" files={[{...file,file_id:'first',execution_id:'exec-first'},file]}/>);
  const buttons=screen.getAllByRole('button',{name:'Download payroll.xlsx',exact:true});
  fireEvent.click(buttons[0]);await waitFor(() => expect(click).toHaveBeenCalledTimes(1));
  expect(axios.get.mock.calls[0][0]).toContain('exec-first/files/first');
  fireEvent.click(buttons[1]);await waitFor(() => expect(click).toHaveBeenCalledTimes(2));
  expect(axios.get.mock.calls[1][0]).toContain('execution-final/files/recalculated-file');
});

test('a failed download keeps the file available to retry and does not create an object URL',async () => {
  axios.get.mockRejectedValueOnce(new Error('Unavailable')).mockResolvedValueOnce({data:new Blob(['retry'])});
  render(<GeneratedFiles runId="run" files={[file]}/>);
  const button=screen.getByRole('button',{name:'Download payroll.xlsx',exact:true});
  fireEvent.click(button);
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not download this file');
  expect(window.URL.createObjectURL).not.toHaveBeenCalled();expect(button).toBeEnabled();
  fireEvent.click(button);await waitFor(() => expect(click).toHaveBeenCalledTimes(1));
  expect(screen.queryByRole('alert')).toBeNull();
});

test('a download response arriving after navigation cannot download the previous conversation file',async () => {
  let release;
  axios.get.mockImplementationOnce(() => new Promise(resolve => {release=resolve;}));
  const {rerender}=render(<GeneratedFiles runId="run-a" scope="chat-a" files={[file]}/>);
  fireEvent.click(screen.getByRole('button',{name:'Download payroll.xlsx'}));
  await waitFor(() => expect(axios.get).toHaveBeenCalledTimes(1));
  rerender(<GeneratedFiles runId="run-b" scope="chat-b" files={[{...file,filename:'other.xlsx'}]}/>);
  await act(async () => release({data:new Blob(['old bytes'])}));
  expect(click).not.toHaveBeenCalled();expect(window.URL.createObjectURL).not.toHaveBeenCalled();
  expect(screen.getByRole('button',{name:'Download other.xlsx'})).toBeEnabled();
});

test('navigation while authentication headers load prevents the stale file request',async () => {
  let release;
  ensureHeaders.mockImplementationOnce(() => new Promise(resolve => {release=resolve;}));
  const {rerender}=render(<GeneratedFiles runId="run-a" files={[file]}/>);
  fireEvent.click(screen.getByRole('button',{name:'Download payroll.xlsx'}));
  rerender(<GeneratedFiles runId="run-b" files={[]}/>);
  await act(async () => release());
  expect(axios.get).not.toHaveBeenCalled();expect(click).not.toHaveBeenCalled();
});

test('restored conversation metadata shows selected files below the answer, and legacy turns keep saved results', () => {
  const content={id:'chat',document_id:'doc',document:{},history:[{id:'turn',run_id:'run',query:'Analyze',response:'[Model download](sandbox:/output/payroll.xlsx)',metadata:{output_files:[file]}},{id:'legacy',run_id:'older',query:'Earlier',response:'Earlier answer'}]};
  const view=render(<ConversationView content={content}/>);
  expect(screen.getByRole('button',{name:'Download payroll.xlsx'})).toBeVisible();
  expect(screen.getAllByText('Saved results')).toHaveLength(2);
  expect(screen.queryByRole('link',{name:'Model download'})).toBeNull();
  expect(axios.get).not.toHaveBeenCalled();
  view.unmount();render(<ConversationView content={JSON.parse(JSON.stringify(content))}/>);
  expect(screen.getByRole('button',{name:'Download payroll.xlsx'})).toBeVisible();
});

test('filenames render as text and incomplete file metadata does not invent a download path', () => {
  const name='<img src=x onerror=alert(1)>.xlsx';
  const {container}=render(<GeneratedFiles runId="run" files={[{...file,filename:name},{filename:'missing-id.xlsx'}]}/>);
  expect(screen.getByRole('button',{name:`Download ${name}`})).toBeVisible();
  expect(container.querySelector('img')).toBeNull();
  expect(screen.queryByRole('button',{name:'Download missing-id.xlsx'})).toBeNull();
});
