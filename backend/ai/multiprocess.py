from backend.constants import SOURCE_CODE_ROOT, PYTHON_EXECUTABLE
import subprocess
import os


def launch_llm_call_process(prompt:str) -> subprocess.Popen:
    # launch subprocess llm_call.py
    os.environ['PYTHONPATH'] = SOURCE_CODE_ROOT + os.pathsep + os.environ.get('PYTHONPATH', '')
    process = subprocess.Popen([PYTHON_EXECUTABLE,
                             'backend/ai/llm_call.py',
                             prompt],
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, 
                             text=True)
    return process


def launch_llm_call_process_id(prompt:str, id:str) -> subprocess.Popen:
    # launch subprocess llm_call.py
    os.environ['PYTHONPATH'] = SOURCE_CODE_ROOT + os.pathsep + os.environ.get('PYTHONPATH', '')
    process = subprocess.Popen([PYTHON_EXECUTABLE,
                             'backend/ai/llm_call_id.py',
                             prompt, id],
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, 
                             text=True)
    return process


def launch_llm_call_process_j(prompt:str) -> subprocess.Popen:
    # launch subprocess llm_call.py
    os.environ['PYTHONPATH'] = SOURCE_CODE_ROOT + os.pathsep + os.environ.get('PYTHONPATH', '')
    process = subprocess.Popen([PYTHON_EXECUTABLE,
                             'backend/ai/llm_call_j.py',
                             prompt],
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
    return process


def launch_llm_call_process_j_id(prompt:str, id:str) -> subprocess.Popen:
    # launch subprocess llm_call.py
    os.environ['PYTHONPATH'] = SOURCE_CODE_ROOT + os.pathsep + os.environ.get('PYTHONPATH', '')

    process = subprocess.Popen([PYTHON_EXECUTABLE,
                             'backend/ai/llm_call_j_id.py',
                             prompt, id],
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,
                            text=True)
    return process


def launch_llm_multicall_process_j_id(id: str, prev_prompt: str, prev_output: str, new_prompt: str) -> subprocess.Popen:
    # launch subprocess llm_call.py
    os.environ['PYTHONPATH'] = SOURCE_CODE_ROOT + os.pathsep + os.environ.get('PYTHONPATH', '')
    process = subprocess.Popen([PYTHON_EXECUTABLE, 'backend/ai/llm_multicall_j_id.py', id, prev_prompt, prev_output, new_prompt],
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,
                            text=True)
    return process