import React, {createRef} from 'react';
import {act, fireEvent, render, screen} from '@testing-library/react';
import WorkspaceUploads, {reviewFolder} from './WorkspaceUploads';
import {promptAttachments} from './promptAttachments';

const file = (name, path = `Research/${name}`, text = 'data') => {
  const value = new File([text], name);
  Object.defineProperty(value, 'webkitRelativePath', {value:path});
  return value;
};
function setup(options = {}) {
  const ref = createRef(), onUpload = jest.fn(), onError = jest.fn();
  const props = {project:'1', chat:'a', documents:[], onUpload, onError, ...options};
  const view = render(<WorkspaceUploads ref={ref} {...props}/>);
  const pickFolder = () => {
    Object.defineProperty(screen.getByLabelText('Upload project folder'), 'webkitdirectory', {value:true, configurable:true});
    act(() => ref.current.folder());
  };
  return {...view, ref, props, onUpload, onError, pickFolder};
}
test('paperclip files go straight to the conversation without a scope dialog', () => {
  const {ref, onUpload} = setup();
  act(() => ref.current.files('chat'));
  fireEvent.change(screen.getByLabelText('Add files'), {target:{files:[file('note.txt')]}});
  expect(onUpload).toHaveBeenCalledWith([{file:expect.any(File)}], {scope:'chat', scopeId:'a'});
  expect(screen.queryByRole('dialog')).toBeNull();
});
test('folder review preserves nested relative paths and reports skipped files', () => {
  const {pickFolder, onUpload} = setup(); pickFolder();
  fireEvent.change(screen.getByLabelText('Upload project folder'), {target:{files:[file('a.txt'),file('b.csv','Research/nested/b.csv'),file('logo.png'),file('~$locked.docx'),file('empty.txt',undefined,'')]}});
  expect(onUpload).not.toHaveBeenCalled();
  expect(screen.getByText('nested/b.csv')).toBeVisible();
  expect(screen.getByText('3 skipped files')).toBeVisible();
  fireEvent.click(screen.getByRole('button',{name:'Upload 2 files'}));
  expect(onUpload).toHaveBeenCalledWith([
    {file:expect.any(File),relativePath:'a.txt'}, {file:expect.any(File),relativePath:'nested/b.csv'}
  ],{scope:'matter',scopeId:'1',folderName:'Research'});
});
test('folder selection can be reduced to fit the project quota', () => {
  const {pickFolder, onUpload} = setup({policy:{matter_files:1}}); pickFolder();
  fireEvent.change(screen.getByLabelText('Upload project folder'), {target:{files:[file('a.txt'),file('b.txt')]}});
  expect(screen.getByRole('button',{name:'Upload 2 files'})).toBeDisabled();
  fireEvent.click(screen.getByLabelText('b.txt'));
  fireEvent.click(screen.getByRole('button',{name:'Upload 1 file'}));
  expect(onUpload.mock.calls[0][0]).toHaveLength(1);
});
test('a pending folder upload can resume when it already occupies the last project slot', () => {
  const {pickFolder,onUpload}=setup({policy:{matter_files:1},documents:[{scope:'matter',needs_reupload:true,filename:'a.txt',file_size:4,relative_path:'a.txt',folder_name:'Research'}]});
  pickFolder();
  fireEvent.change(screen.getByLabelText('Upload project folder'),{target:{files:[file('a.txt')]}});
  fireEvent.click(screen.getByRole('button',{name:'Upload 1 file'}));
  expect(onUpload).toHaveBeenCalledTimes(1);
});
test('matching filenames in another folder do not resume that folder’s pending files', () => {
  const {pickFolder}=setup({policy:{matter_files:1},documents:[{scope:'matter',needs_reupload:true,filename:'a.txt',file_size:4,relative_path:'a.txt',folder_name:'Other folder'}]});
  pickFolder();fireEvent.change(screen.getByLabelText('Upload project folder'),{target:{files:[file('a.txt')]}});
  expect(screen.getByRole('button',{name:'Upload 1 file'})).toBeDisabled();
});
test('cancelling a folder review uploads nothing and restores focus', () => {
  const {pickFolder, onUpload} = setup();
  const trigger = document.createElement('button'); document.body.append(trigger); trigger.focus();
  pickFolder();
  fireEvent.change(screen.getByLabelText('Upload project folder'), {target:{files:[file('a.txt')]}});
  fireEvent.keyDown(document,{key:'Escape'});
  expect(onUpload).not.toHaveBeenCalled();
  expect(trigger).toHaveFocus(); trigger.remove();
});
test('an empty or cancelled directory selection has no side effects', () => {
  const {pickFolder, onUpload} = setup(); pickFolder();
  fireEvent.change(screen.getByLabelText('Upload project folder'), {target:{files:[]}});
  expect(onUpload).not.toHaveBeenCalled(); expect(screen.queryByRole('dialog')).toBeNull();
});
test('unsupported directory pickers retain individual file uploads', () => {
  const {ref,onError} = setup(); act(() => ref.current.folder());
  expect(onError).toHaveBeenCalledWith(expect.stringContaining('Use Upload files'));
});
test('late picker results cannot upload to a newly selected conversation', () => {
  const {ref, rerender, props, onUpload, onError} = setup();
  act(() => ref.current.files('chat'));
  rerender(<WorkspaceUploads ref={ref} {...props} chat="b"/>);
  fireEvent.change(screen.getByLabelText('Add files'), {target:{files:[file('a.txt')]}});
  expect(onUpload).not.toHaveBeenCalled(); expect(onError).toHaveBeenCalled();
});
test('navigation dismisses an unconfirmed folder review without uploading', () => {
  const {ref,rerender,props,pickFolder,onUpload}=setup(); pickFolder();
  fireEvent.change(screen.getByLabelText('Upload project folder'),{target:{files:[file('a.txt')]}});
  rerender(<WorkspaceUploads ref={ref} {...props} project="2" chat="c"/>);
  expect(screen.queryByRole('dialog')).toBeNull(); expect(onUpload).not.toHaveBeenCalled();
});
test('folder review rejects traversal and oversized files', () => {
  const result=reviewFolder([file('a.txt','Research/../a.txt'),file('b.txt')],{matter_file_bytes:2});
  expect(result.entries).toHaveLength(0); expect(result.skipped).toHaveLength(2);
});
test('attachment metadata accepts historical names without modifying the source', () => {
  const metadata={promptAttachments:[{fileId:'a',fileName:'A.txt'},{file_id:'b',filename:'B.csv'},{file_id:'c',name:'C.docx'}]};
  const original=JSON.stringify(metadata);
  expect(promptAttachments(metadata)).toEqual([{id:'a',name:'A.txt'},{id:'b',name:'B.csv'},{id:'c',name:'C.docx'}]);
  expect(JSON.stringify(metadata)).toBe(original);
});
