from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
import re
import json
from datetime import datetime


from fastapi.responses import StreamingResponse
from io import BytesIO
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
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
        "conditions": [
            ("suction_pressure", "lt", 50),
            ("superheat", "gt", 20),
            ("subcooling", "lt", 5),
        ],
        "likely_causes": [
            "Low refrigerant charge",
            "Refrigerant leak",
            "Flashing liquid",
        ],
        "next_checks": [
            "Leak check the system",
            "Inspect sight glass condition",
            "Verify refrigerant charge",
        ],
    },
    {
        "id": "liquid_line_restriction",
        "name": "Liquid Line Restriction",
        "fault_family_id": "low_suction_low_charge",
        "conditions": [
            ("suction_pressure", "lt", 50),
            ("superheat", "gt", 20),
            ("subcooling", "gt", 15),
        ],
        "likely_causes": [
            "Restricted liquid line",
            "Plugged filter drier",
            "Metering device restriction",
        ],
        "next_checks": [
            "Check temperature drop across filter drier",
            "Inspect liquid line restriction points",
            "Verify metering device operation",
        ],
    },
    {
        "id": "dirty_condenser",
        "name": "Dirty Condenser / Airflow Problem",
        "fault_family_id": "high_head_pressure",
        "conditions": [
            ("discharge_pressure", "gt", 275),
            ("subcooling", "gt", 15),
            ("superheat", "gte", 5),
        ],
        "likely_causes": [
            "Dirty condenser",
            "Airflow restriction",
            "Condenser fan issue",
        ],
        "next_checks": [
            "Inspect coil cleanliness",
            "Verify condenser fan operation",
            "Check for recirculating hot air",
        ],
    },
    {
        "id": "overcharge",
        "name": "Overcharge / Backed-Up Liquid",
        "fault_family_id": "high_head_pressure",
        "conditions": [
            ("discharge_pressure", "gt", 275),
            ("subcooling", "gt", 20),
            ("superheat", "lt", 10),
        ],
        "likely_causes": [
            "System overcharged",
            "Backed-up liquid in condenser",
        ],
        "next_checks": [
            "Verify actual charge against factory target",
            "Compare subcooling to expected range",
            "Check condenser condition before removing charge",
        ],
    },
    {
        "id": "flooded_evaporator",
        "name": "Flooded / Overfeeding Evaporator",
        "fault_family_id": "low_suction_low_charge",
        "conditions": [
            ("superheat", "lt", 5),
            ("suction_pressure", "gte", 45),
        ],
        "likely_causes": [
            "Overfeeding metering device",
            "Floodback risk",
            "Poor evaporator feed control",
        ],
        "next_checks": [
            "Verify superheat measurement",
            "Inspect TXV/EEV behavior",
            "Check for liquid floodback signs",
        ],
    },
    {
        "id": "low_evap_flow",
        "name": "Low Evaporator Flow",
        "fault_family_id": "low_flow_pump_fault",
        "conditions": [
            ("flow_rate", "lt", 15),
            ("pump_amps", "lt", 1.5),
        ],
        "likely_causes": [
            "Pump loss of prime",
            "Air in system",
            "Flow restriction",
        ],
        "next_checks": [
            "Verify flow and pump differential",
            "Check tank level and prime",
            "Inspect strainers and valves",
        ],
    },
    {
        "id": "freeze_risk_pattern",
        "name": "Freeze Risk",
        "fault_family_id": "freeze_risk",
        "conditions": [
            ("leaving_temp", "lte", 35),
            ("glycol_percent", "lt", 15),
        ],
        "likely_causes": [
            "Insufficient glycol concentration",
            "Aggressive operating temperature",
            "Freeze protection deficiency",
        ],
        "next_checks": [
            "Test glycol concentration",
            "Verify freeze setpoint",
            "Check actual leaving fluid temperature",
        ],
    },
    {
        "id": "poor_pulldown",
        "name": "Poor Pull-Down / Not Reaching Setpoint",
        "fault_family_id": "not_reaching_setpoint",
        "conditions": [
            ("leaving_temp", "gt", 55),
        ],
        "likely_causes": [
            "Insufficient cooling capacity",
            "High process load",
            "Low flow or refrigeration issue",
        ],
        "next_checks": [
            "Compare leaving temp to setpoint",
            "Check refrigeration circuit performance",
            "Verify process load and flow",
        ],
    },
]

class AlarmRequest(BaseModel):
    alarm_text: str

class PdfReportRequest(BaseModel):
    technician_notes: str | None = None
    matched_fault_family: str | None = None
    snapshot_values: Dict[str, Any] | None = None
    metrics: Dict[str, Any] | None = None
    diagnosis_results: list[Dict[str, Any]] | None = None
    service_summary: str | None = None

class DiagnoseRequest(BaseModel):
    fault_family_id: str
    answers: Dict[str, Any]

class AutoDiagnoseNoteRequest(BaseModel):
    note_text: str

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

class AutoDiagnoseNoteRequest(BaseModel):
    note_text: str

def _confidence(score: int) -> str:
    return "High" if score >= 10 else "Medium" if score >= 6 else "Low"

def build_pdf_report(data: PdfReportRequest) -> BytesIO:
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter

    y = height - 50

    def write_line(text: str, size: int = 11, indent: int = 0):
        nonlocal y
        if y < 60:
            pdf.showPage()
            y = height - 50
        pdf.setFont("Helvetica", size)
        pdf.drawString(50 + indent, y, text[:110])
        y -= 16

    pdf.setTitle("Chiller Service Report")

    write_line("Chiller Service Report", 16)
    write_line("")

    write_line("Matched Fault Family:", 12)
    write_line(data.matched_fault_family or "N/A", 11, 20)
    write_line("")

    write_line("Technician Notes:", 12)
    if data.technician_notes:
        for line in data.technician_notes.splitlines():
            write_line(line, 11, 20)
    else:
        write_line("N/A", 11, 20)
    write_line("")

    write_line("Snapshot Values:", 12)
    if data.snapshot_values:
        for k, v in data.snapshot_values.items():
            write_line(f"{k}: {v}", 11, 20)
    else:
        write_line("N/A", 11, 20)
    write_line("")

    write_line("Calculated Metrics:", 12)
    if data.metrics:
        for k, v in data.metrics.items():
            write_line(f"{k}: {v}", 11, 20)
    else:
        write_line("N/A", 11, 20)
    write_line("")

    write_line("Diagnosis Results:", 12)
    if data.diagnosis_results:
        for idx, result in enumerate(data.diagnosis_results, start=1):
            write_line(f"{idx}. {result.get('cause_name', 'Unknown')} ({result.get('confidence', 'N/A')})", 11, 20)
            write_line(f"Score: {result.get('score', 'N/A')}", 11, 40)
            for action in result.get("actions", []):
                write_line(f"- {action}", 11, 40)
    else:
        write_line("N/A", 11, 20)
    write_line("")

    write_line("Service Summary:", 12)
    if data.service_summary:
        for line in data.service_summary.splitlines():
            write_line(line, 11, 20)
    else:
        write_line("N/A", 11, 20)

    pdf.save()
    buffer.seek(0)
    return buffer

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
        total = len(fp["conditions"])

        for field, op, target in fp["conditions"]:
            value = getattr(req, field, None)
            if _compare(value, op, target):
                passed.append(
                    {
                        "field": field,
                        "operator": op,
                        "target": target,
                        "actual_value": value,
                    }
                )

        score = len(passed) / total if total else 0

        if score >= 0.67:
            matched.append(
                {
                    "id": fp["id"],
                    "name": fp["name"],
                    "fault_family_id": fp["fault_family_id"],
                    "matched_conditions": passed,
                    "matched_count": len(passed),
                    "total_conditions": total,
                    "match_score": round(score, 2),
                    "likely_causes": fp.get("likely_causes", []),
                    "next_checks": fp.get("next_checks", []),
                }
            )

    matched.sort(key=lambda x: x["match_score"], reverse=True)
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

def snapshot_request_from_extracted(extracted: Dict[str, Any]) -> SnapshotRequest:
    return SnapshotRequest(
        suction_pressure=extracted.get("suction_pressure"),
        discharge_pressure=extracted.get("discharge_pressure"),
        superheat=extracted.get("superheat"),
        subcooling=extracted.get("subcooling"),
        leaving_temp=extracted.get("leaving_temp"),
        return_temp=extracted.get("return_temp"),
        flow_confirmed=extracted.get("flow_confirmed"),
        flow_rate=extracted.get("flow_rate"),
        pump_amps=extracted.get("pump_amps"),
        ambient_temp=extracted.get("ambient_temp"),
        fans_running=extracted.get("fans_running"),
        compressor_running=extracted.get("compressor_running"),
        glycol_percent=extracted.get("glycol_percent"),
    )

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

@app.post("/generate-pdf-report")
def generate_pdf_report(req: PdfReportRequest):
    pdf_buffer = build_pdf_report(req)
    return StreamingResponse(
        pdf_buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=chiller_service_report.pdf"},
    )

@app.post("/diagnose")
def diagnose(req: DiagnoseRequest) -> Dict[str, Any]:
    family = next((f for f in seed["fault_families"] if f["id"] == req.fault_family_id), None)
    if not family:
        raise HTTPException(status_code=404, detail="Fault family not found.")
    return diagnose_family(family, req.answers)

@app.post("/parse-tech-note")
def parse_note(req: TechNoteRequest) -> Dict[str, Any]:
    return {"extracted": parse_tech_note(req.note_text)}

@app.get("/")
def root():
    return {
        "app": "Chiller Diagnostic API",
        "status": "running",
        "version": "1.0"
    }


@app.post("/auto-diagnose-note")
def auto_diagnose_note(req: AutoDiagnoseNoteRequest) -> Dict[str, Any]:
    extracted = parse_tech_note(req.note_text)

    snapshot_req = SnapshotRequest(**extracted)
    snapshot_result = snapshot_match(snapshot_req)

    family = next(
        (f for f in seed["fault_families"] if f["id"] == snapshot_result["fault_family_id"]),
        None,
    )

    if not family:
        raise HTTPException(status_code=404, detail="Fault family not found.")

    auto_answers: Dict[str, Any] = {}

    for q in family.get("questions", []):
        var = q["variable_name"]
        if var in extracted:
            auto_answers[var] = extracted[var]

    diagnosis = diagnose_family(family, auto_answers)

    return {
        "note_text": req.note_text,
        "extracted": extracted,
        "snapshot_match": snapshot_result,
        "auto_answers": auto_answers,
        "diagnosis": diagnosis,
    }

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
    points = int(fp["match_score"] * 10)
    scores[fp["fault_family_id"]] += points
    reasons[fp["fault_family_id"]].append(
        f'Fingerprint matched: {fp["name"]} ({fp["matched_count"]}/{fp["total_conditions"]} conditions)'
    )
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

@app.post("/auto-diagnose-note")
def auto_diagnose_note(req: AutoDiagnoseNoteRequest) -> Dict[str, Any]:
    extracted = parse_tech_note(req.note_text)

    snapshot_req = snapshot_request_from_extracted(extracted)
    snapshot_result = snapshot_match(snapshot_req)

    family = next(
        (f for f in seed["fault_families"] if f["id"] == snapshot_result["fault_family_id"]),
        None,
    )
    if not family:
        raise HTTPException(status_code=404, detail="Fault family not found.")

    auto_answers: Dict[str, Any] = {}

    # map extracted values into likely diagnostic answers where possible
    for q in family.get("questions", []):
        var = q["variable_name"]
        if var in extracted:
            auto_answers[var] = extracted[var]

    diagnosis = diagnose_family(family, auto_answers)

    return {
        "note_text": req.note_text,
        "extracted": extracted,
        "snapshot_match": snapshot_result,
        "auto_answers": auto_answers,
        "diagnosis": diagnosis,
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
