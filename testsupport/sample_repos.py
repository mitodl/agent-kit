"""Source files for three checkouts that reach into each other.

``app`` calls ``compute`` in ``lib`` and reads the env var ``infra``'s Pulumi
config provides. witan-code's language-server tests and the editor checks'
``seed.py`` index the same files, so their line and column positions agree.
"""

SERVICE = """\
from lib.maths import compute


class Service:
    def run(self):
        return helper()


def helper():
    return compute()


def main():
    s = Service()
    return s.run()
"""

SETTINGS = """\
import os


def base_url():
    return os.environ["MITOL_APP_BASE_URL"]
"""

PULUMI = """\
config:
  app:env_vars:
    MITOL_APP_BASE_URL: "https://app-qa.example.org"
"""

COMPUTE = """\
def compute():
    return 1


def twice():
    return compute() + compute()
"""
