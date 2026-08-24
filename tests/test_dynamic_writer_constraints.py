from __future__ import annotations

import json
from unittest import TestCase

from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.reading import (
    CanonicalPaperIR,
    DeepSeekPaperReadingModel,
    FullPaperReadRequest,
    PaperIRBlock,
    _full_paper_prompt,
)


class DynamicWriterConstraintTests(TestCase):
    def test_full_paper_prompt_has_generic_preservation_rules(self) -> None:
        candidate = PaperCandidate("generic-paper", "Generic paper", "https://example.com/paper", "testing")
        paper_ir = CanonicalPaperIR(
            candidate.source_id,
            candidate.title,
            (PaperIRBlock("intro", "paragraph", "Introduction", "The paper establishes a concrete problem.", 1),),
        )

        payload = json.loads(_full_paper_prompt(FullPaperReadRequest(candidate, paper_ir, ("intro",))))
        metadata = " ".join([payload["task"], *payload["rules"]])
        for forbidden in ("broker", "safe success", "pre/post audit", "permission gate", "sandbox"):
            self.assertNotIn(forbidden, metadata.casefold())
        self.assertIn("named entities", metadata)
        self.assertIn("metric-to-condition", metadata)
        self.assertIn("conclusion boundary", metadata)
        self.assertIn("overall total", metadata)
        self.assertIn("central method figure", metadata)

    def test_plan_and_writer_prompts_use_validated_state_as_dynamic_allow_list(self) -> None:
        state = {
            "paper_model": {
                "argument_chain": {
                    "conclusion_scope": "The result supports the proposed method only on the evaluated task family.",
                    "mechanisms": "['trajectory filter', 'Model architecture: encoder routes context']",
                },
                "experiments": [{
                    "experiment_id": "experiment:shift",
                    "setup": ["Train on source tasks and evaluate on shifted tasks."],
                    "comparison": ["Compare baseline and adapted model."],
                    "results": ["Metric Z changes from 0.31 to 0.42."],
                    "interpretation": "The change is consistent with the proposed mechanism.",
                    "boundary": "It does not establish performance outside the evaluated family.",
                    "source_block_ids": ["results"],
                }],
                "must_preserve_facts": [{
                    "fact_id": "fact:mechanism",
                    "facet": "method",
                    "statement": "Model-Z applies a trajectory filter before execution.",
                    "eligible": True,
                    "numeric_tokens": ["0.42"],
                    "named_entities": ["Model-Z"],
                    "mechanism_terms": ["trajectory filter"],
                    "source_block_ids": ["method"],
                }],
                "limitations": [{
                    "limitation_id": "limitation:scope",
                    "statement": "The result is limited to the evaluated task family.",
                    "source_block_ids": ["limits"],
                }],
                "definition_neighborhoods": [{
                    "object_block_id": "formula",
                    "kind": "formula",
                    "object_content": "Z = A + B",
                    "context": "Z is the reported metric; A and B are its supported components.",
                    "defined_symbols": ["Z", "A", "B"],
                    "formula_symbols": ["Z", "A", "B"],
                }],
                "visual_interpretations": [{
                    "visual_block_id": "figure-1",
                    "interpretation": "The Host router sends policy actions to execution and telemetry.",
                }],
            },
            "must_preserve_facts": {
                "facts": [{
                    "fact_id": "fact:mechanism",
                    "facet": "method",
                    "statement": "Model-Z applies a trajectory filter before execution.",
                    "eligible": True,
                    "numeric_tokens": ["0.42"],
                    "named_entities": ["Model-Z"],
                    "mechanism_terms": ["trajectory filter"],
                    "source_block_ids": ["method"],
                }],
                "numeric_groups": [["0.42"]],
                "named_entities": ["Model-Z"],
                "mechanism_terms": ["trajectory filter"],
            },
            "asset_plan": {"decisions": []},
            "section_contracts": [{"section_id": "sec_method", "heading": "Method"}],
            "coverage_ledger": {"assignments": {"fact:mechanism": "sec_method"}},
            "unresolved_boundaries": ["The evaluated task family is the evidence boundary."],
        }
        captured: list[tuple[str, str]] = []
        model = DeepSeekPaperReadingModel(text_model="test-model", api_key="test-key")

        def capture(operation: str, _model: str, prompt: str, image: str | None = None) -> dict[str, object]:
            self.assertIsNone(image)
            captured.append((operation, prompt))
            return {"sections": [{"name": "Method", "paragraph_goal": "Explain the method."}]} if operation == "note_plan" else {"markdown": "# Note\n"}

        model._json_call = capture  # type: ignore[method-assign]
        model.plan_note(state)
        model.write_note(state)

        self.assertEqual([item[0] for item in captured], ["note_plan", "note_write"])
        for _operation, prompt in captured:
            payload = json.loads(prompt)
            serialized = json.dumps(payload, ensure_ascii=False)
            for expected in ("0.42", "Model-Z", "trajectory filter", "evaluated task family"):
                self.assertIn(expected, serialized)
            for forbidden in ("broker", "safe success", "pre/post audit", "permission gate", "sandbox"):
                self.assertNotIn(forbidden, serialized.casefold())
        writer_instruction = json.loads(captured[-1][1])["instruction"].casefold()
        self.assertIn("every inline asset", writer_instruction)
        self.assertIn("do not translate away", writer_instruction)
        preservation = json.loads(captured[-1][1])["preservation_contract"]
        self.assertIn("Model architecture", preservation["named_mechanism_terms"])
        self.assertFalse(any(term.startswith("[") for term in preservation["named_mechanism_terms"]))
        writer_payload = json.loads(captured[-1][1])
        self.assertNotIn("paper_model", writer_payload["plan"])
        self.assertEqual(writer_payload["plan"]["coverage_assignments"], {"fact:mechanism": "sec_method"})
        self.assertIn("Z = A + B", json.dumps(preservation["formula_evidence"], ensure_ascii=False))
        self.assertIn("Host router", json.dumps(preservation["visual_evidence"], ensure_ascii=False))


if __name__ == "__main__":
    import unittest

    unittest.main()
