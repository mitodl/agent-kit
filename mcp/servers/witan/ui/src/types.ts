/**
 * Result types for the bound read tools.
 *
 * ★ HAND-WRITTEN, AND THEY HAVE TO BE. Every bound tool is registered without
 * `output_schema=`, so fastmcp derives one from the return annotation and gets
 * `{"type": "object", "additionalProperties": true}` or a `result` wrapper
 * around an untyped array. There is nothing to generate from.
 *
 * They are kept honest from the other side instead: `fixtures/` holds real
 * results recorded from real tool calls (`just ui-fixtures`).
 * `all-fixtures.test.ts` runs every one of them through the unwrapper and then
 * through the guard below for its tool, and drops each field in turn to pin
 * that the guard notices; `fixtures.test.ts` asserts the specific shapes the
 * views lean on. A server change that renames a field fails the frontend suite
 * in the same PR that made it.
 *
 * Fields are optional where the server genuinely omits them, not defensively.
 * `lease_expired` is the clearest case: it is present only on an
 * `in_progress` row, because on any other status a lease is not a thing that
 * exists and `false` there would read as "claim still live".
 */

export type TaskStatus = "open" | "in_progress" | "blocked" | "closed";
export type TaskPriority = "p0" | "p1" | "p2" | "p3";
export type TaskType = "bug" | "feature" | "task" | "chore" | "epic";
export type WorkflowPhase =
	| "discovery"
	| "spec"
	| "implementation"
	| "delivery";

/**
 * What every task-shaped result carries.
 *
 * Split out because the tools do NOT all return the same projection, and
 * pretending otherwise is a type that lies: `task_search` goes through a
 * narrower BM25 projection with no `created_at`, `closed_at`, `claimed_at` or
 * `blocked_by`, so typing its rows as `TaskRow` would have the Gantt read
 * `undefined` off a search result.
 */
export interface TaskCore {
	slug: string;
	title: string;
	repo: string | null;
	type: TaskType;
	status: TaskStatus;
	priority: TaskPriority;
	project_slug: string | null;
	parent_slug: string | null;
	assignee: string | null;
	external_uri: string | null;
	tags: string[] | null;
	updated_at: string;
	/** Present only on `in_progress` rows. See the module note. */
	lease_expired?: boolean;
}

/** A task as `task_list` and `task_ready` return it: the full list projection. */
export interface TaskRow extends TaskCore {
	blocked_by: string[] | null;
	created_at: string;
	closed_at: string | null;
	/** The CURRENT lease, which moves on every renewal. Not a work start. */
	claimed_at: string | null;
	/**
	 * When the task was first claimed, ever. Never cleared.
	 *
	 * Null on a task never claimed, and on any task last claimed before the
	 * field shipped — there is no backfill, so the timeline (spec §3.6) draws
	 * the work-time segment only where this exists rather than guessing it
	 * from `claimed_at`.
	 */
	first_claimed_at: string | null;
}

/** A `task_search` hit. Narrower, and it carries `description` instead. */
export interface TaskSearchRow extends TaskCore {
	description: string | null;
}

export interface TaskComment {
	slug: string;
	task_slug: string;
	body: string;
	author: string;
	created_at: string;
}

export interface TaskChild {
	slug: string;
	title: string;
	status: TaskStatus;
}

export interface CodeBranchRef {
	slug: string;
	repo: string;
	branch: string;
	status: string | null;
	updated_at: string | null;
}

/** `task_get`: the row plus everything the node fields alone cannot express. */
export interface TaskDetail extends TaskRow {
	description: string | null;
	resolution: string | null;
	symbol_refs: string[] | null;
	author: string | null;
	comments: TaskComment[];
	/** Slugs of the tasks THIS one holds back: the inverse of `blocked_by`. */
	blocks: string[];
	children: TaskChild[];
	branches: CodeBranchRef[];
}

/**
 * What both project projections carry.
 *
 * Split for the same reason as `TaskCore`: `workflow_project_list` returns a
 * narrower row than `workflow_project_get`, with `github_issue` rather than
 * `github_pr`, so one interface over both would declare a field half the
 * results do not have.
 */
export interface WorkflowProjectCore {
	slug: string;
	title: string;
	phase: WorkflowPhase;
	status: string;
	repos: string[] | null;
}

/** A row from `workflow_project_list`. */
export interface WorkflowProjectSummary extends WorkflowProjectCore {
	blocked_by: string[] | null;
	github_issue: string | null;
	tags: string[] | null;
	updated_at: string;
}

/**
 * The rollup's `project`, which is a six-field projection.
 *
 * NOT what `workflow_project_get` returns: that one carries nine more fields
 * (see `WorkflowProjectDetail`). Sharing one interface between them made the
 * detail view unable to reach `description`, `blocked_by` or `blocks`, and
 * widening it instead would have promised the rollup fields it does not send.
 */
export interface WorkflowProject extends WorkflowProjectCore {
	github_pr: string | null;
}

/** What `workflow_project_get` returns: the whole node. */
export interface WorkflowProjectDetail extends WorkflowProject {
	description: string | null;
	author: string | null;
	blocked_by: string[] | null;
	/** Slugs of the projects THIS one holds back. Always present. */
	blocks: string[];
	github_issue: string | null;
	tags: string[] | null;
	created_at: string;
	updated_at: string;
	completed_at: string | null;
}

export interface WorkflowSession {
	slug: string;
	project_slug: string | null;
	session_id?: string;
	phase: WorkflowPhase;
	summary: string | null;
	started_at: string;
	ended_at: string | null;
	superseded_by: string | null;
	tools_used?: string[] | null;
	files_changed?: string[] | null;
}

/** One row of `workflow_project_status.ready_tasks`: a narrower projection. */
export interface ReadyTaskRow {
	slug: string;
	title: string;
	priority: TaskPriority;
	status: TaskStatus;
	assignee: string | null;
	lease_expired?: boolean;
}

export interface ProjectStatus {
	project: WorkflowProject;
	ready_tasks: ReadyTaskRow[];
	/** True when `ready_tasks` was capped and `counts.ready` is larger. */
	ready_truncated: boolean;
	last_session: unknown;
	blockers: string[];
	counts: { ready: number; open_tasks: number };
}

// The server's own union (server.py: `MemoryKind`). Transcribed exactly,
// because a view that switches on this to pick a filter chip or an icon
// silently drops every kind the type does not name.
export type MemoryKind =
	| "pattern"
	| "project_fact"
	| "lesson"
	| "agent_context";

export interface Memory {
	slug: string;
	kind: MemoryKind;
	title: string;
	content?: string;
	repo: string | null;
	tags?: string[] | null;
	author?: string | null;
	confidence?: number | null;
	category?: string | null;
	severity?: string | null;
	language?: string | null;
	symbol_refs?: string[] | null;
	created_at: string;
	updated_at?: string;
}

/** A pair of memories that contradict each other, as `recall` reports them. */
export interface Contradiction {
	a: string;
	b: string;
}

export interface RecallResult {
	memories: Memory[];
	seeds: unknown;
	contradictions: Contradiction[];
}

/** One side of a `memory_contradictions` pair: a projection, not a `Memory`. */
export interface ContradictionEndpoint {
	slug: string;
	title: string;
	kind: MemoryKind;
	repo: string | null;
	author: string;
	updated_at: string;
}

/**
 * The link itself. Every field is `null` on an edge written before edge
 * properties existed, and nothing backfills them.
 */
export interface EdgeMeta {
	confidence: "asserted" | "inferred" | null;
	role: string | null;
	author: string | null;
	created_at: string | null;
}

/** One unordered pair; `a` is the side the surviving link was made from. */
export interface MemoryContradiction {
	a: ContradictionEndpoint;
	b: ContradictionEndpoint;
	edge: EdgeMeta;
}

export interface MemoryNeighbors {
	slug: string;
	neighbors: Record<string, unknown>;
}

export interface TopicResult {
	topic: Record<string, unknown>;
	memories: Memory[];
}

// ── Runtime guards ─────────────────────────────────────────────────
//
// EXHAUSTIVE OVER THE FIELDS EACH TYPE DECLARES, not only over the ones that
// discriminate one projection from another. The narrower version of this
// missed exactly what the fixtures exist to catch: `isWorkflowProjectDetail`
// checked three of the thirteen fields it declares, so renaming `github_pr`,
// `author`, `blocked_by`, `github_issue`, `tags`, `updated_at` or
// `completed_at` server-side left the hand-written type stale and the whole
// suite green.
//
// Each guard below is a field table instead: every required field must be
// present AND the right type, and every optional field that IS present must be
// the right type. An absent optional is fine, since that is what makes it
// optional; a `lease_expired: "yes"` is not.
//
// The closed unions (`TaskStatus`, `TaskPriority`, `TaskType`,
// `WorkflowPhase`, `MemoryKind`) are checked for membership rather than for
// `typeof === "string"`. They transcribe the server's own unions, and a view
// switching on one silently drops any value the type does not name, so a
// server that adds a status SHOULD fail here.

type Check = (value: unknown) => boolean;

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

const str: Check = (v) => typeof v === "string";
const num: Check = (v) => typeof v === "number";
const bool: Check = (v) => typeof v === "boolean";
/** A field declared `unknown`: the KEY has to be there, the value is free. */
const anything: Check = () => true;
const nullable =
	(check: Check): Check =>
	(v) =>
		v === null || check(v);
const arrayOf =
	(check: Check): Check =>
	(v) =>
		Array.isArray(v) && v.every(check);
const oneOf =
	(...allowed: readonly string[]): Check =>
	(v) =>
		typeof v === "string" && allowed.includes(v);

const strList = nullable(arrayOf(str));

const taskStatus = oneOf("open", "in_progress", "blocked", "closed");
const taskPriority = oneOf("p0", "p1", "p2", "p3");
const taskType = oneOf("bug", "feature", "task", "chore", "epic");
const workflowPhase = oneOf("discovery", "spec", "implementation", "delivery");
const memoryKind = oneOf("pattern", "project_fact", "lesson", "agent_context");

/**
 * Every required field present and well-typed; every present optional one
 * well-typed.
 *
 * `undefined` counts as absent even when the key exists: `JSON.parse` never
 * produces it, but a hand-built object in a test does.
 */
function matches(
	value: unknown,
	required: Record<string, Check>,
	optional: Record<string, Check> = {},
): boolean {
	if (!isRecord(value)) {
		return false;
	}
	for (const [key, check] of Object.entries(required)) {
		if (!(key in value) || !check(value[key])) {
			return false;
		}
	}
	for (const [key, check] of Object.entries(optional)) {
		if (key in value && value[key] !== undefined && !check(value[key])) {
			return false;
		}
	}
	return true;
}

// The tables mirror the interfaces above, and `extends` becomes a spread, so a
// field added to a base type reaches every guard built on it.

const TASK_CORE: Record<string, Check> = {
	slug: str,
	title: str,
	repo: nullable(str),
	type: taskType,
	status: taskStatus,
	priority: taskPriority,
	project_slug: nullable(str),
	parent_slug: nullable(str),
	assignee: nullable(str),
	external_uri: nullable(str),
	tags: strList,
	updated_at: str,
};
const TASK_CORE_OPTIONAL: Record<string, Check> = { lease_expired: bool };

const TASK_ROW: Record<string, Check> = {
	...TASK_CORE,
	blocked_by: strList,
	created_at: str,
	closed_at: nullable(str),
	claimed_at: nullable(str),
	first_claimed_at: nullable(str),
};

const WORKFLOW_PROJECT_CORE: Record<string, Check> = {
	slug: str,
	title: str,
	phase: workflowPhase,
	status: str,
	repos: strList,
};

const WORKFLOW_PROJECT: Record<string, Check> = {
	...WORKFLOW_PROJECT_CORE,
	github_pr: nullable(str),
};

export function isTaskCore(value: unknown): value is TaskCore {
	return matches(value, TASK_CORE, TASK_CORE_OPTIONAL);
}

export function isTaskComment(value: unknown): value is TaskComment {
	return matches(value, {
		slug: str,
		task_slug: str,
		body: str,
		author: str,
		created_at: str,
	});
}

export function isTaskChild(value: unknown): value is TaskChild {
	return matches(value, { slug: str, title: str, status: taskStatus });
}

export function isCodeBranchRef(value: unknown): value is CodeBranchRef {
	return matches(value, {
		slug: str,
		repo: str,
		branch: str,
		status: nullable(str),
		updated_at: nullable(str),
	});
}

export function isTaskRow(value: unknown): value is TaskRow {
	// The read-gap fields (`created_at`, `closed_at`, `claimed_at`,
	// `blocked_by`) are what the board and the Gantt read, and only the LIST
	// projection carries them. A `task_search` hit correctly fails this and is
	// a `TaskSearchRow`.
	return matches(value, TASK_ROW, TASK_CORE_OPTIONAL);
}

export function isTaskDetail(value: unknown): value is TaskDetail {
	return matches(
		value,
		{
			...TASK_ROW,
			description: nullable(str),
			resolution: nullable(str),
			symbol_refs: strList,
			author: nullable(str),
			comments: arrayOf(isTaskComment),
			blocks: arrayOf(str),
			children: arrayOf(isTaskChild),
			branches: arrayOf(isCodeBranchRef),
		},
		TASK_CORE_OPTIONAL,
	);
}

export function isTaskSearchRow(value: unknown): value is TaskSearchRow {
	return matches(
		value,
		{ ...TASK_CORE, description: nullable(str) },
		TASK_CORE_OPTIONAL,
	);
}

export function isReadyTaskRow(value: unknown): value is ReadyTaskRow {
	return matches(
		value,
		{
			slug: str,
			title: str,
			priority: taskPriority,
			status: taskStatus,
			assignee: nullable(str),
		},
		{ lease_expired: bool },
	);
}

export function isWorkflowProjectCore(
	value: unknown,
): value is WorkflowProjectCore {
	return matches(value, WORKFLOW_PROJECT_CORE);
}

/** The rollup's `project`: six fields, NOT what `workflow_project_get` sends. */
export function isWorkflowProject(value: unknown): value is WorkflowProject {
	return matches(value, WORKFLOW_PROJECT);
}

export function isWorkflowProjectSummary(
	value: unknown,
): value is WorkflowProjectSummary {
	return matches(value, {
		...WORKFLOW_PROJECT_CORE,
		blocked_by: strList,
		github_issue: nullable(str),
		tags: strList,
		updated_at: str,
	});
}

export function isWorkflowProjectDetail(
	value: unknown,
): value is WorkflowProjectDetail {
	return matches(value, {
		...WORKFLOW_PROJECT,
		description: nullable(str),
		author: nullable(str),
		blocked_by: strList,
		blocks: arrayOf(str),
		github_issue: nullable(str),
		tags: strList,
		created_at: str,
		updated_at: str,
		completed_at: nullable(str),
	});
}

export function isWorkflowSession(value: unknown): value is WorkflowSession {
	return matches(
		value,
		{
			slug: str,
			project_slug: nullable(str),
			phase: workflowPhase,
			summary: nullable(str),
			started_at: str,
			ended_at: nullable(str),
			superseded_by: nullable(str),
		},
		{
			session_id: str,
			tools_used: strList,
			files_changed: strList,
		},
	);
}

export function isProjectStatus(value: unknown): value is ProjectStatus {
	return matches(value, {
		project: isWorkflowProject,
		ready_tasks: arrayOf(isReadyTaskRow),
		ready_truncated: bool,
		last_session: anything,
		blockers: arrayOf(str),
		counts: (v) => matches(v, { ready: num, open_tasks: num }),
	});
}

export function isMemory(value: unknown): value is Memory {
	return matches(
		value,
		{
			slug: str,
			kind: memoryKind,
			title: str,
			repo: nullable(str),
			created_at: str,
		},
		{
			content: str,
			tags: strList,
			author: nullable(str),
			confidence: nullable(num),
			category: nullable(str),
			severity: nullable(str),
			language: nullable(str),
			symbol_refs: strList,
			updated_at: str,
		},
	);
}

export function isContradiction(value: unknown): value is Contradiction {
	return matches(value, { a: str, b: str });
}

export function isRecallResult(value: unknown): value is RecallResult {
	return matches(value, {
		memories: arrayOf(isMemory),
		seeds: anything,
		contradictions: arrayOf(isContradiction),
	});
}

function isContradictionEndpoint(
	value: unknown,
): value is ContradictionEndpoint {
	return matches(value, {
		slug: str,
		title: str,
		kind: memoryKind,
		repo: nullable(str),
		author: str,
		updated_at: str,
	});
}

export function isEdgeMeta(value: unknown): value is EdgeMeta {
	return matches(value, {
		confidence: nullable(oneOf("asserted", "inferred")),
		role: nullable(str),
		author: nullable(str),
		created_at: nullable(str),
	});
}

export function isMemoryContradiction(
	value: unknown,
): value is MemoryContradiction {
	return matches(value, {
		a: isContradictionEndpoint,
		b: isContradictionEndpoint,
		edge: isEdgeMeta,
	});
}

export function isMemoryNeighbors(value: unknown): value is MemoryNeighbors {
	return matches(value, { slug: str, neighbors: isRecord });
}

export function isTopicResult(value: unknown): value is TopicResult {
	// Descends into `memories`, because `topic` is an untyped record and the
	// memory list is the only declared structure here.
	return matches(value, { topic: isRecord, memories: arrayOf(isMemory) });
}
