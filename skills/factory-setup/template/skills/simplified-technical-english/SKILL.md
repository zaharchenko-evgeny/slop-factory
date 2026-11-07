---
name: simplified-technical-english
description: Use STE-inspired writing guidance when drafting or editing technical documentation, task and issue descriptions, procedures, reports, or technical explanations, and when asked to simplify technical text or make it clearer. Preserve meaning, uncertainty, and useful domain terms. Do not use for creative or marketing writing.
license: MIT
metadata:
  source: https://github.com/0xpili/simplified-technical-english
  adaptation: Writing guidance only; no checker or strict ASD-STE100 compliance.
---

# Simplified Technical English — writing guidance

Use the clarity principles of Simplified Technical English as flexible guidance.
Follow the user's requested style and the project's writing conventions.
This adaptation does not enforce or certify ASD-STE100 compliance.

## Preserve meaning first

- Keep facts, conditions, quantities, exceptions, and technical distinctions intact.
- Keep uncertainty explicit. Use "may", "might", "could", or "not verified" when the evidence calls for them.
- Distinguish requirements, recommendations, permissions, and predictions. Do not replace "should" with "must" or "may" with "will" to simplify a sentence.
- Preserve the difference between observed behavior, a hypothesis, a proposal, and planned work.
- Name the actor when it is known. If it is unknown or unimportant, passive voice is acceptable; do not invent an actor.

## Make actions and explanations clear

- Lead with the concrete problem, result, or action that matters to the reader.
- Prefer active voice and direct verbs. In procedures, use commands such as "Save the file" or "Check the result".
- Put a condition before the action when the reader needs it first: "If the upload fails, keep the local artifacts."
- Give each sentence one main point. Separate sequential actions when that makes them easier to follow.
- Aim for short sentences: roughly 20 words for instructions and 25 for explanations. Treat these as guidance, not limits that override accuracy.
- Keep each paragraph on one topic. Use short paragraphs, and use lists for steps or parallel items.
- Use familiar words: "use" instead of "utilize", "before" instead of "prior to", and "start" instead of "commence".
- Remove empty introductions, repeated requirements, vague qualifiers, and unnecessary noun chains.
- Name concrete inputs, outputs, components, and checks. Include details only when they help the reader act or assess a conclusion.

## Keep terminology useful

- Use one consistent name for the same component or concept.
- Keep necessary software and domain terms, including familiar verbs such as "test", "check", "verify", "deploy", and "retry".
- Explain unfamiliar terms when the intended reader needs them.
- Preserve code, identifiers, commands, file paths, URLs, quoted errors, and official names exactly.
- Use natural grammar. Contractions, verb tenses, and "-ing" forms are acceptable when they make the meaning clearer.
- Do not require an approved-word dictionary or replace precise terms with awkward circumlocutions.

## Review while writing

Read the draft once for clarity and meaning. Remove repetition, shorten difficult sentences, and check that each recommendation keeps its original strength.
Use judgment; do not run a Python checker, count every word, or repeat checks until a formal score passes.
Apply only the edits that improve the text, and return the requested artifact without a separate compliance report unless asked.

## Source and license

Adapted from [0xpili/simplified-technical-english](https://github.com/0xpili/simplified-technical-english), which summarizes ASD-STE100 Issue 7 (2017).
The local adaptation omits the checker, dictionary, and strict compliance rules.
Retain [LICENSE](LICENSE) and see [NOTICE.md](NOTICE.md) for attribution.
