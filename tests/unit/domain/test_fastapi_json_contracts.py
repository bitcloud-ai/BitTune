from fastapi import FastAPI
from fastapi.testclient import TestClient

from autopilot.domain.base import StrictModel, UtcDatetime
from autopilot.domain.constraints import SloSpec

app = FastAPI()


class TimestampInput(StrictModel):
    captured_at: UtcDatetime


@app.post("/slo")
def accept_slo(slo: SloSpec) -> SloSpec:
    return slo


@app.post("/timestamp")
def accept_timestamp(timestamp: TimestampInput) -> TimestampInput:
    return timestamp


def test_fastapi_accepts_valid_json_enums_and_arrays() -> None:
    response = TestClient(app).post(
        "/slo",
        json={
            "schema_version": "slo/v1",
            "constraints": [
                {
                    "kind": "numeric",
                    "metric": "success_rate",
                    "operator": ">=",
                    "value": 1.0,
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["constraints"][0]["metric"] == "success_rate"


def test_fastapi_rejects_scalar_type_coercion() -> None:
    response = TestClient(app).post(
        "/slo",
        json={
            "schema_version": "slo/v1",
            "constraints": [
                {
                    "kind": "numeric",
                    "metric": "success_rate",
                    "operator": ">=",
                    "value": "1.0",
                }
            ],
        },
    )

    assert response.status_code == 422


def test_fastapi_rejects_numeric_timestamp_coercion() -> None:
    client = TestClient(app)

    accepted = client.post("/timestamp", json={"captured_at": "2026-08-05T04:00:00Z"})
    rejected = client.post("/timestamp", json={"captured_at": 1_785_902_400})

    assert accepted.status_code == 200
    assert rejected.status_code == 422
