from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from autoseguro.extraction import deterministic_extract
from autoseguro.pii import find_pii, redact_pii


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=Path("../dataset/conversations.parquet"))
    parser.add_argument("--output", type=Path, default=Path("../artifacts/dataset-evaluation.json"))
    args = parser.parse_args()

    frame = pd.read_parquet(args.dataset)
    texts = frame[frame["message_type"] == "text"]["message_body"].fillna("").astype(str)
    pii_rows = sum(bool(find_pii(text)) for text in texts)
    leaked_rows = sum(bool(find_pii(redact_pii(text))) for text in texts)

    conversations = frame.sort_values(["conversation_id", "message_index"]).groupby(
        "conversation_id"
    )
    extraction_total = 0
    age_matches = 0
    year_matches = 0
    for _, group in conversations:
        combined = " ".join(group["message_body"].fillna("").astype(str))
        extracted = deterministic_extract(combined)
        expected_age = int(group["lead_idade_informada"].iloc[0])
        expected_year = int(str(group["veiculo_texto"].iloc[0]).rsplit(" ", 1)[-1])
        extraction_total += 1
        age_matches += int(extracted.age == expected_age)
        year_matches += int(extracted.vehicle_year == expected_year)

    report = {
        "dataset_rows": int(len(frame)),
        "conversations": int(frame["conversation_id"].nunique()),
        "text_rows_with_detectable_pii": int(pii_rows),
        "pii_rows_leaking_after_redaction": int(leaked_rows),
        "redaction_pass_rate": 1.0 if pii_rows == 0 else round(1 - leaked_rows / pii_rows, 6),
        "age_extraction_accuracy": round(age_matches / extraction_total, 6),
        "vehicle_year_extraction_accuracy": round(year_matches / extraction_total, 6),
        "media_rows": int((frame["message_type"] != "text").sum()),
        "price_ground_truth_used": False,
        "note": "Preços do histórico são aleatórios e não participam da cotação nem da avaliação.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
