import React from 'react';
import {render,screen,fireEvent,waitFor} from '@testing-library/react';
import axios from 'axios';
import AuthGate from './AuthGate';
import {readSession,setSessionState} from './useSetHeaders';
jest.mock('axios',()=>({post:jest.fn(),interceptors:{response:{use:jest.fn(),eject:jest.fn()}}}));
jest.mock('./useSetHeaders',()=>({readSession:jest.fn(),setSessionState:jest.fn()}));
jest.mock('../workspace/Workspace',()=>({onSignOut})=><div>Private Workspace<button onClick={onSignOut}>Sign out</button></div>);
beforeEach(()=>jest.clearAllMocks());
test('first-run setup creates a project-local account',async()=>{
 readSession.mockResolvedValue({user:null,setup_required:true,csrf_token:'setup'});
 axios.post.mockResolvedValue({data:{user:{id:1,email:'owner@example.test'},setup_required:false,csrf_token:'signed-in'}});
 render(<AuthGate/>);await screen.findByRole('button',{name:'Create local account'});
 fireEvent.change(screen.getByLabelText('Email'),{target:{value:'owner@example.test'}});
 fireEvent.change(screen.getByLabelText('Password'),{target:{value:'A project-only test passphrase'}});
 fireEvent.click(screen.getByRole('button',{name:'Create local account'}));await screen.findByText('Private Workspace');
 expect(axios.post).toHaveBeenCalledWith(expect.stringContaining('auth/setup'),{email:'owner@example.test',password:'A project-only test passphrase'});
});
test('an existing local session restores Workspace',async()=>{
 readSession.mockResolvedValue({user:{id:1,email:'owner@example.test'},setup_required:false,csrf_token:'session'});
 render(<AuthGate/>);await screen.findByText('Private Workspace');
 expect(setSessionState).toHaveBeenCalledWith(expect.objectContaining({user:expect.any(Object)}));
});
test('incorrect local credentials leave the sign-in form available',async()=>{
 readSession.mockResolvedValue({user:null,setup_required:false,csrf_token:'login'});
 axios.post.mockRejectedValue({response:{data:{msg:'Email or password is incorrect.'}}});
 render(<AuthGate/>);await screen.findByRole('button',{name:'Sign in'});
 fireEvent.change(screen.getByLabelText('Email'),{target:{value:'owner@example.test'}});
 fireEvent.change(screen.getByLabelText('Password'),{target:{value:'Wrong test passphrase'}});
 fireEvent.click(screen.getByRole('button',{name:'Sign in'}));await screen.findByRole('alert');
 expect(screen.queryByText('Private Workspace')).toBeNull();
});
