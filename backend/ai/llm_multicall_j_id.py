#
"""
Stand-alone LLM calls for parallel processing with the Subprocessing module.
"""
import sys
import json
from backend.ai.base import multichat_completion_j

if __name__ == "__main__":
    # sys.argv expect scrip, id, prev_prompt, prev_output, new_prompt
    if len(sys.argv) < 3:
        sys.exit(1)

    id = sys.argv[1]
    llm_output = multichat_completion_j(sys.argv[2], sys.argv[3], sys.argv[4])
    output = (id, llm_output)
    # Print the result as a JSON string
    print(json.dumps(output))