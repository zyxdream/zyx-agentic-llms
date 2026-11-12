
import subprocess

def any_process_remaining(p_a: list[subprocess.Popen]) -> bool:
    for p in p_a:
        if p.poll() is None:
            return True
    return False