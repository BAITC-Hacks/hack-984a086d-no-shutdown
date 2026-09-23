"""Keep CI and local test collection network-free by default."""
import os

os.environ.setdefault("WINDAGENT_LIVE_POLL", "0")
