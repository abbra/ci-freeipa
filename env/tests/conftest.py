import os
import sys

# The package lives one level up (env/freeipa_env); tests run from env/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
