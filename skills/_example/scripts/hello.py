"""Print a formal greeting for the name given as the first argument."""

import sys

name = " ".join(sys.argv[1:]) or "there"
print(f"Good day, {name}. It is a pleasure to meet you.")
