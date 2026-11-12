"""Cache public tokenizer data in this project before running offline checks."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ['TIKTOKEN_CACHE_DIR'] = str(ROOT / 'instance/cache/tiktoken')

if __name__ == '__main__':
    import tiktoken
    for name in ('cl100k_base',):
        tiktoken.get_encoding(name)
        print('Ready:', name)
