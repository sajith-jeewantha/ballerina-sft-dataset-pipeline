# QUESTION_GEN_PROMPT = """You are creating training questions for a Ballerina programming \
# language assistant. Read the following documentation/code excerpt and write {n} diverse, \
# specific questions that this excerpt fully answers. Mix question types: conceptual, \
# "how do I...", debugging, and code-writing questions. Avoid yes/no questions.
#
# Excerpt:
# \"\"\"
# {chunk}
# \"\"\"
#
# Return ONLY a JSON list of question strings, nothing else."""

QUESTION_GEN_PROMPT = """You are creating training questions for a Ballerina programming \
language assistant. Read the following documentation/code excerpt and write {n} diverse, \
specific questions that this excerpt fully answers.

At least half of the {n} questions MUST be questions that require writing or completing a \
Ballerina code example to answer properly — e.g. "Write a Ballerina function that...", \
"Show how to configure...", "Complete this service definition to...". The rest can be \
conceptual or "how do I..." questions. Avoid yes/no questions.

Excerpt:
\"\"\"
{chunk}
\"\"\"

Return ONLY a JSON list of question strings, nothing else."""


# ANSWER_GEN_PROMPT = """Answer the following Ballerina programming question using ONLY the \
# context provided below. If the context does not contain enough information to answer \
# confidently, respond with exactly: INSUFFICIENT_CONTEXT
#
# Context:
# {context}
#
# Question: {question}
#
# Write a clear, complete answer. Include working Ballerina code where relevant."""

# ANSWER_GEN_PROMPT = """Answer the following Ballerina programming question using the context \
# provided below as your ONLY source for Ballerina-specific facts: API names, function \
# signatures, module names, syntax, keywords, and behavior.
#
# You MAY use your own general programming knowledge ONLY for universal concepts that are not \
# Ballerina-specific (e.g. what a for-loop is, why error handling matters) — but any code, API \
# name, or Ballerina syntax detail in your answer must come directly from the context, not from \
# your own memory of Ballerina.
#
# If the context does not contain enough Ballerina-specific information to answer confidently, \
# respond with exactly: INSUFFICIENT_CONTEXT
#
# Context:
# {context}
#
# Question: {question}
#
# Write a clear, complete answer. Include working Ballerina code where relevant, using ONLY \
# syntax and APIs shown in the context above."""

ANSWER_GEN_PROMPT = """Answer the following Ballerina programming question using the context \
provided below as your ONLY source for Ballerina-specific facts: API names, function \
signatures, module names, syntax, keywords, and behavior.

You MAY use your own general programming knowledge ONLY for universal concepts that are not \
Ballerina-specific (e.g. what a for-loop is, why error handling matters) — but any code, API \
name, or Ballerina syntax detail in your answer must come directly from the context, not from \
your own memory of Ballerina.

If the context does not contain enough Ballerina-specific information to answer confidently, \
respond with exactly: INSUFFICIENT_CONTEXT

Context:
{context}

Question: {question}

Write a clear, complete answer. If the question asks how to do something, write, or complete \
code, your answer MUST include a working Ballerina code example inside a fenced code block \
using ```ballerina ... ``` — not just a description of what the code would do. Use ONLY \
syntax and APIs shown in the context above."""


# FAITHFULNESS_PROMPT = """You are a strict fact-checker. Given the CONTEXT and the ANSWER \
# below, determine if every factual claim in the ANSWER is supported by the CONTEXT.
#
# Context:
# \"\"\"
# {context}
# \"\"\"
#
# Answer:
# \"\"\"
# {answer}
# \"\"\"
#
# Respond with ONLY "FAITHFUL" or "UNFAITHFUL", followed by a one-sentence reason."""

FAITHFULNESS_PROMPT = """You are a strict fact-checker for a Ballerina programming language \
dataset. Given the CONTEXT and the ANSWER below, determine if every BALLERINA-SPECIFIC claim \
in the ANSWER is supported by the CONTEXT — this includes any API name, function signature, \
module name, keyword, syntax detail, or claimed behavior specific to Ballerina.

Do NOT flag general programming statements that apply to any language (e.g. "a for-loop \
repeats a block of code") as unsupported — those don't need to come from the context. Focus \
only on Ballerina-specific facts and code.

Context:
\"\"\"
{context}
\"\"\"

Answer:
\"\"\"
{answer}
\"\"\"

Respond with ONLY "FAITHFUL" or "UNFAITHFUL", followed by a one-sentence reason."""