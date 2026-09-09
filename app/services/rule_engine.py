"""Deterministic CEL evaluation of a rule over one document object.

No model call happens here: the document object is already aggregated from
extracted facts (app/services/aggregation.py), celpy executes the rule's CEL
over the single root doc, and the verdict is its truth value. A rule reads
doc.<field> for every field its contract declares.
"""

from functools import lru_cache

import celpy

from app.models import CourseRule

_ENV = celpy.Environment()


# Compilation depends only on the expression text, and one analysis run calls
# evaluate() once per (rule, document) pair. Capped so a long-running worker
# cannot grow the cache unboundedly across many distinct rules and edits.
@lru_cache(maxsize=2048)
def _compiled(cel: str):
    """Compile a CEL expression into a runnable program, cached by text."""
    return _ENV.program(_ENV.compile(cel))


def evaluate(rule: CourseRule, document: dict) -> bool:
    """Run a rule's CEL over one aggregated document object.

    Args:
        rule: Rule whose CEL to evaluate.
        document: Aggregated facts, bound as the CEL root doc.

    Returns:
        The verdict: True when the document satisfies the rule.
    """
    program = _compiled(rule.cel)
    result = program.evaluate(celpy.json_to_cel({"doc": document}))
    return bool(result)
