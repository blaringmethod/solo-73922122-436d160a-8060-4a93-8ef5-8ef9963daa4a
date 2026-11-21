#!/usr/bin/env bash
# End-to-end smoke test against a running instance (default http://localhost:8000).
# Usage: BASE_URL=http://localhost:8000 ./scripts/smoke.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
KEY="SMOKE-$(date +%s)"
PATIENT_URN="urn:uuid:11111111-1111-4111-8111-111111111111"
OBS_URN="urn:uuid:22222222-2222-4222-8222-222222222222"
REPORT_URN="urn:uuid:33333333-3333-4333-8333-333333333333"

BUNDLE=$(cat <<JSON
{
  "resourceType": "Bundle",
  "type": "transaction",
  "entry": [
    {"fullUrl": "$REPORT_URN",
     "resource": {"resourceType": "DiagnosticReport", "status": "final",
       "code": {"coding": [{"code": "24323-8"}]},
       "subject": {"reference": "$PATIENT_URN"},
       "result": [{"reference": "$OBS_URN"}]},
     "request": {"method": "POST", "url": "DiagnosticReport"}},
    {"fullUrl": "$OBS_URN",
     "resource": {"resourceType": "Observation", "status": "final",
       "code": {"coding": [{"code": "2345-7"}]},
       "subject": {"reference": "$PATIENT_URN"},
       "valueQuantity": {"value": 5.5, "unit": "mmol/L"}},
     "request": {"method": "POST", "url": "Observation"}},
    {"fullUrl": "$PATIENT_URN",
     "resource": {"resourceType": "Patient",
       "identifier": [{"system": "http://hospital/mrn", "value": "$KEY"}]},
     "request": {"method": "POST", "url": "Patient"}}
  ]
}
JSON
)

echo "1) submit out-of-order bundle"
RECEIPT=$(curl -fsS -X POST "$BASE_URL/fhir" -H "Idempotency-Key: $KEY" \
  -H "Content-Type: application/json" -d "$BUNDLE")
echo "$RECEIPT" | python3 -m json.tool | head -20

echo "2) identical retry must return the same ids"
REPLAY=$(curl -fsS -X POST "$BASE_URL/fhir" -H "Idempotency-Key: $KEY" \
  -H "Content-Type: application/json" -d "$BUNDLE")
canon() { python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)['resourceMap'], sort_keys=True))"; }
diff <(echo "$RECEIPT" | canon) <(echo "$REPLAY" | canon) && echo "   receipts identical (canonical comparison)"

PID=$(echo "$RECEIPT" | python3 -c "import json,sys;print(next(m['id'] for m in json.load(sys.stdin)['resourceMap'] if m['resourceType']=='Patient'))")
OID=$(echo "$RECEIPT" | python3 -c "import json,sys;print(next(m['id'] for m in json.load(sys.stdin)['resourceMap'] if m['resourceType']=='Observation'))")
RID=$(echo "$RECEIPT" | python3 -c "import json,sys;print(next(m['id'] for m in json.load(sys.stdin)['resourceMap'] if m['resourceType']=='DiagnosticReport'))")
echo "3) read patient $PID"
curl -fsS "$BASE_URL/patients/$PID" | python3 -m json.tool | head -12

echo "4) report v1 snapshot must pin the observation to version 1"
SNAP1=$(curl -fsS "$BASE_URL/diagnostic-reports/$RID/versions/1")
echo "$SNAP1" | python3 -m json.tool | head -30
echo "$SNAP1" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['result'][0]['observationVersion']==1 and d['result'][0]['traceable'] is True"

echo "5) correct the report narrative only (same result reference)"
CORRECTED=$(cat <<JSON
{
  "resourceType": "Bundle",
  "type": "transaction",
  "entry": [
    {"resource": {"resourceType": "DiagnosticReport", "id": "$RID", "status": "corrected",
      "code": {"coding": [{"code": "24323-8"}]},
      "subject": {"reference": "Patient/$PID"},
      "result": [{"reference": "Observation/$OID"}]},
     "request": {"method": "PUT", "url": "DiagnosticReport/$RID", "ifMatch": "W/\"1\""}}
  ]
}
JSON
)
curl -fsS -X POST "$BASE_URL/fhir" -H "Idempotency-Key: $KEY-CORR" \
  -H "Content-Type: application/json" -d "$CORRECTED" >/dev/null

echo "6) report v2 keeps the v1 pin; report v1 still shows the original value"
SNAP2=$(curl -fsS "$BASE_URL/diagnostic-reports/$RID/versions/2")
echo "$SNAP2" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['version']==2 and d['result'][0]['observationVersion']==1"
curl -fsS "$BASE_URL/diagnostic-reports/$RID/versions/1" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['resource']['status']=='final'"
echo "SMOKE OK"
