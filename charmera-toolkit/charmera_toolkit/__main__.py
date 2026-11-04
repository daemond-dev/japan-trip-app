"""Allow `python -m charmera_toolkit ...`."""
import sys

from .cli import main

sys.exit(main())
