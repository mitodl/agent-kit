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
 * `all-fixtures.test.ts` walks every one of them through the unwrapper, and
 * `fixtures.test.ts` asserts the specific shapes below against the ones the
 * views lean on. A server change that renames a field fails the frontend
 * suite in the same PR that made it.
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
	claimed_at: string | null;
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
// Narrow, and deliberately so. These exist to catch a server whose shape has
// moved out from under the types above, which shows up as a missing or
// wrong-typed key on the FIRST row. Validating every field of every row would
// be a schema validator, and the fixtures are what play that part.

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function isTaskCore(value: unknown): value is TaskCore {
	return (
		isRecord(value) &&
		typeof value.slug === "string" &&
		typeof value.title === "string" &&
		typeof value.status === "string"
	);
}

export function isTaskRow(value: unknown): value is TaskRow {
	return (
		isTaskCore(value) &&
		// The read-gap fields, which only the LIST projection carries. They are
		// what the board and the Gantt read, and a server without them would
		// otherwise surface as an empty chart. A `task_search` hit correctly
		// fails this and is a `TaskSearchRow`.
		"created_at" in value &&
		"closed_at" in value
	);
}

export function isTaskDetail(value: unknown): value is TaskDetail {
	return (
		isTaskRow(value) &&
		Array.isArray((value as TaskDetail).comments) &&
		Array.isArray((value as TaskDetail).blocks) &&
		Array.isArray((value as TaskDetail).children) &&
		Array.isArray((value as TaskDetail).branches)
	);
}

export function isReadyTaskRow(value: unknown): value is ReadyTaskRow {
	return (
		isRecord(value) &&
		typeof value.slug === "string" &&
		typeof value.title === "string" &&
		typeof value.priority === "string" &&
		typeof value.status === "string" &&
		"assignee" in value
	);
}

export function isProjectStatus(value: unknown): value is ProjectStatus {
	return (
		isRecord(value) &&
		// The rollup's own projection, and its rows. Checking only
		// `isRecord(project)` left the six-field shape and `ready_tasks`
		// unguarded, and `ready_tasks` is what the §7.1 widget reads
		// `lease_expired` off.
		isRecord(value.project) &&
		isWorkflowProjectCore(value.project) &&
		"github_pr" in value.project &&
		Array.isArray(value.ready_tasks) &&
		value.ready_tasks.every(isReadyTaskRow) &&
		typeof value.ready_truncated === "boolean" &&
		isRecord(value.counts) &&
		typeof (value.counts as { ready?: unknown }).ready === "number"
	);
}

export function isMemory(value: unknown): value is Memory {
	return (
		isRecord(value) &&
		typeof value.slug === "string" &&
		typeof value.kind === "string" &&
		typeof value.title === "string"
	);
}

export function isTaskSearchRow(value: unknown): value is TaskSearchRow {
	return isTaskCore(value) && "description" in value;
}

export function isWorkflowProjectCore(
	value: unknown,
): value is WorkflowProjectCore {
	return (
		isRecord(value) &&
		typeof value.slug === "string" &&
		typeof value.title === "string" &&
		typeof value.phase === "string" &&
		typeof value.status === "string"
	);
}

export function isWorkflowProjectDetail(
	value: unknown,
): value is WorkflowProjectDetail {
	return (
		isWorkflowProjectCore(value) &&
		"description" in value &&
		"blocks" in value &&
		"created_at" in value
	);
}

export function isWorkflowProjectSummary(
	value: unknown,
): value is WorkflowProjectSummary {
	return isWorkflowProjectCore(value) && "github_issue" in value;
}

export function isWorkflowSession(value: unknown): value is WorkflowSession {
	return (
		isRecord(value) &&
		typeof value.slug === "string" &&
		typeof value.phase === "string" &&
		"started_at" in value &&
		"ended_at" in value
	);
}

export function isMemoryNeighbors(value: unknown): value is MemoryNeighbors {
	return (
		isRecord(value) &&
		typeof value.slug === "string" &&
		isRecord(value.neighbors)
	);
}

export function isTopicResult(value: unknown): value is TopicResult {
	return (
		isRecord(value) &&
		isRecord(value.topic) &&
		Array.isArray(value.memories) &&
		// Descends, because `topic` is an untyped record and `memories` is the
		// only declared structure here: without this the guard could not go
		// stale, because it was asserting nothing.
		value.memories.every(isMemory)
	);
}

export function isRecallResult(value: unknown): value is RecallResult {
	return (
		isRecord(value) &&
		Array.isArray(value.memories) &&
		Array.isArray(value.contradictions)
	);
}
