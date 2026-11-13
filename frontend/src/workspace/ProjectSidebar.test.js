import React from 'react';
import {render, screen, fireEvent, waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {ProjectDialog, RenameDialog} from './ProjectSidebar';

beforeEach(() => { delete window.showDirectoryPicker; });
test('creates a named project without requesting a folder', async () => {
  const save=jest.fn().mockResolvedValue(), close=jest.fn();
  render(<ProjectDialog onSave={save} onClose={close}/>);
  fireEvent.change(screen.getByLabelText('Project name'),{target:{value:'  Research  '}});
  fireEvent.click(screen.getByRole('button',{name:'Create project'}));
  await waitFor(() => expect(save).toHaveBeenCalledWith('Research'));
  await waitFor(() => expect(close).toHaveBeenCalled());
});
test('uses only the folder name, and preserves a separate custom name', async () => {
  const enumerate=jest.fn();
  window.showDirectoryPicker=jest.fn().mockResolvedValue({name:'Client documents',entries:enumerate});
  const save=jest.fn().mockResolvedValue();
  render(<ProjectDialog onSave={save} onClose={jest.fn()}/>);
  fireEvent.click(screen.getByRole('button',{name:/Choose folder/}));
  await screen.findByText('Client documents');
  expect(screen.getByLabelText('Project name')).toHaveValue('Client documents');
  fireEvent.click(screen.getByLabelText('Custom name'));
  fireEvent.change(screen.getByLabelText('Project name'),{target:{value:'Case notes'}});
  fireEvent.click(screen.getByLabelText('Use folder name'));
  expect(screen.getByLabelText('Project name')).toHaveValue('Client documents');
  fireEvent.click(screen.getByLabelText('Custom name'));
  expect(screen.getByLabelText('Project name')).toHaveValue('Case notes');
  fireEvent.click(screen.getByRole('button',{name:'Create project'}));
  await waitFor(() => expect(save).toHaveBeenCalledWith('Case notes'));
  expect(enumerate).not.toHaveBeenCalled();
});
test('cancelling the native picker keeps the entered name and does not create anything', async () => {
  window.showDirectoryPicker=jest.fn().mockRejectedValue({name:'AbortError'});
  const save=jest.fn();
  render(<ProjectDialog onSave={save} onClose={jest.fn()}/>);
  fireEvent.change(screen.getByLabelText('Project name'),{target:{value:'Keep me'}});
  fireEvent.click(screen.getByRole('button',{name:/Choose folder/}));
  await waitFor(() => expect(window.showDirectoryPicker).toHaveBeenCalled());
  expect(screen.getByLabelText('Project name')).toHaveValue('Keep me');
  expect(save).not.toHaveBeenCalled();
});
test('folder-input fallback reads metadata without reading any file bytes', () => {
  render(<ProjectDialog onSave={jest.fn()} onClose={jest.fn()}/>);
  const file=new File(['sensitive test contents'],'notes.txt');
  Object.defineProperty(file,'webkitRelativePath',{value:'Folder choice/subfolder/notes.txt'});
  file.text=jest.fn(); file.arrayBuffer=jest.fn();
  fireEvent.change(screen.getByLabelText('Choose project folder'),{target:{files:[file]}});
  expect(screen.getByLabelText('Project name')).toHaveValue('Folder choice');
  expect(file.text).not.toHaveBeenCalled(); expect(file.arrayBuffer).not.toHaveBeenCalled();
});
test('empty folder fallback leaves manual naming available', () => {
  render(<ProjectDialog onSave={jest.fn()} onClose={jest.fn()}/>);
  fireEvent.change(screen.getByLabelText('Choose project folder'),{target:{files:[]}});
  expect(screen.getByLabelText('Project name')).toBeEnabled();
  expect(screen.getByRole('button',{name:'Create project'})).toBeDisabled();
});
test('a picker permission error keeps manual project creation available', async () => {
  window.showDirectoryPicker=jest.fn().mockRejectedValue({name:'SecurityError'});
  render(<ProjectDialog onSave={jest.fn()} onClose={jest.fn()}/>);
  fireEvent.click(screen.getByRole('button',{name:/Choose folder/}));
  await screen.findByText(/Folder selection is unavailable/);
  expect(screen.getByLabelText('Project name')).toBeEnabled();
});
test('a duplicate-name error leaves the dialog and entered name available', async () => {
  render(<ProjectDialog onSave={jest.fn().mockRejectedValue({response:{data:{msg:'You already have a project with this name.'}}})} onClose={jest.fn()}/>);
  fireEvent.change(screen.getByLabelText('Project name'),{target:{value:'Duplicate'}});
  fireEvent.click(screen.getByRole('button',{name:'Create project'}));
  expect(await screen.findByRole('alert')).toHaveTextContent('already have a project');
  expect(screen.getByLabelText('Project name')).toHaveValue('Duplicate');
  expect(screen.getByRole('button',{name:'Create project'})).toBeEnabled();
});
test('modal focus is trapped and Escape returns focus to its opener', async () => {
  const user=userEvent.setup();
  function Example(){const [open,setOpen]=React.useState(false);return <><button onClick={()=>setOpen(true)}>Open</button>{open&&<ProjectDialog onSave={jest.fn()} onClose={()=>setOpen(false)}/>}</>;}
  render(<Example/>);
  await user.click(screen.getByRole('button',{name:'Open'}));
  expect(screen.getByLabelText('Project name')).toHaveFocus();
  await user.tab();
  expect(screen.getByRole('button',{name:'Cancel',exact:true})).toHaveFocus();
  await user.tab();
  expect(screen.getByRole('button',{name:'Close New project'})).toHaveFocus();
  await user.keyboard('{Escape}');
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(screen.getByRole('button',{name:'Open'})).toHaveFocus();
});
test('renaming keeps the existing conversation name until saved', async () => {
  const save=jest.fn().mockResolvedValue();
  render(<RenameDialog kind="conversation" name="Old title" onSave={save} onClose={jest.fn()}/>);
  fireEvent.change(screen.getByLabelText('Conversation name'),{target:{value:'New title'}});
  fireEvent.click(screen.getByRole('button',{name:'Save name'}));
  await waitFor(() => expect(save).toHaveBeenCalledWith('New title'));
});
