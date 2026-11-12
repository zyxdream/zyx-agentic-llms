
import sys
import os
from dotenv import load_dotenv
from backend.e2e_config import DEFAULT_E2E_CELERY_QUEUE, DEFAULT_E2E_REDIS_URL

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

# PDF parsing: "local" uses Tesseract OCR, "cloud" uses AWS Textract
PDF_OCR_BACKEND = "cloud"

STATIC_FOLDER = 'backend/static'
UPLOAD_FOLDER = 'backend/uploads'
DASHBOARD_IGNORED_USER_NAMES = ['jobe@lextext.ai', 'peter@lextext.ai', 'monica@lextext.ai', 'kyle@lextext.ai', 'hello@lextext.ai', 'lawrence@lextext.ai']

# Read the deployment target from .env so local updates don't require editing this file.
ENVIRONMENT = os.environ.get("ENVIRONMENT", "test")
E2E_MODE = os.environ.get("LEXINTEL_E2E") == "1"


if E2E_MODE:
    # E2E runs must opt into their own Redis/database resources before any
    # backend module creates a Redis client or Celery app.
    REDIS_URL = os.environ.get("E2E_REDIS_URL", DEFAULT_E2E_REDIS_URL)
    API_URL = os.environ.get("E2E_API_URL", "http://127.0.0.1:5000/")
    SOURCE_CODE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    PYTHON_EXECUTABLE = sys.executable
    DBURI = os.environ.get("E2E_DATABASE_URL", "sqlite:///e2e.db")
    # flask-cognito requires non-empty identifiers during app construction.
    # E2E runs can override these with a dedicated test pool when auth is used.
    COGNITO_USERPOOL = os.environ.get("E2E_COGNITO_USERPOOL", "us-east-1_x8GRP09XU")
    COGNITO_APP = os.environ.get("E2E_COGNITO_APP", "7lq6c66o0l77e7fest715caps8")

elif ENVIRONMENT == "production":
    REDIS_URL = "redis://localhost"
                 
    API_URL = 'https://api.lextext.ai/'
    SOURCE_CODE_ROOT = '/home/ubuntu/lexintel'
    PYTHON_EXECUTABLE = '/home/ubuntu/miniconda3/envs/lexintel/bin/python'
    DBURI = 'mysql+pymysql://admin:vk67-jK$z4L0@testdbserver-instance-1.cns4q40usfdz.us-east-1.rds.amazonaws.com:3306/prod'
    COGNITO_USERPOOL = 'us-east-1_EUWrFCow8'
    COGNITO_APP = '5cgjaf4p967ultmok8oebjst9b'

elif ENVIRONMENT == "test":
    REDIS_URL = "rediss://:@master.nocluster2.5gjkpo.use1.cache.amazonaws.com:6379/0?ssl_cert_reqs=none"

    API_URL = 'https://testapi.lextext.ai/'
    SOURCE_CODE_ROOT = '/home/ubuntu/lexintel'
    PYTHON_EXECUTABLE = '/home/ubuntu/miniconda3/envs/lexintel/bin/python'
    DBURI = 'mysql://admin:vk67-jK$z4L0@testdbserver-instance-1.cns4q40usfdz.us-east-1.rds.amazonaws.com:3306/test'
    COGNITO_USERPOOL = 'us-east-1_x8GRP09XU'
    COGNITO_APP = '7lq6c66o0l77e7fest715caps8'

elif ENVIRONMENT == "staging":
    REDIS_URL = "redis://localhost"

    API_URL = 'https://stagingapi.lextext.ai/'
    SOURCE_CODE_ROOT = '/home/ubuntu/lexintel'
    PYTHON_EXECUTABLE = '/home/ubuntu/miniconda3/envs/lexintel/bin/python'
    DBURI = 'mysql://admin:vk67-jK$z4L0@testdbserver-instance-1.cns4q40usfdz.us-east-1.rds.amazonaws.com:3306/test'
    COGNITO_USERPOOL = 'us-east-1_x8GRP09XU'
    COGNITO_APP = '7lq6c66o0l77e7fest715caps8'

elif ENVIRONMENT == "dev-peter":
    REDIS_URL = "redis://localhost"

    API_URL = 'http://127.0.0.1:5000/'
    SOURCE_CODE_ROOT = '/Users/pbharrin/Documents/GitHub/lexintel'
    PYTHON_EXECUTABLE = '/Users/pbharrin/opt/anaconda3/envs/lexintel/bin/python'
    DBURI = 'sqlite:///myapp.db'
    COGNITO_USERPOOL = 'us-east-1_x8GRP09XU'
    COGNITO_APP = '7lq6c66o0l77e7fest715caps8'

elif ENVIRONMENT == "sls":
    REDIS_URL = "rediss://:@master.stanford.5gjkpo.use1.cache.amazonaws.com:6379/0?ssl_cert_reqs=CERT_NONE"

    API_URL = 'https://slsapi.lextext.ai/'
    SOURCE_CODE_ROOT = '/home/ubuntu/lexintel'
    PYTHON_EXECUTABLE = '/home/ubuntu/miniconda3/envs/lexintel/bin/python'
    DBURI = 'mysql+pymysql://admin:teYX7ztfIo06aopAiCkjwnML@stanford-dbserver-encrypted.cluster-cns4q40usfdz.us-east-1.rds.amazonaws.com:3306/test'
    COGNITO_USERPOOL = 'us-east-1_9yNwBwu6O'
    COGNITO_APP = 'e1p0amoh6kfl0q2k9l2qmlt0o'

elif ENVIRONMENT == "wsgr":  # WSGR
    REDIS_URL = "redis://localhost"

    API_URL = 'https://wsgrapi.lextext.ai/'
    SOURCE_CODE_ROOT = '/home/ubuntu/lexintel'
    PYTHON_EXECUTABLE = '/home/ubuntu/miniconda3/envs/lexintel/bin/python'
    DBURI = 'mysql://admin:AxsUco3I6GcMgrL^0OCRC6pO@wsgr-encrypted-cluster.cluster-cns4q40usfdz.us-east-1.rds.amazonaws.com:3306/test'
    COGNITO_USERPOOL = 'us-east-1_71gFlPbjS'
    COGNITO_APP = '2tno185l0osh1f6o5uufga5j2t'

else:  # Monica
    REDIS_URL = "redis://localhost"

    API_URL = 'http://127.0.0.1:5000/'
    SOURCE_CODE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    PYTHON_EXECUTABLE = sys.executable
    DBURI = 'sqlite:///myapp.db'
    COGNITO_USERPOOL = 'us-east-1_x8GRP09XU'
    COGNITO_APP = '7lq6c66o0l77e7fest715caps8'


CELERY_QUEUE = os.environ.get(
    "E2E_CELERY_QUEUE" if E2E_MODE else "CELERY_QUEUE",
    DEFAULT_E2E_CELERY_QUEUE if E2E_MODE else "celery",
)
