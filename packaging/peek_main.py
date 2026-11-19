"""py2app main script for Peek.app.

In alias builds this file is run from the source tree, so the repository root is
put on sys.path; in full builds `peek` is bundled and the root does not exist.
"""

import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.isdir(os.path.join(_root, "peek")) and _root not in sys.path:
    sys.path.insert(0, _root)

from peek.__main__ import main  # noqa: E402

sys.exit(main())
