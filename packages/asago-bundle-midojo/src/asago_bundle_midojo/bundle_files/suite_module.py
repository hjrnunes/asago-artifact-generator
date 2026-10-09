"""The out-of-tree MiDojo suite of an Asago bundle: ``--load-suite asago_suite.suite``.

``midojo-serve`` and ``midojo-run`` each import this module, so each registers
the verifiers the suite file names before the suite parses its checks.
"""

from pathlib import Path

from midojo.yaml_task_suite import YAMLTaskSuite

from . import asago_verifiers

asago_verifiers.register()
task_suite = YAMLTaskSuite(
    "asago_suite.suite", suite_yaml_path=Path(__file__).with_name("suite.yaml")
)
