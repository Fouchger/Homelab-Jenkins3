#!/usr/bin/env python3
"""Run the repository unittest suite and write a Jenkins JUnit XML report."""

from __future__ import annotations

import time
import unittest
from pathlib import Path
from xml.etree import ElementTree


class TimedTextTestResult(unittest.TextTestResult):
    def startTest(self, test):
        self._started_at = getattr(self, "_started_at", {})
        self._started_at[test.id()] = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test):
        started_at = getattr(self, "_started_at", {}).pop(test.id(), None)
        if started_at is not None:
            self.test_durations = getattr(self, "test_durations", {})
            self.test_durations[test.id()] = time.perf_counter() - started_at
        super().stopTest(test)


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def write_junit_report(cases, result):
    failures = {test.id(): message for test, message in result.failures}
    errors = {test.id(): message for test, message in result.errors}
    skipped = {test.id(): reason for test, reason in result.skipped}
    skipped.update({test.id(): reason for test, reason in result.expectedFailures})
    unexpected = {test.id(): "Test was expected to fail but passed." for test in result.unexpectedSuccesses}
    durations = getattr(result, "test_durations", {})

    suite = ElementTree.Element(
        "testsuite",
        {
            "name": "Python unittest",
            "tests": str(len(cases)),
            "failures": str(len(failures) + len(unexpected)),
            "errors": str(len(errors)),
            "skipped": str(len(skipped)),
            "time": f"{sum(durations.values()):.6f}",
        },
    )
    for test in cases:
        test_id = test.id()
        test_case = ElementTree.SubElement(
            suite,
            "testcase",
            {
                "classname": f"{test.__class__.__module__}.{test.__class__.__qualname__}",
                "name": test._testMethodName,
                "time": f"{durations.get(test_id, 0.0):.6f}",
            },
        )
        if test_id in failures:
            ElementTree.SubElement(test_case, "failure", {"message": "Test assertion failed"}).text = failures[test_id]
        elif test_id in errors:
            ElementTree.SubElement(test_case, "error", {"message": "Test raised an error"}).text = errors[test_id]
        elif test_id in unexpected:
            ElementTree.SubElement(test_case, "failure", {"message": unexpected[test_id]}).text = unexpected[test_id]
        elif test_id in skipped:
            ElementTree.SubElement(test_case, "skipped", {"message": skipped[test_id]})

    report_path = Path("junit-results/unittest.xml")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    ElementTree.indent(suite, space="  ")
    ElementTree.ElementTree(suite).write(report_path, encoding="utf-8", xml_declaration=True)
    print(f"JUnit report written to {report_path}.", flush=True)


def main():
    loader = unittest.TestLoader()
    discovered = loader.discover("tests")
    cases = list(flatten(discovered))
    runner = unittest.TextTestRunner(verbosity=2, resultclass=TimedTextTestResult)
    result = runner.run(discovered)
    write_junit_report(cases, result)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
