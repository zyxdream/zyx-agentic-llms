# file for counting tokens used
from backend.util.db import DiskMemory
import tiktoken

ENC_NAME = 'cl100k_base'


def num_tokens_from_string(string: str, encoding_name: str=ENC_NAME) -> int:
    """Returns the number of tokens in a text string."""
    encoding = tiktoken.get_encoding(encoding_name)
    num_tokens = len(encoding.encode(string))
    return num_tokens

if __name__ == "__main__":
    memory = DiskMemory('backend/memory/')

    total = 0
    total_chars = 0

    # draft FA section
    comand_prompt = open('backend/ai/prompts/DRAFT_FA').read()
    facts_for_prompt = " {} \n Plaintiff: {} \nDefendant: {}".format(
                                                                      memory['inputs/factualAllegations'],
                                                                      memory['inputs/plaintiff'],
                                                                      memory['inputs/defendant'])
    total += num_tokens_from_string(comand_prompt + facts_for_prompt)
    total_chars += len(comand_prompt + facts_for_prompt)
     
    # suggest PfRs
    comand_prompt = open('backend/ai/prompts/SUGGEST_PFR').read()
    total += num_tokens_from_string(comand_prompt + facts_for_prompt)
    total_chars += len(comand_prompt + facts_for_prompt)


    # suggest introduction
    comand_prompt = open('backend/ai/prompts/DRAFT_INTRO').read()
    total += num_tokens_from_string(comand_prompt + facts_for_prompt)
    total_chars += len(comand_prompt + facts_for_prompt)


    # suggest CoAs 
    comand_prompt = open('backend/ai/prompts/SUGGEST_COA').read()
    total += num_tokens_from_string(comand_prompt + facts_for_prompt)
    total_chars += len(comand_prompt + facts_for_prompt)


    # write parties and venue
    comand_prompt = open('backend/ai/prompts/DRAFT_PARTIESVENUE').read()
    facts = facts_for_prompt + "\n Plaintiff Address: {} \nDefendant Address: {} \n Desired Venue: {}".format(
        memory['inputs/plaintiffAddress'], 
        memory['inputs/defendantAddress'], 
        "United States District Court for the " + memory['inputs/venue']
    )
    total += num_tokens_from_string(comand_prompt + facts)
    total_chars += len(comand_prompt + facts_for_prompt)


    print(f'total tokens after step0: {total}')

    fa = " {} \n Plaintiff: {} \nDefendant: {}".format(memory['inputs/factualAllegations'],
                                                       memory['inputs/plaintiff'],
                                                       memory['inputs/defendant'])

    coas = ['(Direct Copyright Infringement)',
            '(Contributory Copyright Infringement)',
            '(Violation of the Digital Millennium Copyright Act)',
            '(Tortious Interference with Prospective Economic Advantage)',
            '(Intentional Infliction of Emotional Distress)']
    
    coa_block_str = "\n".join(coas)
    comand_prompt = open('backend/ai/prompts/DRAFT_COA').read()
    filled_in_prompt = "{} {} \nList of Facts: \n {}".format(comand_prompt, coa_block_str, fa)
    total += num_tokens_from_string(filled_in_prompt)
    total_chars += len(filled_in_prompt)

    print(f'total tokens after step1: {total}')

    fa = " {} \n Plaintiff: {} \nDefendant: {}".format(memory['inputs/factualAllegations'],
                                                       memory['inputs/plaintiff'],
                                                       memory['inputs/defendant'])
    pfrs = ['1. Damages for direct and contributory copyright infringement, including any profits Gawker Media, LLC made from the infringing act.',
            '2. An injunction preventing Gawker Media, LLC from future unauthorized publication, dissemination, or distribution of the screenplay.',
            '3. Cost of attorneys fees and litigation expenses.',
            '4. An order requiring Gawker Media, LLC to remove all unauthorized copies of the screenplay from their website.',
            '5. An order requiring Gawker Media, LLC to issue a public apology detailing its infringement on Quentin Tarantinos copyright.'
            ]
    pfr_block_str = "\n".join(pfrs)
    comand_prompt = open('backend/ai/prompts/DRAFT_PFR').read()
    filled_in_prompt = "{} {} \nList of Facts: \n {}".format(comand_prompt, pfr_block_str, fa)
    total += num_tokens_from_string(filled_in_prompt)
    total_chars += len(filled_in_prompt)

    print(f'total tokens after step2: {total}')
    print(f'total chars after step2: {total_chars}')

