#
"""
Stand-alone LLM calls for parallel processing with the Subprocessing module.
"""
import sys
import json
from backend.ai.base import chat_completion_j

if __name__ == "__main__":
    # sys.argv expect prompt and id
    if len(sys.argv) < 3:
        sys.exit(1)

    llm_output = chat_completion_j(prompt=sys.argv[1])
    id = sys.argv[2]
    output = (id, llm_output)
    # Print the result as a JSON string
    print(json.dumps(output))