import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SLEUTH_HOME", os.path.join(os.path.dirname(os.path.abspath(__file__)), "_home"))
