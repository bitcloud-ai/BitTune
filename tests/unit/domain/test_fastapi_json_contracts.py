from fastapi import FastAPI
from fastapi.testclient import TestClient

from autopilot.domain.constraints import SloSpec

app = FastAPI()


@app.post("/slo")
def accept_slo(slo: SloSpec) -> SloSpec:
    return slo


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
