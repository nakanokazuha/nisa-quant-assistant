"""Test package bootstrap for the src-layout project."""

from pathlib import Path
import sys


SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
