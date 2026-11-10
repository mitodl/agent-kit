"""Every witan command's parameter docs reach ``--help``."""

from testsupport.cli_docstrings import one_line_param_docs

from witan.cli import app


def test_no_command_documents_a_parameter_on_one_line():
    assert one_line_param_docs(app, "witan") == []
