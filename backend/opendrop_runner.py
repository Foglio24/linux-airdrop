#!/usr/bin/python3
"""Run the vendored OpenDrop backend on Ubuntu CPython 3.14."""
from pathlib import Path
import sys
import warnings
warnings.filterwarnings("ignore", message="pkg_resources is deprecated as an API.*", category=UserWarning)
sys.argv[0] = "opendrop"
backend_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(backend_dir / "python"))
sys.path.insert(0, str(backend_dir.parent))
from secure_receiver import install
install()
from opendrop.cli import main
raise SystemExit(main())
