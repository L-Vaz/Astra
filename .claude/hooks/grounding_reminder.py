#!/usr/bin/env python
"""UserPromptSubmit hook: re-assert Astra's engineering discipline every turn.

CLAUDE.md and skills are instructions the model reads and chooses to follow; both can
fade over a long session. This hook is executed by the harness, so it cannot. Its
stdout is appended to the model's context on every prompt.

Keep it short - it is paid for on every single turn. The full protocol lives in
.claude/skills/grounding.md; this is only the part that must never be forgotten.

Edit the text freely. That is why it is a file and not a string buried in settings.json.
"""
import sys

REMINDER = """\
<engineering-discipline>
Protocol: .claude/skills/grounding.md. HEAVY for physical numbers, algorithm
correctness, architectural choices. LIGHT (skip) for refactors, tests, UI, tooling.

NUMBERS - grade every engineering quantity, inline:
  [A] primary source, cited to equation/section, read this session
  [B] textbook, cited to edition + page, read this session
  [C] measured here, reproducing command recorded
  [D] judgment - NOT a fact - needs sensitivity + what would falsify it
"Raymer says" with no page is [D]. "Standard practice" is [D]. Fluency is not
evidence: a number arriving without a source attached is a STOP signal.

FIRST PRINCIPLES - he has an ME degree and rusty aero. Concept from basics, then
the value. Never a bare number.

NO SOURCE? Route it, never guess:
  research    -> fetch it yourself (NTRS/NACA/DATCOM/official docs), or ask for ONE
                 located page
  measurement -> propose the experiment + confirm/refute criteria, decided first
  decision    -> hand back with band, consequence, sensitivity, recommendation.
                 Choosing silently here is taking a decision that is his.

WORK - prove the problem is REAL before fixing: failing test, measured wrong output,
reproduced behaviour. "Looks fragile" is not admissible. Can't demonstrate it? Say so
and do not build. Record substantive decisions in docs/decisions/.

Proceeding on a provisional value is fine. Proceeding quietly is not.
Close engineering answers with the Grounding block. Session verdict must cite
checkable evidence, never opinion - you are grading your own work.
</engineering-discipline>"""

if __name__ == "__main__":
    sys.stdout.write(REMINDER)
