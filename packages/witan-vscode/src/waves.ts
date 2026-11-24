// A project's tasks by dependency wave, as `witan.listProjectTasks` answers.
// The server places every task (witan-council's `witan/waves.py`); this only
// groups its rows for the tree.

import type { TaskRow } from "./tree";

export interface WaveTask extends TaskRow {
	wave: number;
	/** An open blocker from another project, not the project's own work. */
	outside: boolean;
	cycle: boolean;
	critical: boolean;
	downstream: number;
	/** `task_ready` returned it. */
	ready: boolean;
	open_blockers: string[];
}

export interface ProjectWaves {
	repo: string;
	project: string;
	waves: number;
	cycles: string[][];
	disagree: string[];
	/** In row order: by wave, outside blockers first, then the longest chain. */
	tasks: WaveTask[];
}

export interface WaveGroup {
	label: string;
	tasks: WaveTask[];
}

/** The project's own tasks, one group per wave in the server's row order. */
export function groupWaves(result: ProjectWaves): WaveGroup[] {
	const groups = new Map<number, WaveTask[]>();
	for (const task of result.tasks) {
		if (!task.outside) {
			groups.set(task.wave, [...(groups.get(task.wave) ?? []), task]);
		}
	}
	return [...groups].map(([wave, tasks]) => ({
		label: wave === 0 ? "Wave 0 (ready)" : `Wave ${wave}`,
		tasks,
	}));
}

/** What the layout says needs a person's attention, one line each. */
export function wavesNotes(result: ProjectWaves): string[] {
	const title = new Map(result.tasks.map((t) => [t.slug, t.title]));
	const name = (slug: string) => title.get(slug) ?? slug;
	return [
		...result.cycles.map(
			(members) =>
				`In a cycle, so none can become ready: ${members.map(name).join(" → ")}`,
		),
		...(result.disagree.length > 0
			? [
					`task_ready and the waves disagree on ${result.disagree.map(name).join(", ")}; refresh.`,
				]
			: []),
	];
}

export interface WavesMessage {
	text: string;
	/** Shown with the error icon: it needs a person's attention. */
	warning: boolean;
}

/** The lines above a project's waves: its notes, then "nothing open" when
 * no wave has a task. A task the layout has closed can still be in
 * `task_ready`, so an empty project can carry a disagreement. */
export function wavesMessages(
	result: ProjectWaves,
	groups: readonly WaveGroup[],
): WavesMessage[] {
	return [
		...wavesNotes(result).map((text) => ({ text, warning: true })),
		...(groups.length === 0
			? [{ text: "Nothing is open in this project.", warning: false }]
			: []),
	];
}

/** The rows of `task`'s open blockers, in and outside the project. */
export function blockersOf(task: WaveTask, result: ProjectWaves): WaveTask[] {
	const bySlug = new Map(result.tasks.map((t) => [t.slug, t]));
	return task.open_blockers.flatMap((slug) => {
		const row = bySlug.get(slug);
		return row ? [row] : [];
	});
}
