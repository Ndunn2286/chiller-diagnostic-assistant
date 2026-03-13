from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
import re
import json
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from chiller_rules_engine import load_seed, find_family, diagnose_family

BASE_DIR = Path(__file__).resolve().parent
SEED_PATH = BASE_DIR / "chiller_diagnostic_seed_first5.json"
FEEDBACK_PATH = BASE_DIR / "case_feedback.json"

app = FastAPI(title="Chiller Diagnostic API", version="0.4.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

seed = load_seed(SEED_PATH)

OEM_ALIAS_LIBRARY = {
    "Thermal Care": [
        "flow proof lost",
        "process flow fault",
        "low evap flow",
        "pump interlock",
        "high fluid temp",
        "freeze protect",
    ],
    "Mokon": [
        "flow switch open",
        "low process flow",
        "high process temp",
        "low tank level",
    ],
    "Advantage": [
        "flow failure",
        "pump fault",
        "high cond press",
        "low refrigerant pressure",
    ],
    "Carrier": [
        "cond press high",
        "low suction",
        "freeze protect",
        "high discharge temp",
    ],
}

FINGERPRINTS = [
    {
        "id": "low_charge",
        "name": "Low Refrigerant Charge",
        "fault_family_id": "low_suction_low_charge",
        "conditions": [("suction_pressure", "lt", 50), ("superheat", "gt", 20), ("subcooling", "lt", 5)],
        "likely_causes": ["Low refrigerant charge", "Refrigerant leak", "Flashing liquid"],
    },
    {
        "id": "liquid_line_restriction",
        "name": "Liquid Line Restriction",
        "fault_family_id": "low_suction_low_charge",
        "conditions": [("suction_pressure", "lt", 50), ("superheat", "gt", 20), ("subcooling", "gt", 15)],
        "likely_causes": ["Restricted liquid line", "Plugged filter drier", "Metering device restriction"],
    },
    {
        "id": "dirty_condenser",
        "name": "Dirty Condenser / High Head",
        "fault_family_id": "high_head_pressure",
        "conditions": [("discharge_pressure", "gt", 275), ("subcooling", "gt", 15)],
        "likely_causes": ["Dirty condenser", "Airflow restriction", "Fan failure"],
    },
    {
        "id": "overcharge",
        "name": "Overcharge / Backed-Up Liquid",
        "fault_family_id": "high_head_pressure",
        "conditions": [("discharge_pressure", "gt", 275), ("subcooling", "gt", 20), ("superheat", "lt", 8)],
        "likely_causes": ["System overcharged", "Liquid backed up in condenser"],
    },
    {
        "id": "flooded_evaporator",
        "name": "Flooding / Overfeeding Evaporator",
        "fault_family_id": "low_suction_low_charge",
        "conditions": [("superheat", "lt", 5), ("suction_pressure", "gte", 45)],
        "likely_causes": ["Overfeeding metering device", "Floodback risk", "Bad control of evaporator feed"],
    },
    {
        "id": "low_evap_flow",
        "name": "Low Evaporator Flow",
        "fault_family_id": "low_flow_pump_fault",
        "conditions": [("flow_rate", "lt", 15), ("pump_amps", "lt", 1.5)],
        "likely_causes": ["Pump loss of prime", "Air in system", "Flow restriction"],
    },
    {
        "id": "freeze_risk_pattern",
        "name": "Freeze Risk",
        "fault_family_id": "freeze_risk",
        "conditions": [("leaving_temp", "lte", 35), ("glycol_percent", "lt", 15)],
        "likely_causes": ["Insufficient glycol concentration", "Aggressive operating temperature", "Freeze protection deficiency"],
    },
]

class AlarmRequest(BaseModel):
    alarm_text: str

class DiagnoseRequest(BaseModel):
    fault_family_id: str
    answers: Dict[str, Any]

class SnapshotRequest(BaseModel):
    suction_pressure: float | None = None
    discharge_pressure: float | None = None
    superheat: float | None = None
    subcooling: float | None = None
    leaving_temp: float | None = None
    return_temp: float | None = None
    flow_confirmed: str | None = None
    flow_rate: float | None = None
    pump_amps: float | None = None
    ambient_temp: float | None = None
    fans_running: str | None = None
    compressor_running: str | None = None
    glycol_percent: float | None = None

class TechNoteRequest(BaseModel):
    note_text: str

class GuidedRequest(BaseModel):
    compressor_running: str | None = None
    pump_running: str | None = None
    flow_confirmed: str | None = None
    leaving_temp_high: str | None = None
    alarm_present: str | None = None

class FeedbackRequest(BaseModel):
    predicted_top_cause: str | None = None
    actual_root_cause: str
    was_prediction_correct: bool
    technician_notes: str | None = None
    fault_family_id: str | None = None
    matched_fingerprint: str | None = None

def _confidence(score: int) -> str:
    return "High" if score >= 10 else "Medium" if score >= 6 else "Low"

def _compare(value: float | None, operator: str, target: float) -> bool:
    if value is None:
        return False
    if operator == "lt":
        return value < target
    if operator == "lte":
        return value <= target
    if operator == "gt":
        return value > target
    if operator == "gte":
        return value >= target
    return False

def psi_from_bar(bar: float) -> float:
    return bar * 14.5038

def psi_from_kpa(kpa: float) -> float:
    return kpa * 0.145038

def f_from_c(c: float) -> float:
    return (c * 9 / 5) + 32

def gpm_from_lpm(lpm: float) -> float:
    return lpm * 0.264172

def calculate_snapshot_metrics(req: SnapshotRequest) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {}
    if req.leaving_temp is not None and req.return_temp is not None:
        evap_delta_t = req.return_temp - req.leaving_temp
        metrics["evap_delta_t"] = round(evap_delta_t, 2)
        metrics["evap_delta_t_flag"] = "low" if evap_delta_t < 4 else "high" if evap_delta_t > 12 else "normal"
    if req.superheat is not None:
        metrics["superheat_flag"] = "low" if req.superheat < 5 else "high" if req.superheat > 20 else "normal"
    if req.subcooling is not None:
        metrics["subcooling_flag"] = "low" if req.subcooling < 5 else "high" if req.subcooling > 15 else "normal"
    if req.discharge_pressure is not None:
        metrics["head_pressure_flag"] = "high" if req.discharge_pressure > 275 else "elevated" if req.discharge_pressure > 240 else "normal"
    if req.leaving_temp is not None:
        metrics["leaving_temp_flag"] = "high" if req.leaving_temp > 55 else "elevated" if req.leaving_temp > 45 else "normal"
    return metrics

def detect_fingerprints(req: SnapshotRequest) -> list[Dict[str, Any]]:
    matched = []
    for fp in FINGERPRINTS:
        passed = []
        for field, op, target in fp["conditions"]:
            if _compare(getattr(req, field), op, target):
                passed.append({"field": field, "operator": op, "target": target})
        if len(passed) == len(fp["conditions"]):
            matched.append(
                {
                    "id": fp["id"],
                    "name": fp["name"],
                    "fault_family_id": fp["fault_family_id"],
                    "matched_conditions": passed,
                    "likely_causes": fp["likely_causes"],
                }
            )
    return matched

def parse_tech_note(text: str) -> Dict[str, Any]:
    extracted: Dict[str, Any] = {}
    lowered = text.lower()

    # pressure
    pressure_patterns = {
        "suction_pressure": [
            r"suction\s*(?:pressure)?\s*[:=]?\s*(\d+\.?\d*)\s*bar",
            r"suction\s*(?:pressure)?\s*[:=]?\s*(\d+\.?\d*)\s*kpa",
            r"suction\s*(?:pressure)?\s*[:=]?\s*(\d+\.?\d*)",
            r"\bsh?sp\b\s*[:=]?\s*(\d+\.?\d*)",
            r"\bsuct(?:ion)?\b\s*[:=]?\s*(\d+\.?\d*)",
        ],
        "discharge_pressure": [
            r"discharge\s*(?:pressure)?\s*[:=]?\s*(\d+\.?\d*)\s*bar",
            r"discharge\s*(?:pressure)?\s*[:=]?\s*(\d+\.?\d*)\s*kpa",
            r"discharge\s*(?:pressure)?\s*[:=]?\s*(\d+\.?\d*)",
            r"\bhead\b\s*[:=]?\s*(\d+\.?\d*)",
            r"\bdp\b\s*[:=]?\s*(\d+\.?\d*)",
        ],
    }
    for key, pattern_list in pressure_patterns.items():
        for pattern in pattern_list:
            m = re.search(pattern, lowered)
            if m:
                value = float(m.group(1))
                if "bar" in pattern:
                    value = psi_from_bar(value)
                elif "kpa" in pattern:
                    value = psi_from_kpa(value)
                extracted[key] = round(value, 2)
                break

    # temps and misc
    temp_patterns = {
        "superheat": [r"superheat\s*[:=]?\s*(\d+\.?\d*)", r"\bsh\b\s*[:=]?\s*(\d+\.?\d*)"],
        "subcooling": [r"subcooling\s*[:=]?\s*(\d+\.?\d*)", r"\bsc\b\s*[:=]?\s*(\d+\.?\d*)"],
        "leaving_temp": [r"(?:leaving water|leaving fluid|leaving temp|lwt)\s*[:=]?\s*(\d+\.?\d*)\s*c", r"(?:leaving water|leaving fluid|leaving temp|lwt)\s*[:=]?\s*(\d+\.?\d*)"],
        "return_temp": [r"(?:return water|return fluid|return temp|rwt)\s*[:=]?\s*(\d+\.?\d*)\s*c", r"(?:return water|return fluid|return temp|rwt)\s*[:=]?\s*(\d+\.?\d*)"],
        "ambient_temp": [r"(?:ambient|oat|outdoor ambient)\s*[:=]?\s*(\d+\.?\d*)\s*c", r"(?:ambient|oat|outdoor ambient)\s*[:=]?\s*(\d+\.?\d*)"],
        "glycol_percent": [r"(?:glycol|glycol percent|glycol %)\s*[:=]?\s*(\d+\.?\d*)"],
        "pump_amps": [r"(?:pump amps|pump amp draw|pump amperage)\s*[:=]?\s*(\d+\.?\d*)"],
    }
    for key, pattern_list in temp_patterns.items():
        for pattern in pattern_list:
            m = re.search(pattern, lowered)
            if m:
                value = float(m.group(1))
                if pattern.endswith(r"\s*c"):
                    value = f_from_c(value)
                extracted[key] = round(value, 2)
                break

    # flow
    for pattern in [r"(?:flow rate|flow)\s*[:=]?\s*(\d+\.?\d*)\s*lpm", r"(?:flow rate|flow)\s*[:=]?\s*(\d+\.?\d*)", r"\bgpm\b\s*[:=]?\s*(\d+\.?\d*)"]:
        m = re.search(pattern, lowered)
        if m:
            value = float(m.group(1))
            if "lpm" in pattern:
                value = gpm_from_lpm(value)
            extracted["flow_rate"] = round(value, 2)
            break

    # states
    if re.search(r"(flow confirmed|flow)\s*[:=]?\s*yes", lowered):
        extracted["flow_confirmed"] = "yes"
    elif re.search(r"(flow confirmed|flow)\s*[:=]?\s*no", lowered) or "no flow" in lowered:
        extracted["flow_confirmed"] = "no"
    if re.search(r"(fans running|fans)\s*[:=]?\s*yes", lowered):
        extracted["fans_running"] = "yes"
    elif re.search(r"(fans running|fans)\s*[:=]?\s*no", lowered):
        extracted["fans_running"] = "no"
    if re.search(r"(compressor running|compressor)\s*[:=]?\s*yes", lowered):
        extracted["compressor_running"] = "yes"
    elif re.search(r"(compressor running|compressor)\s*[:=]?\s*no", lowered):
        extracted["compressor_running"] = "no"

    return extracted

def _load_feedback() -> list[Dict[str, Any]]:
    if not FEEDBACK_PATH.exists():
        return []
    try:
        return json.loads(FEEDBACK_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []

def _save_feedback(data: list[Dict[str, Any]]) -> None:
    FEEDBACK_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")

@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}

@app.get("/oem-alias-library")
def get_oem_alias_library() -> Dict[str, Any]:
    return {"oem_alias_library": OEM_ALIAS_LIBRARY}

@app.post("/match-alarm")
def match_alarm(req: AlarmRequest) -> Dict[str, Any]:
    family, matches = find_family(seed, req.alarm_text)
    if not family:
        raise HTTPException(status_code=404, detail="No fault family match found.")
    questions = sorted(family.get("questions", []), key=lambda x: x["display_order"])
    return {
        "mode": "alarm",
        "fault_family_id": family["id"],
        "fault_family_name": family["name"],
        "description": family.get("description", ""),
        "matches": matches,
        "questions": questions,
    }

@app.post("/diagnose")
def diagnose(req: DiagnoseRequest) -> Dict[str, Any]:
    family = next((f for f in seed["fault_families"] if f["id"] == req.fault_family_id), None)
    if not family:
        raise HTTPException(status_code=404, detail="Fault family not found.")
    return diagnose_family(family, req.answers)

@app.post("/parse-tech-note")
def parse_note(req: TechNoteRequest) -> Dict[str, Any]:
    return {"extracted": parse_tech_note(req.note_text)}

@app.post("/guided-troubleshoot")
def guided(req: GuidedRequest) -> Dict[str, Any]:
    suggested_family = "not_reaching_setpoint"
    why = []

    if req.compressor_running == "no":
        suggested_family = "not_reaching_setpoint"
        why.append("No compressor operation points toward generic no-cooling / poor-cooling.")
    if req.pump_running == "no" or req.flow_confirmed == "no":
        suggested_family = "low_flow_pump_fault"
        why.append("No pump or no confirmed flow points toward circulation fault.")
    if req.leaving_temp_high == "yes":
        suggested_family = "not_reaching_setpoint"
        why.append("High leaving temperature supports poor cooling performance.")
    if req.alarm_present == "yes" and req.flow_confirmed == "no":
        suggested_family = "low_flow_pump_fault"

    family = next((f for f in seed["fault_families"] if f["id"] == suggested_family), None)
    questions = sorted(family.get("questions", []), key=lambda x: x["display_order"]) if family else []
    return {
        "fault_family_id": suggested_family,
        "fault_family_name": family["name"] if family else suggested_family,
        "why": why or ["Guided mode defaulted to the most likely family from the high-level answers."],
        "questions": questions,
    }

@app.post("/snapshot-match")
def snapshot_match(req: SnapshotRequest) -> Dict[str, Any]:
    scores = {
        "low_suction_low_charge": 0,
        "high_head_pressure": 0,
        "low_flow_pump_fault": 0,
        "freeze_risk": 0,
        "not_reaching_setpoint": 0,
    }
    reasons = {k: [] for k in scores}
    metrics = calculate_snapshot_metrics(req)
    fingerprints = detect_fingerprints(req)

    for fp in fingerprints:
        scores[fp["fault_family_id"]] += 8
        reasons[fp["fault_family_id"]].append(f'Fingerprint matched: {fp["name"]}')

    if req.suction_pressure is not None and req.suction_pressure < 50:
        scores["low_suction_low_charge"] += 4
        reasons["low_suction_low_charge"].append("Low suction pressure suggests starved evaporator or low charge.")
        scores["low_flow_pump_fault"] += 2
        reasons["low_flow_pump_fault"].append("Low suction can also happen with poor evaporator load transfer or low flow.")
    if req.superheat is not None and req.superheat > 20:
        scores["low_suction_low_charge"] += 5
        reasons["low_suction_low_charge"].append("High superheat strongly supports starvation or undercharge.")
    if req.subcooling is not None and req.subcooling < 5:
        scores["low_suction_low_charge"] += 4
        reasons["low_suction_low_charge"].append("Low subcooling can support low charge or flashing liquid.")
    if req.discharge_pressure is not None and req.discharge_pressure > 275:
        scores["high_head_pressure"] += 5
        reasons["high_head_pressure"].append("High discharge pressure points toward condenser heat-rejection issues.")
    if req.subcooling is not None and req.subcooling > 20:
        scores["high_head_pressure"] += 3
        reasons["high_head_pressure"].append("High subcooling can support backed-up liquid or condenser-side issues.")
    if req.fans_running == "no":
        scores["high_head_pressure"] += 5
        reasons["high_head_pressure"].append("Condenser fans not running strongly supports high head pressure.")
    if req.flow_confirmed == "no":
        scores["low_flow_pump_fault"] += 5
        reasons["low_flow_pump_fault"].append("No confirmed flow directly supports circulation fault.")
        scores["freeze_risk"] += 2
        reasons["freeze_risk"].append("Loss of flow can create localized freeze conditions.")
    if req.flow_rate is not None and req.flow_rate < 15:
        scores["low_flow_pump_fault"] += 4
        reasons["low_flow_pump_fault"].append("Low flow rate supports pump/circulation fault.")
        scores["not_reaching_setpoint"] += 2
        reasons["not_reaching_setpoint"].append("Low flow can reduce heat transfer and hurt pull-down.")
    if req.pump_amps is not None and req.pump_amps < 1:
        scores["low_flow_pump_fault"] += 4
        reasons["low_flow_pump_fault"].append("Very low pump amps can indicate no-load, loss of prime, or coupling issue.")
    if req.leaving_temp is not None and req.return_temp is not None:
        delta_t = req.return_temp - req.leaving_temp
        if delta_t < 4:
            scores["low_flow_pump_fault"] += 2
            reasons["low_flow_pump_fault"].append("Very low delta-T can fit poor heat transfer or weak load pickup.")
        elif delta_t > 12:
            scores["low_flow_pump_fault"] += 2
            reasons["low_flow_pump_fault"].append("High delta-T can support low flow or heavy load.")
        if req.leaving_temp > 55:
            scores["not_reaching_setpoint"] += 4
            reasons["not_reaching_setpoint"].append("High leaving fluid temperature suggests poor cooling performance.")
        elif req.leaving_temp > 45:
            scores["not_reaching_setpoint"] += 2
            reasons["not_reaching_setpoint"].append("Leaving temperature is a bit high.")
    if req.leaving_temp is not None and req.glycol_percent is not None and req.leaving_temp <= 35 and req.glycol_percent < 15:
        scores["freeze_risk"] += 5
        reasons["freeze_risk"].append("Low leaving temperature with weak glycol concentration increases freeze risk.")
    if req.compressor_running == "no":
        scores["not_reaching_setpoint"] += 3
        reasons["not_reaching_setpoint"].append("No compressor running supports a no-cooling condition.")

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    best_id, best_score = ranked[0]
    if best_score <= 0:
        best_id = "not_reaching_setpoint"
        best_score = 1
        reasons["not_reaching_setpoint"].append("No strong fingerprint matched, so the engine defaulted to a generic poor-cooling family.")

    family = next((f for f in seed["fault_families"] if f["id"] == best_id), None)
    if not family:
        raise HTTPException(status_code=404, detail="Matched family not found in seed data.")

    questions = sorted(family.get("questions", []), key=lambda x: x["display_order"])
    return {
        "mode": "snapshot",
        "fault_family_id": family["id"],
        "fault_family_name": family["name"],
        "description": family.get("description", ""),
        "confidence": _confidence(best_score),
        "why_matched": reasons[best_id],
        "candidate_scores": [
            {
                "fault_family_id": fam_id,
                "fault_family_name": next((f["name"] for f in seed["fault_families"] if f["id"] == fam_id), fam_id),
                "score": score,
            }
            for fam_id, score in ranked
        ],
        "questions": questions,
        "metrics": metrics,
        "fingerprints": fingerprints,
    }

@app.post("/feedback")
def save_feedback(req: FeedbackRequest) -> Dict[str, Any]:
    feedback = _load_feedback()
    feedback.append(
        {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "predicted_top_cause": req.predicted_top_cause,
            "actual_root_cause": req.actual_root_cause,
            "was_prediction_correct": req.was_prediction_correct,
            "technician_notes": req.technician_notes,
            "fault_family_id": req.fault_family_id,
            "matched_fingerprint": req.matched_fingerprint,
        }
    )
    _save_feedback(feedback)
    return {"status": "saved", "count": len(feedback)}

@app.get("/feedback")
def get_feedback() -> Dict[str, Any]:
    return {"feedback": _load_feedback()}
