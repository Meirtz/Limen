"""Comparison contract on synthetic records."""

from __future__ import annotations

from typing import Any

import pytest

from conftest import record
from limen.compare import compare
from limen.fields import SpecError, matches, parse_argv


def codes(findings: list[Any], severity: str | None = None) -> set[str]:
    return {f.code for f in findings if severity is None or f.severity == severity}


def env(**kv: str | None) -> dict[str, dict[str, Any]]:
    return {k: ({"present": False} if v is None else {"present": True, "value": v}) for k, v in kv.items()}


def test_valid_comparison_has_no_blocking_findings() -> None:
    a = record(id="a", env=env(WIKI=None), metrics={"score": 0.5})
    b = record(id="b", env=env(WIKI="rich"), metrics={"score": 0.7})
    comp = compare([a], [b], treatment=["env:WIKI"])
    assert not codes(comp.findings, "block")
    assert comp.explained["env:WIKI"]["env:WIKI"] == ["<unset>", "rich"]
    assert comp.effects[0]["delta"] == pytest.approx(0.2)


def test_placebo_when_treatment_never_observed() -> None:
    comp = compare([record(id="a")], [record(id="b")], treatment=["env:WIKI"])
    assert "PLACEBO" in codes(comp.findings, "block")


def test_placebo_when_treatment_identical_in_both_arms() -> None:
    a = record(id="a", files={"read": {"/p/wiki/": {"sha256": "same", "dir": True}}, "written": {}})
    b = record(id="b", files={"read": {"/p/wiki/": {"sha256": "same", "dir": True}}, "written": {}})
    comp = compare([a], [b], treatment=["file:wiki/"])
    assert "PLACEBO" in codes(comp.findings, "block")


def test_env_treatment_only_reachable_by_children_is_a_warning() -> None:
    sub = [{"argv": ["node", "agent.js"], "cwd": None, "env": "inherited"}]
    comp = compare([record(id="a", subprocesses=sub)], [record(id="b", subprocesses=sub)], treatment=["env:WIKI"])
    assert "TREATMENT_UNVERIFIED" in codes(comp.findings, "warn")
    assert "PLACEBO" not in codes(comp.findings)


def test_confound_on_any_other_difference() -> None:
    a = record(id="a", env=env(WIKI=None, TOL="1e-2"), distributions={"torch": "2.4"})
    b = record(
        id="b",
        env=env(WIKI="rich", TOL="1e-4"),
        distributions={"torch": "2.5"},
        platform={"system": "Linux", "machine": "x86_64", "gpus": ["H100"]},
    )
    comp = compare([a], [b], treatment=["env:WIKI"])
    confounds = {f.subject for f in comp.findings if f.code == "CONFOUND"}
    assert confounds == {"env:TOL", "dist:torch", "gpu"}


def test_waiver_silences_a_difference() -> None:
    a = record(id="a", env=env(WIKI=None), python={"version": "3.11.0", "implementation": "CPython"})
    b = record(id="b", env=env(WIKI="x"), python={"version": "3.12.0", "implementation": "CPython"})
    assert "CONFOUND" in codes(compare([a], [b], treatment=["env:WIKI"]).findings)
    assert "CONFOUND" not in codes(compare([a], [b], treatment=["env:WIKI"], waive=["python"]).findings)


def test_code_changes_are_confounds_unless_code_is_the_treatment() -> None:
    a = record(id="a", modules={"pkg.model": {"sha256": "v1", "origin": "project"}})
    b = record(id="b", modules={"pkg.model": {"sha256": "v2", "origin": "project"}})
    assert "CONFOUND" in codes(compare([a], [b], treatment=["env:X"]).findings)
    comp = compare([a], [b], treatment=["module:pkg"])
    assert not codes(comp.findings, "block")
    assert not codes(compare([a], [b], treatment=["code"]).findings, "block")


def test_input_read_by_one_arm_is_a_warning_or_explained() -> None:
    def rec(rid: str, wiki: str | None, read: list[str]) -> dict[str, Any]:
        files = {"read": {f"/p/{p}": {"sha256": p, "dir": p.endswith("/")} for p in read}, "written": {}}
        return record(id=rid, env=env(WIKI=wiki), files=files)

    # the control reads a sibling of the treatment path: the alternative the treatment replaced
    comp = compare([rec("a", None, ["kb/base/"])], [rec("b", "kb/rich", ["kb/rich/"])], treatment=["env:WIKI"])
    assert not codes(comp.findings, "block") and not codes(comp.findings, "warn") - {"NO_REPLICATES"}
    assert "EXPLAINED_BY_TREATMENT" in codes(comp.findings, "info")
    # an unrelated input read by one arm only is surfaced
    comp = compare(
        [rec("a", None, ["kb/base/", "extra.csv"])], [rec("b", "kb/rich", ["kb/rich/"])], treatment=["env:WIKI"]
    )
    one = next(f for f in comp.findings if f.code == "INPUT_ONLY_IN_ONE_ARM")
    assert one.evidence["fields"] == ["file:extra.csv"]


def test_treatment_recorded_twice_is_not_a_confound() -> None:
    a = [record(id=f"a{i}", argv=["m.py", "--lr", "0.02"], params={"lr": 0.02}) for i in range(2)]
    b = [record(id=f"b{i}", argv=["m.py", "--lr", "0.3"], params={"lr": 0.3}) for i in range(2)]
    comp = compare(a, b, treatment=["arg:--lr"])
    assert not codes(comp.findings, "block") and "TREATMENT_RECORDED_TWICE" in codes(comp.findings, "info")
    # a default in the control is fine; a different value alongside the treatment is not
    a0 = record(id="a0", argv=["m.py"], params={"lr": 0.1})
    assert not codes(compare([a0], b, treatment=["arg:--lr"]).findings, "block")
    b_bad = record(id="bx", argv=["m.py", "--lr", "0.3"], params={"lr": 0.5})
    assert "CONFOUND" in codes(compare([a0], [b_bad], treatment=["arg:--lr"]).findings, "block")


def test_same_input_with_different_content_is_a_confound() -> None:
    a = record(id="a", env=env(W=None), files={"read": {"/p/data/train.jsonl": {"sha256": "v1"}}, "written": {}})
    b = record(id="b", env=env(W="1"), files={"read": {"/p/data/train.jsonl": {"sha256": "v2"}}, "written": {}})
    comp = compare([a], [b], treatment=["env:W"])
    assert {f.subject for f in comp.findings if f.code == "CONFOUND"} == {"file:data/train.jsonl"}


def test_replicate_fields_vary_within_both_arms() -> None:
    a = [record(id=f"a{s}", env=env(W=None), params={"seed": s}, metrics={"m": 1.0 + s / 100}) for s in (1, 2, 3)]
    b = [record(id=f"b{s}", env=env(W="1"), params={"seed": s + 10}, metrics={"m": 2.0 + s / 100}) for s in (1, 2, 3)]
    comp = compare(a, b, treatment=["env:W"])
    assert "CONFOUND" not in codes(comp.findings)
    assert "REPLICATE_FIELDS" in codes(comp.findings, "info")
    eff = comp.effects[0]
    assert eff["ci95"][0] > 0, "a clear effect with replicates is resolved"


def test_effect_within_noise_and_no_replicates() -> None:
    a = [record(id=f"a{i}", env=env(W=None), metrics={"m": v}) for i, v in enumerate([1.0, 2.0, 3.0])]
    b = [record(id=f"b{i}", env=env(W="1"), metrics={"m": v}) for i, v in enumerate([1.5, 2.5, 3.1])]
    assert "EFFECT_WITHIN_NOISE" in codes(compare(a, b, treatment=["env:W"]).findings, "warn")
    assert "NO_REPLICATES" in codes(compare(a[:1], b[:1], treatment=["env:W"]).findings, "warn")


def outcomes(pattern: str) -> dict[str, dict[str, str]]:
    names = {"p": "pass", "f": "fail", "e": "error", "t": "timeout", "i": "infra"}
    return {f"q{i}": {"status": names[c]} for i, c in enumerate(pattern)}


def test_asymmetric_not_evaluated_blocks() -> None:
    a = record(id="a", env=env(W=None), outcomes=outcomes("eeeeeeeepp"))  # base: harness failed to parse
    b = record(id="b", env=env(W="1"), outcomes=outcomes("ppppppppff"))
    comp = compare([a], [b], treatment=["env:W"])
    assert "ASYMMETRIC_NOT_EVALUATED" in codes(comp.findings, "block")
    pr = next(e for e in comp.effects if e["metric"] == "item_pass_rate")
    assert pr["delta"] > 0 and pr["delta_evaluated_only"] < 0
    assert "SIGN_DEPENDS_ON_ERRORS" in codes(comp.findings, "warn")


def test_items_differ_blocks() -> None:
    a = record(id="a", env=env(W=None), outcomes=outcomes("ppff"))
    b = record(id="b", env=env(W="1"), outcomes={**outcomes("ppf"), "extra": {"status": "pass"}})
    assert "ITEMS_DIFFER" in codes(compare([a], [b], treatment=["env:W"]).findings, "block")


def test_run_level_findings_are_included() -> None:
    bad = record(id="b", env=env(W="1"), status="killed", signal="SIGTERM")
    comp = compare([record(id="a", env=env(W=None))], [bad], treatment=["env:W"])
    assert "RUN_FAILED" in codes(comp.findings, "block")


def test_declared_treatment_is_used_by_default() -> None:
    a = record(id="a", declared={"treatment": ["env:W"]}, env=env(W=None))
    b = record(id="b", declared={"treatment": ["env:W"]}, env=env(W="1"))
    comp = compare([a], [b])
    assert comp.treatment == ["env:W"] and not codes(comp.findings, "block")
    assert "NO_TREATMENT" in codes(compare([record(id="x")], [record(id="y")]).findings, "warn")


def test_argv_fields_and_output_arguments() -> None:
    assert parse_argv(["--lr", "0.1", "--fast", "--out=o", "-n", "-3", "pos"]) == {
        "arg:--lr": "0.1",
        "arg:--fast": "true",
        "arg:--out": "o",
        "arg:-n": "-3",
        "arg:#0": "pos",
    }
    written = {"/p/runs/a/metrics.json": {"sha256": "1"}}
    a = record(id="a", argv=["main.py", "--lr", "0.1", "--out", "runs/a"], files={"read": {}, "written": written})
    b = record(
        id="b",
        argv=["main.py", "--lr", "0.2", "--out", "runs/b"],
        files={"read": {}, "written": {"/p/runs/b/metrics.json": {"sha256": "2"}}},
    )
    comp = compare([a], [b], treatment=["arg:--lr"])
    assert not codes(comp.findings, "block"), "an output directory is not an input"


def test_label_heuristic_does_not_hide_numeric_confounds() -> None:
    wa = {"/p/runs/0.1/m.json": {"sha256": "1"}}
    wb = {"/p/runs/0.2/m.json": {"sha256": "2"}}
    a = record(id="a", env=env(W=None), argv=["main.py", "--lr", "0.1"], files={"read": {}, "written": wa})
    b = record(id="b", env=env(W="1"), argv=["main.py", "--lr", "0.2"], files={"read": {}, "written": wb})
    comp = compare([a], [b], treatment=["env:W"])
    assert {f.subject for f in comp.findings if f.code == "CONFOUND"} == {"arg:--lr"}


def test_spec_language() -> None:
    assert matches("env:WIKI_*", "env:WIKI_ROOT")
    assert matches("file:data/wiki/", "file:data/wiki/a.md")
    assert matches("module:pkg", "module:pkg.sub") and not matches("module:pkg", "module:pkg2")
    assert matches("code", "module:anything")
    with pytest.raises(SpecError):
        compare([record(id="a")], [record(id="b")], treatment=["wiki"])
