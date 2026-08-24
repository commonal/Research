# Full-paper reading with coverage-safe direct writing

## ADDED Requirements

### Requirement: Paper reading is exposed through one deep module

The production workflow MUST invoke paper reading through one `PaperReader` Interface that accepts a candidate, Canonical PaperIR, and bounded reading intent, and returns one `ReadingResult` containing a draft, receipt, and inspectable trace. Callers MUST NOT orchestrate full-paper requests, section planning, coverage assignment, visual calls, writer repair, or targeted rereading.

#### Scenario: A caller reads one canonical paper

- **WHEN** the caller submits a candidate, Canonical PaperIR, and reading intent
- **THEN** it receives one `ReadingResult`
- **AND** production and tests cross the same Interface using different model Adapters
- **AND** internal orchestration remains hidden from the caller

### Requirement: Full ordered PaperModel is the default reading state

`PaperReader` MUST first read the complete available Canonical PaperIR in paper order and produce one evidence-typed PaperModel. The PaperModel MUST cover the central problem, prior gap, argument chain, mechanism, experiments, must-preserve facts, limitations, visual candidates, and material unknowns. Transport Units and local prose memos MUST NOT become the primary paper-level state.

#### Scenario: Complete material covers the core argument

- **WHEN** complete ordered PaperIR supports the problem, method, experiment, and limitation facets without a high-priority material unknown
- **THEN** the receipt records `full_paper` as the strategy
- **AND** the Writer receives the validated PaperModel in paper order
- **AND** no ReadingTarget fallback is created

### Requirement: Section explanation contracts are planned with the PaperModel

The same PaperModel planning operation MUST produce one ordered `SectionExplanationContract` per final note section. Each contract MUST state the reader question or answer contract, necessary prerequisite bridges, reasoning steps, linked experiment slots, asset jobs, transitions, observable stop conditions, and evidence handles. It MUST NOT use target character counts, paragraph counts, or uniform depth tiers as its completion rule.

#### Scenario: Experimental evidence carries more explanatory load than orientation

- **WHEN** a paper has a short motivation but several experiments needed to validate its contribution
- **THEN** the experiment contract names every required setup, comparison, result, interpretation, and boundary
- **AND** the motivation section may remain naturally shorter
- **AND** both sections are judged by their understanding contracts rather than equal length

### Requirement: CoverageLedger makes omission detectable

The reader MUST deterministically build a CoverageLedger from the validated PaperModel and AssetPlan. Every core argument node, key experiment, eligible must-preserve fact, limitation, and inline asset MUST become an obligation assigned to exactly one SectionExplanationContract. Missing, duplicate, invented, or inconsistent assignments MUST block the Writer until one bounded planning repair succeeds or the run is reported bounded/failed.

#### Scenario: One ablation is not assigned to any section

- **WHEN** PaperModel contains a key ablation but no SectionExplanationContract owns its obligation
- **THEN** ledger validation reports the exact missing obligation
- **AND** the system does not silently omit the experiment
- **AND** it may run one planning repair without rereading the paper or resending images

#### Scenario: A fact and experiment overlap

- **WHEN** a must-preserve fact repeats a result already owned by an experiment obligation
- **THEN** both obligation IDs may share one section and be expressed in one natural paragraph
- **AND** each obligation remains independently auditable
- **AND** the Writer is not required to repeat the same sentence

### Requirement: Formula and table meaning preserve definition neighborhoods

When the PaperModel selects a formula or table, the reader MUST retain the object together with its title/caption, footnotes when present, structured content, and nearest same-section prose that defines symbols, metrics, rows, columns, or interpretation. Writer-visible symbol and metric meanings MUST be limited to this validated neighborhood.

#### Scenario: Reward symbols are defined after the formula

- **WHEN** prose following a reward formula defines its symbols
- **THEN** the DefinitionNeighborhood contains the formula and definition block
- **AND** the Writer may use only the supplied meanings
- **AND** unsupported symbol meanings remain unknown

#### Scenario: A result table contains exact metric-to-value relationships

- **WHEN** a key result depends on table rows, columns, caption, or neighboring result prose
- **THEN** the neighborhood preserves those roles and exact values
- **AND** validation rejects a number detached from its metric or compared system

### Requirement: Asset planning is selective and evidence typed

Every formula, table, and figure candidate MUST receive `inline`, `reference`, or `omit` with an explanation job and decision reason. Only a safe real image whose pixels add material understanding beyond structured text, caption, and neighboring prose MAY use the vision Adapter. Quantity limits are cost fuses, not selection quotas.

#### Scenario: A method diagram clarifies the central control loop

- **WHEN** arrows or spatial relations in a safe method figure are necessary to explain the mechanism
- **THEN** the figure may be selected `inline`
- **AND** the visual request contains its caption, neighboring context, and explanation job
- **AND** its output remains a `visual_interpretation`, not a source fact

#### Scenario: A structured table is complete

- **WHEN** HTML rows, columns, caption, and explanatory prose preserve the needed table semantics
- **THEN** the table is handled as text
- **AND** no vision call is made merely because a rendered table image exists

### Requirement: The v2 direct Writer is the quality baseline

The default Writer MUST produce the complete Chinese note in one direct long-form operation from the PaperModel, SectionExplanationContracts, CoverageLedger, DefinitionNeighborhoods, selected visual interpretations, evidence allow-list, and unresolved boundaries. It MUST preserve the paper's causal narrative and MAY vary section length according to explanatory load. It MUST NOT reconstruct the note from Unit Memos or require one model call per section.

#### Scenario: The paper has a long experimental story

- **WHEN** understanding the contribution requires task setup, evaluation protocol, several comparisons, results, and failure analysis
- **THEN** the Writer explains that chain as connected prose
- **AND** it does not compress the experiments into a leaderboard
- **AND** it does not repeat the introduction merely to increase length

### Requirement: Writer output respects evidence and ledger obligations

The Writer MUST NOT emit a formula definition, table interpretation, visual claim, exact number, named mechanism, or source fact absent from its allow-list. Validation MUST detect every unexpressed CoverageLedger obligation. An omission with available evidence MAY trigger one local Writer repair; a missing evidence relationship MUST NOT be repaired by plausible invention.

#### Scenario: Writer omits a supported key experiment

- **WHEN** an experiment obligation and its evidence are present but the draft does not express them
- **THEN** validation identifies the affected obligation and section
- **AND** one local Writer repair may restore it without rewriting unrelated sections
- **AND** the receipt records the repair call

#### Scenario: Writer guesses an undefined symbol

- **WHEN** the draft assigns meaning to a symbol absent from the DefinitionNeighborhood allow-list
- **THEN** the unsupported definition is removed or locally marked unknown
- **AND** the receipt records an unsupported-writer-claim degradation
- **AND** supported surrounding explanation remains intact

### Requirement: Targeted reading is only an evidence-gap fallback

ReadingQuestion, ReadingTarget, EvidenceBundle, ReadingRecord, and ArgumentMap MAY activate only for a recorded high-priority PaperModel material/evidence gap that names the missing relationship. They MUST NOT activate because a draft is short, a transition is weak, or coverage obligations are unassigned.

#### Scenario: Decisive experimental validation is unresolved

- **WHEN** PaperModel explains the mechanism but records a high-priority gap for the experiment that validates it
- **THEN** the reader creates a target scoped to that gap
- **AND** it does not reread already-supported background, mechanism, or limitations
- **AND** Coverage, evidence-boundary, Budget, and No-progress stopping remain active

#### Scenario: Evidence cannot be recovered

- **WHEN** a bounded Target lookup and expansion add no usable source evidence
- **THEN** the reader preserves an unknown and records `evidence_boundary` or `no_progress`
- **AND** synthesis or visual interpretation does not satisfy the missing source-fact facet

### Requirement: Reading quality is not a fixed-length gate

Acceptance MUST combine CoverageLedger completeness, source-fact/facet and numeric validation, formula/table/visual integrity, blind-reader comprehension, and explicit unknown boundaries. Draft length MAY be recorded as an observation but MUST NOT independently fail a note that passes those checks and is accepted by human reading review.

#### Scenario: A concise benchmark note is complete

- **WHEN** a note is shorter than a historical heuristic but uniquely covers every obligation, passes evidence and asset checks, and lets a blind reader reconstruct the paper
- **THEN** length is recorded as a non-blocking observation
- **AND** the run is not marked failed for length alone

### Requirement: Receipt and trace expose the real strategy and cost

Every run MUST record actual text/vision models, strategy, full-read/planning/Writer/repair/Target/expansion/visual calls, budgets, ledger totals and conflicts, asset decisions, degradation, unknowns, and stop reason. Trace artifacts MUST remain outside Layer A and RAG. Human-facing notes MUST exclude raw block IDs, English evidence dumps, provider payloads, parser fragments, trace internals, and local paths.

#### Scenario: A reviewer audits a full-paper run

- **WHEN** the run completes without Target fallback
- **THEN** the receipt records zero target and expansion calls
- **AND** the trace still contains PaperModel, CoverageLedger, DefinitionNeighborhoods, and AssetPlan
- **AND** the note contains no trace internals

### Requirement: Target and generalization acceptance use the same production Interface

The final target-paper replay and both generalization papers MUST use the same `PaperReader` Interface, object model, generic prompts, gates, and non-hand-edited Writer path. Standalone eval scripts MAY preserve experimental evidence but MUST NOT substitute for this acceptance.

#### Scenario: The final reading strategy is accepted

- **WHEN** `2608.18351v1` passes argument, formula, table, visual, evidence, and blind-reader review against its golden note
- **AND** Mamba and PaperBench pass the same production Interface without paper-specific prompts or manual prose edits
- **THEN** the reading change may be closed
- **AND** enabling it as the production default remains a separate decision

### Requirement: Legacy reading remains explicit

Unit Memo, target-primary, and standalone SectionDepthPlan experiment artifacts MAY remain as named historical/debug paths, but they MUST NOT be reported as the final quality baseline. Existing published assets and provenance MUST remain readable, and this change MUST NOT silently fabricate new trace state for older results.

#### Scenario: An older result is loaded

- **WHEN** it lacks PaperModel or CoverageLedger trace state
- **THEN** its existing narrative and evidence mapping remain unchanged
- **AND** the system reports that the revised trace is unavailable
- **AND** it does not fabricate coverage or target history
