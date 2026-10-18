# Engineering Decisions & Concepts

## Transactional Outbox
- Atomically update the database and send messages to a message broker
- It is undesirable to couple the two services, 2PC (Two-Phase Commit) is out.
- Use a separate MessageRelay service to send messages to the message broker after db transaction
- Insert into an OUTBOX table
- Message consumer must be idempotent

## APK Malware Analysis
- Traditional ML models suffer from concept drift, shallow features, lack of semantic understanding and interpretability.
- LLMs have semantic reasoning capabilities, but processing raw code directly incurs token overhead.
- An on-demand deterministic analysis toolset, with an LLM to orchestrate the process based on the ReAct paradigm.
- Deterministic State Representation:
	1. Extract critical configuration metadata from `AndroidManifest.xml`
	2. Dalvik bytecode -> Intermediate representation
	3. Global call graph & API inverted index
- Malware understanding is a reasoning problem under partial observability.
- Forms of grounding:
	1. **Domain grounding** to structure analysis in a manner consistent with professional security practice.
	2. **Semantic grounding** so that it knows where to inspect by using related analysis tools.
	3. **Knowledge grounding** where observed behaviors are connected to externally verifiable threat knowledge.
- The goal is to iteratively build and verify hypotheses about potentially malicious behavior.
	- Turn JVM bytecode into queryable program graph like graphify for better navigability and discovery

## Policy-Enforcement
- The agent can request a tool; it cannot grant itself permission to use it.
- Least privilege: only grant the tools required for the given task.
- Enforce authorization at the tool-execution boundary, on every call, rather than trusting the model.
- Require a separate approval decision before a restricted action executes
