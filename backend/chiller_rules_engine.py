#!/usr/bin/env python3
"""
Working rules engine for the chiller diagnostic seed dataset.

Features:
- Loads the nested JSON structure
- Maps raw alarm text to the best fault family using alias matching
- Returns the family questions
- Scores root causes from user answers
- Explains why each cause ranked where it did

Usage examples:
    python chiller_rules_engine.py --input chiller_diagnostic_seed_first5.json --alarm "Flow Switch Open"
    python chiller_rules_engine.py --input chiller_diagnostic_seed_first5.json --alarm "Flow Switch Open" --answers tankLevelOk=no airInSystem=yes pumpAmps=0.8 valvesOpen=yes
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple


def normalize_text(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


def load_seed(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def find_family(seed: Dict[str, Any], raw_alarm: str) -> Tuple[Dict[str, Any] | None, List[Dict[str, Any]]]:
    alarm_norm = normalize_text(raw_alarm)
    matches: List[Dict[str, Any]] = []

    for family in seed.get("fault_families", []):
        for alias in family.get("aliases", []):
            alias_norm = normalize_text(alias["normalized_alias"])
            score = 0
            if alarm_norm == alias_norm:
                score = 100 + int(alias.get("confidence", 0))
            elif alias_norm in alarm_norm:
                score = 60 + int(alias.get("confidence", 0))
            elif any(tok in alarm_norm for tok in alias_norm.split()):
                score = 20 + int(alias.get("confidence", 0))

            if score:
                matches.append(
                    {
                        "family_id": family["id"],
                        "family_name": family["name"],
                        "score": score,
                        "alias_text": alias["alias_text"],
                    }
                )

    matches.sort(key=lambda x: x["score"], reverse=True)
    if not matches:
        return None, []

    best_family_id = matches[0]["family_id"]
    family = next((f for f in seed["fault_families"] if f["id"] == best_family_id), None)
    return family, matches[:8]


def parse_answers(answer_pairs: List[str]) -> Dict[str, Any]:
    answers: Dict[str, Any] = {}
    for pair in answer_pairs:
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        value = value.strip()

        if value.lower() in {"yes", "true"}:
            parsed: Any = "yes"
        elif value.lower() in {"no", "false"}:
            parsed = "no"
        else:
            try:
                parsed = float(value)
            except ValueError:
                parsed = value

        answers[key.strip()] = parsed
    return answers


def compare(answer: Any, operator: str, compare_value: str) -> bool:
    if operator in {"gt", "gte", "lt", "lte"}:
        try:
            answer_num = float(answer)
            compare_num = float(compare_value)
        except (TypeError, ValueError):
            return False

        if operator == "gt":
            return answer_num > compare_num
        if operator == "gte":
            return answer_num >= compare_num
        if operator == "lt":
            return answer_num < compare_num
        if operator == "lte":
            return answer_num <= compare_num

    answer_text = str(answer).strip().lower()
    compare_text = str(compare_value).strip().lower()

    if operator == "eq":
        return answer_text == compare_text
    if operator == "neq":
        return answer_text != compare_text
    if operator == "contains":
        return compare_text in answer_text

    return False


def diagnose_family(family: Dict[str, Any], answers: Dict[str, Any]) -> Dict[str, Any]:
    scores: Dict[str, int] = {}
    explanations: Dict[str, List[str]] = {}
    causes_by_id = {cause["id"]: cause for cause in family.get("root_causes", [])}

    for cause in family.get("root_causes", []):
        scores[cause["id"]] = 0
        explanations[cause["id"]] = []

        for rule in cause.get("scoring_rules", []):
            variable = rule["question_variable"]
            if variable not in answers:
                continue

            if compare(answers[variable], rule["operator"], rule["compare_value"]):
                scores[cause["id"]] += int(rule["weight"])
                explanations[cause["id"]].append(
                    f'{variable} matched {rule["operator"]} {rule["compare_value"]}: {rule["explanation"]} ({rule["weight"]:+d})'
                )

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    results = []
    for cause_id, score in ranked:
        if score <= 0:
            continue
        cause = causes_by_id[cause_id]
        confidence = "High" if score >= 10 else "Medium" if score >= 5 else "Low"
        results.append(
            {
                "root_cause_id": cause_id,
                "cause_name": cause["cause_name"],
                "score": score,
                "confidence": confidence,
                "why": explanations[cause_id],
                "actions": [a["action_text"] for a in sorted(cause.get("actions", []), key=lambda x: x["display_order"])],
            }
        )

    return {
        "family_id": family["id"],
        "family_name": family["name"],
        "questions": sorted(family.get("questions", []), key=lambda x: x["display_order"]),
        "results": results[:5],
    }


def print_family_questions(family: Dict[str, Any]) -> None:
    print("\nQuestions for this fault family:")
    for q in sorted(family.get("questions", []), key=lambda x: x["display_order"]):
        unit = f" ({q['unit']})" if q.get("unit") else ""
        required = "required" if q.get("is_required") else "optional"
        print(f"  - {q['variable_name']}: {q['question_text']}{unit} [{required}]")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Path to nested JSON seed file")
    parser.add_argument("--alarm", required=True, help="Raw alarm text from the machine")
    parser.add_argument("--answers", nargs="*", default=[], help="Answer pairs like tankLevelOk=no pumpAmps=0.8")
    args = parser.parse_args()

    seed = load_seed(Path(args.input))
    family, matches = find_family(seed, args.alarm)

    if not family:
        print("No fault family match found.")
        return

    print(f"\nBest fault family match: {family['name']} ({family['id']})")
    print("\nTop alias matches:")
    for m in matches:
        print(f"  - {m['family_name']} via '{m['alias_text']}' (score={m['score']})")

    print_family_questions(family)

    answers = parse_answers(args.answers)
    if not answers:
        print("\nNo answers supplied yet. Use --answers key=value pairs to score root causes.")
        return

    diagnosis = diagnose_family(family, answers)

    print("\nRanked root causes:")
    if not diagnosis["results"]:
        print("  No root causes scored above zero with the answers provided.")
        return

    for idx, result in enumerate(diagnosis["results"], start=1):
        print(f"\n{idx}. {result['cause_name']} | score={result['score']} | confidence={result['confidence']}")
        print("   Why:")
        for line in result["why"]:
            print(f"    - {line}")
        print("   Actions:")
        for action in result["actions"]:
            print(f"    - {action}")


if __name__ == "__main__":
    main()
