"""Example script command: greets the sender and echoes the arguments.

Bind it in the portal: Commands → New command → Response type "Run a script"
→ hello.py, with the keyword `hello`. Then `hello one "two words"`.
"""

import json
import os
import sys

context = json.load(sys.stdin)  # the same data as the BOT_* variables, plus more

print(f"Hi @{os.environ['BOT_USERNAME']}!")
if sys.argv[1:]:
    print("You passed: " + ", ".join(f"`{arg}`" for arg in sys.argv[1:]))
else:
    print(f"No arguments. Try `{context['command']} one \"two words\"`.")
