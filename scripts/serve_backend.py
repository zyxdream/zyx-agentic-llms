"""Run this project's local API on its own configured port."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.app import create_app
from backend.config import PUBLIC
app=create_app()
if __name__=='__main__':app.run(host='127.0.0.1',port=PUBLIC['backendPort'],debug=False,use_reloader=False,threaded=True)
