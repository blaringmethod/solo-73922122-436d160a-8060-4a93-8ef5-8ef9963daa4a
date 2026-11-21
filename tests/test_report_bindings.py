"""Version-pinned report/observation bindings and the versioned snapshot API.

Covers the research requirement that a report correction which only edits the
narrative must still expose the exact observation values the original report
used:

* new reports bind the observation version committed in the same bundle;
* report-only corrections keep the previous version pins;
* an observation corrected in the same bundle rebinds the reference, while an
  observation corrected on its own does not move existing pins;
* legacy report versions without bindings and explicitly untraceable
  references are marked untraceable and carry that state forward;
* the new GET .../versions/{version} endpoint returns the report body and each
  bound observation's id/version/body;
* bindings commit and roll back together with the resource versions, and
  idempotent retries do not duplicate them.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func

from app import database
from app.database import SessionLocal
from app.models import ReportResultBinding, ResourceVersion
from tests.conftest import (
    make_bundle,
    observation_entry,
    patient_entry,
    report_entry,
    urn,
)


def _submit(client, key, bundle, expect_code=200):
    resp = client.post("/fhir", json=bundle, headers={"Idempotency-Key": key})
    assert resp.status_code == expect_code, resp.text
    return resp.json()


def _ids(receipt):
    return {m["resourceType"]: m["id"] for m in receipt["resourceMap"]}


def _initial_report(client, key="B1", value1=5.5, value2=7.1):
    """Create patient + two observations + one report (out of order)."""
    p_urn, o1_urn, o2_urn, r_urn = urn(), urn(), urn(), urn()
    p, _ = patient_entry(p_urn, value=f"BIND-{key}")
    o1 = observation_entry(o1_urn, p_urn, code="2345-7", value=value1)
    o2 = observation_entry(o2_urn, p_urn, code="3094-0", value=value2)
    r = report_entry(r_urn, p_urn, [o1_urn, o2_urn])
    receipt = _submit(client, key, make_bundle([r, o1, o2, p]))
    ids = _ids(receipt)
    obs = [m["id"] for m in receipt["resourceMap"] if m["resourceType"] == "Observation"]
    return ids["Patient"], obs[0], obs[1], ids["DiagnosticReport"]


def _report_put(rid, pid, refs, if_match):
    return {
        "resource": {
            "resourceType": "DiagnosticReport",
            "id": rid,
            "status": "corrected",
            "code": {"coding": [{"code": "24323-8"}]},
            "subject": {"reference": f"Patient/{pid}"},
            "result": [{"reference": f"Observation/{oid}"} for oid in refs],
        },
        "request": {
            "method": "PUT",
            "url": f"DiagnosticReport/{rid}",
            "ifMatch": if_match,
        },
    }


def _obs_put(oid, pid, value, if_match, code="2345-7"):
    return {
        "resource": {
            "resourceType": "Observation",
            "id": oid,
            "status": "corrected",
            "code": {"coding": [{"code": code}]},
            "subject": {"reference": f"Patient/{pid}"},
            "valueQuantity": {"value": value, "unit": "mmol/L"},
        },
        "request": {
            "method": "PUT",
            "url": f"Observation/{oid}",
            "ifMatch": if_match,
        },
    }


def test_new_report_binds_committed_observation_versions(client):
    pid, oid1, oid2, rid = _initial_report(client)
    snap = client.get(f"/diagnostic-reports/{rid}/versions/1").json()
    assert snap["resource"]["resourceType"] == "DiagnosticReport"
    assert snap["version"] == 1 and snap["etag"] == 'W/"1"'
    assert [r["observationId"] for r in snap["result"]] == [oid1, oid2]
    assert all(r["traceable"] is True for r in snap["result"])
    assert [r["observationVersion"] for r in snap["result"]] == [1, 1]
    values = [r["resource"]["valueQuantity"]["value"] for r in snap["result"]]
    assert values == [5.5, 7.1]


def test_report_only_correction_keeps_previous_pins(client):
    pid, oid1, oid2, rid = _initial_report(client)
    # Reverse the result order and change only the narrative (status field).
    _submit(
        client,
        "B2",
        make_bundle([_report_put(rid, pid, [oid2, oid1], 'W/"1"')]),
    )
    v2 = client.get(f"/diagnostic-reports/{rid}/versions/2").json()
    # Order follows the new submission, but both pins stay on observation v1.
    assert [(r["observationId"], r["observationVersion"]) for r in v2["result"]] == [
        (oid2, 1),
        (oid1, 1),
    ]
    # The previous version snapshot is immutable and keeps the old order/body.
    v1 = client.get(f"/diagnostic-reports/{rid}/versions/1").json()
    assert [r["observationId"] for r in v1["result"]] == [oid1, oid2]
    assert v1["resource"]["status"] == "final"
    assert v2["resource"]["status"] == "corrected"


def test_observation_only_correction_does_not_move_existing_pins(client):
    pid, oid1, oid2, rid = _initial_report(client)
    _submit(client, "B3", make_bundle([_obs_put(oid1, pid, 6.0, 'W/"1"')]))
    # Report v1 still resolves the observation to the value of v1.
    snap = client.get(f"/diagnostic-reports/{rid}/versions/1").json()
    pinned = next(r for r in snap["result"] if r["observationId"] == oid1)
    assert pinned["observationVersion"] == 1
    assert pinned["resource"]["valueQuantity"]["value"] == 5.5
    # The live observation is at v2 with the new value.
    live = client.get(f"/observations/{oid1}").json()
    assert live["version"] == 2
    assert live["resource"]["valueQuantity"]["value"] == 6.0


def test_same_bundle_observation_correction_rebinds_report(client):
    pid, oid1, oid2, rid = _initial_report(client)
    bundle = make_bundle(
        [
            _obs_put(oid1, pid, 6.4, 'W/"1"'),
            _report_put(rid, pid, [oid1, oid2], 'W/"1"'),
        ]
    )
    _submit(client, "B4", bundle)
    v2 = client.get(f"/diagnostic-reports/{rid}/versions/2").json()
    pins = {r["observationId"]: r["observationVersion"] for r in v2["result"]}
    assert pins[oid1] == 2  # rebound to the version committed in this bundle
    assert pins[oid2] == 1  # untouched observation keeps its old pin
    body = next(r for r in v2["result"] if r["observationId"] == oid1)["resource"]
    assert body["valueQuantity"]["value"] == 6.4


def test_legacy_report_version_without_bindings_is_marked_untraceable(client):
    pid, oid1, oid2, rid = _initial_report(client, key="B5")
    # Simulate a report version written before version bindings existed.
    with SessionLocal() as session:
        session.query(ReportResultBinding).filter_by(
            report_id=rid, report_version=1
        ).delete()
        session.commit()

    legacy = client.get(f"/diagnostic-reports/{rid}/versions/1").json()
    assert legacy["result"][0]["traceable"] is False
    assert legacy["result"][0]["reason"] == "legacy-version-without-bindings"
    assert legacy["result"][0]["observationId"] == oid1

    # A report-only correction materializes explicit untraceable rows and
    # carries the state forward.
    _submit(
        client,
        "B6",
        make_bundle([_report_put(rid, pid, [oid1, oid2], 'W/"1"')]),
    )
    v2 = client.get(f"/diagnostic-reports/{rid}/versions/2").json()
    assert all(r["traceable"] is False for r in v2["result"])
    assert all(r["reason"] == "no-version-binding" for r in v2["result"])
    with SessionLocal() as session:
        rows = (
            session.query(ReportResultBinding)
            .filter_by(report_id=rid, report_version=2)
            .order_by(ReportResultBinding.position)
            .all()
        )
        assert [r.observation_id for r in rows] == [oid1, oid2]
        assert all((not r.traceable) and r.observation_version is None for r in rows)


def test_untraceable_becomes_traceable_on_shared_bundle_correction(client):
    pid, oid1, oid2, rid = _initial_report(client, key="B7")
    with SessionLocal() as session:
        session.query(ReportResultBinding).filter_by(
            report_id=rid, report_version=1
        ).delete()
        session.commit()
    _submit(
        client,
        "B8",
        make_bundle([_report_put(rid, pid, [oid1], 'W/"1"')]),
    )
    # Correcting the observation together with the next report correction
    # establishes a traceable binding to the new observation version.
    bundle = make_bundle(
        [
            _obs_put(oid1, pid, 8.8, 'W/"1"'),
            _report_put(rid, pid, [oid1], 'W/"2"'),
        ]
    )
    _submit(client, "B9", bundle)
    v3 = client.get(f"/diagnostic-reports/{rid}/versions/3").json()
    entry = v3["result"][0]
    assert entry["traceable"] is True
    assert entry["observationVersion"] == 2
    assert entry["resource"]["valueQuantity"]["value"] == 8.8


def test_newly_added_reference_binds_current_version(client):
    pid, oid1, oid2, rid = _initial_report(client, key="B10")
    # Correct the report to reference only the second observation (newly added
    # to this report's result set); it must bind its current version (v1).
    _submit(
        client,
        "B11",
        make_bundle([_report_put(rid, pid, [oid2], 'W/"1"')]),
    )
    v2 = client.get(f"/diagnostic-reports/{rid}/versions/2").json()
    assert len(v2["result"]) == 1
    assert v2["result"][0]["observationId"] == oid2
    assert v2["result"][0]["observationVersion"] == 1
    assert v2["result"][0]["traceable"] is True


def test_failed_bundle_rolls_back_bindings_and_versions(client):
    pid, oid1, oid2, rid = _initial_report(client, key="B12")
    with SessionLocal() as session:
        versions_before = session.query(ResourceVersion).count()
        bindings_before = session.query(ReportResultBinding).count()

    bad = make_bundle(
        [
            {
                "resource": {
                    "resourceType": "DiagnosticReport",
                    "id": rid,
                    "status": "corrected",
                    "code": {"coding": [{"code": "24323-8"}]},
                    "subject": {"reference": f"Patient/{pid}"},
                    "result": [{"reference": "Observation/ghost-id"}],
                },
                "request": {
                    "method": "PUT",
                    "url": f"DiagnosticReport/{rid}",
                    "ifMatch": 'W/"1"',
                },
            }
        ]
    )
    resp = client.post(
        "/fhir", json=bad, headers={"Idempotency-Key": "B13"}
    )
    assert resp.status_code == 422
    with SessionLocal() as session:
        assert session.query(ResourceVersion).count() == versions_before
        assert session.query(ReportResultBinding).count() == bindings_before
    # The report remains at version 1 and its snapshot is intact.
    snap = client.get(f"/diagnostic-reports/{rid}/versions/1").json()
    assert snap["version"] == 1 and len(snap["result"]) == 2


def test_idempotent_retry_does_not_duplicate_bindings(client):
    pid, oid1, oid2, rid = _initial_report(client, key="B14")
    bundle = make_bundle([_report_put(rid, pid, [oid1, oid2], 'W/"1"')])
    first = client.post(
        "/fhir", json=bundle, headers={"Idempotency-Key": "B15"}
    )
    second = client.post(
        "/fhir", json=bundle, headers={"Idempotency-Key": "B15"}
    )
    assert first.status_code == second.status_code == 200
    assert first.content == second.content
    with SessionLocal() as session:
        assert (
            session.query(ResourceVersion)
            .filter_by(resource_type="DiagnosticReport", resource_id=rid)
            .count()
            == 2
        )
        # Two report versions, each with two result references -> 4 rows; the
        # retry must not have added any duplicate binding rows.
        per_version = dict(
            session.query(
                ReportResultBinding.report_version,
                func.count(ReportResultBinding.id),
            )
            .filter_by(report_id=rid)
            .group_by(ReportResultBinding.report_version)
            .all()
        )
        assert per_version == {1: 2, 2: 2}


def test_report_version_snapshot_unknown_versions_404(client):
    pid, oid1, oid2, rid = _initial_report(client, key="B16")
    assert client.get(f"/diagnostic-reports/{rid}/versions/99").status_code == 404
    assert client.get("/diagnostic-reports/missing/versions/1").status_code == 404


@pytest.mark.skipif(
    database.engine.dialect.name == "sqlite",
    reason="row-locking concurrency test requires PostgreSQL",
)
def test_concurrent_report_and_observation_corrections_serialize(client):
    """A report correction must pin the observation version that was committed
    first; the losing concurrent request fails its If-Match check rather than
    silently mixing versions."""
    import concurrent.futures

    pid, oid1, oid2, rid = _initial_report(client, key="B17")

    def correct_report(_):
        return client.post(
            "/fhir",
            json=make_bundle([_report_put(rid, pid, [oid1, oid2], 'W/"1"')]),
            headers={"Idempotency-Key": urn()},
        )

    def correct_observation(_):
        return client.post(
            "/fhir",
            json=make_bundle([_obs_put(oid1, pid, 9.0, 'W/"1"')]),
            headers={"Idempotency-Key": urn()},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(
                lambda fn: fn(0),
                [correct_report, correct_observation] * 2,
            )
        )
    # Every request either succeeds once or is rejected by a stale If-Match;
    # the store never ends up with a pin pointing at a non-existent version.
    assert all(r.status_code in (200, 412) for r in responses)
    with SessionLocal() as session:
        pins = (
            session.query(ReportResultBinding)
            .filter_by(report_id=rid)
            .all()
        )
        versions = {
            (v.resource_id, v.version)
            for v in session.query(ResourceVersion)
            .filter_by(resource_type="Observation")
            .all()
        }
        for pin in pins:
            if pin.traceable:
                assert (pin.observation_id, pin.observation_version) in versions
