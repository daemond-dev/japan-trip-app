"""Charmera toolkit: read-only inspection first, writes only behind explicit confirmation.

Every module is pure Python 3.9+ standard library so it runs the same on Windows and Linux
(including a Steam Deck). Nothing here touches a camera or a card unless the command says so
in its name (card-fix, restore) and the owner confirms.
"""

__version__ = "0.1.0"
