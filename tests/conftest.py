import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="tgclone-test-")
os.environ.update(API_ID="1", API_HASH="x", TGCLONE_DATA=os.path.join(_tmp, "data"),
                  TGCLONE_CONFIG=os.path.join(_tmp, "cfg.json"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
