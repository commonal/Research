# V1 browser acceptance receipt

Date: 2026-09-01

## Question-first golden journey

- Created a session from the research question `长期 Agent 记忆的可靠性方法有哪些？`.
- Started an explicit exploration run and observed `completed`, the phase `探索草稿已生成`, a candidate primary source, budgets, and the draft/unverified label.
- Added the acceptance PDF and opened it in the independent paper pane.
- Attached the real normalized block ID `normalized:<sha256>:text:abstract`; no placeholder or abbreviated ID was used.
- Sent a selection-scoped fixed-flow question, received a resolved citation, and used its page-one jump.
- Generated a fixed formal note, observed `published`, and opened the versioned knowledge entry.

## Paper-first golden journey

- Created an empty session and attached the PDF before asking a question.
- Observed immediate PDF reading and a ready parser projection.
- Sent section- and full-scoped fixed-flow questions. The API receipt recorded the actual Method block for section scope and Abstract plus Method blocks for full scope.
- Observed two resolved page-one citations and a published formal note.

## Product issue found and fixed

The first selection attempt exposed that the UI used the demo ID `B1` while production parsing emits stable full block IDs. Paper projections now include a safe first resolvable block reference, and the UI attaches that exact ID. The fixture keeps `B1` only inside fixture mode.

Browser: Codex in-app browser, local Vite frontend at port 5173, local acceptance API at port 8001.
