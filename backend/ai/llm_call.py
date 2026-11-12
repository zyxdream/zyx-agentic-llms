# 
"""
Stand-alone LLM calls for parallel processing with the Subprocessing module.
"""
import sys

from backend.ai.base import chat_completion

if __name__ == "__main__":
    if len(sys.argv) < 2: 
        sys.exit(1)

    llm_output = chat_completion(prompt=sys.argv[1])
    print(llm_output)