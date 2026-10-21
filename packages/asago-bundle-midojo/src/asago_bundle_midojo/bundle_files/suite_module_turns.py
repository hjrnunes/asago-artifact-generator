"""The out-of-tree MiDojo suite of a sequential reply bundle: ``--load-suite asago_suite.suite``.

As ``suite_module.py``, and it also registers the rubric judge of the conversation
(``asago_rubric_turns``), which the suite file cites, before the suite parses its checks.
"""

from pathlib import Path

from midojo.yaml_task_suite import YAMLTaskSuite

from . import asago_rubric_turns, asago_verifiers

asago_verifiers.register()
asago_rubric_turns.register()
task_suite = YAMLTaskSuite(
    "asago_suite.suite", suite_yaml_path=Path(__file__).with_name("suite.yaml")
)
