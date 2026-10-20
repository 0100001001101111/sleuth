import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sleuth.cli import main
sys.exit(main())
