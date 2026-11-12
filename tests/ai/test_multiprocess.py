import pytest

pytestmark = pytest.mark.skip(reason="Legacy live-LLM multiprocessing script; not suitable for CI unit tests.")

from backend.ai.multiprocess import launch_llm_call_process, launch_llm_call_process_j, launch_llm_call_process_j_id
from backend.util.free_law_tools import filter_raw_citations
from backend.ai.base import chat_completion_j
from backend.util.data_tools import extract_from_json
from backend.util.subprocess_tools import any_process_remaining
from time import time, sleep

def compare_time_v_loop():
    raw_citations = ['Alameda Newspapers, Inc. v. City of Oakland, 95 Cal. App. 4th 668 (2002) (elements include an agreement, wrongful act, and resultant damages)',
                     'inteliClear,LLC v ETC Glob.holdings,Inc 978 F.3d 653', 
                     '166 Cal. App 4th 404']
    
    start = time()
    for each_cit in raw_citations:
        print(each_cit)
        command = (f"Identify the legal citation first, then look for the case name, "
                   f"verify if the case name provided is valid and true for this citation.\n"
                   f"{each_cit}\n"
                   'Output using the following JSON format:\n{"true_case":".....","valid":"yes|no"}')
        # print(command)
        output = chat_completion_j(command)
        print(output)
        llm_case = extract_from_json('true_case', output)
        print('llm_case: ', llm_case)
    
    end = time()
    spent = end-start
    print("time spent: ", spent)   # for 30 loops: 26.3


    start = time()
    processes = []
    for each_cit in raw_citations:
        print(each_cit)
        command = (f"Identify the legal citation first, then look for the case name, "
                   f"verify if the case name provided is valid and true for this citation.\n"
                   f"{each_cit}\n"
                   'Output using the following JSON format:\n{"true_case":".....","valid":"yes|no"}')
        processes.append(launch_llm_call_process_j(command))
 
    print(f"finished launching {len(processes)} processes for Recommendation")
    while any_process_remaining(processes):
        sleep(1)
    print('all the subprocess LLM calls are done')
    # pack all the LLM results together
    llm_cases = []  # this data structure will be sent to the frontend elementchecker.js
    for proc in processes:
        returned = proc.stdout.read()
        llm_case = extract_from_json("true_case", returned)
        print(llm_case)
        llm_cases.append(llm_case)

    end = time()
    spent = end - start
    print("time spent: ", spent)  # for 30 subprocess, 9.49

    

if __name__ == "__main__":
    compare_time_v_loop()
