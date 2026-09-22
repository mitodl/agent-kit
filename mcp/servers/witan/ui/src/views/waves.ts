import { html, nothing, svg, type TemplateResult } from "lit-html";
import { emptyBox } from "../chrome.js";
import { type Route, routeHref } from "../route.js";
import type {
	TaskPriority,
	TaskRow,
	WorkflowProjectSummary,
} from "../types.js";

/**
 * Dependency waves (spec §6.5): how many serialized rounds remain in a project.
 *
 * The x axis is depth in the `Blocks` DAG, not time. Tasks carry no estimate
 * or due date, so a calendar plan would be a guess; the DAG is real data. A
 * task with no open blocker is wave 0, and every other task sits one wave past
 * its deepest open blocker.
 *
 * Presentation over one read, so it lives here rather than in a tool. The one
 * rule it does NOT own is readiness: wave 0 is "no open blocker", and
 * `task_ready` is that plus "nobody holds it". The chart reads `task_ready`
 * alongside and says so when the two disagree, rather than trusting its own
 * copy of the rule.
 *
 * Blocker status follows `task_ready`'s resolver exactly: a blocker that no
 * longer exists holds nothing back, and a row with no status is open.
 *
 * ★ SVG GEOMETRY IN PERCENTAGES, for the reason the timeline's is: `/ui/`'s
 * CSP blocks inline `style=`. `<line>` takes percentage coordinates, which is
 * why every edge is a straight line rather than a `<path>`, whose `d` does not.
 */

/** What the waves view reads, as the app hands it over. */
export interface Waves {
	/** Every task in the project, closed ones included, from `task_list`. */
	tasks: TaskRow[];
	/** `task_ready` for the project. Checked against wave 0, never used to place. */
	ready: TaskRow[];
	/**
	 * Open blockers from OUTSIDE the project, one `task_get` each.
	 *
	 * Only the open ones: a closed or deleted outside blocker holds nothing
	 * back, so it has nothing to draw.
	 */
	outside: TaskRow[];
}

export interface WaveNode {
	task: TaskRow;
	/** A blocker from another project, drawn as a stub. */
	outside: boolean;
	wave: number;
	/** In a `Blocks` cycle, so it can never become ready as the graph stands. */
	cycle: boolean;
	critical: boolean;
	/** Open project tasks that wait on this one, directly or transitively. */
	downstream: number;
	/** `task_ready` returned it. */
	ready: boolean;
}

export interface WaveEdge {
	from: string;
	to: string;
	critical: boolean;
	cycle: boolean;
}

export interface WaveLayout {
	/** In row order: by wave, then the critical path, then priority. */
	nodes: WaveNode[];
	edges: WaveEdge[];
	/** Waves the project's own tasks span. `0` when nothing is open. */
	waves: number;
	/** Slugs along the longest chain, first to last. */
	critical: string[];
	/** Each cycle's members, as slugs. */
	cycles: string[][];
	/** The wave-0 project task with the most work downstream, if any has some. */
	unblocksMost: WaveNode | null;
	/** Tasks `task_ready` and the chart place differently. */
	disagree: TaskRow[];
}

const PRIORITY_RANK: Record<TaskPriority, number> = {
	p0: 0,
	p1: 1,
	p2: 2,
	p3: 3,
};

/** Every task `task` waits on that is still open, by `task_ready`'s resolver. */
function openBlockerSlugs(
	task: TaskRow,
	status: Map<string, TaskRow["status"] | null>,
): string[] {
	return (task.blocked_by ?? []).filter((slug) => {
		if (!status.has(slug)) {
			// Gone, or closed outside the project: `readWaves` fetched every
			// outside blocker and passed on only the open ones.
			return false;
		}
		return (status.get(slug) ?? "open") !== "closed";
	});
}

/**
 * Strongly connected components, dependencies before dependents.
 *
 * Tarjan's algorithm emits a component only once everything reachable from it
 * has been emitted, so with edges pointing blocker → dependent it emits
 * dependents first; the result is reversed. A component of more than one
 * task, or one that blocks itself, is a cycle, and collapsing it into one
 * node is what lets the depth pass below finish instead of looping.
 *
 * Iterative, with an explicit stack of frames: a recursive visit goes one call
 * deeper per task along a chain, and a long enough chain would throw
 * `RangeError` before anything was drawn.
 */
export function components(
	slugs: string[],
	next: Map<string, string[]>,
): string[][] {
	let counter = 0;
	const index = new Map<string, number>();
	const low = new Map<string, number>();
	const stack: string[] = [];
	const onStack = new Set<string>();
	const found: string[][] = [];

	const enter = (slug: string): void => {
		index.set(slug, counter);
		low.set(slug, counter);
		counter += 1;
		stack.push(slug);
		onStack.add(slug);
	};
	const lower = (slug: string, to: number): void => {
		low.set(slug, Math.min(low.get(slug) ?? 0, to));
	};

	for (const root of slugs) {
		if (index.has(root)) {
			continue;
		}
		// Each frame is a task and how many of its dependents it has walked.
		const frames: { slug: string; walked: number }[] = [
			{ slug: root, walked: 0 },
		];
		enter(root);
		while (frames.length > 0) {
			const frame = frames[frames.length - 1] as {
				slug: string;
				walked: number;
			};
			const targets = next.get(frame.slug) ?? [];
			if (frame.walked < targets.length) {
				const to = targets[frame.walked] as string;
				frame.walked += 1;
				if (!index.has(to)) {
					enter(to);
					frames.push({ slug: to, walked: 0 });
				} else if (onStack.has(to)) {
					lower(frame.slug, index.get(to) ?? 0);
				}
				continue;
			}
			// Every dependent walked: the frame returns, as the recursive call
			// would, and hands its `low` to its caller.
			frames.pop();
			const caller = frames[frames.length - 1];
			if (caller) {
				lower(caller.slug, low.get(frame.slug) ?? 0);
			}
			if (low.get(frame.slug) === index.get(frame.slug)) {
				const component: string[] = [];
				let popped: string | undefined;
				do {
					popped = stack.pop();
					if (popped === undefined) {
						break;
					}
					onStack.delete(popped);
					component.push(popped);
				} while (popped !== frame.slug);
				found.push(component.sort());
			}
		}
	}
	return found.reverse();
}

function byPriority(a: TaskRow, b: TaskRow): number {
	return (
		PRIORITY_RANK[a.priority] - PRIORITY_RANK[b.priority] ||
		a.slug.localeCompare(b.slug)
	);
}

/** Everything the chart draws, as data. */
export function layout(data: Waves): WaveLayout {
	const status = new Map<string, TaskRow["status"] | null>();
	for (const task of [...data.outside, ...data.tasks]) {
		status.set(task.slug, task.status);
	}
	const open = data.tasks.filter((task) => task.status !== "closed");
	const rows = new Map<string, { task: TaskRow; outside: boolean }>();
	for (const task of open) {
		rows.set(task.slug, { task, outside: false });
	}

	// Edges run blocker → dependent. An outside blocker only enters the chart
	// as the tail of one, so a stub is drawn exactly when something waits on it.
	const next = new Map<string, string[]>();
	const prev = new Map<string, string[]>();
	for (const task of open) {
		for (const blocker of openBlockerSlugs(task, status)) {
			if (!rows.has(blocker)) {
				const stub = data.outside.find((row) => row.slug === blocker);
				if (!stub) {
					continue;
				}
				rows.set(blocker, { task: stub, outside: true });
			}
			next.set(blocker, [...(next.get(blocker) ?? []), task.slug]);
			prev.set(task.slug, [...(prev.get(task.slug) ?? []), blocker]);
		}
	}

	const slugs = [...rows.keys()].sort();
	const order = components(slugs, next);
	const componentOf = new Map<string, number>();
	order.forEach((members, at) => {
		for (const slug of members) {
			componentOf.set(slug, at);
		}
	});
	const cycles = order.filter(
		(members) =>
			members.length > 1 ||
			(next.get(members[0] ?? "") ?? []).includes(members[0] ?? ""),
	);
	const inCycle = new Set(cycles.flat());

	// Longest path from a source, over the components in dependency order, so
	// every predecessor's wave is final before it is read.
	const wave = new Map<string, number>();
	for (const members of order) {
		let depth = 0;
		for (const slug of members) {
			for (const from of prev.get(slug) ?? []) {
				if (componentOf.get(from) !== componentOf.get(slug)) {
					depth = Math.max(depth, (wave.get(from) ?? 0) + 1);
				}
			}
		}
		for (const slug of members) {
			wave.set(slug, depth);
		}
	}

	const downstream = new Map<string, number>();
	for (const slug of slugs) {
		const seen = new Set<string>();
		const pending = [...(next.get(slug) ?? [])];
		while (pending.length > 0) {
			const at = pending.pop() as string;
			if (seen.has(at)) {
				continue;
			}
			seen.add(at);
			pending.push(...(next.get(at) ?? []));
		}
		seen.delete(slug);
		downstream.set(slug, seen.size);
	}

	const critical = criticalPath(slugs, rows, prev, next, wave, componentOf);
	const onPath = new Set(critical);
	const readySlugs = new Set(data.ready.map((task) => task.slug));

	const nodes: WaveNode[] = slugs.map((slug) => {
		const { task, outside } = rows.get(slug) as {
			task: TaskRow;
			outside: boolean;
		};
		return {
			task,
			outside,
			wave: wave.get(slug) ?? 0,
			cycle: inCycle.has(slug),
			critical: onPath.has(slug),
			downstream: downstream.get(slug) ?? 0,
			ready: readySlugs.has(slug),
		};
	});
	nodes.sort(
		(a, b) =>
			a.wave - b.wave ||
			Number(b.outside) - Number(a.outside) ||
			Number(b.critical) - Number(a.critical) ||
			byPriority(a.task, b.task),
	);

	const edges: WaveEdge[] = [];
	for (const [from, targets] of next) {
		for (const to of targets) {
			edges.push({
				from,
				to,
				critical:
					onPath.has(from) && onPath.has(to) && isStep(critical, from, to),
				cycle: componentOf.get(from) === componentOf.get(to),
			});
		}
	}

	const project = nodes.filter((node) => !node.outside);
	const firsts = project.filter((node) => node.wave === 0 && !node.cycle);
	const unblocksMost = firsts.reduce<WaveNode | null>(
		(best, node) =>
			node.downstream > 0 && node.downstream > (best?.downstream ?? 0)
				? node
				: best,
		null,
	);

	return {
		nodes,
		edges,
		waves:
			project.length === 0 ? 0 : Math.max(...project.map((n) => n.wave)) + 1,
		critical,
		cycles,
		unblocksMost,
		disagree: disagreements(project, data.ready),
	};
}

function isStep(path: string[], from: string, to: string): boolean {
	const at = path.indexOf(from);
	return at >= 0 && path[at + 1] === to;
}

/**
 * The longest chain of project work, first task to last.
 *
 * Traced back from the deepest project task, one blocker a wave shallower at
 * a time. That blocker always exists: a task sits at wave `n` only because
 * some blocker in another component sits at `n - 1`. Ties go to the higher
 * priority, then the slug, so the path does not flicker between polls.
 *
 * A cycle's members share a wave. The step out of one can leave from a member
 * other than the one the path came in by, so the shortest run of real edges
 * between the two, inside the cycle, is spliced in: naming only the ends would
 * record an edge that does not exist. Reaching an outside stub ends the path
 * there: its own blockers are not read.
 */
function criticalPath(
	slugs: string[],
	rows: Map<string, { task: TaskRow; outside: boolean }>,
	prev: Map<string, string[]>,
	next: Map<string, string[]>,
	wave: Map<string, number>,
	componentOf: Map<string, number>,
): string[] {
	const rank = (a: string, b: string): number =>
		byPriority(
			(rows.get(a) as { task: TaskRow }).task,
			(rows.get(b) as { task: TaskRow }).task,
		);
	const deepest = slugs
		.filter((slug) => !rows.get(slug)?.outside)
		.sort((a, b) => (wave.get(b) ?? 0) - (wave.get(a) ?? 0) || rank(a, b))[0];
	if (deepest === undefined || (wave.get(deepest) ?? 0) === 0) {
		return [];
	}
	const path = [deepest];
	let at = deepest;
	while ((wave.get(at) ?? 0) > 0) {
		const depth = wave.get(at) ?? 0;
		const members = [...componentOf].flatMap(([slug, component]) =>
			component === componentOf.get(at) ? [slug] : [],
		);
		// Each candidate keeps the member it feeds, preferring `at` itself, so
		// a path leaves a cycle through another member only when it must.
		const step = members
			.flatMap((member) =>
				(prev.get(member) ?? []).map((from) => ({ from, member })),
			)
			.filter(
				({ from }) =>
					componentOf.get(from) !== componentOf.get(at) &&
					wave.get(from) === depth - 1,
			)
			.sort(
				(a, b) =>
					Number(a.member !== at) - Number(b.member !== at) ||
					rank(a.from, b.from),
			)[0];
		if (step === undefined) {
			break;
		}
		if (step.member !== at) {
			path.unshift(...within(step.member, at, next, componentOf).slice(0, -1));
		}
		path.unshift(step.from);
		at = step.from;
	}
	return path;
}

/**
 * The shortest run of edges from `from` to `to` that stays inside their
 * component, both ends included.
 *
 * Breadth-first, so a cycle is walked no further than it has to be. A path
 * always exists: two members of one strongly connected component reach each
 * other by definition.
 */
function within(
	from: string,
	to: string,
	next: Map<string, string[]>,
	componentOf: Map<string, number>,
): string[] {
	const component = componentOf.get(from);
	const cameFrom = new Map<string, string | null>([[from, null]]);
	const queue = [from];
	for (let head = 0; head < queue.length && !cameFrom.has(to); head += 1) {
		const at = queue[head] as string;
		for (const target of next.get(at) ?? []) {
			if (componentOf.get(target) === component && !cameFrom.has(target)) {
				cameFrom.set(target, at);
				queue.push(target);
			}
		}
	}
	const path: string[] = [];
	for (let at: string | null = to; at !== null; at = cameFrom.get(at) ?? null) {
		path.unshift(at);
	}
	return path;
}

/**
 * Where `task_ready` and wave 0 disagree.
 *
 * A wave-0 task should be in `task_ready` unless someone holds a live claim on
 * it, and nothing past wave 0 should be. Either direction means the reads saw
 * the graph at different moments (they run in parallel), which the next poll
 * settles.
 */
function disagreements(project: WaveNode[], ready: TaskRow[]): TaskRow[] {
	const placed = new Map(project.map((node) => [node.task.slug, node]));
	const found: TaskRow[] = [];
	for (const node of project) {
		const held = node.task.status === "in_progress" && !node.task.lease_expired;
		const expected = node.wave === 0 && !node.cycle && !held;
		if (expected !== node.ready) {
			found.push(node.task);
		}
	}
	for (const task of ready) {
		if (!placed.has(task.slug)) {
			found.push(task);
		}
	}
	return found;
}

// ── Drawing ─────────────────────────────────────────────────────────

/** A row's height in px. The label list's CSS row height has to match it. */
const ROW = 22;

/** Room above the first row for the wave headings. */
const HEAD = 20;

/** Space either side of a bar inside its wave column, in percent of the column. */
const INSET = 0.12;

function pct(value: number): string {
	return `${value.toFixed(3)}%`;
}

interface Column {
	/** Where the column begins, and its dividing line is drawn. */
	start: number;
	/** Midway between `start` and the bars, where same-wave edges run. */
	gutter: number;
	/** Where the column's bars start and end. */
	left: number;
	right: number;
}

/**
 * Each wave's column, as percentages of the chart.
 *
 * Memoized, so two nodes in one wave get the same object: that identity is
 * how the edge drawing tells a same-wave edge apart.
 */
function columns(waves: number): (wave: number) => Column {
	const width = 100 / Math.max(waves, 1);
	const found = new Map<number, Column>();
	return (wave) => {
		let column = found.get(wave);
		if (!column) {
			const start = wave * width;
			column = {
				start,
				gutter: start + (width * INSET) / 2,
				left: start + width * INSET,
				right: start + width - width * INSET,
			};
			found.set(wave, column);
		}
		return column;
	};
}

function middle(row: number): number {
	return HEAD + row * ROW + ROW / 2;
}

/**
 * The page when no project is chosen: waves are per project, so it asks for
 * one, listing those in the repo scope.
 */
export function wavesPicker(
	projects: WorkflowProjectSummary[],
	route: Route,
): TemplateResult {
	if (projects.length === 0) {
		return emptyBox("No projects in this scope to draw waves for.");
	}
	return html`
    <section class="waves" aria-label="Waves">
      <p class="note">
        Waves are drawn for one project at a time. Pick one:
      </p>
      <ul class="wv-pick">
        ${projects.map(
					(project) => html`<li>
            <a href=${routeHref(route, { project: project.slug })}
              >${project.title}</a
            >
          </li>`,
				)}
      </ul>
    </section>
  `;
}

export function waves(data: Waves, route: Route): TemplateResult {
	const plan = layout(data);
	const project = plan.nodes.filter((node) => !node.outside);
	if (project.length === 0) {
		// The notes still render: a task this read saw closed while the
		// parallel `task_ready` read still returned it is the race they name.
		return html`${emptyBox("Nothing is open in this project.")}
      ${notes(plan, route)}`;
	}
	return html`
    <section class="waves" aria-label="Waves">
      <p class="note">
        Columns are depth in the dependency graph, not time: wave 0 has no open
        blocker, and each later wave waits on the one before it.
      </p>
      ${summary(plan, route)} ${notes(plan, route)} ${legend()}
      ${chart(plan, route)}
    </section>
  `;
}

function taskLink(task: TaskRow, route: Route): TemplateResult {
	return html`<a href=${routeHref(route, { slug: task.slug })}>${task.title}</a>`;
}

function summary(plan: WaveLayout, route: Route): TemplateResult {
	const first = plan.unblocksMost;
	const chain = plan.critical.length;
	return html`
    <ul class="wv-summary">
      <li>
        <strong>${plan.waves}</strong>
        ${plan.waves === 1 ? "wave" : "waves"} of work remain.
      </li>
      ${
				chain > 1
					? html`<li>
              The longest chain is <strong>${chain}</strong> tasks, drawn
              heavier below.
            </li>`
					: plan.edges.length === 0
						? html`<li>No task waits on another, so everything open can run at once.</li>`
						: nothing
			}
      ${
				first
					? html`<li>
              Landing ${taskLink(first.task, route)} first unblocks
              <strong>${first.downstream}</strong>
              ${first.downstream === 1 ? "task" : "tasks"} downstream, more
              than any other wave-0 task.
            </li>`
					: nothing
			}
    </ul>
  `;
}

function notes(plan: WaveLayout, route: Route): TemplateResult {
	const titled = new Map(plan.nodes.map((node) => [node.task.slug, node.task]));
	return html`
    ${plan.cycles.map(
			(members) => html`<p class="note bad" role="alert">
        These tasks block each other in a cycle, so none of them can become
        ready until one of the links is removed:
        ${members.map((slug, index) => {
					const task = titled.get(slug);
					return html`${index > 0 ? " → " : ""}${
						task ? taskLink(task, route) : html`<code>${slug}</code>`
					}`;
				})}
      </p>`,
		)}
    ${
			plan.disagree.length > 0
				? html`<p class="note">
            task_ready and this chart disagree on
            ${plan.disagree.map(
							(task, index) =>
								html`${index > 0 ? ", " : ""}${taskLink(task, route)}`,
						)}.
            The reads run in parallel, so a claim or close landing between
            them does this; refresh.
          </p>`
				: nothing
		}
  `;
}

function legend(): TemplateResult {
	const swatch = (body: TemplateResult) =>
		html`<svg class="swatch" viewBox="0 0 24 12" aria-hidden="true">${body}</svg>`;
	return html`
    <ul class="tl-legend">
      <li>${swatch(svg`<rect class="wv-bar ready" x="0" y="2" width="24" height="8"></rect>`)} ready</li>
      <li>${swatch(svg`<rect class="wv-bar held" x="0" y="2" width="24" height="8"></rect>`)} claimed</li>
      <li>${swatch(svg`<rect class="wv-bar" x="0" y="2" width="24" height="8"></rect>`)} waiting</li>
      <li>${swatch(svg`<rect class="wv-bar critical" x="0" y="2" width="24" height="8"></rect>`)} longest chain</li>
      <li>${swatch(svg`<rect class="wv-bar outside" x="0" y="2" width="24" height="8"></rect>`)}
        blocker in another project</li>
      <li>${swatch(svg`<rect class="wv-bar cycle" x="0" y="2" width="24" height="8"></rect>`)} in a cycle</li>
    </ul>
  `;
}

function barClass(node: WaveNode): string {
	const classes = ["wv-bar"];
	if (node.outside) {
		classes.push("outside");
	} else if (node.task.status === "in_progress" && !node.task.lease_expired) {
		classes.push("held");
	} else if (node.ready) {
		classes.push("ready");
	}
	if (node.critical) {
		classes.push("critical");
	}
	if (node.cycle) {
		classes.push("cycle");
	}
	return classes.join(" ");
}

function barTitle(node: WaveNode): string {
	const { task } = node;
	return [
		task.title,
		node.outside
			? `In ${task.project_slug ?? "no project"}, ${task.status}`
			: `Wave ${node.wave}, ${task.status}${
					task.assignee ? `, held by ${task.assignee}` : ""
				}${task.lease_expired ? " (claim lapsed)" : ""}`,
		node.downstream > 0
			? `${node.downstream} ${node.downstream === 1 ? "task waits" : "tasks wait"} on it`
			: "",
		node.cycle ? "In a Blocks cycle" : "",
	]
		.filter(Boolean)
		.join("\n");
}

/**
 * The chart: one label list and one SVG, row for row.
 *
 * One SVG for every row, unlike the timeline's SVG per row, because the edges
 * cross rows. The labels stay HTML, so they are ordinary links that wrap and
 * ellipsize, and the CSS row height (`.wv-labels li`) is held to `ROW`.
 */
function chart(plan: WaveLayout, route: Route): TemplateResult {
	const column = columns(plan.waves);
	const rowOf = new Map(plan.nodes.map((node, row) => [node.task.slug, row]));
	const waveOf = new Map(plan.nodes.map((node) => [node.task.slug, node.wave]));
	const height = HEAD + plan.nodes.length * ROW;
	const headings = Array.from({ length: plan.waves }, (_, wave) => wave);
	return html`
    <div class="wv-chart">
      <ol class="wv-labels">
        ${plan.nodes.map(
					(node) => html`<li
            class=${node.outside ? "outside" : ""}
            data-slug=${node.task.slug}
          >
            ${taskLink(node.task, route)}
            ${
							node.downstream > 0
								? html`<span
                    class="count"
                    title="${node.downstream} waiting on it"
                    >+${node.downstream}</span
                  >`
								: nothing
						}
          </li>`,
				)}
      </ol>
      <svg class="wv-track" height=${height} role="img" aria-label="Dependency waves">
        <defs>
          <marker
            id="wv-arrow"
            viewBox="0 0 6 6"
            refX="6"
            refY="3"
            markerWidth="6"
            markerHeight="6"
            orient="auto"
          >
            <path class="wv-head" d="M0,0 L6,3 L0,6 z"></path>
          </marker>
        </defs>
        ${headings.map((wave) => {
					const { start, left, right } = column(wave);
					return svg`
            ${
							wave > 0
								? svg`<line class="grid" x1=${pct(start)} y1="0" x2=${pct(start)} y2=${height}></line>`
								: nothing
						}
            <text x=${pct((left + right) / 2)} y="13" text-anchor="middle">wave ${wave}</text>
          `;
				})}
        ${plan.edges.map((edge) => {
					const from = rowOf.get(edge.from) ?? 0;
					const to = rowOf.get(edge.to) ?? 0;
					const cls = [
						"wv-edge",
						edge.critical ? "critical" : "",
						edge.cycle ? "cycle" : "",
					]
						.filter(Boolean)
						.join(" ");
					const fromColumn = column(waveOf.get(edge.from) ?? 0);
					const toColumn = column(waveOf.get(edge.to) ?? 0);
					// Within one wave (only a cycle: every other edge crosses a
					// wave) a line from a bar's end to another's start would run
					// back across both bars, so it runs down the column's gutter.
					const [x1, x2] =
						fromColumn === toColumn
							? [fromColumn.gutter, fromColumn.gutter]
							: [fromColumn.right, toColumn.left];
					return svg`<line class=${cls}
            x1=${pct(x1)} y1=${middle(from)} x2=${pct(x2)} y2=${middle(to)}
            marker-end="url(#wv-arrow)"></line>`;
				})}
        ${plan.nodes.map((node, row) => {
					const { left, right } = column(node.wave);
					return svg`<rect class=${barClass(node)} x=${pct(left)} y=${HEAD + row * ROW + 5}
            width=${pct(right - left)} height=${ROW - 10}
            ><title>${barTitle(node)}</title></rect>`;
				})}
      </svg>
    </div>
  `;
}
